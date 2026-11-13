import unittest

from deskpet_platform.client import _bitmap


class TextDisplayTests(unittest.TestCase):
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
