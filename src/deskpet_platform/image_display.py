"""Prepare an image for the board's 320 × 240 RGB565 screen."""
from array import array
from pathlib import Path
from io import BytesIO
import sys

from PIL import Image, ImageOps


def _fit_screen(image):
    # Scale in either direction while preserving the original proportions.
    if image.width * 240 >= image.height * 320:
        size = (320, max(1, round(image.height * 320 / image.width)))
    else:
        size = (max(1, round(image.width * 240 / image.height)), 240)
    image = image.resize(size, Image.Resampling.LANCZOS)
    background = Image.new("RGB", (320, 240), "black")
    background.paste(image, ((320 - image.width) // 2, (240 - image.height) // 2), image)
    return background


def _load_image(source):
    try:
        with Image.open(Path(source)) as original:
            original.load()
            return ImageOps.exif_transpose(original).convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError("无法读取图片，请选择受支持的图片文件。") from error


def _legacy_screen(image):
    image = ImageOps.contain(image, (320, 240), Image.Resampling.LANCZOS)
    background = Image.new("RGB", (320, 240), "black")
    background.paste(image, ((320 - image.width) // 2, (240 - image.height) // 2), image)
    return background


def _rgb565(image):
    raw = image.tobytes()
    pixels = array("H", (((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3)
                         for i in range(0, len(raw), 3)))
    if sys.byteorder != "little":
        pixels.byteswap()
    return pixels.tobytes()


def prepare_image(source):
    return _rgb565(_legacy_screen(_load_image(source)))


def prepare_photo(source):
    return _photo_package(_legacy_screen(_load_image(source)))

def prepare_jpeg_photo(source):
    """Prepare a baseline JPEG for a 480 × 320 device without cropping."""
    image=_load_image(source)
    image.thumbnail((960,640),Image.Resampling.LANCZOS)
    background=Image.new('RGBA',image.size,'white')
    background.alpha_composite(image)
    output=BytesIO()
    background.convert('RGB').save(output,format='JPEG',quality=90,subsampling=2,progressive=False,optimize=True)
    payload=output.getvalue()
    if len(payload)>2*1024*1024:raise ValueError('图片转换后超过设备限制。')
    return payload


def _photo_package(image):
    view = Image.new("RGB", (320, 176), "black")
    view.paste(image.resize((234, 176), Image.Resampling.LANCZOS), (43, 0))
    return _rgb565(view), _rgb565(image.resize((96, 72), Image.Resampling.LANCZOS))


def prepare_photo_with_fullscreen(source):
    image = _load_image(source)
    full, thumb = _photo_package(_legacy_screen(image))
    return full, thumb, _rgb565(_fit_screen(image))
