"""Synthesis for short speech clips played by the device."""
import asyncio
import os
import sys
from pathlib import Path
import tempfile
import wave

VOICE = 'zh-CN-XiaoyiNeural'

class SpeechTooLong(ValueError):
    pass

class SpeechServiceError(RuntimeError):
    def __init__(self, code, *, local=False):
        self.code = code
        super().__init__('本地语音合成暂时不可用。' if local else '在线语音合成暂时不可用。')

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

def local_speech_voice(config_path=None):
    voice = os.environ.get('DESKPET_LOCAL_TTS_VOICE')
    if voice is not None:
        return voice.strip()
    if sys.platform == 'darwin' and config_path is not None:
        try:
            return Path(config_path).with_name('speech-voice').read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            pass
    return ''


async def _wait_for_process(process, operation, timeout):
    try:
        await asyncio.wait_for(operation, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise


async def _synthesize_local(text, voice, target):
    try:
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/say', '-v', voice, '-o', str(target),
            '--file-format=WAVE', '--data-format=LEI16@16000', '-f', '-',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _wait_for_process(process, process.communicate(text.encode('utf-8')), 10)
    except asyncio.TimeoutError:
        raise SpeechServiceError('service_timeout', local=True) from None
    except OSError:
        raise SpeechServiceError('service_unreachable', local=True) from None
    if process.returncode:
        raise SpeechServiceError('service_unreachable', local=True)


async def _synthesize_online(text, source, target):
    import edge_tts
    from aiohttp import ClientConnectionError, ClientError, WSServerHandshakeError
    from edge_tts.exceptions import EdgeTTSException, NoAudioReceived, UnexpectedResponse, UnknownResponse

    try:
        try:
            await asyncio.wait_for(edge_tts.Communicate(text,VOICE).save(str(source)),30)
        except (ClientConnectionError, asyncio.TimeoutError):
            # A fresh request rewrites the temporary file if the first stream was cut short.
            await asyncio.wait_for(edge_tts.Communicate(text,VOICE).save(str(source)),15)
    except WSServerHandshakeError as exc:
        code = {401: 'invalid_token', 403: 'invalid_token', 429: 'rate_limited'}.get(exc.status, 'service_unreachable')
        raise SpeechServiceError(code) from None
    except asyncio.TimeoutError:
        raise SpeechServiceError('service_timeout') from None
    except (NoAudioReceived, UnexpectedResponse, UnknownResponse):
        raise SpeechServiceError('invalid_response') from None
    except (ClientError, EdgeTTSException):
        raise SpeechServiceError('service_unreachable') from None
    process=await asyncio.create_subprocess_exec('/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000',
        '-c','1',str(source),str(target),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    await _wait_for_process(process, process.wait(), 20)
    if process.returncode:raise RuntimeError('speech_conversion_failed')


async def synthesize_speech(text, *, voice=None):
    text = validate_text(text)
    if voice is None:
        voice = local_speech_voice()
    with tempfile.TemporaryDirectory(prefix='deskpet-speech-') as directory:
        target = Path(directory) / 'speech.wav'
        if voice:
            await _synthesize_local(text, voice, target)
        else:
            await _synthesize_online(text, Path(directory) / 'speech.mp3', target)
        with wave.open(str(target),'rb') as audio:
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000 or not audio.getnframes():
                raise ValueError('语音音频无效。')
            if audio.getnframes()>80000:raise SpeechTooLong('语音超过五秒。')
            frames=audio.getnframes();pcm=audio.readframes(frames)
        if len(pcm)!=frames*2:raise ValueError('语音音频无效。')
        return pcm
