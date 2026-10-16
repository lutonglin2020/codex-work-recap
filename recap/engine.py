"""Thread-safe collection, evidence accounting and adaptive recap scheduling."""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import threading
import time
import uuid
from pathlib import Path

from .adapter import CodexAdapter, CodexError, validate_summary, SUMMARY_MODEL, SUMMARY_REASONING_EFFORT
from .store import Store

TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
SCAN_SECONDS = 60
RECENT_SECONDS = 60 * 60
MODEL_DEBOUNCE_SECONDS = 5 * 60
POLICY_VERSION = 2


def iso(timestamp: float | None) -> str | None:
    return dt.datetime.fromtimestamp(timestamp, TZ).isoformat(timespec="seconds") if timestamp else None


def meaningful(source: dict) -> bool:
    text = source.get("text", "").strip()
    if not text:
        return False
    # Small greetings/acknowledgements should not consume a model call.
    simplified = re.sub(r"用户请求：|助手结果：|[\s，。！？,.!?：:]+", "", text).lower()
    if len(simplified) < 80:
        residue = re.sub(r"你好|您好|谢谢|不客气|好的|好|收到|hi|hello|thanks|ok|有什么可以帮你|有什么需要帮忙的|我在", "", simplified)
        if len(residue) < 8:
            return False
    return len(text) >= 24


def adaptive_interval(count: int, characters: int) -> int:
    """Only summary frequency changes; collection remains cheap and once/minute."""
    if count >= 12 or characters >= 18000:
        return 20
    if count >= 5 or characters >= 8000:
        return 30
    if count >= 2 or characters >= 3000:
        return 40
    return 60


def _day_label(day: dt.date, today: dt.date) -> str:
    if day == today:
        return "今天"
    if day == today - dt.timedelta(days=1):
        return "昨天"
    return day.strftime("%m月%d日")


def resolve_window(period: str, sources: list[dict], now: float, initial: bool = False) -> tuple[float, float, str]:
    local = dt.datetime.fromtimestamp(now, TZ)
    today = local.date()
    aliases = {"自动": "auto", "今天": "today", "昨天": "yesterday", "上午": "morning", "下午": "afternoon",
               "晚上": "evening", "凌晨": "night", "最近工作日": "last_workday"}
    period = aliases.get(period, period)
    if period == "auto":
        start = now - RECENT_SECONDS
        first = dt.datetime.fromtimestamp(start, TZ)
        return start, now, f"最近60分钟（{first:%H:%M}–{local:%H:%M}）"
    day, start_hour, end_hour = today, 0, 24
    eligible = [s for s in sources if s.get("eligible") and meaningful(s)]
    if period == "yesterday":
        day -= dt.timedelta(days=1)
    elif period in ("morning", "afternoon", "evening", "night"):
        start_hour, end_hour = {"night": (0, 6), "morning": (6, 12), "afternoon": (12, 18), "evening": (18, 24)}[period]
    elif period == "last_workday":
        if eligible:
            day = dt.datetime.fromtimestamp(max(s["timestamp"] for s in eligible), TZ).date()
    elif period.startswith("date:"):
        try:
            day = dt.date.fromisoformat(period[5:])
        except ValueError as exc:
            raise CodexError("日期应写成 date:YYYY-MM-DD。") from exc
    elif period != "today":
        raise CodexError("支持 auto/today/yesterday/morning/afternoon/evening/night/last_workday 或 date:YYYY-MM-DD。")
    start = dt.datetime.combine(day, dt.time(start_hour), TZ)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), TZ) if end_hour == 24 else dt.datetime.combine(day, dt.time(end_hour), TZ)
    segment = "" if (start_hour, end_hour) == (0, 24) else {0: "凌晨", 6: "上午", 12: "下午", 18: "晚上"}[start_hour]
    return start.timestamp(), min(end.timestamp(), now), _day_label(day, today) + segment


