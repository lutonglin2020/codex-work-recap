"""Windows GDI drawing checks on an isolated DIB; no UI is shown."""
import os
import unittest

from recap.island_widget import PAD_Y, CAPTION_HEIGHT, PITCH


@unittest.skipUnless(os.name == "nt", "GDI+ drawing requires Windows")
class NativeDrawingTests(unittest.TestCase):
    def test_body_survives_layout_at_common_dpis(self):
        from render_native_preview import SYNTHETIC_SNAPSHOT, render_snapshot
        original = SYNTHETIC_SNAPSHOT["summary"]["overview"].replace("\n", "")
        for scale in (1, 1.5, 1.75, 2):
            with self.subTest(scale=scale):
                rendered = render_snapshot(SYNTHETIC_SNAPSHOT, scale=scale, hovering=True)
                self.assertEqual("".join(line[0] for line in rendered["lines"]), original)
                self.assertEqual(rendered["lines"][0][2], PAD_Y + CAPTION_HEIGHT)
                self.assertGreaterEqual(rendered["height"] / scale - 24,
                                        rendered["lines"][-1][2] + PITCH + PAD_Y - 1)
                self.assertEqual(rendered["rgba"][:4], b"\x00\x00\x00\x00")
                self.assertIn(255, rendered["rgba"][3::4])

    def test_retained_summary_has_no_new_dot_or_new_label(self):
        from render_native_preview import SYNTHETIC_SNAPSHOT, render_snapshot
        retained = {**SYNTHETIC_SNAPSHOT, "summary": {**SYNTHETIC_SNAPSHOT["summary"], "retained": True}}
        old = render_snapshot(retained, expanded=False)
        seen = render_snapshot(SYNTHETIC_SNAPSHOT, expanded=False, presented=True)
        pending = render_snapshot(SYNTHETIC_SNAPSHOT, expanded=False)
        self.assertEqual(old["rgba"], seen["rgba"])
        self.assertNotEqual(old["rgba"], pending["rgba"])
        self.assertEqual(old["caption"], "上次回顾  ·  10/16 10:24")
        self.assertFalse(old["pending"])

    def test_wrapped_body_keeps_the_complete_cached_text(self):
        from render_native_preview import SYNTHETIC_SNAPSHOT, render_snapshot
        overview = ("合成排版测试：批量导出已完成，下载入口可以复用已有文件；"
                    "登录异常已定位，修复仍需在真实环境验证。"
                    "长路径 example/exports/" + "artifact_" * 12 + ".zip 也应完整保留。")
        snapshot = {**SYNTHETIC_SNAPSHOT, "summary": {**SYNTHETIC_SNAPSHOT["summary"], "overview": overview}}
        for scale in (1, 1.75):
            with self.subTest(scale=scale):
                rendered = render_snapshot(snapshot, scale=scale)
                self.assertGreater(len(rendered["lines"]), 2)
                self.assertEqual("".join(line[0] for line in rendered["lines"]), overview)
                self.assertGreaterEqual(rendered["height"] / scale - 24,
                                        rendered["lines"][-1][2] + PITCH + PAD_Y - 1)

    def test_empty_status_does_not_gain_summary_metadata_or_full_width(self):
        from render_native_preview import render_snapshot
        rendered = render_snapshot({"state": "idle", "summary": None}, scale=1)
        self.assertEqual(rendered["caption"], "")
        self.assertEqual(rendered["lines"][0][2], PAD_Y)
        self.assertLess(rendered["width"], 240)
        self.assertFalse(rendered["pending"])

    def test_chinese_punctuation_at_actual_native_line_boundary(self):
        from render_native_preview import SYNTHETIC_SNAPSHOT, render_snapshot
        overview = "需求" * 18 + "，" + "方案" * 18 + "。"
        snapshot = {**SYNTHETIC_SNAPSHOT, "summary": {**SYNTHETIC_SNAPSHOT["summary"], "overview": overview}}
        for scale in (1, 1.5, 1.75, 2):
            with self.subTest(scale=scale):
                rendered = render_snapshot(snapshot, scale=scale)
                lines = [line[0] for line in rendered["lines"]]
                self.assertGreater(len(lines), 1)
                self.assertEqual("".join(lines), overview)
                self.assertTrue(all(not line.startswith(("，", "。")) for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
