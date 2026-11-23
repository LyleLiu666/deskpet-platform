import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from deskpet_platform.client import _bitmap, _text_font


class TextDisplayTests(unittest.TestCase):
    def test_windows_loads_chinese_font_from_the_system_font_directory(self):
        root = Path("system directory")
        with patch("deskpet_platform.client.sys.platform", "win32"), patch.dict(
                "os.environ", {"WINDIR": str(root)}), patch(
                "deskpet_platform.client.ImageFont.truetype",
                side_effect=[OSError("YaHei unavailable"), sentinel.simhei]) as truetype:
            self.assertIs(_text_font(16), sentinel.simhei)
        self.assertEqual(truetype.call_args.args, (root / "Fonts" / "simhei.ttf",))

    def test_windows_renders_chinese_with_bundled_font_when_system_fonts_are_missing(self):
        from PIL import ImageFont

        original = ImageFont.truetype

        def bundled_only(path, *args, **kwargs):
            if Path(path).name != "NotoSansCJKsc-Regular.otf":
                raise OSError("system font absent")
            return original(path, *args, **kwargs)

        with patch("deskpet_platform.client.sys.platform", "win32"), patch(
                "deskpet_platform.client.ImageFont.truetype", side_effect=bundled_only):
            bitmap = _bitmap("托肯测试：任务完成 7391")
            self.assertTrue(any(bitmap))
            self.assertNotEqual(bitmap, _bitmap("托肯测试：需要应答 7391"))

    def test_chinese_and_two_lines_are_rendered(self):
        chinese = _bitmap("你好，桌宠！")
        self.assertEqual(len(chinese), 288 * 48 // 8)
        self.assertTrue(any(chinese))
        self.assertNotEqual(chinese, _bitmap("再见，桌宠！"))
        self.assertTrue(any(_bitmap("第一行\n第二行")))

    def test_empty_control_characters_and_overflow_are_rejected(self):
        for text in ("", "   ", "a\tb", "太长" * 100, "一\n二\n三"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    _bitmap(text)
