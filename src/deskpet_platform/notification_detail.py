"""Formal DeskPet detail renderer (esp32-desk-display 1ebe815)."""
from datetime import datetime
from functools import lru_cache
import hashlib
from pathlib import Path
import zlib

from PIL import Image, ImageDraw, ImageFont

from .client import _text_font
from .notifications import SOURCE_NAMES

FONT='/System/Library/Fonts/Hiragino Sans GB.ttc'
UI_FONT=next(Path('/System/Library/AssetsV2').glob('com_apple_MobileAsset_Font*/**/PingFang.ttc'),None)

def ui_font(size):
    try:
        return ImageFont.truetype(str(UI_FONT) if UI_FONT else FONT,size,index=7 if UI_FONT else 0)
    except OSError:
        return _text_font(size)

# NotificationStore already bounds all titles to 64 characters.
WIDTH=288

def wrap_text(text,font,width):
    lines=[];line=''
    for char in text:
        char=char if ord(char)<=0xffff and char not in ('\u200d','\ufe0f') else '□'
        if line and font.getlength(line+char)>width:
            lines.append(line);line=''
        if font.getlength(char)>width:raise ValueError('detail glyph exceeds line')
        line+=char
    return lines+[line]

def balanced_text(text,font,width):
    lines=wrap_text(text,font,width)
    remaining=''.join(lines);count=len(lines);result=[]
    for left in range(count,1,-1):
        target=font.getlength(remaining)/left
        best=1
        for i in range(1,len(remaining)-left+2):
            length=font.getlength(remaining[:i])
            if length>width:break
            if abs(length-target)<=abs(font.getlength(remaining[:best])-target):best=i
            if length>target:break
        result.append(remaining[:best]);remaining=remaining[best:]
    result.append(remaining)
    return result

@lru_cache(maxsize=64)
def _pixels(title,summary,page):
    try:
        font=ImageFont.truetype(FONT,13)
    except OSError:
        font=_text_font(13)
    title_lines=wrap_text(title,font,WIDTH)
    if len(title_lines)>3:raise ValueError('detail title exceeds layout')
    summary_lines=wrap_text(summary or '暂无摘要',font,WIDTH)
    pages=max(1,(len(summary_lines)+3)//4)
    if pages>8:raise ValueError('detail summary exceeds layout')
    page=max(0,min(page,pages-1))
    image=Image.new('L',(WIDTH,112),0);draw=ImageDraw.Draw(image)
    for index,line in enumerate(title_lines):draw.text((0,index*16+1),line,font=font,anchor='lt',fill=255)
    for index,line in enumerate(summary_lines[page*4:page*4+4]):draw.text((0,48+index*16+1),line,font=font,anchor='lt',fill=255)
    data=image.point(lambda value:255 if value>=100 else 0).convert('1',dither=Image.Dither.NONE).tobytes()
    return data,page,pages

def render_detail(note,page):
    data,page,pages=_pixels(note['title'],note['summary'],page)
    return dict(id=note['id'],revision=note['revision'],page=page,pages=pages,
                short_id=note['handle'][-8:],reported=datetime.fromtimestamp(note['reported_at']).strftime('%Y-%m-%d %H:%M'),
                fresh=bool(note['fresh']),crc=zlib.crc32(data),pixels=data)

@lru_cache(maxsize=256)
def _finished_pixels(title,repository,finished,gray,agent=None,workspace_label="仓库"):
    image=Image.new('L',(WIDTH,112),0);draw=ImageDraw.Draw(image)
    title_offset=15 if agent else 0
    for size in range(22,9,-1):
        font=ui_font(size)
        lines=balanced_text(title,font,WIDTH-4)
        if len(lines)*size<=48-title_offset and all(font.getlength(line)<=WIDTH-4 for line in lines):break
    line_height=size
    top=title_offset+max(0,(48-title_offset-len(lines)*line_height)//2)
    for index,line in enumerate(lines):
        draw.text(((WIDTH-font.getlength(line))/2,top+index*line_height),line,font=font,anchor='lt',fill=255)
    font=ui_font(13)
    def fitting(text,width):
        if font.getlength(text)<=width:return text
        while text and font.getlength(text+'…')>width:text=text[:-1]
        return text+'…'
    if agent:
        name=fitting(agent, WIDTH-4)
        draw.text(((WIDTH-font.getlength(name))/2,0),name,font=font,anchor='lt',fill=210)
    for y,label,value in ((53,workspace_label,repository or '暂无'),(85,'结束时间',finished)):
        draw.text((5,y),label,font=font,anchor='lt',fill=255)
        draw.text((80,y),fitting(value,208),font=font,anchor='lt',fill=255)
    if gray:
        levels=[(value*3+127)//255 for value in image.tobytes()]
        return bytes((levels[i]<<6)|(levels[i+1]<<4)|(levels[i+2]<<2)|levels[i+3]
                     for i in range(0,len(levels),4))
    return image.point(lambda value:255 if value>=100 else 0).convert('1',dither=Image.Dither.NONE).tobytes()

def render_finished_detail(note,gray=False):
    # Only session title, verified repository name and end time reach the board.
    finished=datetime.fromtimestamp(note['created']).strftime('%Y-%m-%d %H:%M')
    pixels=_finished_pixels(note['title'],note['repository'],finished,gray,
                            note.get("agent"),note.get("workspace_label","仓库"))
    value=dict(id=note['id'],revision=note['revision'],page=0,pages=1,
               short_id=note['handle'][-8:],reported=finished,fresh=bool(note['fresh']),
               crc=zlib.crc32(pixels),pixels=pixels)
    if gray:value['gray']=True
    return value

def render_cached_detail(record, *, finished_gray=False):
    session = record["session"]
    suffix = session[-8:]
    handle = session if len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix) else (
        hashlib.sha256(session.encode()).hexdigest())
    note = {
        "id": record["cache_id"], "revision": 1, "handle": handle,
        "title": record["title"], "summary": "", "repository": None,
        "reported_at": record["created_at"], "created": record["created_at"], "fresh": False,
    }
    if record["category"] == "completed":
        note["workspace_label"], note["repository"] = record.get("workspace_context", ("工作区", "未记录"))
        note["agent"] = SOURCE_NAMES.get(record["source"], record["source"])
        return render_finished_detail(note, gray=finished_gray)
    return render_detail(note, 0)
