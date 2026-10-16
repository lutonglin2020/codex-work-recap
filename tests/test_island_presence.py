import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from recap.island_widget import IslandWidget, empty_status, inner_width, summary_caption, PAD_X
from recap.reveal_policy import RevealPolicy


class IslandPresenceTests(unittest.TestCase):
    def setUp(self):
        self.widget = IslandWidget.__new__(IslandWidget)
        self.widget.api = Mock()
        self.widget.api.foreground.side_effect = AssertionError("Codex foreground filtering must not gate the island")
        self.widget.api.user.GetForegroundWindow.return_value = 200
        self.widget.api.dpi.side_effect = lambda hwnd: {99: 96, 200: 144, 300: 192}[hwnd]
        self.widget.api.work_area.side_effect = lambda hwnd: {
            99: (0, 0, 1920, 1040), 200: (1920, 0, 3840, 1400), 300: (-2560, 0, 0, 1400)
        }[hwnd]
        self.widget.fullscreen = Mock(return_value=False)
        self.widget.hwnd = 99
        self.widget.screen_initialized = False
        self.widget.overview = "已完成项目工作。"
        self.widget.allow_display = True
        self.widget.allowed = False
        self.widget.visible = False
        self.widget.menu_hwnd = None
        self.widget.last_topmost_check = 0.0
        self.idle = patch("recap.island_widget.windows_idle_seconds", return_value=37)
        self.idle.start()
        self.addCleanup(self.idle.stop)

    def tearDown(self):
        self.widget.api.foreground.assert_not_called()

    def test_other_application_foreground_still_allows_presence(self):
        self.widget.refresh_environment(100)
        self.assertTrue(self.widget.allowed)
        self.assertEqual(self.widget.foreground_hwnd, 200)
        self.assertFalse(self.widget.foreground_fullscreen)
        self.assertEqual(self.widget.idle, 37)
        self.assertEqual(self.widget.last_environment, 100)

    def test_zero_foreground_does_not_hide_and_initial_anchor_is_own_window(self):
        self.widget.api.user.GetForegroundWindow.return_value = 0
        self.widget.refresh_environment(100)
        self.assertTrue(self.widget.allowed)
        self.assertFalse(self.widget.foreground_fullscreen)
        self.widget.fullscreen.assert_not_called()
        self.widget.api.dpi.assert_called_once_with(99)
        self.widget.api.work_area.assert_called_once_with(99)

    def test_minimized_codex_is_not_consulted_as_visibility_gate(self):
        self.widget.api.user.IsIconic.return_value = True
        self.widget.refresh_environment(100)
        self.assertTrue(self.widget.allowed)
        self.widget.api.user.IsIconic.assert_not_called()

    def test_initial_screen_then_own_monitor_does_not_follow_other_apps(self):
        self.widget.refresh_environment(100)
        self.assertEqual(self.widget.scale, 1.5)
        self.assertEqual(self.widget.work, (1920, 0, 3840, 1400))
        # The controller sets this after placing its first visible window.
        self.widget.screen_initialized = True
        self.widget.api.user.GetForegroundWindow.return_value = 300
        self.widget.refresh_environment(101)
        self.assertEqual(self.widget.scale, 1)
        self.assertEqual(self.widget.work, (0, 0, 1920, 1040))
        self.widget.api.dpi.assert_called_with(99)
        self.widget.api.work_area.assert_called_with(99)

    def test_fullscreen_is_a_reveal_flag_not_a_presence_gate(self):
        self.widget.fullscreen.return_value = True
        self.widget.refresh_environment(100)
        self.assertTrue(self.widget.foreground_fullscreen)
        self.assertTrue(self.widget.allowed)

    def test_empty_summary_keeps_strip_but_explicit_disable_hides(self):
        for overview, enabled in (("", True), ("项目工作", False), ("", False)):
            with self.subTest(overview=overview, enabled=enabled):
                self.widget.overview, self.widget.allow_display = overview, enabled
                self.widget.refresh_environment(100)
                self.assertEqual(self.widget.allowed, enabled)

    def test_expired_summary_is_replaced_by_status_without_old_id(self):
        self.widget.path = Path("unused")
        self.widget.policy = RevealPolicy()
        self.widget.summary_id = "old-recap"
        self.widget.read_json = Mock(return_value={"state": "idle", "summary": None})
        self.widget.read_display()
        self.widget.refresh_environment(100)
        self.assertTrue(self.widget.allowed)
        self.assertIsNone(self.widget.summary_id)
        self.assertEqual("暂无新进展", self.widget.overview)
        self.assertNotIn("已完成项目工作", self.widget.overview)

    def test_retained_summary_survives_idle_error_and_updates_without_new_badge(self):
        self.widget.path = Path("unused")
        self.widget.policy = RevealPolicy()
        previous = {"id": "previous", "period_label": "过去的工作片段",
                    "overview": "导出功能已完成。", "retained": True,
                    "generated_at": "2026-10-15T18:40:00+08:00"}
        for state in ("idle", "collecting", "summarizing", "error", "paused"):
            self.widget.read_json = Mock(return_value={"state": state, "summary": previous})
            self.widget.read_display()
            self.assertEqual(self.widget.overview, previous["overview"])
            self.assertEqual(self.widget.summary_id, "previous|过去的工作片段")
            self.assertTrue(self.widget.retained)
            self.assertFalse(self.widget.can_auto_reveal())
            self.assertFalse(self.widget.has_new_summary())
            self.assertEqual(self.widget.caption, "上次回顾  ·  10/15 18:40")

        self.widget.read_json = Mock(return_value={"state": "ready", "summary": {
            "id": "new", "overview": "新修复已完成。", "retained": False}})
        self.widget.read_display()
        self.assertEqual(self.widget.overview, "新修复已完成。")
        self.assertFalse(self.widget.retained)
        self.assertTrue(self.widget.can_auto_reveal())
        self.assertTrue(self.widget.has_new_summary())
        self.assertEqual(self.widget.caption, "最新回顾")

    def test_caption_uses_cached_generation_time_in_recap_timezone(self):
        self.assertEqual(summary_caption({"overview": "合成回顾", "generated_at": "2026-10-16T02:24:00Z"}),
                         "最新回顾  ·  10/16 10:24")
        self.assertEqual(summary_caption({"overview": "合成回顾", "generated_at": "2026-10-16T10:24:00"}),
                         "最新回顾  ·  10/16 10:24")

    def test_missing_or_invalid_timestamp_does_not_invent_a_fresh_time(self):
        for value in (None, "", "invalid", 1790000000):
            self.assertEqual(summary_caption({"overview": "合成回顾", "generated_at": value}), "最新回顾")
        self.assertEqual(summary_caption(None), "")
        self.assertEqual(summary_caption({"overview": "  "}), "")

    def test_status_clears_caption_from_a_previous_summary(self):
        self.widget.path = Path("unused")
        self.widget.policy = RevealPolicy()
        self.widget.caption = "上次回顾  ·  10/15 18:40"
        self.widget.read_json = Mock(return_value={"state": "error", "summary": None})
        self.widget.read_display()
        self.assertEqual(self.widget.caption, "")
        self.assertFalse(self.widget.has_new_summary())

    def test_status_fits_content_while_recap_keeps_reading_width(self):
        for scale in (1, 1.5, 1.75, 2):
            with self.subTest(scale=scale):
                text_width = round(117*scale)
                width = inner_width(1920, scale, False, text_width)
                self.assertGreaterEqual(width, text_width+round(2*PAD_X*scale))
                self.assertLess(width, round(240*scale))
                self.assertEqual(inner_width(1920, scale, True), round(560*scale))
                self.assertLessEqual(inner_width(150, scale, False, text_width), 150)

    def test_error_status_stays_truthful_and_never_auto_expands(self):
        self.assertEqual(empty_status({"state": "error"}), "更新失败，稍后重试")
        policy = RevealPolicy()
        self.assertEqual(policy.step(100, "status:empty", True, 90,
                                    automatic_allowed=False), "closed")

    def test_visible_island_checks_topmost_at_most_once_per_second(self):
        self.widget.visible = True
        self.widget.refresh_environment(100)
        self.widget.api.ensure_topmost.assert_called_once_with(99)
        self.widget.refresh_environment(100.2)
        self.widget.refresh_environment(100.9)
        self.widget.api.ensure_topmost.assert_called_once_with(99)
        self.widget.refresh_environment(101)
        self.assertEqual(self.widget.api.ensure_topmost.call_count, 2)

    def test_hidden_or_disabled_island_does_not_request_topmost(self):
        self.widget.refresh_environment(100)
        self.widget.visible, self.widget.allow_display = True, False
        self.widget.refresh_environment(101)
        self.widget.api.ensure_topmost.assert_not_called()

    def test_visible_menu_uses_same_nonactivating_topmost_check(self):
        self.widget.visible, self.widget.menu_hwnd = True, 101
        self.widget.refresh_environment(100)
        self.assertEqual([call.args for call in self.widget.api.ensure_topmost.call_args_list], [(99,), (101,)])


if __name__ == "__main__":
    unittest.main()
