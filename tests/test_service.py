"""Service integration tests with an isolated engine and a real loopback socket."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from recap import service


class FakeEngine:
    def __init__(self, block=False):
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.ticks = []
        self.actions = []

    def snapshot(self):
        with self.guard:
            return {"summaries": [], "run_count": len(self.ticks)}

    def tick(self, force=False, period="auto"):
        with self.guard:
            self.ticks.append((force, period))
        self.started.set()
        if self.block and not self.release.wait(3):
            raise AssertionError("test did not release the fake worker")
        return self.snapshot()

    def mark_seen(self, summary_id):
        self.actions.append(("seen", summary_id))
        return self.snapshot()

    def set_paused(self, paused):
        self.actions.append(("paused", paused))
        return self.snapshot()

    def record_event(self, payload):
        self.actions.append(("event", payload))
        return self.snapshot()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine(block=True)
        self.coordinator = service.Coordinator(self.engine)

    def tearDown(self):
        self.engine.release.set()
        self.coordinator.stop.set()
        self.coordinator.worker.shutdown(wait=True, cancel_futures=True)

    def test_concurrent_refresh_does_not_queue_duplicate_generation(self):
        barrier = threading.Barrier(12)

        def refresh():
            barrier.wait(timeout=2)
            return self.coordinator.refresh(force=True, period="today")

        with ThreadPoolExecutor(max_workers=12) as callers:
            futures = [callers.submit(refresh) for _ in range(12)]
            snapshots = [future.result(timeout=3) for future in futures]

        self.assertTrue(self.engine.started.wait(1))
        self.assertEqual(self.engine.ticks, [(True, "today")])
        self.assertTrue(all("summaries" in snapshot for snapshot in snapshots))
        self.assertFalse(self.coordinator.future.done())

        self.engine.release.set()
        self.coordinator.future.result(timeout=2)
        self.assertEqual(len(self.engine.ticks), 1)

    def test_new_refresh_can_run_after_prior_generation_finishes(self):
        self.coordinator.refresh(force=True, period="yesterday")
        self.assertTrue(self.engine.started.wait(1))
        self.engine.release.set()
        self.coordinator.future.result(timeout=2)

        self.coordinator.refresh(force=True, period="today")
        self.coordinator.future.result(timeout=2)
        self.assertEqual(self.engine.ticks, [(True, "yesterday"), (True, "today")])

    def test_invalid_period_never_reaches_worker(self):
        with self.assertRaises(ValueError):
            self.coordinator.action({"action": "refresh", "period": "all-history"})
        self.assertIsNone(self.coordinator.future)
        self.assertEqual(self.engine.ticks, [])


class HttpServiceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="recap-http-test-")
        root = Path(self.workspace.name)
        (root / "ui").mkdir()
        (root / "ui/index.html").write_text("<!doctype html><title>Test recap</title>", encoding="utf-8")
        self.root_patch = patch.object(service, "ROOT", root)
        self.root_patch.start()
        self.engine = FakeEngine()
        self.coordinator = service.Coordinator(self.engine)
        self.token = "test-only-loopback-token"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), service.handler_class(self.coordinator, self.token)
        )
        self.handler_errors = []
        self.server.handle_error = lambda *_args: self.handler_errors.append(True)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.coordinator.stop.set()
        self.coordinator.worker.shutdown(wait=True, cancel_futures=True)
        self.root_patch.stop()
        self.workspace.cleanup()

    def request(self, method="GET", path="/api/state", body=None, headers=None, authenticated=True):
        request_headers = {
            "Host": "127.0.0.1:" + str(self.server.server_port),
            "Content-Type": "application/json",
            "Connection": "close",
        }
        if authenticated:
            request_headers["Authorization"] = "Bearer " + self.token
        request_headers.update(headers or {})
        if body is not None and "Content-Length" not in request_headers:
            request_headers["Content-Length"] = str(len(body))
        header = method + " " + path + " HTTP/1.1\r\n"
        header += "".join(key + ": " + value + "\r\n" for key, value in request_headers.items())
        # Send a complete small request together. Split header/body writes can
        # race an early denial and Windows may reset an unread incoming body.
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        try:
            connection.sendall((header + "\r\n").encode("iso-8859-1") + (body or b""))
            response = http.client.HTTPResponse(connection)
            response.begin()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def post(self, payload, **kwargs):
        return self.request("POST", "/api/action", json.dumps(payload).encode("utf-8"), **kwargs)

    def test_server_socket_is_bound_only_to_ipv4_loopback(self):
        self.assertEqual(self.server.socket.getsockname()[0], "127.0.0.1")
        status, headers, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["run_count"], 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.engine.ticks, [])

    def test_missing_or_wrong_token_cannot_read_state_or_change_actions(self):
        for headers, authenticated in (({}, False), ({"Authorization": "Bearer wrong"}, True)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers, authenticated=authenticated)[0], 403)
                self.assertEqual(self.post({"action": "pause"}, headers=headers, authenticated=authenticated)[0], 403)
        self.assertEqual(self.engine.actions, [])
        self.assertEqual(self.engine.ticks, [])

    def test_external_origin_is_rejected_even_with_valid_token(self):
        for origin in ("https://external.example", "null", "http://localhost:9999"):
            with self.subTest(origin=origin):
                headers = {"Origin": origin}
                self.assertEqual(self.request(headers=headers)[0], 403)
                self.assertEqual(self.post({"action": "pause"}, headers=headers)[0], 403)
        self.assertEqual(self.engine.actions, [])

    def test_rebound_host_is_rejected_even_with_valid_token(self):
        self.assertEqual(self.request(headers={"Host": "external.example"})[0], 403)
        self.assertEqual(self.engine.ticks, [])

    def test_same_origin_can_mark_seen_without_starting_generation(self):
        origin = "http://127.0.0.1:" + str(self.server.server_port)
        status, _headers, _body = self.post(
            {"action": "seen", "summary_id": "synthetic-summary-1"}, headers={"Origin": origin}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.actions, [("seen", "synthetic-summary-1")])
        self.assertEqual(self.engine.ticks, [])

    def test_invalid_actions_and_payloads_return_400_without_engine_changes(self):
        payloads = [
            {"action": "delete-all"},
            {"action": "refresh", "period": "forever"},
            {"action": "seen"},
            {"action": "event", "payload": []},
            [],
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload)[0], 400)
        self.assertEqual(self.engine.actions, [])
        self.assertEqual(self.engine.ticks, [])

    def test_malformed_and_oversized_requests_return_400(self):
        self.assertEqual(self.request("POST", "/api/action", body=b"not-json")[0], 400)
        self.assertEqual(
            self.request("POST", "/api/action", body=b"{}", headers={"Content-Length": "65537"})[0],
            400,
        )
        self.assertEqual(self.engine.actions, [])

    def test_non_ascii_authorization_is_denied_without_crashing_handler(self):
        headers = {"Authorization": "Bearer caf\u00e9"}
        self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.post({"action": "pause"}, headers=headers)[0], 403)
        self.assertEqual(self.handler_errors, [])
        self.assertEqual(self.engine.actions, [])


if __name__ == "__main__":
    unittest.main()
