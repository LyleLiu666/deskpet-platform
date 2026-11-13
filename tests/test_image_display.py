from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest

from PIL import Image

from deskpet_platform.image_display import prepare_image, prepare_photo, prepare_photo_with_fullscreen


class ImageDisplayTests(unittest.TestCase):
    def test_image_is_fitted_without_distortion_and_converted_to_rgb565(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "red.png"
            Image.new("RGB", (20, 10), "red").save(source)
            pixels = prepare_image(source)
            self.assertEqual(len(pixels), 320 * 240 * 2)
            def pixel(x, y):
                offset = 2 * (y * 320 + x)
                return int.from_bytes(pixels[offset:offset + 2], "little")
            self.assertEqual(pixel(0, 0), 0)  # black letterbox
            self.assertEqual(pixel(160, 120), 0xf800)
            self.assertEqual(pixel(1, 40), 0xf800)  # small images use the available width
            view, thumb = prepare_photo(source)
            self.assertEqual(len(view), 320 * 176 * 2)
            self.assertEqual(len(thumb), 96 * 72 * 2)
            self.assertEqual(view[2*(88*320+160):2*(88*320+160)+2], b"\x00\xf8")

    def test_bad_image_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad"
            source.write_bytes(b"not an image")
            with self.assertRaises(ValueError):
                prepare_image(source)

    def test_fullscreen_album_photo_fills_width_or_height_and_preserves_old_package(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.png"
            for size, edge_points in (
                ((40, 20), ((0, 120), (319, 120))),
                ((20, 40), ((160, 0), (160, 239))),
                ((20, 15), ((0, 0), (319, 239))),
            ):
                with self.subTest(size=size):
                    Image.new("RGB", size, "red").save(source)
                    full, thumb, screen = prepare_photo_with_fullscreen(source)
                    self.assertEqual((full, thumb), prepare_photo(source))
                    self.assertEqual(len(screen), 320 * 240 * 2)
                    for x, y in edge_points:
                        offset = 2 * (y * 320 + x)
                        self.assertEqual(screen[offset:offset + 2], b"\x00\xf8")
