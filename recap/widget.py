"""Small Windows-native recap overlay; import starts no UI, service, or model."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import queue
import re
import threading
import time

from .runtime import data_path, request

Rect = tuple[int, int, int, int]
PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
CARD_WIDTH = 380
TEXT_SIZE = 14
LINE_PITCH = 23
PADDING_X, PADDING_Y = 22, 18
PARAGRAPH_GAP = 7


def wrap_lines(text, width, measure):
    """One shared layout for height calculation and painting; no text truncation."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for token in re.findall(r"[A-Za-z0-9_./:@#%+-]+|\s+|.", paragraph):
            if line and measure(line + token) > width:
                lines.append(line)
                line = ""
            if measure(token) <= width:
                line += token
            else:
                for character in token:
                    if line and measure(line + character) > width:
                        lines.append(line)
                        line = ""
                    line += character
        lines.append(line)
    return lines or [""]


def clamp_rect(rect: Rect, work: Rect) -> Rect:
    left, top, right, bottom = work
    width = min(max(1, rect[2] - rect[0]), max(1, right - left))
    height = min(max(1, rect[3] - rect[1]), max(1, bottom - top))
    x, y = min(max(rect[0], left), right - width), min(max(rect[1], top), bottom - height)
    return x, y, x + width, y + height


def anchored_rect(owner: Rect, work: Rect, width: int, height: int,
                  scale: float = 1, offset: tuple[float, float] = (-24, -90)) -> Rect:
    x, y = owner[2] + round(offset[0] * scale) - width, owner[3] + round(offset[1] * scale) - height
    return clamp_rect((x, y, x + width, y + height), work)


def relative_offset(rect: Rect, owner: Rect, scale: float) -> tuple[float, float]:
    return (rect[2] - owner[2]) / scale, (rect[3] - owner[3]) / scale


def atomic_json(path: Path, value: dict):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class WindowClass(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", PROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class PaintStruct(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL), ("rgbReserved", wt.BYTE * 32)]


class Margins(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class WinApi:
    NOACTIVATE, TOOLWINDOW, APPWINDOW = 0x08000000, 0x80, 0x40000
    TOPMOST = 0x00000008
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    SWP_SHOWWINDOW, SWP_NOOWNERZORDER = 0x0040, 0x0200

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows-only widget")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        definitions = {
            "GetForegroundWindow": ([], wt.HWND), "GetAncestor": ([wt.HWND, wt.UINT], wt.HWND),
            "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
            "GetClientRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
            "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            "IsWindowVisible": ([wt.HWND], wt.BOOL), "IsIconic": ([wt.HWND], wt.BOOL),
            "MonitorFromWindow": ([wt.HWND, wt.DWORD], wt.HANDLE),
            "GetMonitorInfoW": ([wt.HANDLE, ctypes.POINTER(MonitorInfo)], wt.BOOL),
            "GetCursorPos": ([ctypes.POINTER(wt.POINT)], wt.BOOL),
            "ShowWindow": ([wt.HWND, ctypes.c_int], wt.BOOL),
            "SetWindowPos": ([wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
            "RegisterClassW": ([ctypes.POINTER(WindowClass)], wt.WORD),
            "UnregisterClassW": ([wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
            "CreateWindowExW": ([wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p], wt.HWND),
            "DefWindowProcW": ([wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], ctypes.c_ssize_t),
            "DestroyWindow": ([wt.HWND], wt.BOOL), "PostQuitMessage": ([ctypes.c_int], None),
            "SetTimer": ([wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p], ctypes.c_size_t),
            "KillTimer": ([wt.HWND, ctypes.c_size_t], wt.BOOL),
            "GetMessageW": ([ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], ctypes.c_int),
            "TranslateMessage": ([ctypes.POINTER(wt.MSG)], wt.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wt.MSG)], ctypes.c_ssize_t),
            "LoadCursorW": ([wt.HINSTANCE, ctypes.c_void_p], wt.HANDLE),
            "InvalidateRect": ([wt.HWND, ctypes.POINTER(wt.RECT), wt.BOOL], wt.BOOL),
            "BeginPaint": ([wt.HWND, ctypes.POINTER(PaintStruct)], wt.HDC),
            "EndPaint": ([wt.HWND, ctypes.POINTER(PaintStruct)], wt.BOOL),
            "GetDC": ([wt.HWND], wt.HDC), "ReleaseDC": ([wt.HWND, wt.HDC], ctypes.c_int),
            "FillRect": ([wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH], ctypes.c_int),
            "DrawTextW": ([wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT], ctypes.c_int),
            "SetCapture": ([wt.HWND], wt.HWND), "ReleaseCapture": ([], wt.BOOL),
        }
        for name, (args, result) in definitions.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel.GetModuleHandleW.argtypes, self.kernel.GetModuleHandleW.restype = [wt.LPCWSTR], wt.HINSTANCE
        self.kernel.OpenProcess.argtypes, self.kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wt.BOOL
        self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [wt.HANDLE], wt.BOOL
        for name, args, result in (
            ("CreateSolidBrush", [wt.DWORD], wt.HBRUSH), ("DeleteObject", [wt.HANDLE], wt.BOOL),
            ("SelectObject", [wt.HDC, wt.HANDLE], wt.HANDLE), ("SetBkMode", [wt.HDC, ctypes.c_int], ctypes.c_int),
            ("SetTextColor", [wt.HDC, wt.DWORD], wt.DWORD),
            ("GetStockObject", [ctypes.c_int], wt.HANDLE),
            ("Ellipse", [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int], wt.BOOL),
            ("GetTextExtentPoint32W", [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.SIZE)], wt.BOOL),
            ("CreateFontW", [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR], wt.HFONT),
        ):
            function = getattr(self.gdi, name)
            function.argtypes, function.restype = args, result
        suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        self.get_long = getattr(self.user, "GetWindowLong" + suffix)
        self.get_long.argtypes, self.get_long.restype = [wt.HWND, ctypes.c_int], ctypes.c_ssize_t
        self.pid_cache = {}
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        self.dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        self.dwm.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, ctypes.POINTER(Margins)]
        self.dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

    def soften(self, hwnd):
        # Windows 11 owns corner clipping and its natural inactive-window shadow.
        for attribute, number in ((33, 2), (34, 0x00E8E9EB), (3, 1)):
            value = wt.DWORD(number)
            self.dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        margin = Margins(1, 1, 1, 1)
        self.dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margin))

    @staticmethod
    def enable_dpi_awareness():
        if os.name == "nt":
            try:
                function = ctypes.WinDLL("user32").SetProcessDpiAwarenessContext
                function.argtypes, function.restype = [ctypes.c_void_p], wt.BOOL
                function(ctypes.c_void_p(-4))
            except (OSError, AttributeError):
                try: ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
                except (OSError, AttributeError): pass

    def image_is_codex(self, pid):
        now = time.monotonic()
        cached = self.pid_cache.get(pid)
        if cached and now - cached[0] < 5: return cached[1]
        process = self.kernel.OpenProcess(0x1000, False, pid)
        matched = False
        if process:
            try:
                text = ctypes.create_unicode_buffer(32768)
                size = wt.DWORD(len(text))
                if self.kernel.QueryFullProcessImageNameW(process, 0, text, ctypes.byref(size)):
                    path = text.value.lower().replace("/", "\\")
                    matched = path.rsplit("\\", 1)[-1] in {"codex.exe", "chatgpt.exe"} and "openai.codex" in path
            finally: self.kernel.CloseHandle(process)
        self.pid_cache[pid] = now, matched
        if len(self.pid_cache) > 64: self.pid_cache = {pid: (now, matched)}
        return matched

    def foreground(self):
        hwnd = self.user.GetForegroundWindow()
        if not hwnd: return None
        hwnd = self.user.GetAncestor(hwnd, 2) or hwnd
        if not self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd): return None
        pid = wt.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(hwnd) if self.image_is_codex(pid.value) else None

    def rect(self, hwnd):
        value = wt.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(value)): raise ctypes.WinError(ctypes.get_last_error())
        return value.left, value.top, value.right, value.bottom

    def work_area(self, hwnd):
        value = MonitorInfo(); value.cbSize = ctypes.sizeof(value)
        if not self.user.GetMonitorInfoW(self.user.MonitorFromWindow(hwnd, 2), ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.rcWork.left, value.rcWork.top, value.rcWork.right, value.rcWork.bottom

    def dpi(self, hwnd):
        try:
            function = self.user.GetDpiForWindow
            function.argtypes, function.restype = [wt.HWND], wt.UINT
            return function(hwnd) or 96
        except AttributeError: return 96

    def cursor(self):
        point = wt.POINT(); self.user.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def show(self, hwnd, rect):
        # Make visibility and the topmost band one checked, non-activating
        # operation. SWP_NOZORDER must not be used with HWND_TOPMOST.
        flags = self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW | self.SWP_NOOWNERZORDER
        if not self.user.SetWindowPos(hwnd, ctypes.c_void_p(-1), rect[0], rect[1],
                                      rect[2]-rect[0], rect[3]-rect[1], flags):
            raise ctypes.WinError(ctypes.get_last_error())

    def is_topmost(self, hwnd):
        return bool(self.get_long(hwnd, -20) & self.TOPMOST)

    def ensure_topmost(self, hwnd):
        # Check cheaply; do not reorder an already-topmost window or compete
        # with other overlays on every animation frame.
        if self.is_topmost(hwnd):
            return False
        flags = self.SWP_NOSIZE | self.SWP_NOMOVE | self.SWP_NOACTIVATE | self.SWP_NOOWNERZORDER
        if not self.user.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return True

    def hide(self, hwnd):
        if hwnd: self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0004 | 0x0080)


