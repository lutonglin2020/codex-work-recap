"""A single, non-activating capsule that expands at the screen's top edge."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import datetime as dt
import json
import os
from pathlib import Path
import queue
import threading
import time

from .island_paint import IslandPainter, SHADOW, BODY_FONT
from .frame_clock import FrameClock, WM_FRAME
from .reveal_policy import RevealPolicy
from .runtime import request
from .service import windows_idle_seconds
from .widget import (WinApi, WindowClass, MonitorInfo, PaintStruct, PROC,
                     atomic_json, clamp_rect, wrap_lines)

WIDTH, FONT, PITCH = 560, BODY_FONT, 23
PAD_X, PAD_Y, GAP = 28, 16, 8
CAPTION_HEIGHT = 22
COLLAPSED = (120, 22)


def empty_status(snapshot):
    """A manual-only status, never presented as a generated work summary."""
    if snapshot.get("paused"):
        return "自动回顾已暂停"
    return {
        "collecting": "正在查看新进展…",
        "summarizing": "正在整理…",
        "error": "更新失败，稍后重试",
    }.get(snapshot.get("state"), "暂无新进展")


def summary_caption(summary):
    """Only label cached source facts; never infer freshness from the clock."""
    if not isinstance(summary, dict) or not str(summary.get("overview") or "").strip():
        return ""
    label = "上次回顾" if summary.get("retained") else "最新回顾"
    generated_at = summary.get("generated_at")
    if not isinstance(generated_at, str):
        return label
    try:
        generated = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated.tzinfo is not None:
            generated = generated.astimezone(dt.timezone(dt.timedelta(hours=8)))
        return f"{label}  ·  {generated:%m/%d %H:%M}"
    except ValueError:
        return label


def inner_width(work_width, scale, has_summary, text_width=0):
    available = max(1, work_width-round(2*SHADOW*scale))
    preferred = round(WIDTH*scale) if has_summary else max(
        round(COLLAPSED[0]*scale), text_width+round(2*PAD_X*scale))
    return min(available, preferred)


def ease(t):
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def centered_rect(work, size, scale):
    width, height = size
    left = (work[0] + work[2] - width) // 2
    top = work[1] + round(2 * scale)
    return clamp_rect((left, top, left + width, top + height), work)


class IslandWidget:
    def __init__(self, path: Path, *, allow_display=True):
        self.path, self.allow_display = path, allow_display
        self.api, self.painter = WinApi(), IslandPainter()
        self.hwnd = self.menu_hwnd = None
        self.visible = self.closed = self.running = False
        self.snapshot, self.overview, self.summary_id = {}, empty_status({}), None
        self.caption = ""
        self.retained = False
        self.owner = None
        self.screen_initialized = False
        self.foreground_hwnd = None
        self.foreground_fullscreen = False
        self.manual_peek = False
        self.scale = 1.0
        self.work = (0, 0, 1, 1)
        self.rect = self.menu_rect = (0, 0, 0, 0)
        self.font = None
        self.font_scale = None
        self.lines, self.signature = [], None
        self.expanded_size = (WIDTH + 2 * SHADOW, 140)
        self.size = tuple(value + 2 * SHADOW for value in COLLAPSED)
        self.start_size = self.target_size = self.size
        self.anim_start = 0.0
        self.anim_duration = .3
        self.target_open = False
        self.text_alpha = self.start_text_alpha = 0.0
        self.hovering = False
        self.hover_since = None
        self.manual = self.dismiss = False
        self.last_read = self.last_environment = 0.0
        self.last_topmost_check = 0.0
        self.last_state_write = 0.0
        self.frame_trace = []
        self.trace_direction = None
        self.frame_history = []
        self.last_frame_tick = 0.0
        self.clock_watchdog_used = False
        self.last_render = self.last_state = self.last_persisted = None
        self.allowed = False
        self.idle = 0.0
        self.actions = queue.Queue()
        self.action_busy = False
        saved = self.read_json(path / "island-presentation.json")
        try:
            self.policy = RevealPolicy(last_auto_wall=saved.get("last_auto_wall", 0),
                                       last_presented_id=saved.get("last_presented_id"))
        except (ValueError, TypeError):
            self.policy = RevealPolicy()
        self.instance = self.api.kernel.GetModuleHandleW(None)
        self.class_name = f"WorkRecapIsland_{os.getpid()}_{id(self)}"
        self.callback = PROC(self.window_proc)
        window_class = WindowClass(0, self.callback, 0, 0, self.instance, None,
            self.api.user.LoadCursorW(None, ctypes.c_void_p(32512)), None, None, self.class_name)
        if not self.api.user.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = self.create_window()
        self.api.user.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        self.api.user.GetClassNameW.restype = ctypes.c_int
        self.frame_clock = FrameClock(self.hwnd)

    @staticmethod
    def read_json(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def create_window(self):
        hwnd = self.api.user.CreateWindowExW(
            self.api.NOACTIVATE | self.api.TOOLWINDOW | self.api.TOPMOST | 0x80000,
            self.class_name, "Codex 工作回顾", 0x80000000,
            0, 0, 1, 1, None, None, self.instance, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(hwnd)

    def fullscreen(self, hwnd):
        name = ctypes.create_unicode_buffer(128)
        self.api.user.GetClassNameW(hwnd, name, len(name))
        if name.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return False  # The desktop is not a fullscreen video/presentation.
        info = MonitorInfo(); info.cbSize = ctypes.sizeof(info)
        if not self.api.user.GetMonitorInfoW(self.api.user.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
            return False
        rect, screen = self.api.rect(hwnd), info.rcMonitor
        covers = (rect[0] <= screen.left and rect[1] <= screen.top
                  and rect[2] >= screen.right and rect[3] >= screen.bottom)
        return covers and not (self.api.get_long(hwnd, -16) & 0x00C00000)

    def read_display(self):
        value = self.read_json(self.path / "display.json")
        if not value:
            return  # Keep the real last summary while the service recovers.
        self.snapshot = value
        summary = value.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("overview"), str) and summary["overview"].strip():
            self.overview = summary["overview"].strip()
            self.policy.set_idle_threshold(summary.get("display_after_idle_seconds") or 60)
            # A reused morning recap is a new presentation period, not a new
            # model result. Deduplicate repeated popups within that period.
            self.summary_id = f"{summary.get('id')}|{summary.get('period_label', '')}"
            self.retained = bool(summary.get("retained", False))
            self.caption = summary_caption(summary)
        else:
            self.overview, self.summary_id = empty_status(value), None
            self.retained = False
            self.caption = ""
        self.policy.duration = min(25, max(15, len(self.overview) / 5 + 4))

    def can_auto_reveal(self):
        return bool(self.summary_id and not self.retained)

    def has_new_summary(self):
        return self.can_auto_reveal() and self.summary_id != self.policy.last_presented_id

    def refresh_environment(self, now):
        self.foreground_hwnd = self.api.user.GetForegroundWindow()
        try:
            self.foreground_fullscreen = bool(self.foreground_hwnd and self.fullscreen(self.foreground_hwnd))
        except OSError:
            self.foreground_fullscreen = False
        # The first placement uses the current screen; later it stays with its
        # own monitor, independently of Codex and other applications.
        anchor = self.hwnd if self.screen_initialized else (self.foreground_hwnd or self.hwnd)
        self.scale, self.work = self.api.dpi(anchor)/96, self.api.work_area(anchor)
        # An expired/empty recap must not make the resident strip disappear.
        self.allowed = bool(self.allow_display)
        if self.allowed and self.visible and now-self.last_topmost_check >= 1.0:
            self.api.ensure_topmost(self.hwnd)
            if self.menu_hwnd:
                self.api.ensure_topmost(self.menu_hwnd)
            self.last_topmost_check = now
        self.idle = windows_idle_seconds() or 0.0
        self.last_environment = now

    def measure(self):
        if self.font_scale != self.scale:
            if self.font:
                self.api.gdi.DeleteObject(self.font)
            self.font = self.api.gdi.CreateFontW(-round(FONT*self.scale), 0, 0, 0, 400,
                0, 0, 0, 1, 0, 0, 6, 0, "Microsoft YaHei UI")
            self.font_scale = self.scale
        dc = self.api.user.GetDC(self.hwnd)
        old = self.api.gdi.SelectObject(dc, self.font)
        def extent(text):
            size = wt.SIZE()
            self.api.gdi.GetTextExtentPoint32W(dc, text, len(text.encode("utf-16-le"))//2, ctypes.byref(size))
            return size.cx
        y, lines = PAD_Y + (CAPTION_HEIGHT if self.caption else 0), []
        # A wide, shallow island follows existing paragraph breaks only; do not
        # force every sentence onto a fresh row and turn it back into a tall card.
        import re
        try:
            width = inner_width(self.work[2]-self.work[0], self.scale,
                                bool(self.summary_id), extent(self.overview) if not self.summary_id else 0)
            content_width = max(1, width-round(2*PAD_X*self.scale))
            for index, paragraph in enumerate(filter(None, re.split(r"\n+", self.overview))):
                if index:
                    y += GAP
                for line in wrap_lines(paragraph, content_width, extent):
                    lines.append((line, PAD_X, y))
                    y += PITCH
        finally:
            self.api.gdi.SelectObject(dc, old)
            self.api.user.ReleaseDC(self.hwnd, dc)
        self.lines = lines
        self.expanded_size = (width+round(2*SHADOW*self.scale),
            min(round((y+PAD_Y+2*SHADOW)*self.scale), self.work[3]-self.work[1]))
        self.painter.reserve(*self.expanded_size)

    def inside(self, rect, point):
        return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]

    def content_rect(self, rect=None):
        rect = rect or self.rect
        inset = round(SHADOW*self.scale)
        return rect[0]+inset, rect[1]+inset, rect[2]-inset, rect[3]-inset

    def close_hit(self, point):
        rect = self.content_rect()
        return point[0] >= rect[2]-round(32*self.scale) and point[1] < rect[1]+round(30*self.scale)

    def animate_to(self, opened, now):
        target = self.expanded_size if opened else tuple(round((v+2*SHADOW)*self.scale) for v in COLLAPSED)
        if opened == self.target_open and target == self.target_size:
            return
        self.start_size, self.target_size = self.size, target
        self.target_open = opened
        self.anim_start = now
        self.last_frame_tick = now
        self.anim_duration = .30 if opened else .20
        self.start_text_alpha = self.text_alpha
        self.frame_trace = []
        self.trace_direction = "expand" if opened else "collapse"

    def close_menu(self):
        if self.menu_hwnd:
            hwnd, self.menu_hwnd = self.menu_hwnd, None
            self.api.user.DestroyWindow(hwnd)

    def show_menu(self):
        self.close_menu()
        self.menu_hwnd = self.create_window()
        x, y = self.api.cursor()
        width, height = round(188*self.scale), round(108*self.scale)
        self.menu_rect = clamp_rect((x, y, x+width, y+height), self.work)
        lines = [("恢复自动总结" if self.snapshot.get("paused") else "暂停自动总结", 16, 12),
                 ("退出", 16, 47)]
        self.painter.render(self.menu_hwnd, (self.menu_rect[0], self.menu_rect[1], width, height),
                            self.scale, lines, expanded=True)
        self.api.show(self.menu_hwnd, self.menu_rect)

    def toggle_paused(self):
        self.close_menu()
        if self.action_busy:
            return
        self.action_busy = True
        action = "resume" if self.snapshot.get("paused") else "pause"
        def worker():
            try:
                self.actions.put(request("/api/action", {"action": action}, timeout=5))
            except Exception:
                self.actions.put({})
        threading.Thread(target=worker, daemon=True, name="recap-island-action").start()

    def window_proc(self, hwnd, message, first, second):
        try:
            if message == 0x0021:
                return 3  # WM_MOUSEACTIVATE / MA_NOACTIVATE.
            if message == 0x0014:
                return 1
            if message == 0x000F:
                paint = PaintStruct()
                self.api.user.BeginPaint(hwnd, ctypes.byref(paint))
                self.api.user.EndPaint(hwnd, ctypes.byref(paint))
                return 0
            if message == WM_FRAME and hwnd == self.hwnd:
                try:
                    self.last_frame_tick = time.monotonic()
                    self.tick()
                finally:
                    self.frame_clock.acknowledge()
                return 0
            if message == 0x0113 and hwnd == self.hwnd:
                if not self.frame_clock.active.is_set():
                    self.tick()
                elif time.monotonic()-self.last_frame_tick > .12:
                    # A suspended/failing compositor must not strand the UI or
                    # prevent its own stop request from being serviced.
                    self.clock_watchdog_used = True
                    self.tick()
                return 0
            if message == 0x0205 and hwnd == self.hwnd:
                self.show_menu(); return 0
            if message == 0x0202:
                if hwnd == self.menu_hwnd:
                    y = (self.api.cursor()[1]-self.menu_rect[1])/self.scale-SHADOW
                    if y < 40:
                        self.toggle_paused()
                    else:
                        self.quit()
                elif hwnd == self.hwnd:
                    if self.target_open and self.close_hit(self.api.cursor()):
                        self.dismiss = True
                    elif not self.target_open:
                        self.manual = True
                return 0
            if message == 0x0010 and hwnd == self.hwnd:
                self.quit(); return 0
            if message == 0x0002 and hwnd == self.hwnd and self.running:
                self.api.user.PostQuitMessage(0); return 0
        except Exception:
            import logging
            logging.exception("recap island event failed")
        return self.api.user.DefWindowProcW(hwnd, message, first, second)

    def publish_state(self, mode):
        now = time.monotonic()
        animating = mode != "hidden" and now-self.anim_start < self.anim_duration
        # Diagnostic disk writes must not compete with each animation frame.
        if animating:
            return
        value = {"pid": os.getpid(), "hwnd": self.hwnd, "visible": self.visible,
                 "rect": list(self.rect), "state": self.snapshot.get("state", "idle"),
                 "presentation": "top-island", "mode": mode,
                 "animating": animating}
        if value != self.last_state:
            atomic_json(self.path/"widget-state.json", value)
            self.last_state = value
            self.last_state_write = now

    def tick(self):
        if self.closed:
            return
        now, wall = time.monotonic(), time.time()
        in_motion = self.visible and now-self.anim_start < self.anim_duration
        if not in_motion and (self.path/"widget.stop").exists():
            self.quit(); return
        if not in_motion and now-self.last_read >= 2:
            self.read_display(); self.last_read = now
        if now-self.last_environment >= .15:
            self.refresh_environment(now)
        if not in_motion:
            reveal = self.path/"island-reveal.json"
            if reveal.exists():
                instruction = self.read_json(reveal)
                reveal.unlink(missing_ok=True)
                requested_at = instruction.get("requested_at", 0)
                if isinstance(requested_at, (int, float)) and 0 <= wall-requested_at <= 15:
                    self.manual = True
        while not self.actions.empty():
            value = self.actions.get_nowait(); self.action_busy = False
            if "paused" in value:
                self.snapshot["paused"] = bool(value["paused"])
        point = self.api.cursor()
        self.hovering = self.visible and self.inside(self.content_rect(), point)
        menu_hover = self.menu_hwnd and self.inside(self.content_rect(self.menu_rect), point)
        if self.hovering and not self.target_open:
            if self.hover_since is None:
                self.hover_since = now
            elif now-self.hover_since > .25:
                self.manual = True
        else:
            self.hover_since = None
        if self.manual:
            self.manual_peek = True
        reveal_allowed = self.allowed and bool(self.foreground_hwnd) and (
            not self.foreground_fullscreen or self.manual_peek)
        display_id = self.summary_id or "status:empty"
        desired = self.policy.step(wall, display_id, reveal_allowed, self.idle,
            hovering=bool(self.hovering or menu_hover), manual=self.manual, dismiss=self.dismiss,
            automatic_allowed=self.can_auto_reveal())
        if desired == "closed":
            self.manual_peek = False
        self.manual = self.dismiss = False
        if not self.allowed:
            self.frame_clock.set_active(False)
            self.api.hide(self.hwnd); self.close_menu(); self.visible = False
            self.target_open = False; self.text_alpha = 0
            self.size = self.start_size = self.target_size = tuple(round((v+2*SHADOW)*self.scale) for v in COLLAPSED)
            self.last_render = None
            self.publish_state("hidden")
            return
        signature = (self.overview, self.caption, bool(self.summary_id), self.scale, self.work)
        if signature != self.signature:
            self.measure(); self.signature = signature
        self.animate_to(desired == "open", now)
        progress = min(1, max(0, (now-self.anim_start)/self.anim_duration))
        # Keep complete sentences inside the island throughout the morph:
        # fade them out before shrinking, and in only after expansion has room.
        curve = ease(progress if self.target_open else (progress-.30)/.70)
        self.size = tuple(round(a+(b-a)*curve) for a, b in zip(self.start_size, self.target_size))
        if self.target_open:
            self.text_alpha = self.start_text_alpha+(1-self.start_text_alpha)*ease((progress-.72)/.28)
        else:
            self.text_alpha = self.start_text_alpha*max(0, 1-progress/.3)
        self.rect = centered_rect(self.work, self.size, self.scale)
        pending = self.has_new_summary()
        show_content_shape = self.target_open or (progress < .75 and self.start_text_alpha > 0)
        render_key = (self.rect, self.scale, round(self.text_alpha, 3), show_content_shape,
                      self.hovering, pending, self.signature)
        if render_key != self.last_render:
            rect = self.rect
            render_start = time.perf_counter()
            self.painter.render(self.hwnd, (rect[0], rect[1], rect[2]-rect[0], rect[3]-rect[1]),
                self.scale, self.lines, text_alpha=self.text_alpha,
                expanded=show_content_shape, hovering=self.hovering, pending=pending,
                caption=self.caption)
            if self.trace_direction and len(self.frame_trace) < 180:
                self.frame_trace.append({"at": render_start,
                    "render_ms": round((time.perf_counter()-render_start)*1000, 3),
                    "width": rect[2]-rect[0], "height": rect[3]-rect[1]})
            self.last_render = render_key
        if not self.visible:
            self.api.show(self.hwnd, self.rect); self.visible = True
            self.screen_initialized = True
        if self.menu_hwnd and not (self.hovering or menu_hover):
            self.close_menu()
        if self.summary_id and not self.retained and self.target_open and progress >= 1:
            persisted = {"last_auto_wall": self.policy.last_auto_wall,
                         "last_presented_id": self.policy.last_presented_id}
            if persisted != self.last_persisted:
                atomic_json(self.path/"island-presentation.json", persisted)
                self.last_persisted = persisted
        self.frame_clock.set_active(progress < 1)
        if progress >= 1 and self.trace_direction:
            frames = self.frame_trace
            intervals = sorted((b["at"]-a["at"])*1000 for a, b in zip(frames, frames[1:]))
            costs = sorted(frame["render_ms"] for frame in frames)
            def percentile(values, fraction):
                return round(values[min(len(values)-1, int((len(values)-1)*fraction))], 3) if values else None
            record = {"direction": self.trace_direction, "clock": self.frame_clock.mode,
                "clock_watchdog_used": self.clock_watchdog_used,
                "frames": len(frames), "interval_p50_ms": percentile(intervals, .5),
                "interval_p95_ms": percentile(intervals, .95),
                "interval_max_ms": max(intervals, default=None),
                "render_p50_ms": percentile(costs, .5), "render_p95_ms": percentile(costs, .95)}
            self.frame_history = (self.frame_history+[record])[-4:]
            atomic_json(self.path/"island-frame-profile.json", {"transitions": self.frame_history})
            self.trace_direction = None
        self.publish_state("expanded" if self.target_open else "collapsed")

    def loop(self):
        self.running = True
        self.api.user.SetTimer(self.hwnd, 1, 50, None)
        self.tick()
        message = wt.MSG()
        while not self.closed:
            result = self.api.user.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                break
            self.api.user.TranslateMessage(ctypes.byref(message))
            self.api.user.DispatchMessageW(ctypes.byref(message))

    def quit(self):
        if self.closed:
            return
        self.closed = True; self.visible = False
        self.frame_clock.set_active(False)
        self.api.hide(self.hwnd); self.close_menu()
        self.publish_state("hidden")
        self.api.user.KillTimer(self.hwnd, 1)
        self.api.user.DestroyWindow(self.hwnd)

    def dispose(self):
        if not self.closed:
            self.quit()
        if self.font:
            self.api.gdi.DeleteObject(self.font); self.font = None
        self.frame_clock.close()
        self.painter.close()
        self.api.user.UnregisterClassW(self.class_name, self.instance)
