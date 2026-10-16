"""Readable Chinese line breaks without losing text or exceeding the width."""
import unittest

from recap.widget import wrap_lines


class WrapLinesTests(unittest.TestCase):
    def assert_complete_and_fitting(self, text, width, measure=len):
        lines = wrap_lines(text, width, measure)
        self.assertEqual("".join(lines), text.replace("\n", ""))
        self.assertTrue(all(measure(line) <= width for line in lines), lines)
        return lines

    def test_comma_at_boundary_moves_with_preceding_character(self):
        lines = self.assert_complete_and_fitting("完成需求，继续实现。", 4)
        self.assertEqual(lines, ["完成需", "求，继续", "实现。"])

    def test_closing_marks_and_quotes_never_start_wrapped_lines(self):
        closing = "，。、；：！？）》】〕〉」』〗〙〛”’…,.!?;:%‰)]}"
        for mark in closing:
            with self.subTest(mark=mark):
                lines = self.assert_complete_and_fitting("完成需求" + mark + "继续验证。", 4)
                self.assertTrue(all(not line.startswith(mark) for line in lines), lines)
        lines = self.assert_complete_and_fitting("需求确认。”继续实现！", 4)
        self.assertTrue(all(not line.startswith(tuple(closing)) for line in lines), lines)

    def test_exact_fit_and_existing_paragraph_breaks_are_preserved(self):
        self.assertEqual(self.assert_complete_and_fitting("甲乙，丙。", 3), ["甲乙，", "丙。"])
        self.assertEqual(self.assert_complete_and_fitting("第一段。\n\n第二段，继续。", 4),
                         ["第一段。", "", "第二段，", "继续。"])

    def test_long_filename_fallback_keeps_closing_marks_attached(self):
        lines = self.assert_complete_and_fitting("prefix-long-filename.zip，验证完成。", 8)
        self.assertTrue(all(not line.startswith(("，", "。", ".")) for line in lines), lines)

    def test_measured_width_controls_mixed_chinese_and_ascii_text(self):
        def measure(text):
            return sum(2 if "\u4e00" <= character <= "\u9fff" else 1 for character in text)
        lines = self.assert_complete_and_fitting("功能 abcdef，完成。", 8, measure)
        self.assertTrue(all(not line.startswith(("，", "。")) for line in lines), lines)

    def test_impossibly_narrow_group_preserves_text_and_width(self):
        # A one-character line cannot contain both an anchor and punctuation.
        # Keep the width/content guarantees rather than losing or clipping it.
        self.assertEqual(self.assert_complete_and_fitting("甲，乙", 1), ["甲", "，", "乙"])


if __name__ == "__main__":
    unittest.main()
