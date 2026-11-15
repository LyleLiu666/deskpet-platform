"""One-bit Agent marks and text for the device's small notification regions."""
import base64
from functools import lru_cache
from importlib.resources import files
from io import BytesIO

from PIL import Image, ImageDraw

from .notification_detail import ui_font
from .notifications import SOURCE_NAMES


ICON_FILES = {"codex": "chatgpt", "claude_code": "claudecode", "workbuddy": "workbuddy",
              "antigravity": "antigravity", "opencode": "opencode"}
HOME_MARK_KEYS = {"codex": "g", "claude_code": "c", "workbuddy": "w", "antigravity": "a", "opencode": "o"}
HOME_MARK_SIZE = 12
HOME_MARK_PITCH = 14


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
        font = ui_font(size)
        draw.text((x, y), "?", fill=255, font=font, anchor="lt")
    else:
        image.paste(255, (x, y), mask)


def _text(draw, x, width, height, text, *, size, minimum=None):
    # Match the formal inbox's panel-size raster and its printable glyphs.
    text = "".join(c if ord(c) <= 0xffff and c not in ("\u200d", "\ufe0f") else "□" for c in text)
    font = ui_font(size)
    available = max(0, width - x)
    while minimum is not None and size > minimum and font.getlength(text) > available:
        size -= 1
        font = ui_font(size)
    if font.getlength(text) > available:
        while text and draw.textlength(text + "…", font=font) > available:
            text = text[:-1]
        text = text + "…" if text else ""
    if not text:
        return
    _, _, _, ink_height = font.getbbox(text, anchor="lt")
    draw.text((x, max(0, (height - ink_height) // 2)), text, fill=255, font=font, anchor="lt")


def _packed(image):
    return image.point(lambda value: 255 if value >= 100 else 0).convert("1", dither=Image.Dither.NONE).tobytes()


def list_title(item):
    """A 216×18 list title with the actual source mark and an abbreviated title."""
    image = Image.new("L", (216, 18))
    source = item["source"]
    _draw_icon(image, source, 0, 1, 16)
    text = item["title"]
    if source not in ICON_FILES:
        text = f"{SOURCE_NAMES.get(source, source)} · {text}"
    _text(ImageDraw.Draw(image), 19, image.width, image.height, text, size=18)
    return _packed(image)


def notification_row(item):
    if item["resolved"]:
        kind = 3
        item = {**item, "title": "已应答 " + item["title"]}
    else:
        kind = 0 if item["category"] == "response" else 4
    return {"kind": kind, "title": "~" + base64.b64encode(list_title(item)).decode("ascii")}


def _home_line(category, sources, count, label, compact_label):
    image = Image.new("L", (128, 16))
    draw = ImageDraw.Draw(image)
    visible = sources[:4] if len(sources) > 5 else sources
    x = 0
    for source in visible:
        _draw_icon(image, source, x, 2, HOME_MARK_SIZE)
        x += HOME_MARK_PITCH
    if len(sources) > len(visible):
        more = "+" + str(len(sources) - len(visible))
        font = ui_font(9)
        draw.text((x + 1, 3), more, font=font, fill=255, anchor="lt")
        x += 1 + int(draw.textlength(more, font=font))
    count_text = "999+" if count > 999 else str(count)
    x += 2
    text = f"{count_text} {label}"
    if ui_font(11).getlength(text) > image.width - x:
        text = f"{count_text} {compact_label}"
    if ui_font(11).getlength(text) > image.width - x:
        text = compact_label
    _text(draw, x, image.width, image.height, text, size=14, minimum=11)
    marks = "".join(HOME_MARK_KEYS.get(source, "u") for source in visible)
    return {"category": category, "sources": sources, "marks": marks, "bitmap": _packed(image)}


def home_lines(summary):
    """Render up to two cards, each with the source marks for that category."""
    lines = []
    if count := summary["completed_count"]:
        lines.append(_home_line("completed", tuple(summary["completed_sources"]), count, "个未读", "未读"))
    responses = summary["response_sources"]
    if count := summary.get("response_count", sum(entry["count"] for entry in responses)):
        sources = tuple(dict.fromkeys(entry["source"] for entry in responses))
        lines.append(_home_line("response", sources, count, "条待应答", "待应答"))
    return lines
