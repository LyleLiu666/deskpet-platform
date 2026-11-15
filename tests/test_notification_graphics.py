import unittest

from PIL import Image

from deskpet_platform import notification_graphics as graphics


class NotificationGraphicsTests(unittest.TestCase):
    def test_codex_keeps_its_source_name_with_the_chatgpt_mark(self):
        self.assertEqual(graphics.ICON_FILES["codex"], "chatgpt")
        self.assertEqual(graphics.SOURCE_NAMES["codex"], "Codex")

    def test_all_requested_sources_have_distinct_packaged_marks(self):
        sources = ("codex", "claude_code", "workbuddy", "antigravity", "opencode")
        bitmaps = [graphics.list_title({"source": source, "title": "example"}) for source in sources]
        self.assertEqual(len(set(bitmaps)), len(sources))
        for bitmap in bitmaps:
            self.assertEqual(len(bitmap), 216 * 18 // 8)
            icon = Image.frombytes("1", (216, 18), bitmap).crop((0, 0, 18, 18))
            self.assertIsNotNone(icon.getbbox())

    def test_home_summary_combines_each_category_into_one_row(self):
        summary = {"completed_count": 50,
                   "completed_sources": ["codex", "claude_code", "workbuddy", "antigravity", "opencode"],
                   "response_count": 4,
                   "response_sources": [{"source": source, "count": 1, "id": index, "title": "测试项目"}
                                        for index, source in enumerate(("codex", "claude_code", "workbuddy", "opencode"), 1)]}
        lines = graphics.home_lines(summary)
        self.assertEqual([line["category"] for line in lines], ["completed", "response"])
        self.assertEqual(lines[0]["sources"], tuple(summary["completed_sources"]))
        self.assertEqual(lines[1]["sources"], ("codex", "claude_code", "workbuddy", "opencode"))
        self.assertEqual(lines[1]["marks"], "gcwo")
        pending = Image.frombytes("1", (128, 16), lines[1]["bitmap"])
        for index in range(4):
            self.assertIsNotNone(pending.crop((index * 14, 2, index * 14 + 12, 14)).getbbox())
            self.assertIsNone(pending.crop((index * 14 + 12, 0, index * 14 + 14, 16)).getbbox())
        for line in lines:
            self.assertEqual(len(line["bitmap"]), 128 * 16 // 8)
            self.assertIsNotNone(Image.frombytes("1", (128, 16), line["bitmap"]).getbbox())

    def test_empty_and_unknown_source_are_safe_and_identifiable(self):
        self.assertEqual(graphics.home_lines({"completed_count": 0, "completed_sources": [], "response_sources": []}), [])
        known = graphics.list_title({"source": "codex", "title": "task"})
        unknown = graphics.list_title({"source": "new_agent", "title": "task"})
        self.assertNotEqual(known, unknown)
        self.assertEqual(len(unknown), 216 * 18 // 8)


if __name__ == "__main__":
    unittest.main()
