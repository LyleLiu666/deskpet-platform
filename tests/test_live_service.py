import asyncio
import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

from deskpet_platform.device_lock import DeviceLock
from deskpet_platform.live_service import run_live_service
from deskpet_platform.notifications import request_worker_yield
from deskpet_platform.pairing import DeviceConfig


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("receiver_agent", ROOT / "tools/macos/receiver_agent.py")
receiver_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(receiver_agent)


class _FakeChannel:
    def __init__(self):
        self.capabilities = {
            "token_stats": 1,
            "computer_metrics": 1,
            "computer_preferences": 1,
            "speed_windows": 1,
            "speed_snapshot": 1,
        }
        self.epoch = 1


class _FakeBoardClient:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.device_id = config.device_id
        self.channel = _FakeChannel()
        self._lock = None

    async def __aenter__(self):
        self._lock = DeviceLock(self.config.identity, root=self.state["lock_root"])
        self._lock.__enter__()
        self.state["connections"] += 1
        self.state["active"] = True
        return self

    async def close(self):
        self.state["active"] = False
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None

    async def __aexit__(self, *exc):
        await self.close()
        return False

    async def status(self):
        self.state["heartbeats"] += 1
        return {
            "identity": self.config.identity,
            "device_id": self.device_id,
            "speed_windows": {"recent_seconds": 1800, "long_days": 7},
            "computer_preferences": 3,
        }

    async def compact_status(self):
        return await self.status()

    async def acknowledge_computer_preferences(self, _state):
        return {}

    async def set_computer_preferences(self, **_kwargs):
        return {}

    async def update_computer_metrics(self, **_kwargs):
        self.state["computer_updates"] += 1
        return {}

    async def update_tokens(self, _snapshot, **_kwargs):
        self.state["token_updates"] += 1
        return {}

    async def update_speed_history(self, **_kwargs):
        return True


class LiveServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_service_connects_yields_to_mcp_and_resumes_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "device.json"
            config = DeviceConfig(
                identity="48:af:f3:70:8f:e4",
                key=b"\x22" * 32,
                address="192.168.1.50",
                device_id="d1763f55-6e47-459f-a3bc-27b542515288",
            )
            config.save(config_path)
            state = {
                "lock_root": root / "locks",
                "connections": 0,
                "active": False,
                "heartbeats": 0,
                "computer_updates": 0,
                "token_updates": 0,
            }

            task = asyncio.create_task(
                run_live_service(
                    config_path,
                    no_token_probe=True,
                    client_factory=lambda cfg, **_kw: _FakeBoardClient(cfg, state),
                    poll_interval=0.05,
                    handoff_delay=0.2,
                )
            )
            try:
                for _ in range(60):
                    if task.done():
                        task.result()
                    if state["active"] and state["heartbeats"] >= 1:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(state["active"])

                # Duplicate service on the same config fails fast with a clear message.
                with self.assertRaisesRegex(RuntimeError, "已在运行"):
                    await run_live_service(config_path, no_token_probe=True, max_seconds=0.1)

                # Foreground MCP requests handoff, acquires DeviceLock, then releases it.
                self.assertTrue(request_worker_yield(config_path))
                for _ in range(40):
                    if not state["active"]:
                        break
                    await asyncio.sleep(0.05)
                self.assertFalse(state["active"])

                with DeviceLock(config.identity, root=state["lock_root"]):
                    await asyncio.sleep(0.25)
                    self.assertFalse(state["active"])

                # Service automatically resumes after foreground owner exits.
                for _ in range(60):
                    if state["active"] and state["connections"] >= 2:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(state["active"])
                self.assertGreaterEqual(state["connections"], 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_launchd_generator_supports_primary_service_without_shared_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "DeskPet Network Access.app"
            macos = app / "Contents/MacOS"
            macos.mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "local.deskpet.network-access"})
            )
            launcher = macos / "DeskPetReceiverLauncher"
            launcher.touch()
            launcher.chmod(0o700)
            exe = root / "deskpet-platform"
            exe.touch()
            exe.chmod(0o700)
            cfg = root / "device.json"
            cfg.touch()

            plist = receiver_agent.make_agent(app, exe, cfg, no_token_probe=True)
            self.assertTrue(plist["Label"].startswith("local.deskpet.service."))
            self.assertEqual(
                plist["ProgramArguments"],
                [str(launcher), str(exe), "--config", str(cfg), "service", "--no-token-probe"],
            )


if __name__ == "__main__":
    unittest.main()
