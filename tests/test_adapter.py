import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recap.adapter import CodexAdapter, CodexError, has_tool_calls, restore_summary_references, run_diagnostics, source_from_turn, unknown_reference_count, validate_summary


def final_turn(turn_id="t1", start=100, end=120):
    return {"id": turn_id, "status": "completed", "startedAt": start, "completedAt": end,
            "items": [{"type": "userMessage", "content": [{"type": "text", "text": "实现一个有边界约束的功能"}]},
                      {"type": "agentMessage", "phase": "commentary", "text": "NOISE"},
                      {"type": "reasoning", "text": "PRIVATE"},
                      {"type": "commandExecution", "stdout": "RAW LOG"},
                      {"type": "agentMessage", "phase": "final_answer", "text": "完成实现，自动验证通过但真实环境仍未验证。"}]}


def summary(ref="a:t1", surface=True):
    return {"overview": "完成部分工作。", "work_items": [{"topic": "实现", "status": "in_progress", "progress": "待真实验证", "source_ids": [ref]}] if surface else [],
            "suggestions": [], "should_surface": surface, "next_review_minutes": 40, "timing_reason": "工作量适中。"}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.threads = {"a": {"id": "a", "name": "主会话", "updatedAt": 500, "createdAt": 90, "source": "vscode"},
                        "fork": {"id": "fork", "name": "分叉会话", "updatedAt": 500, "createdAt": 400, "forkedFromId": "a", "source": "vscode"},
                        "archived": {"id": "archived", "name": "归档工作", "updatedAt": 500, "createdAt": 90, "source": "vscode"}}
        self.turns = {"a": [final_turn()], "fork": [final_turn(), final_turn("new", 410, 450)], "archived": [final_turn("archived-turn", 200, 300)]}
    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            keys = ["archived"] if params["archived"] else ["a", "fork"]
            return {"data": [self.threads[k] for k in keys], "nextCursor": None}
        if method == "thread/read": return {"thread": self.threads[params["threadId"]]}
        if method == "thread/turns/list": return {"data": self.turns[params["threadId"]], "nextCursor": None, "backwardsCursor": "cursor"}
        raise AssertionError(method)
    def close(self): pass


