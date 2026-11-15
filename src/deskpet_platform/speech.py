"""Explicit online synthesis for short, locally played speech."""
import asyncio
from pathlib import Path
import tempfile
import wave

VOICE = 'zh-CN-XiaoyiNeural'

def validate_text(text):
    if not isinstance(text,str):raise ValueError('请输入 1 至 80 字的单行文字。')
    if any(ord(char)<32 or ord(char)==127 for char in text):raise ValueError('请输入 1 至 80 字的单行文字。')
    text=text.strip()
    if not 1<=len(text)<=80:
        raise ValueError('请输入 1 至 80 字的单行文字。')
    return text

async def synthesize_speech(text):
    """Send only the requested text to Edge TTS; never retain audio files."""
    import edge_tts
    text=validate_text(text)
    with tempfile.TemporaryDirectory(prefix='deskpet-speech-') as directory:
        source=Path(directory)/'speech.mp3';target=Path(directory)/'speech.wav'
        await asyncio.wait_for(edge_tts.Communicate(text,VOICE).save(str(source)),30)
        process=await asyncio.create_subprocess_exec('/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000',
            '-c','1',str(source),str(target),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:await asyncio.wait_for(process.wait(),20)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            if process.returncode is None:process.kill();await process.wait()
            raise
        if process.returncode:raise RuntimeError('speech_conversion_failed')
        with wave.open(str(target),'rb') as audio:
            if (audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000 or
                not 0<audio.getnframes()<=80000):
                raise ValueError('语音须不超过五秒，请缩短文字。')
            frames=audio.getnframes();pcm=audio.readframes(frames)
        if len(pcm)!=frames*2:raise ValueError('语音音频无效。')
        return pcm
