import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from aiohttp import ClientConnectionError

from deskpet_platform.channel import DeviceError
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.speech import SpeechServiceError, SpeechTooLong, speech_mp3_duration_ms, synthesize_speech_mp3


# The host validates frame headers and boundaries. The board owns MP3 decoding.
FRAME = b"\xff\xf3\x64\xc4" + bytes(140)


class SpeechMP3(unittest.IsolatedAsyncioTestCase):
    def session(self, play):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        session = BoardSession(Path(directory.name) / "device.json", no_token_probe=True)
        session.client = SimpleNamespace(
            channel=SimpleNamespace(capabilities={"speech_mp3": 1, "speech_pcm": 1, "read_speech_ticket": 1},
                                    online=True, epoch="initial"),
            play_speech_mp3=AsyncMock(side_effect=play),
            play_speech_pcm=AsyncMock(),
        )
        return session

    def client(self, send):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        client._speech = asyncio.Lock()
        client.channel = SimpleNamespace(capabilities={"speech_mp3": 1, "capture_speech_ticket": 1,
                                                      "capture_speech_cancel_ticket": 1}, send=send)
        return client

    def test_frame_validation_and_five_second_limit(self):
        self.assertEqual(speech_mp3_duration_ms(FRAME * 208), 4992)
        with self.assertRaises(SpeechTooLong):
            speech_mp3_duration_ms(FRAME * 209)
        for audio in (b"", FRAME + b"\0", bytes(144), FRAME[:3] + b"\x04" + FRAME[4:]):
            with self.subTest(audio=audio[:4]), self.assertRaises(ValueError):
                speech_mp3_duration_ms(audio)

    async def test_synthesis_retries_without_retaining_partial_stream(self):
        async def first_stream():
            yield {"type": "audio", "data": FRAME[:17]}
            raise ClientConnectionError()

        async def second_stream():
            yield {"type": "SentenceBoundary"}
            yield {"type": "audio", "data": FRAME[:5]}
            yield {"type": "audio", "data": FRAME[5:]}

        with patch("edge_tts.Communicate", side_effect=[
            SimpleNamespace(stream=first_stream),
            SimpleNamespace(stream=second_stream),
        ]) as communicate:
            self.assertEqual(await synthesize_speech_mp3("测试"), FRAME)
        self.assertEqual(communicate.call_count, 2)

    async def test_synthesis_rejects_invalid_stream_and_stops_overlong_stream(self):
        async def invalid_stream():
            yield {"type": "audio", "data": FRAME[:-1]}

        async def too_long_stream():
            yield {"type": "audio", "data": FRAME * 209}
            self.fail("read beyond the MP3 limit")

        with patch("edge_tts.Communicate", return_value=SimpleNamespace(stream=invalid_stream)):
            with self.assertRaises(SpeechServiceError) as error:
                await synthesize_speech_mp3("测试")
            self.assertEqual(error.exception.code, "invalid_response")
        with patch("edge_tts.Communicate", return_value=SimpleNamespace(stream=too_long_stream)):
            with self.assertRaises(SpeechTooLong):
                await synthesize_speech_mp3("测试")

    async def test_upload_keeps_the_capture_ticket_and_uses_device_duration(self):
        async def reply(kind, **fields):
            if kind == "speech_commit_mp3":
                return {"duration_ms": 72}
            return {"speech_status": "finished"} if kind == "speech_status" else {}

        send = AsyncMock(side_effect=reply)
        client = self.client(send)
        audio = FRAME * 3
        self.assertEqual(await client.play_speech_mp3(audio, capture_ticket=7),
                         {"status": "finished", "duration_ms": 72})
        requests = send.await_args_list
        self.assertEqual([request.args[0] for request in requests],
                         ["speech_begin", "speech_chunk", "speech_chunk", "speech_commit_mp3", "speech_status"])
        self.assertEqual(requests[0].kwargs, {"bytes": len(audio), "crc32": zlib.crc32(audio), "capture_ticket": 7})
        self.assertEqual([request.kwargs["offset"] for request in requests[1:3]], [0, 384])
        self.assertEqual(b"".join(bytes.fromhex(request.kwargs["data"]) for request in requests[1:3]), audio)

    async def test_invalid_commit_reply_triggers_ticket_scoped_cleanup(self):
        for duration in (True, 0, 5001, None):
            with self.subTest(duration=duration):
                send = AsyncMock(side_effect=lambda kind, **fields: {"duration_ms": duration})
                with self.assertRaisesRegex(DeviceError, "invalid_speech_duration"):
                    await self.client(send).play_speech_mp3(FRAME, capture_ticket=7)
                self.assertEqual(send.await_args.args, ("speech_cancel",))
                self.assertEqual(send.await_args.kwargs, {"ticket": 7})

    async def test_prefetch_synthesizes_one_segment_while_playing_and_preserves_ticket(self):
        second_started = asyncio.Event()
        synthesized = []
        played = []

        async def synth(part):
            synthesized.append(part)
            if part == "乙。":
                second_started.set()
            return part.encode()

        async def play(audio, *, read_ticket):
            self.assertEqual(read_ticket, 7)
            played.append(audio.decode())
            if len(played) == 1:
                await asyncio.wait_for(second_started.wait(), 1)
                self.assertEqual(synthesized, ["甲。", "乙。"])
            return {"duration_ms": 24}

        session = self.session(play)
        with patch("deskpet_platform.mcp_server.synthesize_speech_mp3", side_effect=synth):
            result = await session.speak("甲。乙。丙。", read_ticket=7)
        self.assertEqual(result, {"status": "finished", "duration_ms": 72})
        self.assertEqual(played, ["甲。", "乙。", "丙。"])
        session.client.play_speech_pcm.assert_not_awaited()

    async def test_prefetch_splits_overlong_audio_in_order(self):
        async def synth(part):
            if part == "乙丙。":
                raise SpeechTooLong()
            return part.encode()

        session = self.session(lambda audio: {"duration_ms": 24})
        with patch("deskpet_platform.mcp_server.synthesize_speech_mp3", side_effect=synth):
            result = await session.speak("甲。乙丙。丁。")
        self.assertEqual(result["duration_ms"], 96)
        self.assertEqual([call.args[0].decode() for call in session.client.play_speech_mp3.await_args_list],
                         ["甲。", "乙", "丙。", "丁。"])

    async def test_continuation_prefetch_keeps_the_entire_reply_serialized(self):
        first_playing = asyncio.Event()
        second_synthesized = asyncio.Event()
        release_first = asyncio.Event()
        continuation = asyncio.get_running_loop().create_future()
        spoken = []

        async def synth(part):
            if part == "乙。":
                self.assertTrue(first_playing.is_set())
                second_synthesized.set()
            return part.strip().encode()

        async def play(audio, *, capture_ticket=None):
            part = audio.decode()
            spoken.append((part, capture_ticket))
            if part == "甲。":
                first_playing.set()
                await release_first.wait()
            return {"duration_ms": 24}

        session = self.session(play)
        with patch("deskpet_platform.mcp_server.synthesize_speech_mp3", side_effect=synth):
            async with asyncio.TaskGroup() as tasks:
                streamed = tasks.create_task(
                    session.speak_voice_reply("甲。", capture_ticket=7, continuation=continuation))
                await asyncio.wait_for(first_playing.wait(), 1)
                independent = tasks.create_task(session.speak("丁。"))
                continuation.set_result("乙。\n丙。")
                await asyncio.wait_for(second_synthesized.wait(), 1)
                self.assertEqual(spoken, [("甲。", 7)])
                release_first.set()

        self.assertEqual(streamed.result(), {"status": "finished", "duration_ms": 72})
        self.assertEqual(independent.result(), {"status": "finished", "duration_ms": 24})
        self.assertEqual(spoken, [("甲。", 7), ("乙。", 7), ("丙。", 7), ("丁。", None)])

    async def test_prefetch_is_cancelled_and_drained_on_disconnect_or_cancellation(self):
        for disconnect in (True, False):
            with self.subTest(disconnect=disconnect):
                second_started = asyncio.Event()
                second_stopped = asyncio.Event()

                async def synth(part):
                    if part == "甲。":
                        return FRAME
                    try:
                        second_started.set()
                        await asyncio.Future()
                    finally:
                        second_stopped.set()

                async def play(audio):
                    await asyncio.wait_for(second_started.wait(), 1)
                    if disconnect:
                        session.client.channel.epoch = "replacement"
                        return {"duration_ms": 24}
                    await asyncio.Future()

                session = self.session(play)
                with patch("deskpet_platform.mcp_server.synthesize_speech_mp3", side_effect=synth):
                    task = asyncio.create_task(session.speak("甲。乙。"))
                    await asyncio.wait_for(second_started.wait(), 1)
                    if disconnect:
                        self.assertEqual((await asyncio.wait_for(task, 1))["status"], "not_connected")
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 1)
                self.assertTrue(second_stopped.is_set())
                session.client.play_speech_mp3.assert_awaited_once()
