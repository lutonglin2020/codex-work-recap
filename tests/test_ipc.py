"""Temporary-file IPC tests: no listener, service, or model is started."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from recap import ipc


class FakeDispatch:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, path, payload):
        with self.lock:
            self.calls.append((path, payload))
        if not isinstance(payload, dict) or payload.get("malformed"):
            raise RuntimeError("synthetic operation failure must not break mailbox")
        time.sleep(0.002)
        return {"path": path, "reply_id": payload.get("id"), "body": payload.get("body")}


class FileIpcTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.token = "test-only-mailbox-token"
        self.dispatch = FakeDispatch()
        self.server = ipc.FileServer(self.directory, self.token, self.dispatch)

    def tearDown(self):
        self.server.close()
        self.temporary.cleanup()

    def write_request(self, value, size=None):
        identifier = uuid.uuid4().hex
        request = self.directory / f"request-{identifier}.json"
        response = self.directory / f"response-{identifier}.json"
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if size is not None:
            self.assertLessEqual(len(content), size)
            content += b" " * (size - len(content))
        request.write_bytes(content)
        return request, response

    def valid_request(self, payload=None):
        return {"token": self.token, "path": "/echo", "payload": payload or {"id": "raw"},
                "expires_at": time.time() + 5}

    def test_concurrent_uuid_responses_do_not_cross_callers(self):
        self.server.start()
        actual_atomic = ipc.atomic_json
        observed, lock = [], threading.Lock()

        def observe(path, value):
            if path.name.startswith("request-"):
                with lock:
                    observed.append(path.name)
            return actual_atomic(path, value)

        def invoke(number):
            return ipc.call(self.directory, self.token, f"/echo/{number}",
                            {"id": number, "body": f"independent-{number}"}, timeout=3)

        with patch("recap.ipc.atomic_json", observe), concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(invoke, range(24)))
        self.assertEqual(len(observed), 24)
        self.assertEqual(len(set(observed)), 24)
        self.assertTrue(all(ipc.NAME.fullmatch(name) for name in observed))
        for number, result in enumerate(results):
            self.assertEqual(result, {"path": f"/echo/{number}", "reply_id": number, "body": f"independent-{number}"})
        self.assertEqual(len(self.dispatch.calls), 24)
        self.assertFalse(list(self.directory.glob("request-*")))
        self.assertFalse(list(self.directory.glob("response-*")))

    def test_unknown_token_never_dispatches_and_returns_at_client_deadline(self):
        self.server.start()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            ipc.call(self.directory, "wrong-test-token", "/echo", {"id": "forbidden"}, timeout=0.18)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.dispatch.calls, [])
        self.assertFalse(list(self.directory.glob("request-*")))
        self.assertFalse(list(self.directory.glob("response-*")))
        self.assertEqual(ipc.call(self.directory, self.token, "/echo", {"id": "allowed"}, timeout=2)["reply_id"], "allowed")

    def test_expired_authenticated_request_is_never_executed(self):
        value = self.valid_request({"id": "expired"})
        value["expires_at"] = time.time() - 1
        request, response = self.write_request(value)
        self.server._handle(request)
        self.assertFalse(request.exists())
        self.assertFalse(response.exists())
        self.assertEqual(self.dispatch.calls, [])
        self.server.start()
        self.assertEqual(ipc.call(self.directory, self.token, "/echo", {"id": "fresh"}, timeout=2)["reply_id"], "fresh")

    def test_server_request_limit_accepts_exact_64kb_rejects_one_extra_byte(self):
        request, response = self.write_request(self.valid_request({"id": "boundary"}), size=ipc.MAX_REQUEST)
        self.assertEqual(request.stat().st_size, 65536)
        self.server._handle(request)
        self.assertTrue(json.loads(response.read_text(encoding="utf-8"))["ok"])
        self.assertEqual(len(self.dispatch.calls), 1)
        response.unlink()
        oversized, oversized_response = self.write_request(self.valid_request({"id": "too-large"}), size=ipc.MAX_REQUEST + 1)
        self.server._handle(oversized)
        self.assertFalse(oversized.exists())
        self.assertFalse(oversized_response.exists())
        self.assertEqual(len(self.dispatch.calls), 1)

    def test_call_limits_utf8_bytes_before_writing_request(self):
        self.server.start()
        for body in ("x" * 65536, "汉" * 22000):
            with self.subTest(unicode=body.startswith("汉")):
                with self.assertRaises(ValueError):
                    ipc.call(self.directory, self.token, "/echo", {"id": "oversized", "body": body}, timeout=1)
                self.assertFalse(list(self.directory.glob("request-*")))
                self.assertFalse(list(self.directory.glob("response-*")))
        self.assertEqual(self.dispatch.calls, [])

    def test_no_server_timeout_cleans_only_its_own_request_and_response(self):
        unrelated = self.directory / f"request-{'f' * 32}.json"
        unrelated.write_text("unrelated caller", encoding="utf-8")
        actual_atomic = ipc.atomic_json
        created = []

        def observe(path, value):
            created.append(path)
            return actual_atomic(path, value)

        with patch("recap.ipc.atomic_json", observe):
            with self.assertRaises(TimeoutError):
                ipc.call(self.directory, self.token, "/echo", {"id": "no-server"}, timeout=0.12)
        self.assertEqual(len(created), 1)
        own_request = created[0]
        self.assertFalse(own_request.exists())
        self.assertFalse(own_request.with_name(own_request.name.replace("request-", "response-", 1)).exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(list(self.directory.iterdir()), [unrelated])

    def test_malformed_payload_dispatch_failure_does_not_break_next_request(self):
        self.server.start()
        with self.assertRaises(RuntimeError) as failed:
            ipc.call(self.directory, self.token, "/echo", {"malformed": True}, timeout=2)
        self.assertNotIn("synthetic", str(failed.exception))
        result = ipc.call(self.directory, self.token, "/echo", {"id": "after-failure"}, timeout=2)
        self.assertEqual(result["reply_id"], "after-failure")
        self.assertTrue(self.server.thread.is_alive())

    def test_malformed_json_does_not_dispatch_and_server_stays_usable(self):
        identifier = uuid.uuid4().hex
        request = self.directory / f"request-{identifier}.json"
        response = self.directory / f"response-{identifier}.json"
        request.write_text("{not JSON", encoding="utf-8")
        self.server._handle(request)
        self.assertEqual(self.dispatch.calls, [])
        self.assertFalse(request.exists())
        self.assertFalse(json.loads(response.read_text(encoding="utf-8"))["ok"])
        response.unlink()
        self.server.start()
        self.assertEqual(ipc.call(self.directory, self.token, "/echo", {"id": "healthy"}, timeout=2)["reply_id"], "healthy")


if __name__ == "__main__":
    unittest.main()
