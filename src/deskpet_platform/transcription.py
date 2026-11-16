"""Explicit, in-memory adapter for the reference project's speech service.

This module does not capture audio or invoke the service by itself. Callers must
obtain consent before sending recordings and supply their own credentials.
"""

import io
import json
import re
import wave

import httpx

MAX_PCM_BYTES = 24000 * 2 * 120
_MAX_RESPONSE_BYTES = 32768
_MAX_TEXT_BYTES = 4096
_ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class TranscriptionError(RuntimeError):
    """A sanitized service failure, without audio, credentials or server text."""


async def transcribe_pcm(pcm: bytes, *, api_key: str, model: str,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send 24 kHz mono signed 16-bit PCM and return bounded recognized text."""
    if not isinstance(pcm, bytes) or not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError("invalid PCM")
    if (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 128
            or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
        raise ValueError("invalid API key")
    if not isinstance(model, str) or len(model) > 128 or not _MODEL.fullmatch(model):
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
                    raise TranscriptionError("service_error")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise TranscriptionError("invalid_response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise TranscriptionError("invalid_response")
                    body.extend(chunk)
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


async def transcribe_to_device(client, pcm: bytes, *, api_key: str, model: str,
                               transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Explicitly send a recording for recognition and queue the result on the paired device.

    The caller is responsible for obtaining consent and audio. Queued does not
    imply that the device has already displayed the text.
    """
    from .channel import DeviceError
    if client.channel.capabilities.get("transcript_display") != 1:
        raise DeviceError("transcript_not_supported")
    text = await transcribe_pcm(pcm, api_key=api_key, model=model, transport=transport)
    await client.queue_transcript(text)
