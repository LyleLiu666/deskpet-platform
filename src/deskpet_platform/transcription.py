"""Explicit, in-memory adapter for the reference project's speech service.

This module does not capture audio or invoke the service by itself. Callers must
obtain consent before sending recordings and supply their own credentials.
"""

import asyncio
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import json
import re
import uuid
import wave

import httpx

MAX_PCM_BYTES = 24000 * 2 * 120
_MAX_RESPONSE_BYTES = 32768
_MAX_TEXT_BYTES = 4096
_ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DASHSCOPE_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_DASHSCOPE_SAMPLE_RATE = 24000
_DASHSCOPE_FRAME_BYTES = _DASHSCOPE_SAMPLE_RATE * 2 // 10


def _valid_key(key):
    return (isinstance(key, str) and bool(key.strip()) and len(key) <= 128
            and all(33 <= ord(c) <= 126 for c in key))


def _valid_model(model):
    return isinstance(model, str) and len(model) <= 128 and bool(_MODEL.fullmatch(model))


@dataclass(frozen=True)
class LocalTranscription:
    cli: str
    model: str
    vad_model: str


@dataclass(frozen=True)
class DashScopeTranscription:
    api_key: str = field(repr=False)
    workspace: str = ''


def _local_service(environ):
    paths = [environ.get(name, '') for name in (
        'DESKPET_LOCAL_ASR_CLI', 'DESKPET_LOCAL_ASR_MODEL', 'DESKPET_LOCAL_ASR_VAD_MODEL')]
    if not all(isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file() for value in paths):
        return None
    if not os.access(paths[0], os.X_OK):
        return None
    return LocalTranscription(*paths)


def configured_service(environ):
    """Explicit opt-in only; invalid, missing or conflicting settings disable recording."""
    local = environ.get('DESKPET_ENABLE_LOCAL_TRANSCRIPTION') == '1'
    online = environ.get('DESKPET_ENABLE_ONLINE_TRANSCRIPTION') == '1'
    dashscope = environ.get('DESKPET_ENABLE_DASHSCOPE_TRANSCRIPTION') == '1'
    if sum((local, online, dashscope)) != 1:
        return None
    if local:
        return _local_service(environ)
    if dashscope:
        key = environ.get('DASHSCOPE_API_KEY')
        workspace = environ.get('DASHSCOPE_WORKSPACE_ID', '')
        if not _valid_key(key) or (workspace and not _DASHSCOPE_WORKSPACE.fullmatch(workspace)):
            return None
        return DashScopeTranscription(key, workspace)
    key, model = environ.get("DESKPET_ASR_API_KEY"), environ.get("DESKPET_ASR_MODEL")
    return (key, model) if _valid_key(key) and _valid_model(model) else None


class TranscriptionError(RuntimeError):
    """A sanitized service failure, without audio, credentials or server text."""


def _http_failure(status):
    if status in (401, 403):
        return 'invalid_token'
    if status == 429:
        return 'rate_limited'
    return 'service_error'


async def _dashscope_event(socket, task_id):
    try:
        message = await socket.receive_json()
        header = message['header']
        if header['task_id'] != task_id:
            raise ValueError
        event = header['event']
        if event == 'task-failed':
            raise TranscriptionError('service_error')
        return event, message['payload']
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise TranscriptionError('invalid_response') from None


async def transcribe_dashscope_pcm(pcm: bytes, service: DashScopeTranscription) -> str:
    """Recognize buffered board audio over Paraformer's duplex WebSocket."""
    import aiohttp

    if not isinstance(pcm, bytes) or not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError('invalid PCM')
    if not _valid_key(service.api_key) or (service.workspace and not _DASHSCOPE_WORKSPACE.fullmatch(service.workspace)):
        raise ValueError('invalid DashScope configuration')
    host = f'{service.workspace}.cn-beijing.maas.aliyuncs.com' if service.workspace else 'dashscope.aliyuncs.com'
    task_id = str(uuid.uuid4())
    parameters = {'format': 'pcm', 'sample_rate': _DASHSCOPE_SAMPLE_RATE, 'language_hints': ['zh', 'en']}

    async def send_audio(socket):
        for offset in range(0, len(pcm), _DASHSCOPE_FRAME_BYTES):
            if offset:
                await asyncio.sleep(.1)
            await socket.send_bytes(pcm[offset:offset + _DASHSCOPE_FRAME_BYTES])
        await socket.send_json({
            'header': {'action': 'finish-task', 'task_id': task_id, 'streaming': 'duplex'},
            'payload': {'input': {}},
        })

    async def receive_text(socket):
        parts, size = [], 0
        while True:
            event, payload = await _dashscope_event(socket, task_id)
            if event == 'task-finished':
                if not parts:
                    raise TranscriptionError('no_speech')
                return ' '.join(parts)
            if event != 'result-generated':
                raise TranscriptionError('invalid_response')
            try:
                sentence = payload['output']['sentence']
                if sentence.get('heartbeat') or sentence.get('sentence_end') is not True:
                    continue
                part = sentence['text'].strip()
                if not part:
                    continue
                size += len(part.encode('utf-8')) + bool(parts)
                if size > _MAX_TEXT_BYTES or any(ord(c) < 32 or ord(c) == 127 for c in part):
                    raise ValueError
                parts.append(part)
            except (AttributeError, ValueError, TypeError, KeyError, UnicodeError):
                raise TranscriptionError('invalid_response') from None

    try:
        timeout = len(pcm) / (_DASHSCOPE_SAMPLE_RATE * 2) + 25
        async with asyncio.timeout(timeout), aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False,
        ) as session:
            async with session.ws_connect(
                f'wss://{host}/api-ws/v1/inference',
                headers={'Authorization': f'Bearer {service.api_key}'},
                max_msg_size=_MAX_RESPONSE_BYTES,
            ) as socket:
                await socket.send_json({
                    'header': {'action': 'run-task', 'task_id': task_id, 'streaming': 'duplex'},
                    'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                                'model': 'paraformer-realtime-v2', 'parameters': parameters, 'input': {}},
                })
                event, _ = await _dashscope_event(socket, task_id)
                if event != 'task-started':
                    raise TranscriptionError('invalid_response')
                sender = asyncio.create_task(send_audio(socket))
                receiver = asyncio.create_task(receive_text(socket))
                try:
                    _, text = await asyncio.gather(sender, receiver)
                    return text
                finally:
                    sender.cancel()
                    receiver.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
    except aiohttp.WSServerHandshakeError as exc:
        raise TranscriptionError(_http_failure(exc.status)) from None
    except asyncio.TimeoutError:
        raise TranscriptionError('service_timeout') from None
    except (aiohttp.ClientError, OSError):
        raise TranscriptionError('network_error') from None