class Widget:
    def __init__(self, path: Path, *, allow_display=True):
        self.path, self.allow_display = path, allow_display
        self.api = WinApi()
        self.hwnd = self.menu_hwnd = None
        self.rect, self.menu_rect = (0, 0, 0, 0), (0, 0, 0, 0)
        self.visible = self.closed = self.running = self.hover = self.action_busy = False
        self.snapshot, self.overview, self.owner = {}, "", None
        self.scale, self.font, self.font_scale = 1.0, None, None
        self.offset, self.drag_start, self.last_read, self.last_state = self.load_offset(), None, 0, None
        self.signature, self.size, self.actions = None, (CARD_WIDTH, 1), queue.Queue()
        self.lines = []
        self.instance = self.api.kernel.GetModuleHandleW(None)
        self.class_name = f"WorkRecapOverlay_{os.getpid()}_{id(self)}"
        self.callback = PROC(self.window_proc)
        value = WindowClass(3, self.callback, 0, 0, self.instance, None,
                            self.api.user.LoadCursorW(None, ctypes.c_void_p(32512)), None, None, self.class_name)
        if not self.api.user.RegisterClassW(ctypes.byref(value)): raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = self.create_window()

    def create_window(self):
        hwnd = self.api.user.CreateWindowExW(self.api.NOACTIVATE | self.api.TOOLWINDOW | self.api.TOPMOST,
                    self.class_name, "Codex 工作回顾", 0x80000000 | 0x00040000,
                    0, 0, 1, 1, None, None, self.instance, None)
        if not hwnd: raise ctypes.WinError(ctypes.get_last_error())
        self.api.soften(hwnd)
        return int(hwnd)

    def load_offset(self):
        try:
            value = json.loads((self.path / "widget-position.json").read_text(encoding="utf-8"))
            numbers = tuple(float(value[key]) for key in ("dx", "dy"))
            if all(abs(number) <= 10000 for number in numbers): return numbers
        except (OSError, ValueError, TypeError, KeyError): pass
        return -24.0, -90.0

    def read_display(self):
        try:
            value = json.loads((self.path / "display.json").read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                self.snapshot = value
                summary = value.get("summary")
                text = summary.get("overview", "") if isinstance(summary, dict) else ""
                self.overview = text.strip() if isinstance(text, str) else ""
        except (OSError, ValueError): self.snapshot = {**self.snapshot, "state": "error"}

    def font_for(self):
        if self.font_scale != self.scale:
            if self.font: self.api.gdi.DeleteObject(self.font)
            self.font = self.api.gdi.CreateFontW(-round(TEXT_SIZE*self.scale),0,0,0,400,0,0,0,1,0,0,6,0,"Microsoft YaHei UI")
            self.font_scale = self.scale
        return self.font

    def measure(self, width, work):
        dc = self.api.user.GetDC(self.hwnd)
        old = self.api.gdi.SelectObject(dc, self.font_for())
        content_width = max(1, width - 2 - 2 * round(PADDING_X*self.scale))
        def extent(text):
            size = wt.SIZE()
            length = len(text.encode("utf-16-le")) // 2
            self.api.gdi.GetTextExtentPoint32W(dc, text, length, ctypes.byref(size))
            return size.cx
        try:
            # Only visual paragraph breaks; the generated summary stays intact.
            paragraphs = [part for part in re.split(r"(?<=。)(?=\S)|\n+", self.overview) if part]
            self.lines = []
            for index, paragraph in enumerate(paragraphs):
                if index:
                    self.lines.append(None)
                self.lines.extend(wrap_lines(paragraph, content_width, extent))
        finally: self.api.gdi.SelectObject(dc, old); self.api.user.ReleaseDC(self.hwnd, dc)
        height = 2 + 2 * round(PADDING_Y*self.scale) + sum(
            round((PARAGRAPH_GAP if line is None else LINE_PITCH)*self.scale) for line in self.lines)
        return min(max(1, height), work[3]-work[1])

    @staticmethod
    def contains(rect, point):
        return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]

    def invalidate(self, hwnd=None):
        self.api.user.InvalidateRect(hwnd or self.hwnd, None, False)

    def fill(self, dc, rect, color):
        brush = self.api.gdi.CreateSolidBrush(color)
        self.api.user.FillRect(dc, ctypes.byref(rect), brush)
        self.api.gdi.DeleteObject(brush)

    def close_backdrop(self, dc, rect):
        brush = self.api.gdi.CreateSolidBrush(0x00F0F1F2)
        old_brush = self.api.gdi.SelectObject(dc, brush)
        old_pen = self.api.gdi.SelectObject(dc, self.api.gdi.GetStockObject(8))
        try:
            self.api.gdi.Ellipse(dc, rect.left, rect.top, rect.right, rect.bottom)
        finally:
            self.api.gdi.SelectObject(dc, old_pen)
            self.api.gdi.SelectObject(dc, old_brush)
            self.api.gdi.DeleteObject(brush)

    def paint(self, hwnd):
        state = PaintStruct(); dc = self.api.user.BeginPaint(hwnd, ctypes.byref(state))
        rect = wt.RECT(); self.api.user.GetClientRect(hwnd, ctypes.byref(rect))
        old = self.api.gdi.SelectObject(dc, self.font_for())
        try:
            self.fill(dc, rect, 0x00FAFCFD)
            self.api.gdi.SetBkMode(dc, 1); self.api.gdi.SetTextColor(dc, 0x0045413E)
            if hwnd == self.menu_hwnd:
                row = round(28*self.scale)
                for index, text in enumerate(("恢复自动总结" if self.snapshot.get("paused") else "暂停自动总结", "退出")):
                    bounds = wt.RECT(round(12*self.scale),index*row+round(6*self.scale),rect.right-4,(index+1)*row)
                    self.api.user.DrawTextW(dc,text,-1,ctypes.byref(bounds),0x0800)
            else:
                padding, pitch = round(PADDING_X*self.scale), round(LINE_PITCH*self.scale)
                top = round(PADDING_Y*self.scale)
                for line in self.lines:
                    if line is None:
                        top += round(PARAGRAPH_GAP*self.scale)
                        continue
                    bounds = wt.RECT(padding, top, rect.right-padding, top+pitch)
                    self.api.user.DrawTextW(dc,line,-1,ctypes.byref(bounds),0x0020 | 0x0004 | 0x0800)
                    top += pitch
                if self.hover:
                    close = wt.RECT(rect.right-round(22*self.scale),round(3*self.scale),
                                    rect.right-round(5*self.scale),round(20*self.scale))
                    self.close_backdrop(dc, close)
                    self.api.gdi.SetTextColor(dc,0x007B7774)
                    self.api.user.DrawTextW(dc,"×",-1,ctypes.byref(close),0x0020 | 0x0004 | 0x0001 | 0x0800)
        finally:
            self.api.gdi.SelectObject(dc,old); self.api.user.EndPaint(hwnd,ctypes.byref(state))

    def close_menu(self):
        if self.menu_hwnd:
            hwnd,self.menu_hwnd = self.menu_hwnd,None
            self.api.user.DestroyWindow(hwnd)

    def menu(self):
        self.close_menu(); self.menu_hwnd = self.create_window()
        x,y = self.api.cursor(); width,height = round(144*self.scale),round(56*self.scale)
        self.menu_rect = clamp_rect((x,y,x+width,y+height), self.api.work_area(self.owner or self.hwnd))
        if self.allow_display: self.api.show(self.menu_hwnd,self.menu_rect)
        self.invalidate(self.menu_hwnd)

    def toggle_paused(self):
        self.close_menu()
        if self.action_busy: return
        self.action_busy = True
        action = "resume" if self.snapshot.get("paused") else "pause"
        def worker():
            try: self.actions.put(request("/api/action",{"action":action},timeout=5))
            except Exception: self.actions.put({"state":"error"})
        threading.Thread(target=worker,daemon=True,name="recap-widget-action").start()

    def window_proc(self, hwnd, message, first, second):
        try:
            if message == 0x0083:  # WM_NCCALCSIZE: leave only a 1px DWM frame, no caption.
                bounds = ctypes.cast(second, ctypes.POINTER(wt.RECT)).contents
                bounds.left += 1; bounds.top += 1; bounds.right -= 1; bounds.bottom -= 1
                return 0
            if message == 0x0084: return 1  # HTCLIENT: preserve drag; do not expose resize handles.
            if message == 0x0021: return 3  # WM_MOUSEACTIVATE => MA_NOACTIVATE, including the menu.
            if message == 0x0014: return 1  # Paint the tiny surface ourselves without flicker.
            if message == 0x000F: self.paint(hwnd); return 0
            if message == 0x0113 and hwnd == self.hwnd: self.tick(); return 0
            if message == 0x0205 and hwnd == self.hwnd: self.menu(); return 0
            if message == 0x0201 and hwnd == self.hwnd:
                point=self.api.cursor()
                if self.hover and point[0] >= self.rect[2]-round(24*self.scale) and point[1] < self.rect[1]+round(26*self.scale): return 0
                self.drag_start=(point,self.rect); self.api.user.SetCapture(hwnd); return 0
            if message == 0x0200 and self.drag_start and hwnd == self.hwnd:
                if self.api.foreground() != self.owner: self.drag_start=None; self.api.user.ReleaseCapture(); return 0
                point,old=self.drag_start; cursor=self.api.cursor();dx,dy=cursor[0]-point[0],cursor[1]-point[1]
                self.rect=clamp_rect((old[0]+dx,old[1]+dy,old[2]+dx,old[3]+dy),self.api.work_area(self.owner))
                self.api.show(self.hwnd,self.rect);return 0
            if message == 0x0202:
                if hwnd == self.menu_hwnd:
                    if self.api.cursor()[1]-self.menu_rect[1] < round(28*self.scale): self.toggle_paused()
                    else: self.quit()
                    return 0
                if hwnd == self.hwnd:
                    point=self.api.cursor()
                    if self.drag_start and self.owner:
                        self.offset=relative_offset(self.rect,self.api.rect(self.owner),self.scale)
                        atomic_json(self.path/"widget-position.json",dict(zip(("dx","dy"),self.offset)))
                    elif self.hover and point[0] >= self.rect[2]-round(24*self.scale) and point[1] < self.rect[1]+round(26*self.scale): self.quit()
                    self.drag_start=None;self.api.user.ReleaseCapture();return 0
            if message == 0x0010 and hwnd == self.hwnd: self.quit();return 0
            if message == 0x0002 and hwnd == self.hwnd and self.running: self.api.user.PostQuitMessage(0);return 0
        except Exception:
            self.snapshot={**self.snapshot,"state":"error"}
        return self.api.user.DefWindowProcW(hwnd,message,first,second)

    def publish_state(self):
        state=self.snapshot.get("state","idle")
        if not isinstance(state,str) or state not in {"idle","collecting","summarizing","ready","paused","error"}:state="error"
        value={"pid":os.getpid(),"hwnd":self.hwnd,"visible":self.visible,"rect":list(self.rect),"state":state}
        if value != self.last_state: atomic_json(self.path/"widget-state.json",value);self.last_state=value

    def tick(self):
        if self.closed:return
        if (self.path/"widget.stop").exists(): self.quit();return
        now=time.monotonic()
        if now-self.last_read >= 2:self.read_display();self.last_read=now
        while not self.actions.empty():
            value=self.actions.get_nowait();self.action_busy=False
            if isinstance(value,dict) and "paused" in value:self.snapshot["paused"]=bool(value["paused"])
            if isinstance(value,dict) and value.get("state")=="error":self.snapshot["state"]="error"
        owner=self.api.foreground()
        if not owner or not self.overview or not self.allow_display:
            self.api.hide(self.hwnd);self.visible=False;self.drag_start=None;self.close_menu()
        else:
            self.owner=owner;self.scale=self.api.dpi(owner)/96;work=self.api.work_area(owner)
            signature=(self.overview,self.scale,work[2]-work[0],work[3]-work[1])
            if signature != self.signature:
                width=min(round(CARD_WIDTH*self.scale),work[2]-work[0]);self.size=(width,self.measure(width,work));self.signature=signature;self.invalidate()
            if not self.drag_start:
                rect=anchored_rect(self.api.rect(owner),work,*self.size,self.scale,self.offset)
                if rect != self.rect or not self.visible:self.rect=rect;self.api.show(self.hwnd,rect)
                self.visible=True
            point=self.api.cursor();hover=self.contains(self.rect,point)
            if hover != self.hover:self.hover=hover;self.invalidate()
            if self.menu_hwnd and not hover and not self.contains(self.menu_rect,point):self.close_menu()
        self.publish_state()

    def loop(self):
        self.running=True;self.api.user.SetTimer(self.hwnd,1,200,None);self.tick()
        message=wt.MSG()
        while not self.closed:
            result=self.api.user.GetMessageW(ctypes.byref(message),None,0,0)
            if result <= 0:break
            self.api.user.TranslateMessage(ctypes.byref(message));self.api.user.DispatchMessageW(ctypes.byref(message))

    def quit(self):
        if self.closed:return
        self.closed=True;self.visible=False;self.api.hide(self.hwnd);self.close_menu();self.publish_state()
        self.api.user.KillTimer(self.hwnd,1);self.api.user.DestroyWindow(self.hwnd)

    def dispose(self):
        if not self.closed:self.quit()
        if self.font:self.api.gdi.DeleteObject(self.font);self.font=None
        self.api.user.UnregisterClassW(self.class_name,self.instance)


def run():
    if os.name != "nt":return
    from .service import InstanceLock
    path=data_path()
    try:lock=InstanceLock(path/"widget.lock")
    except OSError:return
    widget=None
    try:
        (path/"widget.stop").unlink(missing_ok=True)
        from .island_widget import IslandWidget
        WinApi.enable_dpi_awareness();widget=IslandWidget(path);widget.loop()
    finally:
        if widget is not None:widget.dispose()
        lock.close()