class AdapterTests(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory()
    def tearDown(self): self.temp.cleanup()

    def test_public_read_only_collection_includes_archive_and_skips_fork_history(self):
        client = FakeClient(); adapter = CodexAdapter(self.temp.name, client=client)
        result = adapter.collect(80, 600)
        self.assertEqual({s["id"] for s in result["sources"]}, {"a:t1", "fork:new", "archived:archived-turn"})
        self.assertEqual(result["stats"]["fork_copies_skipped"], 1)
        self.assertEqual({p["archived"] for m, p in client.calls if m == "thread/list"}, {True, False})
        self.assertTrue(all(m in ("thread/list", "thread/read", "thread/turns/list") for m, _ in client.calls))

    def test_pending_turn_rereads_even_when_thread_updated_timestamp_unchanged(self):
        client = FakeClient(); client.turns["a"][0]["completedAt"] = None; client.turns["a"][0]["status"] = "interrupted"
        adapter = CodexAdapter(self.temp.name, client=client)
        first = adapter.collect(80, 600)
        pending = next(s for s in first["sources"] if s["thread_id"] == "a")
        self.assertFalse(pending["eligible"])
        client.turns["a"][0] = final_turn()
        second = adapter.collect(80, 610)
        resolved = next(s for s in second["sources"] if s["thread_id"] == "a")
        self.assertTrue(resolved["eligible"])
        self.assertEqual(pending["id"], resolved["id"])
        self.assertNotEqual(pending["hash"], resolved["hash"])

    def test_unchanged_threads_are_skipped_until_hook_marks_dirty(self):
        client = FakeClient(); adapter = CodexAdapter(self.temp.name, client=client)
        adapter.collect(80, 600); client.calls.clear()
        adapter.collect(80, 610)
        self.assertFalse([m for m, p in client.calls if m == "thread/turns/list"])
        adapter.mark_dirty("a"); adapter.collect(80, 620)
        self.assertTrue([m for m, p in client.calls if m == "thread/turns/list"])

    def test_source_excludes_reasoning_and_tool_noise(self):
        value = source_from_turn({"id": "a", "name": "主会话"}, final_turn(), 500)
        self.assertIn("真实环境仍未验证", value["text"])
        self.assertNotIn("NOISE", value["text"])
        self.assertNotIn("RAW LOG", value["text"])
        self.assertNotIn("PRIVATE", value["text"])

    def test_validation_rejects_hallucinated_refs_and_out_of_range_schedule(self):
        with self.assertRaises(CodexError): validate_summary(summary("fake"), {"a:t1"})
        value = summary(); value["next_review_minutes"] = 10
        with self.assertRaises(CodexError): validate_summary(value, {"a:t1"})
        self.assertFalse(validate_summary(summary(surface=False), {"a:t1"})["should_surface"])

    def test_model_display_pause_is_preserved_and_bounded(self):
        value = summary()
        value["display_after_idle_seconds"] = 150
        self.assertEqual(validate_summary(value, {"a:t1"})["display_after_idle_seconds"], 150)
        for invalid in (True, 0, 301, "60"):
            value["display_after_idle_seconds"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(CodexError):
                validate_summary(value, {"a:t1"})

    def test_silent_empty_overview_survives_both_reference_validation_passes(self):
        for overview in ("", "  \n", "不应该作为正文保存的占位语"):
            with self.subTest(overview=overview):
                value = summary("S1")
                value.update(should_surface=False, overview=overview)
                restored = restore_summary_references(value, {"S1": "a:t1"})
                self.assertEqual(restored["overview"], "")
                self.assertFalse(restored["should_surface"])
                self.assertEqual(restored["work_items"][0]["source_ids"], ["a:t1"])
        value = summary(surface=False)
        value["overview"] = ""
        self.assertEqual(restore_summary_references(value, {"S1": "a:t1"})["work_items"], [])

    def test_visible_result_still_requires_nonempty_overview(self):
        for overview in ("", " \n", None, [], 0):
            value = summary("S1")
            value["overview"] = overview
            with self.subTest(overview=overview), self.assertRaises(CodexError):
                restore_summary_references(value, {"S1": "a:t1"})

    def test_silent_result_does_not_bypass_required_fields_types_or_schedule(self):
        for field in ("overview", "should_surface", "work_items", "suggestions", "next_review_minutes", "timing_reason"):
            value = summary(surface=False)
            value["overview"] = ""
            del value[field]
            with self.subTest(missing=field), self.assertRaises(CodexError):
                restore_summary_references(value, {"S1": "a:t1"})
        for field, invalid in (("overview", None), ("overview", []), ("should_surface", 0),
                               ("should_surface", "false"), ("brief_points", [""]),
                               ("work_items", {}), ("suggestions", ""),
                               ("next_review_minutes", True), ("next_review_minutes", 61),
                               ("display_after_idle_seconds", 301), ("timing_reason", "")):
            value = summary(surface=False)
            value["overview"] = ""
            value[field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(CodexError):
                restore_summary_references(value, {"S1": "a:t1"})

    def test_silent_result_still_requires_complete_items_and_exact_evidence(self):
        for ref in ("S9", "a:t1", "s1", "S1 ", None):
            value = summary(ref)
            value.update(overview="", should_surface=False)
            with self.subTest(ref=ref), self.assertRaises(CodexError):
                restore_summary_references(value, {"S1": "a:t1"})
        for field in ("topic", "status", "progress", "source_ids"):
            value = summary("S1")
            value.update(overview="", should_surface=False)
            del value["work_items"][0][field]
            with self.subTest(missing_item_field=field), self.assertRaises(CodexError):
                restore_summary_references(value, {"S1": "a:t1"})

    def test_compact_points_survive_reference_mapping_and_old_results_remain_valid(self):
        value = summary("S1")
        value["brief_points"] = ["Demo已经可用，真实账号验证仍待完成。"]
        restored = restore_summary_references(value, {"S1": "a:t1"})
        self.assertEqual(restored["brief_points"], value["brief_points"])
        self.assertEqual(validate_summary(summary(), {"a:t1"})["brief_points"], [])
        value["brief_points"] = ["过多进展"] * 4
        with self.assertRaises(CodexError): validate_summary(value, {"S1"})

    def test_runtime_tool_detection(self):
        self.assertFalse(has_tool_calls('{"type":"item.completed","item":{"type":"agent_message","text":"result"}}'))
        self.assertTrue(has_tool_calls('{"type":"item.started","item":{"type":"command_execution"}}'))
        self.assertTrue(has_tool_calls('{"type":"item.completed","item":{"type":"mcp_tool_call"}}'))

    def test_isolated_cli_configuration_without_real_model_call(self):
        captured = {}
        class Process:
            returncode = 0
            def __init__(self, command, **kwargs):
                captured["command"], captured["kwargs"] = command, kwargs
                self.command = command
            def communicate(self, prompt=None, timeout=None):
                captured["prompt"] = prompt
                Path(self.command[self.command.index("--output-last-message") + 1]).write_text(json.dumps(summary("S1")), encoding="utf-8")
                captured["schema"] = json.loads(Path(self.command[self.command.index("--output-schema") + 1]).read_text(encoding="utf-8"))
                return '{"type":"item.completed","item":{"type":"agent_message"}}\n', ""
        adapter = CodexAdapter(self.temp.name, executable="test-codex.exe")
        material = source_from_turn({"id": "a", "name": "主会话"}, final_turn(), 500)
        material["activity"] = {"thread_share": 0.85, "thread_turn_count": 12, "is_new": True}
        with patch("recap.adapter.subprocess.Popen", Process), patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder", "CODEX_API_KEY": "test-placeholder"}):
            result = adapter.summarize([material], "昨天", context={"window_kind": "morning", "work_span_minutes": 55,
                "previous_overview": {"text": "此前已完成的成果。", "not_source": True},
                "recent_overviews": [{"text": "此前已完成的成果。", "not_source": True}]})
        command = captured["command"]
        self.assertIn("--ephemeral", command); self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("features.hooks=false", command)
        self.assertNotIn("OPENAI_API_KEY", captured["kwargs"]["env"])
        self.assertNotIn("CODEX_API_KEY", captured["kwargs"]["env"])
        self.assertEqual(result["next_review_minutes"], 40)
        self.assertEqual(result["work_items"][0]["source_ids"], ["a:t1"])
        self.assertNotIn('"id": "a:t1"', captured["prompt"])
        self.assertIn('"id": "S1"', captured["prompt"])
        self.assertIn('"work_span_minutes": 55', captured["prompt"])
        self.assertIn('"window_kind": "morning"', captured["prompt"])
        self.assertIn('"thread_share": 0.85', captured["prompt"])
        self.assertIn('"thread_turn_count": 12', captured["prompt"])
        submitted = json.loads(captured["prompt"].rsplit("\n", 1)[-1])
        self.assertNotIn("previous_overview", submitted["context"])
        self.assertEqual(len(submitted["context"]["recent_overviews"]), 1)
        self.assertTrue(submitted["context"]["recent_overviews"][0]["not_source"])
        self.assertEqual(captured["schema"]["properties"]["work_items"]["items"]["properties"]["source_ids"]["items"]["enum"], ["S1"])
        self.assertFalse(list(Path(self.temp.name).glob("summary-*")))
        diagnostic = json.loads((Path(self.temp.name) / "last-model-run.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["event_counts"], {"item.completed:agent_message": 1})
        self.assertEqual(diagnostic["model"], "gpt-6.1-sol")
        self.assertEqual(diagnostic["reasoning_effort"], "medium")
        self.assertNotIn("prompt", diagnostic)

    def test_exact_aliases_restore_stable_ids_and_reject_raw_or_unknown_refs(self):
        aliases = {"S1": "a:t1", "S2": "b:t2"}
        value = summary("S1")
        value["work_items"][0]["source_ids"] = ["S1", "S2"]
        restored = restore_summary_references(value, aliases)
        self.assertEqual(restored["work_items"][0]["source_ids"], ["a:t1", "b:t2"])
        for wrong in ("a:t1", "S9", "s1", "S1 "):
            with self.subTest(wrong=wrong):
                invalid = summary(wrong)
                self.assertEqual(unknown_reference_count(invalid, aliases), 1)
                with self.assertRaises(CodexError): restore_summary_references(invalid, aliases)
        mixed = summary("S1")
        mixed["work_items"][0]["source_ids"] = ["S1", "a:t1"]
        with self.assertRaises(CodexError): restore_summary_references(mixed, aliases)

    def test_model_output_diagnostic_records_shape_and_failure_without_text(self):
        responses = []
        class Process:
            returncode = 0
            def __init__(self, command, **kwargs):
                self.output = Path(command[command.index("--output-last-message") + 1])
            def communicate(self, prompt=None, timeout=None):
                self.output.write_text(json.dumps(responses.pop(0)), encoding="utf-8")
                return "", ""
        adapter = CodexAdapter(self.temp.name, executable="test-codex.exe")
        material = source_from_turn({"id": "a", "name": "主会话"}, final_turn(), 500)
        for surface, overview, expected_error in ((False, "", None), (True, "", "overview"),
                                                  (False, None, "overview"),
                                                  ("private-decision", "private-body", "should_surface")):
            value = summary("S1")
            value.update(should_surface=surface, overview=overview, timing_reason="private-reason")
            responses.append(value)
            with self.subTest(surface=surface, overview=overview), patch("recap.adapter.subprocess.Popen", Process):
                if expected_error:
                    with self.assertRaises(CodexError):
                        adapter.summarize([material], "最近一小时")
                else:
                    self.assertEqual(adapter.summarize([material], "最近一小时")["overview"], "")
                diagnostic = json.loads((Path(self.temp.name) / "last-model-run.json").read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["validation_failure"], expected_error)
                self.assertEqual(diagnostic["summary_shape"]["should_surface"], surface if isinstance(surface, bool) else None)
                self.assertEqual(diagnostic["summary_shape"]["overview_length"], len(overview) if isinstance(overview, str) else None)
                self.assertEqual(diagnostic["summary_shape"]["overview_type"], type(overview).__name__)
                for private in ("private-decision", "private-body", "private-reason", material["text"]):
                    self.assertNotIn(private, json.dumps(diagnostic, ensure_ascii=False))
                self.assertFalse(list(Path(self.temp.name).glob("summary-*")))

    def test_diagnostics_only_keep_event_types_usage_and_error_categories(self):
        raw = '\n'.join([json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "private chat"}}),
                         json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})])
        result = run_diagnostics(raw, "network retry unauthorized fake-secret", 3, 1, False, 2, 1000)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["stderr_categories"]["retry"], 1)
        self.assertNotIn("private chat", json.dumps(result))
        self.assertNotIn("fake-secret", json.dumps(result))


if __name__ == "__main__": unittest.main()
