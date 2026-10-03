"""Pure API contracts and opt-in smoke tests using only our own windows."""
import ctypes
from ctypes import wintypes as wt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from recap.widget import WinApi


class WindowZOrderTests(unittest.TestCase):
    def setUp(self):
        self.api = WinApi.__new__(WinApi)
        self.api.user = Mock()
        self.api.get_long = Mock(return_value=0)
        self.api.user.SetWindowPos.return_value = True

    def test_show_sets_topmost_and_visibility_without_activation(self):
        self.api.show(42, (10, 20, 130, 60))
        args = self.api.user.SetWindowPos.call_args.args
        self.assertEqual(args[0], 42)
        self.assertEqual(args[1].value, ctypes.c_void_p(-1).value)
        self.assertEqual(args[2:6], (10, 20, 120, 40))
        self.assertTrue(args[6] & 0x0010)  # SWP_NOACTIVATE
        self.assertTrue(args[6] & 0x0040)  # SWP_SHOWWINDOW
        self.assertFalse(args[6] & 0x0004)  # Never ignore HWND_TOPMOST.
        self.api.user.ShowWindow.assert_not_called()

    def test_already_topmost_window_is_not_reordered(self):
        self.api.get_long.return_value = self.api.TOPMOST | self.api.NOACTIVATE
        self.assertFalse(self.api.ensure_topmost(42))
        self.api.user.SetWindowPos.assert_not_called()

    def test_lost_topmost_is_restored_without_moving_resizing_or_activating(self):
        self.assertTrue(self.api.ensure_topmost(42))
        args = self.api.user.SetWindowPos.call_args.args
        self.assertEqual(args[0], 42)
        self.assertEqual(args[1].value, ctypes.c_void_p(-1).value)
        self.assertEqual(args[2:6], (0, 0, 0, 0))
        self.assertEqual(args[6] & (0x0001 | 0x0002 | 0x0010), 0x0013)
        self.assertFalse(args[6] & (0x0004 | 0x0040))

    def test_show_and_repair_report_setwindowpos_failure(self):
        self.api.user.SetWindowPos.return_value = False
        with self.assertRaises(OSError):
            self.api.show(42, (10, 20, 130, 60))
        with self.assertRaises(OSError):
            self.api.ensure_topmost(42)


@unittest.skipUnless(os.name == "nt" and os.environ.get("WORK_RECAP_NATIVE_UI_TEST") == "1",
                     "Opt-in native smoke test creates only temporary test windows")
class NativeWindowZOrderTests(unittest.TestCase):
    def test_layered_island_stays_above_ordinary_test_window_without_focus_theft(self):
        from recap.island_widget import IslandWidget

        with tempfile.TemporaryDirectory() as directory:
            widget = IslandWidget(Path(directory), allow_display=False)
            api, ordinary = widget.api, None
            foreground = api.user.GetForegroundWindow()
            api.user.GetWindow.argtypes, api.user.GetWindow.restype = [wt.HWND, wt.UINT], wt.HWND

            def above(first, second):
                candidate = first
                for _ in range(4096):
                    candidate = api.user.GetWindow(candidate, 2)  # GW_HWNDNEXT
                    if candidate == second:
                        return True
                    if not candidate:
                        return False
                return False

            try:
                self.assertTrue(api.is_topmost(widget.hwnd))
                self.assertTrue(api.get_long(widget.hwnd, -20) & api.NOACTIVATE)
                work = api.work_area(widget.hwnd)
                x, y = work[0] + 24, work[3] - 48
                ordinary = api.user.CreateWindowExW(api.NOACTIVATE | api.TOOLWINDOW,
                    widget.class_name, "WorkRecap temporary ordinary test window", 0x80000000,
                    x, y, 60, 24, None, None, widget.instance, None)
                self.assertTrue(ordinary)
                self.assertTrue(api.user.SetWindowPos(ordinary, None, x, y, 60, 24, 0x0010 | 0x0040))
                widget.painter.render(widget.hwnd, (x, y, 60, 24), 1, [], expanded=False)
                api.show(widget.hwnd, (x, y, x + 60, y + 24))
                self.assertTrue(api.is_topmost(widget.hwnd))
                self.assertTrue(above(widget.hwnd, ordinary))
                self.assertEqual(api.user.GetForegroundWindow(), foreground)

                # Simulate the observed missing bit on our own widget only.
                self.assertTrue(api.user.SetWindowPos(widget.hwnd, ctypes.c_void_p(-2),
                                                     0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002))
                self.assertFalse(api.is_topmost(widget.hwnd))
                self.assertTrue(api.ensure_topmost(widget.hwnd))
                self.assertFalse(api.ensure_topmost(widget.hwnd))
                widget.painter.render(widget.hwnd, (x, y, 72, 28), 1, [], expanded=False)
                self.assertTrue(api.user.SetWindowPos(ordinary, None, x, y, 60, 24, 0x0010 | 0x0040))
                self.assertTrue(api.is_topmost(widget.hwnd))
                self.assertTrue(above(widget.hwnd, ordinary))
                self.assertEqual(api.user.GetForegroundWindow(), foreground)

                api.hide(widget.hwnd)
                api.show(widget.hwnd, (x, y, x + 60, y + 24))
                self.assertTrue(api.is_topmost(widget.hwnd))
                self.assertEqual(api.user.GetForegroundWindow(), foreground)
            finally:
                if ordinary:
                    api.user.DestroyWindow(ordinary)
                widget.dispose()


if __name__ == "__main__":
    unittest.main()
