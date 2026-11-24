"""Explicit online synthesis for short, locally played speech."""
import asyncio
from contextlib import aclosing
from pathlib import Path
import tempfile
import wave

VOICE = 'zh-CN-XiaoyiNeural'
# A 48 kbps MPEG-2 Layer III frame at 24 kHz carries 576 samples.
MP3_FRAME_BYTES = 144
MP3_FRAME_MS = 24
SPEECH_MAX_MS = 5000
MP3_MAX_BYTES = SPEECH_MAX_MS // MP3_FRAME_MS * MP3_FRAME_BYTES

class SpeechTooLong(ValueError):
    pass

class SpeechServiceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('在线语音合成暂时不可用。')

def split_speech(text,limit=12):
    """Keep each phrase short while preserving all text and punctuation."""
    parts=[];start=0
    for index,char in enumerate(text):
        if index-start+1>=limit or char in '，。！？；,.!?;':
            if index==start and char in '，。！？；,.!?;' and parts and len(parts[-1])>1:
                start-=1;parts[-1]=parts[-1][:-1]
            parts.append(text[start:index+1]);start=index+1
    if start<len(text):parts.append(text[start:])
    return parts

def validate_text(text):
    if not isinstance(text,str):raise ValueError('请输入 1 至 80 字的单行文字。')
    if any(ord(char)<32 or ord(char)==127 for char in text):raise ValueError('请输入 1 至 80 字的单行文字。')
    text=text.strip()
    if not 1<=len(text)<=80:
        raise ValueError('请输入 1 至 80 字的单行文字。')
    return text

def speech_mp3_duration_ms(audio):
    """Validate the raw MPEG-2 Layer III frames used by the board."""
    if not isinstance(audio, bytes) or not audio or len(audio) % MP3_FRAME_BYTES:
        raise ValueError('语音音频须为 24 kHz、48 kbps 单声道 MP3。')
    if len(audio) > MP3_MAX_BYTES:
        raise SpeechTooLong('语音超过五秒。')
    for offset in range(0, len(audio), MP3_FRAME_BYTES):
        sync, layer, bitrate, channel = audio[offset:offset + 4]
        if (sync != 0xff or (layer & 0xfe) != 0xf2
                or (bitrate & 0xfe) != 0x64 or (channel & 0xc0) != 0xc0):
            raise ValueError('语音音频须为 24 kHz、48 kbps 单声道 MP3。')
    return len(audio) // MP3_FRAME_BYTES * MP3_FRAME_MS


async def _request_edge_speech(text, receive):
    import edge_tts
    from aiohttp import ClientConnectionError, ClientError, WSServerHandshakeError
    from edge_tts.exceptions import EdgeTTSException, NoAudioReceived, UnexpectedResponse, UnknownResponse

    text = validate_text(text)
    try:
        try:
            return await asyncio.wait_for(receive(edge_tts.Communicate(text, VOICE)), 30)
        except (ClientConnectionError, asyncio.TimeoutError):
            return await asyncio.wait_for(receive(edge_tts.Communicate(text, VOICE)), 15)
    except WSServerHandshakeError as exc:
        code = {401: 'invalid_token', 403: 'invalid_token', 429: 'rate_limited'}.get(exc.status, 'service_unreachable')
        raise SpeechServiceError(code) from None
    except asyncio.TimeoutError:
        raise SpeechServiceError('service_timeout') from None
    except (NoAudioReceived, UnexpectedResponse, UnknownResponse):
        raise SpeechServiceError('invalid_response') from None
    except (ClientError, EdgeTTSException):
        raise SpeechServiceError('service_unreachable') from None


async def synthesize_speech_mp3(text):
    """Return the original Edge TTS audio in memory for a capable board."""
    async def receive(communicator):
        audio = bytearray()
        async with aclosing(communicator.stream()) as stream:
            async for message in stream:
                if message['type'] == 'audio':
                    data = message['data']
                    if len(audio) + len(data) > MP3_MAX_BYTES:
                        raise SpeechTooLong('语音超过五秒。')
                    audio.extend(data)
        return bytes(audio)

    audio = await _request_edge_speech(text, receive)
    try:
        speech_mp3_duration_ms(audio)
    except ValueError:
        raise SpeechServiceError('invalid_response') from None
    return audio


async def synthesize_speech(text):
    """Send only the requested text to Edge TTS; never retain audio files."""
    with tempfile.TemporaryDirectory(prefix='deskpet-speech-') as directory:
        source=Path(directory)/'speech.mp3';target=Path(directory)/'speech.wav'
        await _request_edge_speech(text, lambda communicator: communicator.save(str(source)))
        process=await asyncio.create_subprocess_exec('/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000',
            '-c','1',str(source),str(target),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:await asyncio.wait_for(process.wait(),20)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            if process.returncode is None:process.kill();await process.wait()
            raise
        if process.returncode:raise RuntimeError('speech_conversion_failed')
        with wave.open(str(target),'rb') as audio:
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000 or not audio.getnframes():
                raise ValueError('语音音频无效。')
            if audio.getnframes()>80000:raise SpeechTooLong('语音超过五秒。')
            frames=audio.getnframes();pcm=audio.readframes(frames)
        if len(pcm)!=frames*2:raise ValueError('语音音频无效。')
        return pcm
