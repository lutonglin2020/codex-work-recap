import copy
import datetime as dt
import json
import tempfile
import threading
import unittest

from recap.adapter import CodexError, source_from_turn
from recap.engine import Engine, TZ, adaptive_interval, resolve_window


def timestamp(day=2, hour=20, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


def source(number, now, age=1500, text=None, completed=True, thread="thread-a"):
    return source_from_turn({"id": thread, "name": "实现工作功能", "updatedAt": now}, {
        "id": f"turn-{number}", "status": "completed" if completed else "interrupted",
        "startedAt": now - age - 120, "completedAt": now - age if completed else None,
        "items": [{"type": "userMessage", "content": [{"type": "text", "text": "实现功能并验证边界"}]},
                  {"type": "agentMessage", "phase": "final_answer", "text": text or "完成了功能修复和验证，发现问题已解决。" * 15}]}, now)


class FakeClock:
    def __init__(self, now=timestamp()): self.now = now
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class FakeAdapter:
    def __init__(self):
        self.sources, self.calls, self.labels, self.contexts, self.dirty = [], [], [], [], []
        self.fail = False
        self.surface, self.interval = True, 30
        self.overview = "进行了功能实现及验证。"
        self.display_idle = 60
        self.reference_ids = None
        self.started, self.release = None, None
    def collect(self, start, end, force=False):
        return {"sources": copy.deepcopy(self.sources), "stats": {"threads_scanned": 2},
                "activity_at": max([s.get("completed_at") or s.get("started_at") or 0 for s in self.sources] or [0])}
    def summarize(self, sources, label, context=None):
        self.calls.append(copy.deepcopy(sources)); self.labels.append(label); self.contexts.append(copy.deepcopy(context))
        if self.started:
            self.started.set(); self.release.wait(3)
        if self.fail: raise CodexError("测试网络故障")
        return {"overview": self.overview, "work_items": [{"topic": "项目实现", "status": "in_progress", "progress": "完成部分实现，仍待真实环境验证。",
                 "source_ids": self.reference_ids if self.reference_ids is not None else [s["id"] for s in sources]}] if self.surface else [], "suggestions": [],
                "should_surface": self.surface, "next_review_minutes": self.interval, "timing_reason": "按实际进展调整。",
                "display_after_idle_seconds": self.display_idle}
    def mark_dirty(self, thread_id): self.dirty.append(thread_id)
    def close(self):
        if self.release: self.release.set()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock, self.adapter = FakeClock(), FakeAdapter()
        self.engine = Engine(self.temp.name, adapter=self.adapter, clock=self.clock)
    def tearDown(self): self.engine.close(); self.temp.cleanup()
    def ready_sources(self): self.adapter.sources = [source(1, self.clock()), source(2, self.clock(), age=1300)]

    def test_ordinary_work_is_evaluated_without_round_count_gate(self):
        self.ready_sources()
        state = self.engine.tick()
        self.assertEqual(state["state"], "ready")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertTrue(state["summary"]["unread"])

    def test_work_outside_recent_window_is_not_sent_to_model(self):
        self.adapter.sources = [source(1, self.clock(), age=7200, text="长解释" * 1500)]
        self.engine.tick()
        self.clock.advance(7200); self.engine.tick()
        self.assertFalse(self.adapter.calls)

    def test_single_work_item_can_be_evaluated_while_user_is_active(self):
        self.adapter.sources = [source(1, self.clock(), age=0)]
        self.engine.record_event({"type": "idle_hint", "user_idle_seconds": 0})
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.contexts[-1]["user_idle_seconds"], 0)
        self.assertEqual(self.adapter.contexts[-1]["pending_work_count"], 1)

    def test_manual_force_can_override_minimum_workload(self):
        self.adapter.sources = [source(1, self.clock(), age=2)]
        self.assertIsNotNone(self.engine.tick(force=True)["summary"])

    def test_empty_and_greeting_do_not_call_model_even_forced(self):
        self.engine.tick(force=True)
        greeting = source(1, self.clock(), text="你好！", completed=True)
        greeting["text"] = "用户请求：你好\n助手结果：你好！有什么可以帮你？"
        self.adapter.sources = [greeting]
        self.engine.tick(force=True)
        self.assertFalse(self.adapter.calls)

    def test_unfinished_reconstructed_interrupted_never_counts_completed(self):
        self.adapter.sources = [source(1, self.clock(), completed=False)]
        snap = self.engine.tick(force=True)
        self.assertFalse(self.adapter.calls)
        self.assertEqual(snap["stats"]["pending_turns"], 1)
        self.assertEqual(snap["sources"][0]["status"], "unknown")

    def test_late_completion_overwrites_same_source_id(self):
        self.adapter.sources = [source(1, self.clock(), completed=False)]
        first = self.engine.tick(force=True)["sources"][0]["id"]
        self.adapter.sources = [source(1, self.clock(), completed=True)]
        final = self.engine.tick(force=True)
        self.assertEqual(final["stats"]["sources_total"], 1)
        self.assertEqual(final["sources"][0]["id"], first)
        self.assertEqual(final["stats"]["pending_turns"], 0)

    def test_same_turn_across_fork_is_not_double_counted(self):
        original, fork = source(1, self.clock()), source(1, self.clock(), thread="fork")
        self.adapter.sources = [original, fork]
        self.engine.tick()
        self.assertEqual(self.engine.snapshot()["stats"]["sources_total"], 1)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_failure_does_not_advance_and_model_retries_back_off(self):
        self.ready_sources(); self.adapter.fail = True
        self.assertEqual(self.engine.tick()["state"], "error")
        self.assertEqual(len(self.engine.store.sources(new_only=True)), 2)
        self.clock.advance(60); self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.adapter.fail = False
        self.assertEqual(self.engine.tick(force=True)["state"], "ready")
        self.assertEqual(len(self.engine.store.sources(new_only=True)), 0)

    def test_suppressed_review_consumes_sources_and_preserves_unread_summary(self):
        self.ready_sources(); old = self.engine.tick(force=True)["summary"]
        self.clock.advance(1900)
        self.adapter.sources += [source(3, self.clock()), source(4, self.clock(), age=1400)]
        self.adapter.surface = False
        state = self.engine.tick(force=True)
        self.assertEqual(state["summary"]["id"], old["id"])
        self.assertTrue(state["summary"]["unread"])
        calls = len(self.adapter.calls)
        self.clock.advance(7200); self.engine.tick()
        self.assertEqual(len(self.adapter.calls), calls)

    def test_empty_silent_result_consumes_once_without_failure_or_popup(self):
        self.ready_sources()
        self.adapter.surface, self.adapter.overview = False, ""
        state = self.engine.tick(force=True)
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["summary"])
        self.assertFalse(self.engine.store.sources(new_only=True))
        self.assertFalse(self.engine.store.get("last_error"))
        self.assertFalse(self.engine.store.get("retry_after"))
        self.clock.advance(1900)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_seen_does_not_change_consumption_and_new_summary_unread(self):
        self.ready_sources(); first = self.engine.tick(force=True)["summary"]
        seen = self.engine.mark_seen(first["id"])
        self.assertFalse(seen["summary"]["unread"])
        self.assertFalse(self.engine.store.sources(new_only=True))
        self.clock.advance(3600)
        self.adapter.sources += [source(3, self.clock()), source(4, self.clock())]
        self.assertTrue(self.engine.tick(force=True)["summary"]["unread"])

    def test_unread_cross_window_sources_are_not_model_input(self):
        self.adapter.sources = [source(1, timestamp(1, 10)), source(2, timestamp(1, 10), age=1200)]
        old = self.engine.tick(force=True, period="yesterday")["summary"]
        fresh = [source(3, self.clock()), source(4, self.clock())]
        self.adapter.sources += fresh
        result = self.engine.tick(force=True)
        self.assertEqual({s["id"] for s in self.adapter.calls[-1]}, {s["id"] for s in fresh})
        self.assertFalse(any(s.get("carry_unread") for s in self.adapter.calls[-1]))
        self.assertIn(old["id"], {s["id"] for s in result["unread_history"]})
        self.assertNotIn("previous_overview", self.adapter.contexts[-1])

    def test_valid_new_recap_can_omit_unread_history_without_marking_it_read(self):
        self.adapter.sources = [source(1, timestamp(1, 10)), source(2, timestamp(1, 10), age=1200)]
        old = self.engine.tick(force=True, period="yesterday")["summary"]
        fresh = [source(3, self.clock()), source(4, self.clock())]
        self.adapter.sources += fresh
        self.adapter.reference_ids = [s["id"] for s in fresh]
        result = self.engine.tick(force=True, period="evening")
        self.assertEqual(result["state"], "ready")
        self.assertNotEqual(result["summary"]["id"], old["id"])
        self.assertTrue(self.engine.store.summary(old["id"])["unread"])
        self.assertIn(old["id"], {s["id"] for s in result["unread_history"]})
        self.assertEqual(result["summary"]["period_label"], "今天晚上")
        self.assertEqual({s["id"] for s in result["sources"]}, set(self.adapter.reference_ids))
        self.assertFalse(self.engine.store.sources(new_only=True))

    def test_partial_morning_evaluation_finishes_opportunity_and_preserves_uncited_evidence(self):
        self.clock.now = timestamp(2, 8, 20)
        self.adapter.sources = [source(1, timestamp(1, 10)), source(2, timestamp(1, 11), age=1200)]
        self.adapter.reference_ids = [self.adapter.sources[0]["id"]]
        result = self.engine.tick()
        self.assertEqual(result["state"], "ready")
        self.assertEqual({s["id"] for s in self.engine.store.sources(new_only=True)}, {self.adapter.sources[1]["id"]})
        self.assertEqual({s["id"] for s in result["sources"]}, set(self.adapter.reference_ids))
        self.assertFalse(self.engine.store.get("window_cache_v2", {}))
        self.assertEqual(self.engine.store.get("morning_opportunities_v2")["2026-10-02"], "generated")
        self.clock.advance(1900)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_unknown_reference_still_rejects_without_advancing_evidence(self):
        self.ready_sources()
        self.adapter.reference_ids = ["unknown-source"]
        result = self.engine.tick(force=True)
        self.assertEqual(result["state"], "error")
        self.assertIsNone(result["summary"])
        self.assertEqual(len(self.engine.store.sources(new_only=True)), 2)

    def test_ai_interval_controls_following_review_without_same_batch_repeats(self):
        self.ready_sources(); self.adapter.interval = 60; self.engine.tick(force=True)
        self.clock.advance(1500)
        self.adapter.sources += [source(3, self.clock(), age=0), source(4, self.clock(), age=0)]
        self.engine.tick(); self.assertEqual(len(self.adapter.calls), 1)
        self.clock.advance(2200); self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.clock.advance(3600); self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)

    def test_hook_input_activity_and_stop_dirty_thread(self):
        self.engine.record_event({"session_id": "main", "hook_event_name": "UserPromptSubmit", "last_assistant_message": "do not retain raw hook text"})
        self.assertEqual(self.engine.last_activity_at, self.clock())
        self.engine.record_event({"session_id": "main", "hook_event_name": "Stop"})
        self.assertEqual(self.adapter.dirty, ["main", "main"])

    def test_pause_survives_restart_and_no_work_runs_while_paused(self):
        self.ready_sources(); self.engine.set_paused(True)
        self.engine.tick(force=True); self.assertFalse(self.adapter.calls)
        self.engine.close()
        self.engine = Engine(self.temp.name, adapter=self.adapter, clock=self.clock)
        self.assertTrue(self.engine.snapshot()["paused"])

    def test_snapshot_stays_responsive_while_model_runs_and_pause_wins(self):
        self.ready_sources(); self.adapter.started, self.adapter.release = threading.Event(), threading.Event()
        worker = threading.Thread(target=lambda: self.engine.tick(force=True))
        worker.start(); self.assertTrue(self.adapter.started.wait(2))
        self.assertEqual(self.engine.snapshot()["state"], "summarizing")
        self.engine.set_paused(True); self.adapter.release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.engine.snapshot()["state"], "paused")

    def test_morning_today_one_question_does_not_displace_prior_workday(self):
        self.clock.now = timestamp(2, 8, 10)
        self.adapter.sources = [source(1, timestamp(1, 16)), source(2, timestamp(1, 16), age=1200),
                                source(3, self.clock(), age=600, text="一个简短问题的普通解释")]
        self.engine.tick()
        self.assertEqual(self.adapter.labels, ["昨天"])
        self.clock.advance(60); self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_noon_has_only_recent_work_and_no_special_morning_recap(self):
        self.clock.now = timestamp(2, 12, 20)
        self.adapter.sources = [source(1, timestamp(2, 10)), source(2, timestamp(2, 11), age=1200),
                                source(3, timestamp(2, 12, 10), age=100, completed=False),
                                source(4, self.clock(), age=300)]
        result = self.engine.tick()
        self.assertEqual(result["summary"]["window_kind"], "recent")
        self.assertEqual({s["turn_id"] for s in self.adapter.calls[0]}, {"turn-4"})
        self.assertEqual(self.adapter.contexts[-1]["unfinished_turns_in_window"], 1)

    def test_daily_natural_opportunities_are_not_only_first_process_summary(self):
        self.ready_sources(); self.engine.tick(force=True)
        self.clock.now = timestamp(3, 8)
        self.adapter.sources += [source(3, timestamp(2, 23)), source(4, timestamp(2, 23), age=1200)]
        self.engine.tick()
        self.assertEqual(self.adapter.labels[-1], "昨天")

    def test_complete_v2_yesterday_cache_can_be_reused_for_morning(self):
        self.clock.now = timestamp(2, 8, 10)
        self.adapter.sources = [source(1, timestamp(1, 10)), source(2, timestamp(1, 11), age=1200)]
        first = self.engine.tick(force=True, period="yesterday")["summary"]
        self.clock.advance(60)
        result = self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(result["summary"]["reused_from"], first["id"])
        self.assertEqual(result["summary"]["generated_at"], first["generated_at"])
        self.assertEqual(result["summary"]["window_kind"], "morning")

    def test_empty_yesterday_does_not_fall_back_to_older_work_or_retry_minutely(self):
        self.clock.now = timestamp(4, 8)
        self.adapter.sources = [source(1, timestamp(2, 16))]
        self.engine.tick(); self.clock.advance(60); self.engine.tick()
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.engine.store.get("morning_opportunities_v2")["2026-10-04"], "empty")

    def test_recent_label_is_fixed_and_expired_summary_does_not_fall_back(self):
        self.ready_sources()
        first = self.engine.tick(force=True)["summary"]
        self.clock.advance(1200)
        self.assertEqual(self.engine.snapshot()["summary"]["period_label"], first["period_label"])
        self.clock.advance(2400)
        self.assertIsNone(self.engine.snapshot()["summary"])
        self.assertIsNotNone(self.engine.store.summary(first["id"]))

    def test_cache_is_bounded(self):
        self.adapter.sources = [source(i, self.clock(), age=i + 2000) for i in range(700)]
        self.engine.tick()
        self.assertLessEqual(self.engine.snapshot()["stats"]["sources_total"], 600)
        self.assertLessEqual(len(self.engine.snapshot()["sources"]), 80)

    def test_human_windows_and_interval_bounds(self):
        start, end, label = resolve_window("昨天", [], timestamp(2, 8))
        self.assertEqual(label, "昨天")
        self.assertEqual(dt.datetime.fromtimestamp(start, TZ).date(), dt.date(2026, 10, 1))
        self.assertEqual(end - start, 86400)
        self.assertEqual(adaptive_interval(1, 100), 60)
        self.assertEqual(adaptive_interval(12, 100), 20)

    def test_input_keeps_a_bounded_secondary_candidate_beside_active_work(self):
        a = [source(i, self.clock(), age=i + 1300, text="高频会话的长结果。" * 600, thread="a") for i in range(20)]
        b = source(99, self.clock(), age=3000, text="另一个项目的重要实际修复。" * 200, thread="b")
        selected, truncated = self.engine._input(a + [b], a + [b])
        self.assertTrue(truncated)
        self.assertEqual({s["thread_id"] for s in selected}, {"a", "b"})
        self.assertLessEqual(len(selected), 20)

    def test_sustained_active_chat_gets_most_text_over_one_old_long_reply(self):
        active = [source(i, self.clock(), age=60 + i * 240, text="当前项目的具体实现与验证。" * 150,
                         thread="active") for i in range(12)]
        secondary = source(99, self.clock(), age=3300, text="一条较早完成的普通咨询长回复。" * 200, thread="secondary")
        selected, truncated = self.engine._input(active + [secondary], active + [secondary])
        main = [item for item in selected if item["thread_id"] == "active"]
        extra = [item for item in selected if item["thread_id"] == "secondary"]
        self.assertTrue(main)
        self.assertTrue(extra)
        self.assertGreaterEqual(sum(len(item["text"]) for item in main) / sum(len(item["text"]) for item in selected), .7)
        self.assertLessEqual(len(extra), 1)
        self.assertLessEqual(len(extra[0]["text"]), 600)
        self.assertGreater(main[0]["activity"]["thread_share"], extra[0]["activity"]["thread_share"])
        self.assertEqual(main[0]["activity"]["thread_turn_count"], 12)

    def test_many_weak_chats_cannot_each_claim_a_baseline_and_bury_main_work(self):
        active = [source(i, self.clock(), age=60 + i * 240, text="持续项目的实际成果与待办。" * 120,
                         thread="active") for i in range(12)]
        weak = [source(100 + i, self.clock(), age=3300, text="一条独立普通咨询的长解释。" * 200,
                       thread=f"weak-{i}") for i in range(20)]
        selected, truncated = self.engine._input(active + weak, active + weak)
        self.assertTrue(truncated)
        self.assertLessEqual(len({item["thread_id"] for item in selected if item["thread_id"] != "active"}), 5)
        self.assertGreaterEqual(sum(len(item["text"]) for item in selected if item["thread_id"] == "active") /
                                sum(len(item["text"]) for item in selected), .7)

    def test_activity_metadata_counts_toward_the_material_budget(self):
        records = [source(i, self.clock(), age=30 + i * 90, text="实际工作完成情况和细节。" * 500,
                          thread=f"thread-{i % 4}") for i in range(35)]
        selected, truncated = self.engine._input(records, records)
        self.assertTrue(truncated)
        self.assertLessEqual(len(selected), 20)
        size = sum(len(item["text"]) + len(item["title"]) + 200 +
                   len(json.dumps(item["activity"], ensure_ascii=False)) for item in selected)
        self.assertLessEqual(size, 13000)
        for item in selected:
            self.assertTrue(0 <= item["activity"]["thread_share"] <= 1)
            self.assertTrue(item["activity"]["is_new"])
            self.assertNotIn("_score", item["activity"])

    def test_reply_length_does_not_change_activity_shares(self):
        records = [source(1, self.clock(), age=300, thread="a"), source(2, self.clock(), age=2400, thread="b")]
        before = self.engine._activity_profiles(records, self.clock() - 3600, self.clock(), "recent")
        records[1]["text"] *= 100
        after = self.engine._activity_profiles(records, self.clock() - 3600, self.clock(), "recent")
        self.assertEqual(before, after)

    def test_daily_activity_does_not_discount_equivalent_morning_work_against_late_night(self):
        start, end = timestamp(1, 0), timestamp(2, 0)
        early = [source(i, timestamp(1, 9, 30), age=i * 300, thread="morning") for i in range(4)]
        late = [source(10 + i, timestamp(1, 23, 30), age=i * 300, thread="night") for i in range(4)]
        profiles = self.engine._activity_profiles(early + late, start, end, "morning")
        self.assertEqual(profiles["morning"]["thread_share"], profiles["night"]["thread_share"])

    def test_recent_start_of_unfinished_main_work_prevents_completed_side_chat_claiming_all_activity(self):
        ongoing = source(1, self.clock(), age=0, completed=False, thread="main")
        old_side = source(2, self.clock(), age=3300, thread="side")
        self.adapter.sources = [ongoing, old_side]
        self.engine.tick()
        self.assertEqual({item["thread_id"] for item in self.adapter.calls[-1]}, {"side"})
        self.assertLess(self.adapter.calls[-1][0]["activity"]["thread_share"], 1)
        self.assertLessEqual(len(self.adapter.calls[-1][0]["text"]), 600)
        context = self.adapter.contexts[-1]
        self.assertEqual(context["activity_threads_with_unfinished_work"], 1)
        self.assertGreater(context["activity_share_with_unfinished_work"], .5)
        self.assertTrue(context["activity_is_not_importance"])
        self.assertTrue(context["no_equal_session_coverage"])

    def test_old_work_without_window_events_does_not_inflate_activity_share(self):
        old = [source(i, self.clock(), age=7200 + i * 300, thread="old") for i in range(20)]
        current = source(99, self.clock(), age=100, thread="current")
        selected, _ = self.engine._input([current], [current], activity_sources=old + [current])
        self.assertEqual(selected[0]["activity"]["thread_share"], 1)
        self.assertEqual(selected[0]["activity"]["thread_turn_count"], 1)

    def test_long_task_just_completed_remains_work_even_after_an_hour_without_input(self):
        task = source(1, self.clock(), age=0)
        task["started_at"] = self.clock() - 7200
        self.adapter.sources = [task]
        self.engine.record_event({"type": "idle_hint", "user_idle_seconds": 3600})
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        activity = self.adapter.calls[-1][0]["activity"]
        self.assertEqual(activity["thread_share"], 1)
        self.assertEqual(activity["recent_turn_count"], 1)
        self.assertEqual(activity["window_span_minutes"], 60)
        self.assertEqual(self.adapter.contexts[-1]["user_idle_seconds"], 3600)

    def test_summary_sources_are_only_its_actual_evidence(self):
        self.ready_sources()
        self.adapter.sources += [source(3, timestamp(1, 12)), source(4, self.clock(), completed=False)]
        result = self.engine.tick(force=True, period="today")
        cited = {ref for item in result["summary"]["work_items"] for ref in item["source_ids"]}
        self.assertEqual({s["id"] for s in result["sources"]}, cited)

    def test_recent_window_has_exact_sliding_boundaries_even_when_forced_in_morning(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, self.clock(), age=3601), source(2, self.clock(), age=3600),
                                source(3, self.clock(), age=0), source(4, timestamp(1, 16))]
        result = self.engine.tick(force=True)
        self.assertEqual({s["turn_id"] for s in self.adapter.calls[-1]}, {"turn-2", "turn-3"})
        summary = result["summary"]
        self.assertEqual(summary["window_kind"], "recent")
        self.assertEqual(summary["window_start"], self.clock() - 3600)
        self.assertEqual(summary["window_end"], self.clock())
        self.assertEqual(summary["policy_version"], 2)
        self.assertEqual(summary["period_label"], "最近60分钟（07:00–08:00）")
        self.assertFalse(self.engine.store.get("morning_opportunities_v2", {}))

    def test_single_yesterday_work_gets_morning_evaluation_then_survives_restart(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        first = self.engine.tick()["summary"]
        self.assertEqual(first["window_kind"], "morning")
        self.assertEqual(len(self.adapter.calls[-1]), 1)
        self.engine.close()
        self.engine = Engine(self.temp.name, adapter=self.adapter, clock=self.clock)
        self.clock.advance(60)
        result = self.engine.tick()
        self.assertEqual(result["summary"]["id"], first["id"])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_dawn_work_does_not_consume_later_morning_opportunity(self):
        self.clock.now = timestamp(2, 1)
        self.adapter.sources = [source(1, timestamp(1, 16)), source(2, self.clock(), age=100)]
        self.assertEqual(self.engine.tick()["summary"]["window_kind"], "recent")
        self.clock.now = timestamp(2, 8)
        self.assertEqual(self.engine.tick()["summary"]["window_kind"], "morning")
        self.assertEqual({s["turn_id"] for s in self.adapter.calls[-1]}, {"turn-1"})
        self.assertEqual(self.adapter.contexts[-1]["work_since_06_today"]["pending_work_count"], 0)

    def test_morning_context_reports_current_work_without_adding_today_as_evidence(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16)), source(2, self.clock(), age=300),
                                source(3, self.clock(), age=10, completed=False, thread="other")]
        self.adapter.surface = False
        self.engine.tick()
        context = self.adapter.contexts[-1]
        self.assertEqual(context["work_since_06_today"]["pending_work_count"], 2)
        self.assertEqual(context["work_since_06_today"]["unfinished_turns"], 1)
        self.assertEqual({s["turn_id"] for s in self.adapter.calls[-1]}, {"turn-1"})
        self.assertEqual(self.engine.store.get("morning_opportunities_v2")["2026-10-02"], "suppressed")
        self.clock.advance(60)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_morning_summary_expires_at_noon_without_erasing_history(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        first = self.engine.tick()["summary"]
        self.clock.now = timestamp(2, 12)
        self.assertIsNone(self.engine.snapshot()["summary"])
        self.assertTrue(self.engine.store.summary(first["id"])["unread"])

    def test_failed_morning_can_retry_and_does_not_finish_the_daily_opportunity(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        self.adapter.fail = True
        self.engine.tick()
        self.assertFalse(self.engine.store.get("morning_opportunities_v2", {}))
        self.assertEqual(len(self.engine.store.sources(new_only=True)), 1)
        self.adapter.fail = False
        self.clock.advance(1200)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.engine.store.get("morning_opportunities_v2")["2026-10-02"], "generated")

    def test_relative_workload_can_advance_evaluation_after_five_minute_debounce(self):
        self.ready_sources()
        self.adapter.interval = 60
        self.engine.tick()
        self.clock.advance(120)
        self.adapter.sources += [source(i, self.clock(), age=0, text="重要工作的新实现及验证结果。" * 150,
                                        thread=f"chat-{i}") for i in range(3, 7)]
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.clock.advance(180)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.contexts[-1]["evaluation_reason"], "increased_workload")
        self.assertEqual(self.adapter.contexts[-1]["pending_work_chats"], 4)

    def test_small_new_work_does_not_call_model_every_debounce_period(self):
        self.ready_sources()
        self.adapter.interval = 60
        self.engine.tick()
        self.clock.advance(300)
        self.adapter.sources.append(source(3, self.clock(), age=0, text="关于功能的一次简短说明，暂无额外修改。"))
        self.engine.tick()
        self.clock.advance(300)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_single_pending_source_is_evaluated_before_it_exits_recent_window(self):
        self.ready_sources()
        self.adapter.interval = 60
        self.engine.tick()
        self.clock.advance(600)
        self.adapter.sources.append(source(3, self.clock(), age=3300))
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.clock.advance(120)
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.contexts[-1]["evaluation_reason"], "pending_work_nearing_window_end")
        self.assertIn("turn-3", {s["turn_id"] for s in self.adapter.calls[-1]})

    def test_previous_overview_can_dedup_adjacent_same_work_without_extending_sources(self):
        self.ready_sources()
        first = self.engine.tick(force=True)["summary"]
        self.clock.advance(300)
        self.adapter.sources.append(source(3, self.clock(), age=0))
        self.engine.tick(force=True)
        prior = self.adapter.contexts[-1]["previous_overview"]
        self.assertEqual(prior["text"], first["overview"])
        self.assertTrue(prior["not_source"])
        self.clock.advance(4000)
        self.adapter.sources.append(source(4, self.clock(), age=0))
        self.engine.tick(force=True)
        self.assertTrue(self.adapter.contexts[-1]["previous_overview"]["not_source"])
        self.assertEqual(self.adapter.contexts[-1]["previous_overview"]["relation"], "recent_same_work_source")
        self.assertEqual({item["turn_id"] for item in self.adapter.calls[-1]}, {"turn-4"})

    def test_recent_summary_history_is_bounded_ordered_and_independent_of_read_state(self):
        self.ready_sources()
        original = self.engine.tick(force=True)["summary"]
        for index in range(14):
            saved = {**original, "id": f"saved-{index}", "overview": f"已记录成果{index}"}
            self.engine.store.save_summary(saved, {}, self.clock())
            if index % 2:
                self.engine.store.mark_seen(saved["id"], self.clock())
        history = self.engine.store.recent_summaries(99)
        self.assertEqual(len(history), 12)
        self.assertEqual([item["id"] for item in history], [f"saved-{index}" for index in range(13, 1, -1)])
        self.assertFalse(history[0]["unread"])
        self.assertTrue(history[1]["unread"])
        self.assertFalse(self.engine.store.recent_summaries(0))

    def test_dedup_keeps_three_bounded_overviews_even_after_they_are_marked_read(self):
        self.ready_sources()
        original = self.engine.tick(force=True)["summary"]
        for index in range(4):
            self.clock.advance(60)
            saved = {**original, "id": f"seen-{index}", "overview": f"独立成果{index}。" + "具体使用结果。" * 100,
                     "generated_at": dt.datetime.fromtimestamp(self.clock(), TZ).isoformat()}
            self.engine.store.save_summary(saved, {}, self.clock())
            self.engine.store.mark_seen(saved["id"], self.clock())
        records = self.engine.store.sources()
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), records,
                                             records, records, self.clock(), "test")
        references = context["recent_overviews"]
        self.assertEqual(len(references), 3)
        self.assertLessEqual(sum(len(item["text"]) for item in references), 1200)
        self.assertTrue(references[0]["text"].startswith("独立成果3"))
        self.assertTrue(all(item["not_source"] and "source_ids" not in item for item in references))
        self.assertEqual(context["previous_overview"], references[0])
        self.assertTrue(context["fresh_record_is_not_new_outcome"])

    def test_latest_other_scope_does_not_mask_earlier_relevant_seen_overview(self):
        self.ready_sources()
        recent = self.engine.tick(force=True)["summary"]
        self.engine.mark_seen(recent["id"])
        self.clock.advance(300)
        explicit = {**recent, "id": "different-scope", "window_kind": "explicit", "overview": "另一个明确查询范围的回顾。"}
        self.engine.store.save_summary(explicit, {}, self.clock())
        records = self.engine.store.sources()
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), records,
                                             records, records, self.clock(), "test")
        self.assertEqual([item["text"] for item in context["recent_overviews"]], [recent["overview"]])

    def test_new_turn_id_can_get_duplicate_outcome_context_without_shared_evidence_id(self):
        self.ready_sources()
        previous = self.engine.tick(force=True)["summary"]
        self.clock.advance(300)
        new_record = source(99, self.clock(), age=0)
        all_sources = self.engine.store.sources() + [new_record]
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), [new_record],
                                             [new_record], all_sources, self.clock(), "test")
        self.assertEqual(context["recent_overviews"][0]["text"], previous["overview"])
        self.assertTrue(context["recent_overviews"][0]["not_source"])

    def test_adjacent_unrelated_work_and_overviews_older_than_two_hours_are_excluded(self):
        self.ready_sources()
        self.engine.tick(force=True)
        old_sources = self.engine.store.sources()
        self.clock.advance(4000)
        unrelated = source(99, self.clock(), age=0, thread="unrelated")
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), [unrelated],
                                             [unrelated], old_sources + [unrelated], self.clock(), "test")
        self.assertNotIn("recent_overviews", context)
        self.clock.advance(3300)
        same_work = source(100, self.clock(), age=0)
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), [same_work],
                                             [same_work], old_sources + [same_work], self.clock(), "test")
        self.assertNotIn("recent_overviews", context)

    def test_morning_overview_references_only_same_morning_window(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        morning = self.engine.tick()["summary"]
        records = self.engine.store.sources()
        # Even recently saved rolling/explicit recaps must not prohibit the
        # intentional new morning recap of yesterday's outcomes.
        recent = {**morning, "id": "rolling-copy", "window_kind": "recent", "overview": "昨晚滚动回顾已提过的结果。"}
        self.engine.store.save_summary(recent, {}, self.clock())
        self.clock.advance(60)
        context = self.engine._model_context("morning", morning["window_start"], morning["window_end"], records,
                                             records, records, self.clock(), "test")
        self.assertEqual([item["text"] for item in context["recent_overviews"]], [morning["overview"]])
        context = self.engine._model_context("morning", morning["window_start"] - 86400,
                                             morning["window_end"] - 86400, records, records, records,
                                             self.clock(), "test")
        self.assertNotIn("recent_overviews", context)

    def test_duplicate_saved_overviews_do_not_use_multiple_dedup_slots(self):
        self.ready_sources()
        previous = self.engine.tick(force=True)["summary"]
        for index in range(3):
            self.engine.store.save_summary({**previous, "id": f"copy-{index}"}, {}, self.clock())
        records = self.engine.store.sources()
        context = self.engine._model_context("recent", self.clock() - 3600, self.clock(), records,
                                             records, records, self.clock(), "test")
        self.assertEqual(len(context["recent_overviews"]), 1)

    def test_old_policy_summary_is_preserved_but_never_default_visible(self):
        self.ready_sources()
        old = self.engine.tick(force=True)["summary"]
        old = {**old, "id": "legacy", "policy_version": 1}
        self.engine.store.save_summary(old, {}, self.clock())
        self.engine.store.set("visible_summary_id", old["id"])
        self.engine.close()
        self.engine = Engine(self.temp.name, adapter=self.adapter, clock=self.clock)
        self.assertIsNone(self.engine.snapshot()["summary"])
        self.assertIsNotNone(self.engine.store.summary(old["id"]))

    def test_explicitly_empty_visible_slot_does_not_fall_back_to_latest_summary(self):
        self.ready_sources()
        old = self.engine.tick(force=True)["summary"]
        self.engine.store.set("visible_summary_id", None)
        self.assertIsNone(self.engine.snapshot()["summary"])
        self.assertIsNotNone(self.engine.store.summary(old["id"]))

    def test_first_empty_window_has_no_retained_display(self):
        state = self.engine.tick()
        self.assertIsNone(state["summary"])
        self.assertIsNone(state["display_summary"])
        self.assertFalse(state["display_summary_retained"])
        self.assertFalse(self.adapter.calls)

    def test_expired_recap_remains_displayable_without_reentering_current_window(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.clock.advance(7200)
        state = self.engine.tick()
        self.assertIsNone(state["summary"])
        self.assertEqual(state["display_summary"], old)
        self.assertTrue(state["display_summary_retained"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.engine.store.get("visible_summary_id"), old["id"])

    def test_retained_recap_survives_seen_state_and_restart_without_rewriting_it(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.engine.mark_seen(old["id"])
        saved = self.engine.store.summary(old["id"])
        self.clock.advance(86400)
        self.engine.close()
        self.engine = Engine(self.temp.name, adapter=self.adapter, clock=self.clock)
        state = self.engine.snapshot()
        self.assertIsNone(state["summary"])
        self.assertEqual(state["display_summary"], saved)
        self.assertFalse(state["display_summary"]["unread"])
        self.assertTrue(state["display_summary_retained"])
        self.assertEqual(self.engine.store.summary(old["id"]), saved)

    def test_failed_update_keeps_previous_display_and_pending_evidence(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.clock.advance(7200)
        self.adapter.sources.append(source(3, self.clock(), age=0))
        self.adapter.fail = True
        state = self.engine.tick()
        self.assertEqual(state["state"], "error")
        self.assertIsNone(state["summary"])
        self.assertEqual(state["display_summary"], old)
        self.assertTrue(state["display_summary_retained"])
        self.assertEqual([item["id"] for item in self.engine.store.sources(new_only=True)],
                         [self.adapter.sources[-1]["id"]])

    def test_inflight_update_keeps_previous_display(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.clock.advance(7200)
        self.adapter.sources.append(source(3, self.clock(), age=0))
        self.adapter.started, self.adapter.release = threading.Event(), threading.Event()
        worker = threading.Thread(target=self.engine.tick)
        worker.start()
        try:
            self.assertTrue(self.adapter.started.wait(2))
            state = self.engine.snapshot()
            self.assertEqual(state["state"], "summarizing")
            self.assertEqual(state["display_summary"], old)
            self.assertTrue(state["display_summary_retained"])
        finally:
            self.adapter.release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_successful_update_replaces_retained_recap(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.clock.advance(7200)
        self.adapter.sources.append(source(3, self.clock(), age=0))
        self.adapter.overview = "完成了新的工作。"
        state = self.engine.tick()
        self.assertNotEqual(state["summary"]["id"], old["id"])
        self.assertEqual(state["display_summary"], state["summary"])
        self.assertFalse(state["display_summary_retained"])
        self.assertNotIn(old["id"], [item["id"] for item in self.adapter.calls[-1]])
        self.assertEqual(len(self.adapter.calls[-1]), 1)

    def test_retained_display_skips_empty_or_old_policy_history(self):
        self.ready_sources()
        old = self.engine.tick()["summary"]
        self.engine.store.save_summary({**old, "id": "legacy", "policy_version": 1}, {}, self.clock() + 1)
        self.engine.store.save_summary({**old, "id": "empty", "overview": "  "}, {}, self.clock() + 2)
        self.engine.store.set("visible_summary_id", None)
        state = self.engine.snapshot()
        self.assertEqual(state["display_summary"]["id"], old["id"])
        self.assertTrue(state["display_summary_retained"])

    def test_old_morning_opportunity_does_not_disable_v2_morning_policy(self):
        self.clock.now = timestamp(2, 8)
        self.engine.store.set("natural_opportunities", {"2026-10-02:morning": "generated"})
        self.adapter.sources = [source(1, timestamp(1, 16))]
        self.assertEqual(self.engine.tick()["summary"]["window_kind"], "morning")

    def test_ai_display_idle_preference_is_saved_and_survives_snapshot(self):
        self.ready_sources()
        self.adapter.display_idle = 180
        result = self.engine.tick()
        self.assertEqual(result["summary"]["display_after_idle_seconds"], 180)
        self.assertEqual(self.engine.store.summary(result["summary"]["id"])["display_after_idle_seconds"], 180)

    def test_forced_auto_does_not_reconsume_unchanged_evidence(self):
        self.ready_sources()
        self.engine.tick(force=True)
        self.clock.advance(300)
        self.engine.tick(force=True)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_current_activity_prevents_cache_reuse_from_bypassing_morning_ai_decision(self):
        self.clock.now = timestamp(2, 7)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        self.engine.tick(force=True, period="yesterday")
        self.clock.advance(600)
        self.adapter.sources.append(source(2, self.clock(), age=0))
        self.adapter.surface = False
        self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.contexts[-1]["work_since_06_today"]["pending_work_count"], 1)
        self.assertIsNone(self.engine.snapshot()["summary"])

    def test_recent_suppression_clears_previous_morning_presentation_but_keeps_history(self):
        self.clock.now = timestamp(2, 8)
        self.adapter.sources = [source(1, timestamp(1, 16))]
        morning = self.engine.tick()["summary"]
        self.clock.advance(60)
        self.engine.tick()
        self.assertEqual(self.engine.snapshot()["summary"]["id"], morning["id"])
        self.clock.advance(240)
        self.adapter.sources.append(source(2, self.clock(), age=0))
        self.adapter.surface = False
        result = self.engine.tick()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertIsNone(result["summary"])
        self.assertTrue(self.engine.store.summary(morning["id"])["unread"])
        self.assertEqual(result["display_summary"]["id"], morning["id"])
        self.assertEqual(result["display_summary"]["generated_at"], morning["generated_at"])
        self.assertTrue(result["display_summary_retained"])


if __name__ == "__main__": unittest.main()
