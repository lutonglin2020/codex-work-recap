"""MCP protocol tests; service startup and every HTTP request are patched."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recap import mcp


class McpTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="recap-mcp-test-")
        root = Path(self.workspace.name)
        (root / "ui").mkdir()
        (root / "ui/index.html").write_text("<!doctype html><title>Synthetic recap</title>", encoding="utf-8")
        self.root_patch = patch.object(mcp, "ROOT", root)
        self.root_patch.start()
        self.ensure_patch = patch.object(mcp, "ensure_service", return_value={"port": 12345, "token": "test-only"})
        self.ensure_service = self.ensure_patch.start()
        self.request_patch = patch.object(mcp, "request", return_value={"summaries": [], "paused": False})
        self.request = self.request_patch.start()

    def tearDown(self):
        self.request_patch.stop()
        self.ensure_patch.stop()
        self.root_patch.stop()
        self.workspace.cleanup()

    def transport(self, lines):
        source = io.StringIO("\n".join(lines) + "\n")
        target = io.StringIO()
        with patch.object(mcp.sys, "stdin", source), patch.object(mcp.sys, "stdout", target):
            mcp.run()
        return [json.loads(line) for line in target.getvalue().splitlines()]

    def test_initialize_tools_and_resources_do_not_start_service_or_call_model(self):
        initialized = mcp.dispatch("initialize", {"protocolVersion": "2025-06-18"})
        listed = mcp.dispatch("tools/list", {})
        resources = mcp.dispatch("resources/list", {})
        panel = mcp.dispatch("resources/read", {"uri": mcp.UI_URI})

        self.assertEqual(initialized["protocolVersion"], "2025-06-18")
        self.assertIn("tools", initialized["capabilities"])
        self.assertEqual(resources["resources"][0]["uri"], mcp.UI_URI)
        self.assertIn("Synthetic recap", panel["contents"][0]["text"])
        tools = {tool["name"]: tool for tool in listed["tools"]}
        self.assertEqual(len(tools), len(listed["tools"]))
        self.assertTrue(tools["get_recap"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["refresh_recap"]["annotations"]["readOnlyHint"])
        self.assertEqual(tools["get_recap"]["_meta"]["ui"]["resourceUri"], mcp.UI_URI)
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_get_recap_reads_cache_without_refresh_action(self):
        result = mcp.dispatch("tools/call", {"name": "get_recap", "arguments": {}})
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"], {"summaries": [], "paused": False})
        self.request.assert_called_once_with("/api/state")

    def test_refresh_is_explicit_and_asynchronous(self):
        result = mcp.dispatch("tools/call", {"name": "refresh_recap", "arguments": {}})
        self.assertFalse(result.get("isError", False))
        self.request.assert_called_once_with("/api/action", {"action": "refresh"})

    def test_pause_requires_real_boolean_and_invalid_input_never_reaches_http(self):
        for value in ("false", 0, None):
            with self.subTest(value=value):
                result = mcp.dispatch("tools/call", {"name": "set_recap_paused", "arguments": {"paused": value}})
                self.assertTrue(result["isError"])
        self.request.assert_not_called()
        self.ensure_service.assert_not_called()

    def test_no_argument_tools_reject_extra_parameters_without_starting_service(self):
        for name in ("get_recap", "refresh_recap"):
            with self.subTest(name=name):
                result = mcp.dispatch("tools/call", {"name": name, "arguments": {"unexpected": True}})
                self.assertTrue(result.get("isError", False))
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_mark_seen_requires_string_before_service_startup(self):
        for args in ({}, {"summary_id": 1}, {"summary_id": None}, {"summary_id": []}):
            with self.subTest(args=args):
                result = mcp.dispatch("tools/call", {"name": "mark_recap_seen", "arguments": args})
                self.assertTrue(result.get("isError", False))
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_valid_seen_and_pause_arguments_forward_only_the_expected_action(self):
        mcp.dispatch("tools/call", {"name": "mark_recap_seen", "arguments": {"summary_id": "synthetic-id"}})
        self.request.assert_called_once_with("/api/action", {"action": "seen", "summary_id": "synthetic-id"})
        self.request.reset_mock()
        mcp.dispatch("tools/call", {"name": "set_recap_paused", "arguments": {"paused": True}})
        self.request.assert_called_once_with("/api/action", {"action": "pause"})

    def test_unknown_tool_is_rejected_before_service_startup(self):
        result = mcp.dispatch("tools/call", {"name": "not-a-recap-tool", "arguments": {}})
        self.assertTrue(result["isError"])
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_transport_returns_parse_and_method_errors_and_keeps_processing(self):
        replies = self.transport([
            "this is not JSON",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "unknown/method", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}),
        ])
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertIsNone(replies[0]["id"])
        self.assertEqual(replies[1]["error"]["code"], -32601)
        self.assertEqual(replies[1]["id"], 2)
        self.assertEqual(replies[2]["result"], {})
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_transport_rejects_invalid_jsonrpc_envelopes(self):
        malformed = [
            {"id": 1, "method": "ping", "params": {}},
            {"jsonrpc": "1.0", "id": 2, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": {"unexpected": "object"}, "method": "ping", "params": {}},
            42,
            [],
        ]
        for value in malformed:
            with self.subTest(value=value):
                replies = self.transport([json.dumps(value)])
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0].get("error", {}).get("code"), -32600)
                if isinstance(value, dict) and isinstance(value.get("id"), dict):
                    self.assertIsNone(replies[0]["id"])
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()

    def test_notifications_have_no_reply_and_unknown_resource_is_parameter_error(self):
        replies = self.transport([
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://unknown"}}),
        ])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 4)
        self.assertEqual(replies[0]["error"]["code"], -32602)
        self.ensure_service.assert_not_called()
        self.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