async def transcribe_pcm(pcm: bytes, *, api_key: str, model: str,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send 24 kHz mono signed 16-bit PCM and return bounded recognized text."""
    if not isinstance(pcm, bytes) or not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError("invalid PCM")
    if not _valid_key(api_key):
        raise ValueError("invalid API key")
    if not _valid_model(model):
        raise ValueError("invalid model")

    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30, follow_redirects=False,
                                     trust_env=False) as client:
            async with client.stream(
                "POST", _ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"},
                data={"model": model}, files={"file": ("speech.wav", wav.getvalue(), "audio/wav")},
            ) as response:
                if response.status_code != 200:
                    raise TranscriptionError(_http_failure(response.status_code))
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise TranscriptionError("invalid_response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise TranscriptionError("invalid_response")
                    body.extend(chunk)
    except httpx.TimeoutException:
        raise TranscriptionError('service_timeout') from None
    except httpx.HTTPError:
        raise TranscriptionError("network_error") from None
    try:
        result = json.loads(body)
        text = result["text"]
        if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError
        if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in text):
            raise ValueError
        return text
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise TranscriptionError("invalid_response") from None


async def transcribe_local_pcm(pcm: bytes, service: LocalTranscription) -> str:
    """Recognize 24 kHz PCM locally; the WAV travels through a pipe, never a file."""
    if not isinstance(pcm, bytes) or not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError('invalid PCM')
    if not isinstance(service, LocalTranscription):
        raise ValueError('invalid local service')
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm)
    try:
        process = await asyncio.create_subprocess_exec(
            service.cli, '-m', service.model, '-f', '-', '-l', 'auto', '-nt', '-np',
            '-t', '4', '-otxt', '-of', '-', '--vad', '-vm', service.vad_model,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise TranscriptionError('service_error') from None
    async def send_audio():
        try:
            process.stdin.write(wav.getvalue())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def receive_text():
        data = bytearray()
        while True:
            chunk = await process.stdout.read(_MAX_TEXT_BYTES + 1 - len(data))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > _MAX_TEXT_BYTES:
                raise TranscriptionError('invalid_response')

    async def discard_output():
        while await process.stdout.read(8192):
            pass

    writer = asyncio.create_task(send_audio())
    try:
        try:
            async with asyncio.timeout(120):
                stdout = await receive_text()
                await writer
                await process.wait()
        except asyncio.TimeoutError:
            raise TranscriptionError('service_error') from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if not writer.done():
            writer.cancel()
        # Drain after killing so a full stdout pipe cannot prevent reaping.
        await asyncio.gather(process.wait(), discard_output(), writer, return_exceptions=True)
    if process.returncode != 0:
        raise TranscriptionError('service_error')
    try:
        if len(stdout) > _MAX_TEXT_BYTES:
            raise ValueError
        text = stdout.decode('utf-8').strip()
        if any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in text):
            raise ValueError
    except (ValueError, UnicodeError):
        raise TranscriptionError('invalid_response') from None
    if not text:
        raise TranscriptionError('no_speech')
    return text


async def transcribe_to_device(client, pcm: bytes, *, api_key: str, model: str,
                               transport: httpx.AsyncBaseTransport | None = None, capture_ticket: int | None = None) -> str:
    """Explicitly send a recording for recognition and queue the result on the paired device.

    The caller is responsible for obtaining consent and audio. Queued does not
    imply that the device has already displayed the text.
    """
    from .channel import DeviceError
    if client.channel.capabilities.get("transcript_display") != 1:
        raise DeviceError("transcript_not_supported")
    if capture_ticket is not None:
        if type(capture_ticket) is not int or not 0<capture_ticket<=0xffffffff:raise ValueError('invalid capture ticket')
        if client.channel.capabilities.get('capture_transcript_ticket')!=1:raise DeviceError('capture_cancel_not_supported')
    text = await transcribe_pcm(pcm, api_key=api_key, model=model, transport=transport)
    if capture_ticket is None:await client.queue_transcript(text)
    else:await client.queue_transcript(text,capture_ticket=capture_ticket)
    return text
