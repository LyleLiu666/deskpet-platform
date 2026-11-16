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
