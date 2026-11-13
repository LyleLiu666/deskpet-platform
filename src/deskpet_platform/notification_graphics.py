"""One-bit Agent marks and text for the device's small notification regions."""
import base64
from functools import lru_cache
from importlib.resources import files
from io import BytesIO

from PIL import Image, ImageDraw

from .client import _text_font
from .notifications import SOURCE_NAMES


ICON_FILES = {"codex": "codex", "claude_code": "claudecode", "workbuddy": "workbuddy",
              "antigravity": "antigravity", "opencode": "opencode"}
HOME_NAMES = {**SOURCE_NAMES, "claude_code": "Claude"}
SCALE = 3


@lru_cache(maxsize=16)
def _icon(source, pixels):
    name = ICON_FILES.get(source)
    if name is None:
        return None
    asset = files("deskpet_platform").joinpath("assets", "agent_icons", name + ".png")
    with Image.open(BytesIO(asset.read_bytes())) as image:
        return image.convert("RGBA").getchannel("A").resize((pixels, pixels), Image.Resampling.LANCZOS)


def _draw_icon(image, source, x, y, size):
    mask = _icon(source, size)
    if mask is None:
        draw = ImageDraw.Draw(image)
        font = _text_font(size)
        draw.text((x, y), "?", fill=255, font=font, anchor="lt")
    else:
        image.paste(255, (x, y), mask)


def _text(draw, x, width, height, text, *, size):
    font = _text_font(size * SCALE)
    available = max(0, width - x)
    if draw.textlength(text, font=font) > available:
        while text and draw.textlength(text + "…", font=font) > available:
            text = text[:-1]
        text = text + "…" if text else ""
    if not text:
        return
    _, top, _, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((x, max(0, (height - (bottom - top)) // 2 - top)), text, fill=255, font=font)


def _packed(image):
    width, height = image.size
    image = image.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
    return image.point(lambda value: 255 if value >= 96 else 0).convert("1", dither=Image.Dither.NONE).tobytes()


def list_title(item):
    """A 216×18 list title with the actual source mark and an abbreviated title."""
    image = Image.new("L", (216 * SCALE, 18 * SCALE))
    source = item["source"]
    _draw_icon(image, source, 0, SCALE, 16 * SCALE)
    text = f"{SOURCE_NAMES.get(source, source)} · {item['title']}"
    _text(ImageDraw.Draw(image), 19 * SCALE, image.width, image.height, text, size=14)
    return _packed(image)


def notification_row(item):
    if item["resolved"]:
        kind = 3
        item = {**item, "title": "已应答 " + item["title"]}
    else:
        kind = 0 if item["category"] == "response" else 4
    return {"kind": kind, "title": "~" + base64.b64encode(list_title(item)).decode("ascii")}


def home_lines(summary):
    """One completed summary plus at most three explicit pending sources."""
    lines = []
    count = summary["completed_count"]
    sources = tuple(summary["completed_sources"])
    if count:
        image = Image.new("L", (128 * SCALE, 16 * SCALE))
        x = 0
        for source in sources[:5]:
            _draw_icon(image, source, x, 2 * SCALE, 12 * SCALE)
            x += 12 * SCALE
        if len(sources) > 5:
            x += SCALE
            draw = ImageDraw.Draw(image)
            font = _text_font(9 * SCALE)
            label = "+" + str(len(sources) - 5)
            draw.text((x, 3 * SCALE), label, font=font, fill=255, anchor="lt")
            x += int(draw.textlength(label, font=font))
        _text(ImageDraw.Draw(image), x + 2 * SCALE, image.width, image.height, f"{count} 个提醒", size=11)
        lines.append({"category": "completed", "sources": sources, "bitmap": _packed(image)})
    for entry in summary["response_sources"][:3]:
        image = Image.new("L", (128 * SCALE, 16 * SCALE))
        source = entry["source"]
        _draw_icon(image, source, 0, 2 * SCALE, 12 * SCALE)
        suffix = f" ×{entry['count']}" if entry["count"] > 1 else ""
        text = HOME_NAMES.get(source, source) + suffix + " " + entry["title"]
        _text(ImageDraw.Draw(image), 15 * SCALE, image.width, image.height, text, size=11)
        lines.append({"category": "response", "source": source, "id": entry["id"], "bitmap": _packed(image)})
    return lines