class Engine:
    def __init__(self, data_dir: str | Path, adapter=None, clock=None):
        self.data_dir = Path(data_dir)
        self.clock = clock or time.time
        self.store = Store(self.data_dir)
        self.adapter = adapter or CodexAdapter(self.data_dir)
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.closed = False
        self.paused = bool(self.store.get("paused", False))
        self.state = "paused" if self.paused else "ready" if self._visible_summary() else "idle"
        self.message = "已暂停自动回顾。" if self.paused else "等待新的工作记录。"
        self.last_scan_at = self.store.get("last_scan_at")
        self.next_check_at = None if self.paused else self.clock()
        self.last_activity_at = self.store.get("last_activity_at", 0)
        self.stats_extra = self.store.get("stats", {})
        self.user_idle_seconds = None
        self.idle_observed_at = 0

    def _state(self, state: str, message: str, next_at: float | None = None) -> None:
        with self.lock:
            if self.paused:
                self.state, self.message, self.next_check_at = "paused", "已暂停自动回顾。", None
            else:
                self.state, self.message, self.next_check_at = state, message, next_at

    def _public_summary(self, summary: dict) -> dict:
        summary = dict(summary)
        start, end = summary.get("window_start"), summary.get("window_end")
        carry_outside = summary.pop("carry_unread_outside_period", False)
        if summary.get("policy_version") != POLICY_VERSION and start is not None and end is not None:
            date = dt.datetime.fromtimestamp(start, TZ)
            today = dt.datetime.fromtimestamp(self.clock(), TZ).date()
            segment = {0: "凌晨", 6: "上午", 12: "下午", 18: "晚上"}.get(date.hour, "") if end - start == 21600 else ""
            summary["period_label"] = _day_label(date.date(), today) + segment
        if carry_outside:
            summary["period_label"] += "（含历史进展）"
        return summary

    def _visible_summary(self) -> dict | None:
        # An explicitly empty current slot must never fall back to old history.
        summary = self.store.summary(self.store.get("visible_summary_id"))
        if not summary or summary.get("policy_version") != POLICY_VERSION:
            return None
        kind, end, now = summary.get("window_kind"), summary.get("window_end"), self.clock()
        if not isinstance(end, (int, float)):
            return None
        if kind == "recent" and now >= end + RECENT_SECONDS:
            return None
        if kind == "morning" and not end <= now < end + 12 * 3600:
            return None
        return summary

    def _display_summary(self, current: dict | None) -> tuple[dict | None, bool]:
        """Keep the last useful recap available without making it current again."""
        if current:
            return current, False
        for previous in self.store.recent_summaries(12):
            overview = previous.get("overview")
            if (previous.get("policy_version") == POLICY_VERSION
                    and isinstance(overview, str) and overview.strip()):
                return self._public_summary(previous), True
        return None, False

    def snapshot(self) -> dict:
        with self.lock:
            if self.closed:
                return {"state": "paused", "message": "回顾服务已关闭。", "paused": True, "last_scan_at": iso(self.last_scan_at),
                        "next_check_at": None, "summary": None, "display_summary": None,
                        "display_summary_retained": False, "sources": [], "stats": {"closed": True}}
            summary = self._visible_summary()
            if summary:
                summary = self._public_summary(summary)
            display_summary, retained = self._display_summary(summary)
            all_sources = self.store.sources()
            by_id = {s["id"]: s for s in all_sources}
            required = [] if summary is None else list(dict.fromkeys(ref for item in summary["work_items"] for ref in item["source_ids"]))
            chosen = [by_id[ref] for ref in required if ref in by_id]
            chosen_ids = {s["id"] for s in chosen}
            if summary is None:
                for source in reversed(all_sources):
                    if source["id"] not in chosen_ids:
                        chosen.append(source)
                        chosen_ids.add(source["id"])
                    if len(chosen) >= 80:
                        break
            public_sources = [{"id": s["id"], "thread_id": s["thread_id"], "title": s["title"],
                               "timestamp": iso(s["timestamp"]), "text": s["text"], "status": s["status"],
                               "completed_at": iso(s.get("completed_at"))} for s in chosen]
            stats = {**self.store.stats(), **self.stats_extra, "model": SUMMARY_MODEL, "reasoning_effort": SUMMARY_REASONING_EFFORT,
                     "scope": "本机主会话，含归档（最近14天、有限分页）", "coverage": "local_persisted", "realtime_subscription": False}
            return copy.deepcopy({"state": self.state, "message": self.message, "paused": self.paused,
                                  "last_scan_at": iso(self.last_scan_at), "next_check_at": iso(self.next_check_at),
                                  "summary": summary, "display_summary": display_summary,
                                  "display_summary_retained": retained, "sources": public_sources, "stats": stats,
                                  "unread_history": [{**self._public_summary(s), "unread": True} for s in self.store.unread_summaries() if summary is None or s["id"] != summary["id"]]})

    @staticmethod
    def _activity_profiles(sources: list[dict], start: float, end: float, kind: str) -> dict[str, dict]:
        """Observed start/end activity, never reply length or measured human effort."""
        grouped = {}
        span = max(1, end - start)
        for source in sources:
            completed = source.get("completed_at") or (source["timestamp"] if source.get("eligible") else None)
            events = [value for value in (source.get("started_at"), completed)
                      if isinstance(value, (int, float)) and start <= value <= end]
            if not events:
                continue
            grouped.setdefault(source["thread_id"], []).append((source, events))
        profiles = {}
        for thread_id, records in grouped.items():
            buckets = {}
            for _, events in records:
                for event in events:
                    # Daily recaps represent the whole day, not merely its last hour.
                    weight = .25 + .75 * max(0, 1 - (end - event) / span) if kind == "recent" else 1.0
                    bucket = int(event // 300)
                    buckets[bucket] = max(buckets.get(bucket, 0), weight)
            latest = max(max(events) for _, events in records)
            first = min(max(start, source.get("started_at") or min(events)) for source, events in records)
            score = sum(buckets.values()) + .15 * len(records) ** .5
            profiles[thread_id] = {
                "_score": score, "thread_turn_count": len(records),
                "recent_turn_count": sum(max(events) >= end - 900 for _, events in records),
                "active_buckets": len(buckets), "latest_activity_at": iso(latest),
                "window_span_minutes": round(max(0, latest - first) / 60, 2),
                "thread_unfinished_turn_count": sum(not source.get("eligible") for source, _ in records)}
        total = sum(profile["_score"] for profile in profiles.values())
        for profile in profiles.values():
            profile["thread_share"] = round(profile["_score"] / total, 4) if total else 0.0
        return profiles

    def _input(self, window_sources: list[dict], fresh_sources: list[dict], *,
               window_start: float | None = None, window_end: float | None = None,
               kind: str = "recent", activity_sources: list[dict] | None = None) -> tuple[list[dict], bool]:
        # Focus the material budget on sustained activity. Small secondary probes
        # let the model notice important results without guaranteeing equal coverage.
        end = self.clock() if window_end is None else window_end
        start = end - RECENT_SECONDS if window_start is None else window_start
        profiles = self._activity_profiles(window_sources if activity_sources is None else activity_sources, start, end, kind)
        fresh = {source["id"] for source in fresh_sources}
        grouped = {}
        for source in window_sources:
            if source.get("eligible") and start <= source["timestamp"] <= end and source["thread_id"] in profiles:
                grouped.setdefault(source["thread_id"], []).append(source)
        for items in grouped.values():
            items.sort(key=lambda source: (source["id"] in fresh, source["timestamp"]), reverse=True)
        ranked = sorted(profiles, key=lambda key: (profiles[key]["_score"], profiles[key]["latest_activity_at"]), reverse=True)
        focus, covered = [], 0.0
        for thread_id in ranked:
            focus.append(thread_id)
            covered += profiles[thread_id]["thread_share"]
            if covered >= .75 or len(focus) >= 3 or (len(focus) == 1 and covered >= .6):
                break
        probes = [key for key in ranked if key not in focus and key in grouped][:3]
        budget, maximum = 13000, 20
        main_budget = budget - (1800 if probes else 0)
        main_slots = maximum - len(probes)
        focus_score = sum(profiles[key]["_score"] for key in focus)
        selected, size, main_characters = [], 0, 0

        def material(source, limit):
            item = dict(source)
            item["activity"] = {key: value for key, value in profiles[source["thread_id"]].items() if key != "_score"}
            item["activity"]["is_new"] = source["id"] in fresh
            item["carry_unread"] = False
            item.pop("history_context", None)
            overhead = len(item["title"]) + 200 + len(json.dumps(item["activity"], ensure_ascii=False))
            text = source["text"]
            if len(text) > limit:
                first = min(100, max(24, limit // 4))
                text = text[:first] + "\n…\n" + text[-(limit - first - 3):]
            item["text"] = text
            return item, len(text) + overhead

        def overhead(source):
            return material(source, max(80, len(source["text"])))[1] - len(source["text"])

        for thread_id in focus:
            items = grouped.get(thread_id, [])
            share = profiles[thread_id]["_score"] / focus_score if focus_score else 0
            quota = int(main_budget * share)
            slots = min(len(items), max(1, int(main_slots * share)))
            for source in items[:slots]:
                extra = overhead(source)
                limit = min(2800, max(240, int(main_budget * share / max(1, slots)) - extra), quota - extra)
                if limit < 80:
                    continue
                item, weight = material(source, limit)
                if size + weight > budget or len(selected) >= maximum:
                    continue
                selected.append(item)
                size += weight
                quota -= weight
                main_characters += len(item["text"])
        # Total probe text is capped relative to the material actually retained
        # for the focus, so one long secondary reply cannot take over the recap.
        probe_limit = min(600, main_characters // (3 * len(probes))) if probes and main_characters else 600
        probe_spent = 0
        for thread_id in probes:
            if probe_limit < 80:
                break
            item, weight = material(grouped[thread_id][0], probe_limit)
            if probe_spent + weight > 1800 or size + weight > budget or len(selected) >= maximum:
                continue
            selected.append(item)
            size += weight
            probe_spent += weight
        selected.sort(key=lambda source: source["timestamp"])
        return selected, len(selected) < len(window_sources)

    def _deduplicate(self, incoming: list[dict]) -> list[dict]:
        existing = self.store.sources()
        canonical = {s.get("turn_id"): s["id"] for s in existing if s.get("turn_id")}
        result = {}
        for source in incoming:
            source = dict(source)
            turn_id = source.get("turn_id")
            if turn_id:
                source["id"] = canonical.setdefault(turn_id, source["id"])
            previous = result.get(source["id"])
            # Prefer proven terminal data over another process's unknown copy.
            if previous is None or source.get("eligible") or not previous.get("eligible"):
                result[source["id"]] = source
        return list(result.values())

    def _finish_opportunity(self, key: str | None, status: str) -> None:
        if key:
            opportunities = self.store.get("morning_opportunities_v2", {})
            opportunities[key] = status
            self.store.set("morning_opportunities_v2", dict(sorted(opportunities.items())[-28:]))

    def _natural_window(self, sources: list[dict], now: float) -> tuple[str, float, float, str] | None:
        local = dt.datetime.fromtimestamp(now, TZ)
        if not 6 <= local.hour < 12:
            return None
        key = local.date().isoformat()
        if key in self.store.get("morning_opportunities_v2", {}):
            return None
        end = dt.datetime.combine(local.date(), dt.time(), TZ).timestamp()
        start = end - 86400
        if not any(start <= s["timestamp"] < end and s.get("eligible") and meaningful(s) for s in sources):
            self._finish_opportunity(key, "empty")
            return None
        return key, start, end, "昨天"

    @staticmethod
    def _window_key(start: float, end: float) -> str:
        return f"v2:{start:.3f}:{end:.3f}"

    @staticmethod
    def _work_metrics(sources: list[dict], start: float, end: float, now: float) -> dict:
        first = min((max(start, s.get("started_at") or s["timestamp"]) for s in sources), default=now)
        last = max((min(end, s["timestamp"]) for s in sources), default=first)
        earliest_completed = min((s["timestamp"] for s in sources), default=now)
        return {"pending_work_count": len(sources),
                "pending_work_characters": sum(len(s["text"]) for s in sources),
                "pending_work_chats": len({s["thread_id"] for s in sources}),
                "work_span_minutes": round(max(0, last - first) / 60, 2),
                "new_progress_accumulation_minutes": round(max(0, now - earliest_completed) / 60, 2)}

    @staticmethod
    def _work_score(metrics: dict) -> float:
        # A cheap relative load estimate only schedules evaluation; it never
        # determines whether the work is meaningful enough to show.
        return (metrics.get("pending_work_characters", 0) / 1000
                + metrics.get("pending_work_count", 0) * .3
                + metrics.get("pending_work_chats", 0) * .5
                + metrics.get("work_span_minutes", 0) / 20)

    def _review_due(self, kind: str, pending: list[dict], metrics: dict,
                    interval: int, now: float) -> tuple[float, str]:
        attempted = self.store.get("last_model_attempt_at_v2", 0)
        if not attempted:
            return now, "first_evaluation"
        earliest = attempted + MODEL_DEBOUNCE_SECONDS
        previous = self.store.get("last_review_context_v2", {})
        reviewed = self.store.get("last_review_at", 0)
        due, reason = reviewed + interval * 60 if reviewed else earliest, "ai_interval"
        if kind == "morning" or previous.get("window_kind") != kind:
            due, reason = earliest, "new_window_kind"
        elif self._work_score(metrics) >= max(4.0, self._work_score(previous) * 1.75):
            due, reason = earliest, "increased_workload"
        if kind == "recent" and pending:
            # Leave time for the next cheap scan and a bounded model run.
            expiry_due = min(s["timestamp"] for s in pending) + RECENT_SECONDS - 180
            if expiry_due < due:
                due, reason = expiry_due, "pending_work_nearing_window_end"
        return max(earliest, due), reason

    def _dedup_overviews(self, kind: str, start: float, end: float, available: list[dict],
                        all_sources: list[dict], now: float) -> list[dict]:
        # These are negative references (already described outcomes), never
        # additional facts or evidence IDs for the current material window.
        by_id = {source["id"]: source for source in all_sources}
        current_threads = {source["thread_id"] for source in available}
        candidates = []
        for previous in self.store.recent_summaries(12):
            if previous.get("policy_version") != POLICY_VERSION or previous.get("window_kind") != kind:
                continue
            try:
                generated = dt.datetime.fromisoformat(previous["generated_at"]).timestamp()
                previous_start, previous_end = float(previous["window_start"]), float(previous["window_end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= now - generated <= 2 * 3600:
                continue
            overlap = max(start, previous_start) < min(end, previous_end)
            refs = {ref for item in previous["work_items"] for ref in item["source_ids"]}
            same_threads = bool(current_threads.intersection(by_id[ref]["thread_id"] for ref in refs if ref in by_id))
            if kind == "morning":
                relevant = previous_start == start and previous_end == end
            elif kind == "recent":
                relevant = overlap or same_threads
            else:
                relevant = previous_start == start and overlap
            if relevant and previous.get("overview", "").strip():
                candidates.append((overlap, same_threads, generated, previous))
        candidates.sort(key=lambda value: value[:3], reverse=True)
        result, seen = [], set()
        for overlap, _, _, previous in candidates:
            text = previous["overview"].strip()[:400]
            if text in seen:
                continue
            seen.add(text)
            result.append({"text": text, "not_source": True,
                           "window_kind": kind, "window_start": iso(previous["window_start"]),
                           "window_end": iso(previous["window_end"]), "generated_at": previous["generated_at"],
                           "relation": "overlapping_window" if overlap else "recent_same_work_source",
                           "purpose": "只判断工作内容或进展是否已提过；不能补充本次窗口事实，也不能据此判定当前任务已完成。"})
            if len(result) >= 3:
                break
        return result

    def _model_context(self, kind: str, start: float, end: float, available: list[dict],
                       pending: list[dict], all_sources: list[dict], now: float, reason: str) -> dict:
        reviewed = self.store.get("last_review_at", 0)
        observed_idle = (self.user_idle_seconds + now - self.idle_observed_at
                         if self.user_idle_seconds is not None and 0 <= now - self.idle_observed_at <= 90 else None)
        context = {"window_kind": kind, "window_start": iso(start), "window_end": iso(end),
                   **self._work_metrics(pending, start, end, now),
                   "user_idle_seconds": observed_idle,
                   "time_since_last_review_minutes": round(max(0, now - reviewed) / 60, 2) if reviewed else None,
                   "evaluation_reason": reason,
                   "previous_overview_is_source": False,
                   "activity_is_not_importance": True, "no_equal_session_coverage": True}
        profiles = self._activity_profiles(all_sources, start, end, kind)
        unfinished_profiles = [profile for profile in profiles.values() if profile["thread_unfinished_turn_count"]]
        context["activity_threads_with_unfinished_work"] = len(unfinished_profiles)
        context["activity_share_with_unfinished_work"] = round(sum(profile["thread_share"] for profile in unfinished_profiles), 4)
        context["activity_observation"] = "窗口内开始与完成事件；跨度包含等待，不代表实际投入工时。"
        if kind == "morning":
            day_start = end + 6 * 3600
            current = [s for s in all_sources if day_start <= s["timestamp"] <= now and meaningful(s)]
            context["work_since_06_today"] = self._work_metrics(current, day_start, now, now)
            context["work_since_06_today"]["unfinished_turns"] = sum(not s.get("eligible") for s in current)
        references = self._dedup_overviews(kind, start, end, available, all_sources, now)
        if references:
            context["recent_overviews"] = references
            context["previous_overview"] = references[0]
        context["fresh_record_is_not_new_outcome"] = True
        return context

    def tick(self, force: bool = False, period: str = "auto") -> dict:
        if not self.tick_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            with self.lock:
                if self.closed or self.paused:
                    return self.snapshot()
                now = self.clock()
                if not force and self.last_scan_at and now - self.last_scan_at < SCAN_SECONDS:
                    return self.snapshot()
            self._state("collecting", "正在读取本机多个会话的工作记录。")
            collected = self.adapter.collect(now - 14 * 86400, now, force=force)
            with self.lock:
                if self.closed:
                    return self.snapshot()
                self.store.upsert_sources(self._deduplicate(collected.get("sources", [])), now)
                self.last_scan_at = now
                self.store.set("last_scan_at", now)
                self.last_activity_at = max(self.last_activity_at, collected.get("activity_at", 0))
                self.store.set("last_activity_at", self.last_activity_at)
                self.stats_extra.update(collected.get("stats", {}))
                all_sources = self.store.sources()
                new_sources = [s for s in self.store.sources(new_only=True) if meaningful(s)]
                if self.paused:
                    return self.snapshot()
            automatic = period in ("auto", "自动")
            visible = self._visible_summary()
            if automatic and visible and (visible.get("window_kind") == "explicit" or
                                         (force and visible.get("window_kind") != "recent")):
                self.store.set("visible_summary_id", None)
            opportunity = self._natural_window(all_sources, now) if automatic and not force else None
            opportunity_key = opportunity[0] if opportunity else None
            if opportunity:
                _, start, end, label = opportunity
                kind = "morning"
            else:
                start, end, label = resolve_window(period, all_sources, now)
                kind = "recent" if automatic else "explicit"

            def in_window(source):
                return start <= source["timestamp"] < end or (kind == "recent" and source["timestamp"] == end)

            available = [s for s in all_sources if in_window(s) and s.get("eligible") and meaningful(s)]
            fresh = [s for s in new_sources if in_window(s)]
            pending = available if opportunity else fresh
            metrics = self._work_metrics(pending, start, end, now)
            interval = self.store.get("ai_next_review_minutes", adaptive_interval(len(pending), metrics["pending_work_characters"]))
            self.stats_extra.update({"new_completed_turns": len(fresh), "new_characters": sum(len(s["text"]) for s in fresh),
                                     "interval_minutes": interval, "model_calls": self.store.get("model_calls", 0),
                                     "window_kind": kind, "carried_unread_sources": 0})
            self.store.set("stats", self.stats_extra)
            cache_key = self._window_key(start, end)
            hashes = {s["id"]: s["hash"] for s in available}
            cache = self.store.get("window_cache_v2", {}).get(cache_key)
            cached = self.store.summary(cache.get("summary_id")) if cache else None
            refs = {ref for item in cached["work_items"] for ref in item["source_ids"]} if cached else set()
            morning_activity = opportunity and any(end + 6 * 3600 <= s["timestamp"] <= now and meaningful(s)
                                                   for s in all_sources)
            if (opportunity and cache and cached and cached.get("policy_version") == POLICY_VERSION
                    and not morning_activity
                    and cache.get("source_hashes") == hashes and refs == set(hashes)
                    and cached.get("window_start") == start and cached.get("window_end") == end):
                # Reuse the evidence and original generation timestamp, but
                # give this morning its own bounded presentation identity.
                summary = {**cached, "id": uuid.uuid4().hex, "window_kind": "morning", "period_label": label,
                           "unread": True, "reused_from": cached["id"]}
                self.store.save_summary(summary, hashes, now)
                self.store.set("visible_summary_id", summary["id"])
                self._finish_opportunity(opportunity_key, "cached")
                self._state("ready", "已复用昨天的完整回顾，未再次调用模型。", now + SCAN_SECONDS)
                return self.snapshot()
            if not available or (not fresh and not opportunity and (not force or automatic)):
                self._state("ready" if self._visible_summary() else "idle", "当前窗口没有新增的已结束工作记录；未调用模型。", now + SCAN_SECONDS)
                return self.snapshot()
            reason = "manual_refresh" if force else "morning_evaluation" if opportunity else "first_evaluation"
            if not force:
                retry_after = self.store.get("retry_after", 0)
                if now < retry_after:
                    self._state("error", "上次摘要未成功，稍后自动重试；原有回顾仍保留。", retry_after)
                    return self.snapshot()
                due, reason = self._review_due(kind, pending, metrics, interval, now)
                if now < due:
                    self._state("ready" if self._visible_summary() else "idle", "已有新增工作，等待下一次回顾评估。", due)
                    return self.snapshot()
            selected, truncated = self._input(available, pending, window_start=start, window_end=end,
                                              kind=kind, activity_sources=all_sources)
            if not selected:
                raise CodexError("没有可用于回顾的有效会话文本。")
            context = self._model_context(kind, start, end, available, pending, all_sources, now, reason)
            context["unfinished_turns_in_window"] = sum(in_window(s) and not s.get("eligible") for s in all_sources)
            with self.lock:
                if self.closed or self.paused:
                    return self.snapshot()
                visible = self._visible_summary()
                if kind == "recent" and visible and visible.get("window_kind") == "morning":
                    # Work has reached a new evaluation. Yesterday remains in
                    # history, but must not become a pending daytime popup.
                    self.store.set("visible_summary_id", None)
                self.stats_extra.update({"summary_input_truncated": truncated, "summary_source_count": len(selected),
                                         "summary_thread_count": len({s["thread_id"] for s in selected}),
                                         "deferred_window_sources": len(available) - len(selected)})
                calls = self.store.get("model_calls", 0) + 1
                self.store.set("model_calls", calls)
                self.store.set("last_model_attempt_at_v2", now)
                self.stats_extra["model_calls"] = calls
            self._state("summarizing", f"正在整理{label}的工作内容。")
            result = self.adapter.summarize(selected, label, context=context)
            diagnostics = getattr(self.adapter, "last_run_diagnostics", None)
            if isinstance(diagnostics, dict):
                with self.lock:
                    self.stats_extra["last_model_run"] = diagnostics
            result = validate_summary(result, {s["id"] for s in selected})
            generated_at = self.clock()
            output_refs = {ref for item in result["work_items"] for ref in item["source_ids"]}
            # A visible recap advances only cited evidence; suppression consumes
            # only the selected reviewed batch. Failures consume nothing.
            processed_refs = output_refs if result["should_surface"] else {s["id"] for s in selected}
            window_complete = {s["id"] for s in available}.issubset(processed_refs)
            summary = {"id": uuid.uuid4().hex, "period_label": label,
                       **{k: result[k] for k in ("overview", "brief_points", "work_items", "suggestions")},
                       "generated_at": iso(generated_at), "unread": True,
                       "window_kind": kind, "window_start": start, "window_end": end,
                       "policy_version": POLICY_VERSION,
                       "display_after_idle_seconds": result.get("display_after_idle_seconds", 60)}
            with self.lock:
                if self.closed or self.paused:
                    return self.snapshot()
                consumed_hashes = {s["id"]: s["hash"] for s in selected if s["id"] in processed_refs}
                if result["should_surface"]:
                    self.store.save_summary(summary, consumed_hashes, generated_at)
                    self.store.set("visible_summary_id", summary["id"])
                    if window_complete:
                        caches = self.store.get("window_cache_v2", {})
                        caches[cache_key] = {"summary_id": summary["id"], "source_hashes": hashes}
                        self.store.set("window_cache_v2", dict(list(caches.items())[-48:]))
                else:
                    self.store.consume_without_surface(consumed_hashes, generated_at)
                # A morning gets one successful evaluation, even if a bounded
                # batch or the model leaves some yesterday evidence uncited.
                self._finish_opportunity(opportunity_key, "generated" if result["should_surface"] else "suppressed")
                interval = result["next_review_minutes"]
                self.store.set("ai_next_review_minutes", interval)
                self.store.set("timing_reason", result["timing_reason"])
                self.store.set("last_review_context_v2", {"window_kind": kind, **metrics})
                remaining = [s for s in self.store.sources(new_only=True) if in_window(s) and meaningful(s)]
                self.stats_extra.update({"interval_minutes": interval, "timing_reason": result["timing_reason"],
                                         "last_should_surface": result["should_surface"], "new_completed_turns": len(remaining)})
                self.store.set("stats", self.stats_extra)
            self._state("ready" if self._visible_summary() else "idle",
                        ("工作回顾已更新。" if result["should_surface"] else "AI判断没有值得提示的新进展；本批记录已处理。")
                        + ("输入量较大，尚未引用的记录仍保留。" if truncated else ""), generated_at + interval * 60)
            return self.snapshot()
        except Exception as error:
            with self.lock:
                if self.closed:
                    return self.snapshot()
                message = str(error) if isinstance(error, CodexError) else f"回顾处理失败：{type(error).__name__}；原有回顾和待处理记录仍保留。"
                now = self.clock()
                self.store.set("last_error", message[:500])
                self.store.set("retry_after", now + 20 * 60)
                diagnostics = getattr(self.adapter, "last_run_diagnostics", None)
                if isinstance(diagnostics, dict):
                    self.stats_extra["last_model_run"] = diagnostics
                    self.store.set("stats", self.stats_extra)
            self._state("error", message[:500], now + 20 * 60)
            return self.snapshot()
        finally:
            self.tick_lock.release()

    def mark_seen(self, summary_id: str) -> dict:
        with self.lock:
            if not self.closed and isinstance(summary_id, str):
                self.store.mark_seen(summary_id, self.clock())
            return self.snapshot()

    def set_paused(self, paused: bool) -> dict:
        with self.lock:
            if self.closed:
                return self.snapshot()
            self.paused = bool(paused)
            self.store.set("paused", self.paused)
            self.state = "paused" if self.paused else "ready" if self._visible_summary() else "idle"
            self.message = "已暂停自动回顾。" if self.paused else "已恢复自动回顾。"
            self.next_check_at = None if self.paused else self.clock()
            return self.snapshot()

    def record_event(self, payload: dict) -> dict:
        """Optional host activity hint; event payloads never become model input."""
        with self.lock:
            if self.closed or not isinstance(payload, dict):
                return self.snapshot()
            now = self.clock()
            safe = {k: payload[k] for k in ("type", "hook_event_name", "session_id", "thread_id", "turn_id", "user_idle_seconds") if k in payload}
            for key in ("type", "hook_event_name", "session_id", "thread_id", "turn_id"):
                if key in safe:
                    safe[key] = str(safe[key])[:200]
            idle = payload.get("user_idle_seconds")
            if isinstance(idle, (int, float)) and not isinstance(idle, bool) and 0 <= idle <= 86400:
                self.user_idle_seconds, self.idle_observed_at = float(idle), now
            event_name = payload.get("hook_event_name")
            if payload.get("type") == "user_input" or event_name in ("UserPromptSubmit", "Stop", "Interrupt"):
                self.last_activity_at = now
                self.store.set("last_activity_at", now)
                session_id = payload.get("session_id") or payload.get("thread_id")
                if session_id and hasattr(self.adapter, "mark_dirty"):
                    self.adapter.mark_dirty(str(session_id))
            self.store.event(safe, now)
            return self.snapshot()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.adapter.close()
        # Cancel adapter work first; then wait for tick to observe closed before
        # releasing the sqlite connection underneath it.
        with self.tick_lock:
            self.store.close()
