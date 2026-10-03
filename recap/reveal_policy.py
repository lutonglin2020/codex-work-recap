"""Pure presentation policy; timestamps are wall-clock seconds from the host."""
from __future__ import annotations


class RevealPolicy:
    """Return open/closed without marking model summaries read.

    ``last_auto_wall`` and ``last_presented_id`` are presentation-only values
    the host may persist. Manual viewing bypasses the automatic cooldown but
    never the foreground/fullscreen ``allowed`` gate. The latest pending recap
    replaces older pending ones; summaries already shown are not auto-repeated.
    """

    def __init__(self, duration=20, cooldown=1200, last_auto_wall=0,
                 last_presented_id=None, idle_before_open=60):
        self.duration = min(25, max(15, float(duration)))
        self.cooldown = max(1200, float(cooldown))
        self.last_auto_wall = float(last_auto_wall or 0)
        self.last_presented_id = last_presented_id
        self._has_auto = self.last_auto_wall > 0
        self._handled = {last_presented_id} if last_presented_id else set()
        self._pending_id = None
        self._return_armed = False
        self._return_ready = False
        self._last_idle = None
        self._active_id = None
        self._open_until = 0
        self._hovering = False
        self._leave_until = 0
        self.set_idle_threshold(idle_before_open)

    def set_idle_threshold(self, seconds):
        self.idle_before_open = min(300, max(15, float(seconds)))
        self.away_seconds = max(180, self.idle_before_open + 60)

    def _close(self):
        self._active_id = None
        self._hovering = False
        self._leave_until = 0
        return "closed"

    def _open(self, now, summary_id, hovering, automatic, record_presentation=True):
        self._active_id = summary_id
        self._open_until = now + self.duration
        self._hovering = bool(hovering)
        self._leave_until = 0
        if record_presentation:
            self._handled.add(summary_id)
            self.last_presented_id = summary_id
        if self._pending_id == summary_id:
            self._pending_id = None
            self._return_armed = self._return_ready = False
        if automatic:
            self.last_auto_wall = now
            self._has_auto = True
        return "open"

    def step(self, now, summary_id, allowed, idle_seconds, hovering=False,
             manual=False, dismiss=False, automatic_allowed=True):
        """Observe host activity and return the desired reveal state.

        A recap discovered while away waits for a return to <2s idle.
        During active work, the model-selected pause must pass. Blocked/early
        candidates remain pending, including a return noticed while disallowed.
        ``dismiss`` suppresses only this ID; future summaries remain eligible.
        Status-only content uses ``automatic_allowed=False``: manual viewing
        works, but it never consumes recap presentation history or pops up.
        """
        now = float(now)
        idle = max(0, float(idle_seconds))
        previous_idle = self._last_idle
        self._last_idle = idle
        if not summary_id:
            self._pending_id = None
            self._return_armed = self._return_ready = False
            return self._close()
        if not automatic_allowed:
            self._pending_id = None
            self._return_armed = self._return_ready = False
            if self._active_id != summary_id:
                self._close()
        if automatic_allowed and summary_id not in self._handled and summary_id != self._pending_id:
            self._pending_id = summary_id
            self._return_armed = idle >= self.away_seconds or (previous_idle is not None and previous_idle >= self.away_seconds)
            self._return_ready = False
        if self._pending_id:
            if idle >= self.away_seconds:
                self._return_armed = True
            elif idle < 2 and previous_idle is not None and previous_idle >= self.away_seconds and self._return_armed:
                self._return_ready = True
                self._return_armed = False
        if dismiss:
            dismissed_id = self._active_id or summary_id
            if dismissed_id:
                self._handled.add(dismissed_id)
            if self._pending_id == dismissed_id:
                self._pending_id = None
                self._return_armed = self._return_ready = False
            return self._close()
        if not allowed:
            return self._close()
        if manual and summary_id:
            return self._open(now, summary_id, hovering, automatic=False,
                              record_presentation=automatic_allowed)
        if self._active_id:
            if self._hovering and not hovering:
                self._leave_until = now + 2
            self._hovering = bool(hovering)
            if hovering:
                self._leave_until = 0
                return "open"
            deadline = self._leave_until or self._open_until
            if now < deadline:
                return "open"
            self._close()
        due = not self._has_auto or now - self.last_auto_wall >= self.cooldown
        rested = not self._return_armed and self.idle_before_open <= idle < self.away_seconds
        if automatic_allowed and self._pending_id and due and (self._return_ready or rested):
            return self._open(now, self._pending_id, hovering, automatic=True)
        return "closed"
