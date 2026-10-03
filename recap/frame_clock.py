"""Coalesced frame notifications paced by the Windows compositor, while active."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import threading
import time

WM_FRAME = 0x8000 + 37


class FrameClock:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.active = threading.Event()
        self.stopping = threading.Event()
        self.pending = threading.Event()
        self.user = ctypes.WinDLL("user32", winmode=0x800)
        self.user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self.user.PostMessageW.restype = wt.BOOL
        self.kernel = ctypes.WinDLL("kernel32", winmode=0x800)
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        self.kernel.CreateEventW.restype = wt.HANDLE
        self.kernel.SetEvent.argtypes = [wt.HANDLE]
        self.kernel.SetEvent.restype = wt.BOOL
        self.kernel.CloseHandle.argtypes = [wt.HANDLE]
        self.kernel.CloseHandle.restype = wt.BOOL
        self.stop_handle = self.kernel.CreateEventW(None, True, False, None)
        if not self.stop_handle:
            raise ctypes.WinError()
        self.handles = (wt.HANDLE * 1)(self.stop_handle)
        self.wait_for_frame = None
        self.mode = "dwm-flush"
        try:
            self.compositor = ctypes.WinDLL("dcomp", winmode=0x800)
            self.wait_for_frame = self.compositor.DCompositionWaitForCompositorClock
            self.wait_for_frame.argtypes = [wt.UINT, ctypes.POINTER(wt.HANDLE), wt.DWORD]
            self.wait_for_frame.restype = wt.DWORD
            self.mode = "compositor-clock"
        except (OSError, AttributeError):
            self.compositor = ctypes.WinDLL("dwmapi", winmode=0x800)
            self.compositor.DwmFlush.argtypes = []
            self.compositor.DwmFlush.restype = ctypes.c_long
        self.thread = threading.Thread(target=self._run, daemon=True, name="recap-compositor-clock")
        self.thread.start()

    def set_active(self, value):
        if value:
            self.active.set()
        else:
            self.active.clear()

    def acknowledge(self):
        self.pending.clear()

    def _run(self):
        try:
            self._run_loop()
        except Exception:
            self.mode = "clock-error"
            self.active.clear()
            import logging
            logging.exception("recap compositor clock failed")

    def _run_loop(self):
        while not self.stopping.is_set():
            if not self.active.wait(.25):
                continue
            if self.stopping.is_set():
                break
            if self.wait_for_frame:
                status = self.wait_for_frame(1, self.handles, 50)
                if status == 0 or self.stopping.is_set():
                    break  # The stop event, not the compositor, was signaled.
                if status != 1:
                    # Occlusion/suspended displays must never cause a busy loop.
                    self.stopping.wait(.025)
                    continue
            else:
                started = time.monotonic()
                self.compositor.DwmFlush()
                if time.monotonic()-started < .002:
                    self.stopping.wait(.008)
            if self.active.is_set() and not self.pending.is_set():
                self.pending.set()
                if not self.user.PostMessageW(self.hwnd, WM_FRAME, 0, 0):
                    self.pending.clear()

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.active.set()
        self.kernel.SetEvent(self.stop_handle)
        self.thread.join(timeout=1)
        self.kernel.CloseHandle(self.stop_handle)
