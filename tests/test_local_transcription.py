"""Offline transcription is explicit, bounded and cancellable."""
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deskpet_platform.transcription import LocalTranscription, TranscriptionError, configured_service, transcribe_local_pcm


class LocalTranscriptionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='deskpet-asr-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'whisper-cli'
        self.model = self.root / 'model.bin'
        self.vad = self.root / 'vad.bin'
        self.model.write_bytes(b'model')
        self.vad.write_bytes(b'vad')
        self.exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.buffer.read()\nprint("你好")\n')
        self.exe.chmod(0o700)
        self.env = {'DESKPET_ENABLE_LOCAL_TRANSCRIPTION': '1',
                    'DESKPET_LOCAL_ASR_CLI': str(self.exe),
                    'DESKPET_LOCAL_ASR_MODEL': str(self.model),
                    'DESKPET_LOCAL_ASR_VAD_MODEL': str(self.vad)}

    def test_requires_explicit_opt_in_and_all_paths(self):
        self.assertIsNone(configured_service({}))
        self.assertIsNone(configured_service({**self.env, 'DESKPET_ENABLE_LOCAL_TRANSCRIPTION': '0'}))
        self.assertIsNone(configured_service({**self.env, 'DESKPET_LOCAL_ASR_VAD_MODEL': ''}))
        self.assertIsNone(configured_service({**self.env, 'DESKPET_LOCAL_ASR_CLI': 'relative'}))
        self.assertIsInstance(configured_service(self.env), LocalTranscription)
        self.assertIsNone(configured_service({**self.env, 'DESKPET_ENABLE_ONLINE_TRANSCRIPTION': '1'}))

    async def test_sends_wav_on_stdin_and_requests_vad(self):
        self.exe.write_text('''#!/usr/bin/env python3
import sys
assert sys.stdin.buffer.read().startswith(b'RIFF')
assert '--vad' in sys.argv and '-vm' in sys.argv and '-of' in sys.argv
assert sys.argv[sys.argv.index('-l')+1] == 'auto'
print('你好')
''')
        result = await transcribe_local_pcm(b'\0\0' * 240, configured_service(self.env))
        self.assertEqual(result, '你好')
        self.assertEqual(sorted(x.name for x in self.root.iterdir()), ['model.bin', 'vad.bin', 'whisper-cli'])

    async def test_empty_or_failed_output_is_not_a_transcript(self):
        for program, error in (('print("")', 'no_speech'), ('raise SystemExit(2)', 'service_error')):
            self.exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.buffer.read()\n' + program + '\n')
            with self.assertRaisesRegex(TranscriptionError, error):
                await transcribe_local_pcm(b'\0\0' * 240, configured_service(self.env))

    async def test_malformed_and_oversized_output_is_rejected(self):
        for payload in (b'\xff', b'a' * 4097, b'hello\x00there'):
            self.exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(' + repr(payload) + ')\n')
            with self.subTest(payload_length=len(payload)), self.assertRaisesRegex(TranscriptionError, 'invalid_response'):
                await transcribe_local_pcm(b'\0\0' * 240, configured_service(self.env))

    async def test_oversized_output_is_rejected_without_waiting_for_child_exit(self):
        marker=self.root/'flood-started'
        self.exe.write_text('#!/usr/bin/env python3\nimport os,sys,time\n'
                            'open('+repr(str(marker))+',"w").write(str(os.getpid()))\n'
                            'sys.stdout.buffer.write(b"a"*4097);sys.stdout.buffer.flush()\n'
                            'time.sleep(30)\n')
        with self.assertRaisesRegex(TranscriptionError,'invalid_response'):
            await asyncio.wait_for(transcribe_local_pcm(b'\0\0'*240,configured_service(self.env)),timeout=2)
        pid=int(marker.read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    async def test_output_flood_is_stopped_even_if_child_does_not_read_audio(self):
        marker=self.root/'flood-pid'
        self.exe.write_text('#!/usr/bin/env python3\nimport os\n'
                            'open('+repr(str(marker))+',"w").write(str(os.getpid()))\n'
                            'while True: os.write(1,b"x"*65536)\n')
        with self.assertRaisesRegex(TranscriptionError,'invalid_response'):
            await asyncio.wait_for(transcribe_local_pcm(b'\0\0'*240000,configured_service(self.env)),timeout=3)
        pid=int(marker.read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    async def test_exact_output_limit_is_accepted(self):
        self.exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.buffer.read()\n'
                            'sys.stdout.buffer.write(b"a"*4096)\n')
        self.assertEqual(await transcribe_local_pcm(b'\0\0'*240,configured_service(self.env)),'a'*4096)

    async def test_cancel_stops_child_while_audio_pipe_is_blocked(self):
        marker=self.root/'blocked-pid'
        self.exe.write_text('#!/usr/bin/env python3\nimport os,time\n'
                            'open('+repr(str(marker))+',"w").write(str(os.getpid()))\n'
                            'time.sleep(30)\n')
        task=asyncio.create_task(transcribe_local_pcm(b'\0\0'*240000,configured_service(self.env)))
        for _ in range(100):
            if marker.exists():break
            await asyncio.sleep(.01)
        self.assertTrue(marker.exists());pid=int(marker.read_text());task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,timeout=3)
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    async def test_timeout_stops_and_reaps_unresponsive_child(self):
        self.exe.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        spawn = asyncio.create_subprocess_exec
        deadline = asyncio.timeout(.5)
        child = None

        async def start_child(*args, **kwargs):
            nonlocal child
            child = await spawn(*args, **kwargs)
            return child

        with (
            patch('deskpet_platform.transcription.asyncio.create_subprocess_exec', new=start_child),
            patch('deskpet_platform.transcription.asyncio.timeout', return_value=deadline),
        ):
            with self.assertRaisesRegex(TranscriptionError, 'service_error'):
                await transcribe_local_pcm(b'\0\0' * 240000, configured_service(self.env))
        self.assertTrue(deadline.expired())
        self.assertIsNotNone(child)
        self.assertIsNotNone(child.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)

    async def test_invalid_audio_never_launches_child(self):
        service=configured_service(self.env)
        for pcm in (b'', b'0', 'not bytes'):
            with self.subTest(pcm=pcm), self.assertRaises(ValueError):
                await transcribe_local_pcm(pcm, service)

    async def test_cancellation_stops_child_process(self):
        marker = self.root / 'started'
        self.exe.write_text('#!/usr/bin/env python3\nimport sys,time\nsys.stdin.buffer.read()\nopen(' + repr(str(marker)) + ',"w").write(str(__import__("os").getpid()))\ntime.sleep(30)\n')
        task = asyncio.create_task(transcribe_local_pcm(b'\0\0' * 240, configured_service(self.env)))
        for _ in range(100):
            if marker.exists(): break
            await asyncio.sleep(.01)
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

if __name__ == '__main__': unittest.main()
