import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskpet_platform.agent_speed_probe import AgentSpeedProbe
from deskpet_platform.mcp_server import BoardSession, create_server


class AgentSpeedProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_uses_the_installed_probe_even_with_a_custom_quota_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / "bin" / ("AgentSpeedProbe.exe" if sys.platform == "win32" else "AgentSpeedProbe")
            probe.parent.mkdir()
            probe.write_bytes(b"fixture")
            with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                create_server(root / "device.json")
                factory.assert_called_once_with(probe)
            with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                create_server(root / "device.json", token_root=root / "synthetic-sessions")
                factory.assert_called_once_with(probe)
            with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                server = create_server(root / "device.json", no_token_probe=True)
                result = await server.call_tool("desk_token_status", {})
                self.assertEqual(result[1], {"status": "unavailable", "reason": "token_collection_disabled"})
                factory.assert_not_called()

    async def test_server_discovers_windows_probe_but_preserves_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "device.json"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gui = bin_dir / "AgentSpeed.exe"
            upstream = bin_dir / "AgentSpeed.Probe.exe"
            installed = bin_dir / "AgentSpeedProbe.exe"
            explicit = root / "custom.exe"
            gui.touch()
            explicit.touch()
            with patch("deskpet_platform.agent_speed_probe.platform", "win32"):
                with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                    create_server(config)
                    factory.assert_not_called()
                upstream.touch()
                with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                    create_server(config)
                    factory.assert_called_once_with(upstream)
                installed.touch()
                with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                    create_server(config)
                    factory.assert_called_once_with(installed)
                with patch("deskpet_platform.mcp_server.AgentSpeedProbe") as factory:
                    create_server(config, token_probe=explicit)
                    factory.assert_called_once_with(explicit)

    async def test_sampling_continues_while_device_send_is_delayed(self):
        class Collector:
            metric_version = "fixture"
            speed_state = "L"
            def __init__(self):
                self.count = 0
                self.sampled_three = asyncio.Event()
            async def sample(self):
                self.count += 1
                if self.count >= 3:
                    self.sampled_three.set()
                return {"rate10": 120, "sessions": 1, "trend": [0] * 12, "quotas": []}
        class Client:
            channel = type("Channel", (), {"capabilities": {"token_stats": 1}})()
            async def update_tokens(self, value, *, windows=None):
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as directory:
            collector = Collector()
            session = BoardSession(Path(directory) / "device.json", probe_collector=collector)
            session.client = Client()
            task = asyncio.create_task(session.poll_tokens())
            try:
                await asyncio.wait_for(collector.sampled_three.wait(), 4)
                self.assertGreaterEqual(collector.count, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_sampling_continues_when_device_temporarily_disconnects(self):
        class Collector:
            metric_version = "fixture"
            speed_state = "L"
            def __init__(self):
                self.count = 0
                self.third = asyncio.Event()
            async def sample(self):
                self.count += 1
                if self.count == 3:
                    self.third.set()
                return {"rate10": 120, "sessions": 1, "trend": [0] * 12, "quotas": []}
        class Client:
            channel = type("Channel", (), {"capabilities": {"token_stats": 1}})()
            async def update_tokens(self, value, *, windows=None):
                pass
        with tempfile.TemporaryDirectory() as directory:
            collector = Collector()
            session = BoardSession(Path(directory) / "device.json", probe_collector=collector)
            session.client = Client()
            task = asyncio.create_task(session.poll_tokens())
            try:
                async with asyncio.timeout(2):
                    while not collector.count:
                        await asyncio.sleep(.01)
                session.client = None
                await asyncio.wait_for(collector.third.wait(), 4)
                self.assertGreaterEqual(collector.count, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_idle_probe_is_terminated_and_a_later_sample_restarts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "probe"
            executable.write_text("#!/bin/sh\nwhile true; do sleep 1; done\n")
            executable.chmod(0o700)
            probe = AgentSpeedProbe(executable)
            try:
                await probe.sample()
                first = probe.process
                self.assertIsNotNone(first)
                probe.last_requested = -1000000
                await asyncio.wait_for(probe.reader, 4)
                self.assertIsNotNone(first.returncode)
                await probe.sample()
                self.assertIsNot(probe.process, first)
                self.assertIsNone(probe.process.returncode)
            finally:
                await probe.close()

    async def test_same_client_new_epoch_reestablishes_initial_token_state(self):
        class Collector:
            metric_version = "fixture"
            speed_state = "L"
            async def sample(self):
                return {"rate10": 120, "sessions": 1, "trend": [0] * 12, "quotas": []}
        class Channel:
            capabilities = {"token_stats": 1, "speed_snapshot": 1, "quota_sources": 1}
            epoch = "first"
        class Client:
            def __init__(self):
                self.channel = Channel()
                self.token_updates = 0
                self.history_updates = 0
                self.second = asyncio.Event()
            async def update_tokens(self, value, *, windows=None):
                self.token_updates += 1
            async def update_speed_history(self, **kwargs):
                self.history_updates += 1
                if self.history_updates == 1:
                    self.channel.epoch = "second"
                if self.history_updates == 2:
                    self.second.set()
                return True
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", probe_collector=Collector())
            session.client = client = Client()
            task = asyncio.create_task(session.poll_tokens())
            try:
                await asyncio.wait_for(client.second.wait(), 4)
                self.assertEqual(client.token_updates, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_missing_probe_is_reported_and_never_falls_back_to_a_simplified_rate(self):
        class Channel:
            capabilities = {"token_stats": 1, "speed_windows": 1}
        class Client:
            channel = Channel()
            def __init__(self): self.sent = []
            async def update_tokens(self, value, *, windows=None): self.sent.append(value)
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = client = Client()
            session.snapshot["speed_windows"] = {"recent_seconds": 1800, "long_days": 7}
            session.token_metrics.sample = sample = Mock(return_value={
                "rate10": 999, "sessions": 1, "trend": [999] * 12, "quotas": []})
            task = asyncio.create_task(session.poll_tokens())
            try:
                await asyncio.sleep(.1)
                self.assertFalse(task.done())
                sample.assert_not_called()
                self.assertEqual(client.sent, [])
                self.assertEqual(session.token_status()["reason"], "agent_speed_probe_missing")
                self.assertEqual(session.token_status()["status"], "unavailable")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failing_probe_does_not_switch_to_the_simplified_rate(self):
        class BrokenProbe:
            metric_version = "fixture"
            speed_state = None
            async def sample(self): raise OSError("probe unavailable")
        class Channel:
            capabilities = {"token_stats": 1, "speed_windows": 1}
        class Client:
            channel = Channel()
            def __init__(self): self.sent = []
            async def update_tokens(self, value, *, windows=None): self.sent.append(value)
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", probe_collector=BrokenProbe())
            session.client = client = Client()
            session.snapshot["speed_windows"] = {"recent_seconds": 1800, "long_days": 7}
            session.token_metrics.sample = sample = Mock(return_value={
                "rate10": 999, "sessions": 1, "trend": [999] * 12, "quotas": []})
            task = asyncio.create_task(session.poll_tokens())
            try:
                await asyncio.sleep(.1)
                self.assertFalse(task.done())
                sample.assert_not_called()
                self.assertEqual(client.sent, [])
                self.assertEqual(session.token_status(), {"status": "waiting"})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_uses_the_same_output_rate_and_session_count_as_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text("fixture")
            collector = AgentSpeedProbe(path)
            sample = {"t": 1_000_000, "state": "running", "live": {"output": 12.35, "total": 999},
                      "estimatedShare": 0.4, "generating": 2, "tools": 1}
            value = collector.normalize(sample, 1001)
            self.assertEqual((value["rate10"], value["sessions"], value["trend"][-1]), (124, 3, 124))
            self.assertEqual(collector.speed_state, "E")
            sample["estimatedShare"] = 0
            self.assertEqual(collector.normalize(sample, 1001)["rate10"], 124)
            self.assertEqual(collector.speed_state, "L")
            sample["live"]["output"] = 0
            sample["generating"] = sample["tools"] = 0
            self.assertEqual(collector.normalize(sample, 1001)["rate10"], 0)
            self.assertEqual(collector.speed_state, "I")
            self.assertIsNone(collector.normalize(sample, 1005))
            sample["state"] = "starting"
            self.assertIsNone(collector.normalize(sample, 1001))
            sample["state"] = "running"
            sample["live"]["output"] = float("nan")
            self.assertIsNone(collector.normalize(sample, 1001))

    async def test_device_limits_do_not_silently_change_the_original_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text("fixture")
            collector = AgentSpeedProbe(path)
            sample = {"t": 1_000_000, "state": "running", "live": {"output": 9999.9},
                      "estimatedShare": 0, "generating": 255, "tools": 0}
            self.assertEqual(collector.normalize(sample, 1001)["rate10"], 99999)
            sample["live"]["output"] = 10000
            self.assertIsNone(collector.normalize(sample, 1001))
            sample["live"]["output"] = 20
            sample["generating"] = 256
            value = collector.normalize(sample, 1001)
            self.assertEqual(value["rate10"], 200)
            self.assertIsNone(value["sessions"])

    async def test_watch_process_is_shared_and_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text("#!/usr/bin/env python3\nimport json,time\n"
                            "print(json.dumps({'t':int(time.time()*1000),'state':'running',"
                            "'live':{'output':23.4},'estimatedShare':0,'generating':1,'tools':0}),flush=True)\n"
                            "time.sleep(30)\n")
            path.chmod(0o700)
            collector = AgentSpeedProbe(path)
            try:
                await asyncio.gather(collector.sample(), collector.sample())
                pid = collector.process.pid
                for _ in range(40):
                    reading = await collector.sample()
                    if reading is not None:
                        break
                    await asyncio.sleep(.05)
                self.assertEqual(reading["rate10"], 234)
                self.assertEqual(collector.process.pid, pid)
                self.assertEqual(set(reading), {"rate10", "sessions", "trend", "quotas"})
            finally:
                process = collector.process
                await collector.close()
            self.assertIsNotNone(process.returncode)

    async def test_independent_collectors_share_one_process_and_take_over(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text("#!/usr/bin/env python3\nimport json,time\n"
                            "while True:\n"
                            " print(json.dumps({'t':int(time.time()*1000),'state':'running',"
                            "'live':{'output':23.4},'estimatedShare':0,'generating':1,'tools':0}),flush=True)\n"
                            " time.sleep(.05)\n")
            path.chmod(0o700)
            first, second = AgentSpeedProbe(path), AgentSpeedProbe(path)
            try:
                await first.sample()
                async with asyncio.timeout(4):
                    while await second.sample() is None:
                        await asyncio.sleep(.05)
                self.assertEqual(second.latest["live"]["output"], 23.4)
                self.assertIsNone(second.process)
                first.last_requested = -1000000
                await asyncio.sleep(.2)
                self.assertIsNone(first.process.returncode)
                leader = first.process
                await first.close()
                self.assertIsNotNone(leader.returncode)
                async with asyncio.timeout(4):
                    while second.process is None:
                        await second.sample()
                        await asyncio.sleep(.05)
                self.assertIsNotNone(second.process)
                async with asyncio.timeout(4):
                    while await second.sample() is None:
                        await asyncio.sleep(.05)
            finally:
                await first.close()
                await second.close()

    async def test_separate_processes_share_the_same_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe"
            path.write_text("#!/usr/bin/env python3\nimport json,time\n"
                            "# " + directory + "\n"
                            "while True:\n"
                            " print(json.dumps({'t':int(time.time()*1000),'state':'running',"
                            "'live':{'output':19},'estimatedShare':0,'generating':1,'tools':0}),flush=True)\n"
                            " time.sleep(.05)\n")
            path.chmod(0o700)
            program = """
import asyncio, sys
from deskpet_platform.agent_speed_probe import AgentSpeedProbe
async def run():
    probe = AgentSpeedProbe(sys.argv[1])
    try:
        while await probe.sample() is None:
            await asyncio.sleep(.05)
        print("leader" if probe.process else "follower", flush=True)
        command = await asyncio.to_thread(sys.stdin.readline)
        if command.strip() == "takeover":
            while probe.process is None:
                await probe.sample()
                await asyncio.sleep(.05)
            print("takeover", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
    finally:
        await probe.close()
asyncio.run(run())
"""
            async def start():
                return await asyncio.create_subprocess_exec(sys.executable, "-c", program, str(path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            first = await start()
            second = None
            try:
                self.assertEqual(await asyncio.wait_for(first.stdout.readline(), 5), b"leader\n")
                second = await start()
                self.assertEqual(await asyncio.wait_for(second.stdout.readline(), 5), b"follower\n")
                first.stdin.write(b"stop\n")
                await first.stdin.drain()
                await asyncio.wait_for(first.wait(), 5)
                self.assertEqual(first.returncode, 0)
                second.stdin.write(b"takeover\n")
                await second.stdin.drain()
                self.assertEqual(await asyncio.wait_for(second.stdout.readline(), 5), b"takeover\n")
            finally:
                for process in (first, second):
                    if process:
                        process.stdin.close()
                        await asyncio.wait_for(process.wait(), 5)
                        self.assertEqual(process.returncode, 0)

    async def test_board_session_sends_the_shared_probe_reading(self):
        class Collector:
            metric_version = "agent-speed-output-v1-fixture"
            speed_state = "E"
            closed = False
            async def sample(self):
                return {"rate10": 234, "sessions": 2, "trend": [234] * 12, "quotas": []}
            async def close(self): self.closed = True
        class Channel:
            capabilities = {"token_stats": 1, "speed_windows": 1, "token_quality": 1}
        class Client:
            channel = Channel()
            def __init__(self): self.received = asyncio.Event()
            async def update_tokens(self, value, *, windows=None):
                self.snapshot = value.copy();self.received.set()
        with tempfile.TemporaryDirectory() as directory:
            collector, client = Collector(), Client()
            session = BoardSession(Path(directory) / "device.json", probe_collector=collector)
            session.client = client
            session.snapshot["speed_windows"] = {"recent_seconds": 1800, "long_days": 7}
            task = asyncio.create_task(session.poll_tokens())
            try:
                await asyncio.wait_for(client.received.wait(), 3)
                self.assertEqual(client.snapshot["rate10"], 234)
                self.assertEqual(client.snapshot["sessions"], 2)
                self.assertEqual(client.snapshot["speed_state"], "E")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(collector.closed)  # The server owns a shared collector.
