import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from deskpet_platform.agent_config import AgentConfigUnconfirmed, encode_agent_config
from deskpet_platform.channel import AckTimeout, Channel, CommandRejected, DeviceError
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig


class AgentConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "device.json"
        self.pairing = DeviceConfig("aa:bb:cc:dd:ee:ff", bytes(range(32)))
        self.pairing.save(self.path)

    def client(self, *, wireless=True):
        client = Client.__new__(Client)
        client.config = self.pairing
        client.device_id = None
        client._operation = asyncio.Lock()
        client.link = SimpleNamespace(wireless=wireless)
        client.channel = SimpleNamespace(capabilities={"assistant_onboard": 1}, online=True, epoch="test",
                                         dropped_events=0, send=AsyncMock())
        client.close = AsyncMock()
        return client

    @staticmethod
    def status(tx=None):
        return {"mode": "auto", "configured": {"chat": True, "asr": False, "tts": True}, "last_applied_tx": tx}

    async def test_maximum_prompt_with_escaping_is_staged_with_p1_budgets(self):
        # UTF-8 content plus JSON backslashes and quotes exercise both encoding layers.
        prompt = '搭"\\\n' * 170 + "终x"
        self.assertEqual(len(prompt.encode("utf-8")), 1024)
        patch = {"mode": "auto", "chat": {"url": "http://192.168.3.4:3000/v1", "token": "s" * 512,
                                          "model": "model", "tool_format": "json"},
                 "asr": {"token": "a" * 512}, "tts": {"token": "t" * 512},
                 "persona": {"prompt": prompt, "user_name": "我", "device_name": "搭搭"}, "wake_enabled": False}
        prefix = "http://192.168.3.4:3000/v1/"
        for group in ("chat", "asr", "tts"):
            patch[group]["url"] = prefix + "x" * (256 - len(prefix))
        patch["tts"]["model"] = "m" * 24
        frames = []
        response = b""

        async def write(frame):
            nonlocal response
            frames.append(frame)
            request = json.loads(frame[3:])
            reply = dict(epoch=request["epoch"], seq=request["seq"], boot=1, ok=True)
            if request["type"] == "agent_config_begin":
                reply.update(next_offset=0)
            elif request["type"] == "agent_config_part":
                reply.update(next_offset=request["offset"] + len(base64.b64decode(request["data"], validate=True)))
            elif request["type"] == "agent_config_commit":
                reply.update(applied=True)
            response = b"P1," + json.dumps(reply, separators=(",", ":")).encode() + b"\n"

        async def readline():
            self.assertLessEqual(len(response), 1152)
            return response

        client = self.client()
        client.channel = Channel(SimpleNamespace(wireless=True, write=write, readline=readline), self.pairing.identity)
        client.channel.capabilities["assistant_onboard"] = 1
        client.channel.online = True
        client.channel.boot = 1
        client.channel.epoch = "e" * 32
        client.channel.seq = 4_294_960_000
        tx = await client.apply_agent_config(patch)
        self.assertLessEqual(max(map(len, frames)), 1024)
        commands = [json.loads(frame[3:]) for frame in frames]
        self.assertEqual({command["tx"] for command in commands}, {tx})
        self.assertEqual(commands[0]["type"], "agent_config_begin")
        self.assertEqual(commands[-1]["type"], "agent_config_commit")
        self.assertGreater(len(commands[1:-1]), 1)
        data = bytearray()
        for part in commands[1:-1]:
            self.assertEqual(part["type"], "agent_config_part")
            self.assertEqual(part["offset"], len(data))
            data.extend(base64.b64decode(part["data"], validate=True))
        self.assertEqual(commands[0]["size"], 4096)
        self.assertEqual(len(data), commands[0]["size"])
        self.assertEqual(json.loads(data), patch)

    async def test_discard_bad_transfer_and_do_not_replay_commit(self):
        client = self.client()
        client.channel.send.side_effect = [{"next_offset": 0}, {"next_offset": 99}, {}]
        with self.assertRaisesRegex(DeviceError, "invalid_agent_config_offset"):
            await client.apply_agent_config({"mode": "auto"})
        sent = client.channel.send.await_args_list
        self.assertEqual([call.args[0] for call in sent],
                         ["agent_config_begin", "agent_config_part", "agent_config_cancel"])
        self.assertEqual(len({call.kwargs["tx"] for call in sent}), 1)

        patch = {"mode": "auto"}
        for failure in (AckTimeout("commit reply lost"), CommandRejected("out_of_memory")):
            with self.subTest(failure=failure):
                client.channel.send.reset_mock()
                client.channel.send.side_effect = [{"next_offset": 0},
                    {"next_offset": len(encode_agent_config(patch))}, failure]
                with self.assertRaises(AgentConfigUnconfirmed) as raised:
                    await client.apply_agent_config(patch)
                sent = client.channel.send.await_args_list
                self.assertEqual([call.args[0] for call in sent],
                                 ["agent_config_begin", "agent_config_part", "agent_config_commit"])
                self.assertEqual(sent[-1].kwargs["tx"], raised.exception.tx)

    async def test_requires_encrypted_write_and_never_returns_secret_fields(self):
        client = self.client(wireless=False)
        with self.assertRaisesRegex(DeviceError, "requires_paired_wifi"):
            await client.apply_agent_config({"chat": {"token": "synthetic-value"}})
        with self.assertRaisesRegex(DeviceError, "requires_paired_wifi"):
            await client.send_voice_permissions({"device_restart"})
        client.channel.send.assert_not_awaited()
        client.channel.send.return_value = {**self.status(), "token": "synthetic-value"}
        self.assertEqual(await client.agent_config_status(), self.status())
        client.channel.send.return_value["configured"]["chat"] = "synthetic-value"
        with self.assertRaisesRegex(DeviceError, "invalid_agent_config_status"):
            await client.agent_config_status()

    async def test_config_commit_is_resolved_by_same_pairing_status_only(self):
        first, replacement = self.client(), self.client()
        first.channel.online = False
        first.apply_agent_config = AsyncMock(side_effect=AgentConfigUnconfirmed("a3fake"))
        replacement.channel.send.return_value = self.status("a3fake")
        context = Mock(__aenter__=AsyncMock(return_value=replacement), __aexit__=AsyncMock())
        factory = Mock(return_value=context)
        session = BoardSession(self.path, client_factory=factory, no_token_probe=True)
        session.client = first
        self.assertEqual(await session.agent_config({"mode": "auto"}), {"status": "applied", "tx": "a3fake"})
        first.apply_agent_config.assert_awaited_once()
        first.close.assert_awaited_once()
        factory.assert_called_once_with(self.pairing)
        replacement.channel.send.assert_awaited_once_with("agent_config_status")

    async def test_pairing_change_and_new_epoch_do_not_inherit_policy(self):
        client = self.client()
        client.channel.send.return_value = {"applied": True}
        session = BoardSession(self.path, no_token_probe=True)
        session.client = client
        result = await session.voice_tool_settings(["device_restart"])
        self.assertEqual(result["onboard_sync"], "applied")
        client.channel.send.assert_awaited_once_with("voice_permissions_set", enabled=["device_restart"])
        await session.sync_onboard_voice_permissions(client)
        self.assertEqual(client.channel.send.await_count, 1)
        client.channel.epoch = "new"
        await session.sync_onboard_voice_permissions(client)
        self.assertEqual(client.channel.send.await_count, 2)
        other_pair = DeviceConfig(self.pairing.identity, bytes(reversed(range(32))))
        other_pair.save(self.path)
        self.assertEqual(await session.sync_onboard_voice_permissions(client), "pending")
        self.assertEqual(client.channel.send.await_count, 2)
        new = self.client()
        new.config = other_pair
        new.channel.send.return_value = {"applied": True}
        self.assertEqual(await session.sync_onboard_voice_permissions(new), "applied")
        new.channel.send.assert_awaited_once_with("voice_permissions_set", enabled=[])

    def test_invalid_config_never_echoes_the_supplied_value(self):
        cases = [
            {"chat": {"url": "http://127.0.0.1/v1", "token": "sensitive"}},
            {"chat": {"url": "http://example.test/v1", "token": "sensitive"}},
            {"tts": {"url": "https://sensitive@example.test/v1"}},
            {"tts": {"url": "https://example.test/模型"}},
            {"tts": {"url": "https://example.test/v1/%broken"}},
            {"persona": {"prompt": "搭" * 342}},
            {"chat": {"token": "sensitive" * 57}},
            {"chat": {"token": "\\" * 512}, "asr": {"token": "\\" * 512}, "tts": {"token": "\\" * 512},
             "persona": {"prompt": "\\" * 1024}},
            {"wake_enabled": 1},
            {"mode": "auto", "unknown": "sensitive"},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid.keys()), self.assertRaises(ValueError) as error:
                encode_agent_config(invalid)
            self.assertNotIn("sensitive", str(error.exception))

        for url in ("https://example.test/v1/%E6%A8%A1", "http://[fd00::2]:8080/v1"):
            self.assertTrue(encode_agent_config({"tts": {"url": url}}))
