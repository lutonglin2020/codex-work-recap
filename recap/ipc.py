"""Atomic JSON mailbox in the current user's private app data directory."""
from __future__ import annotations
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

MAX_REQUEST = 65536
MAX_RESPONSE = 2 * 1024 * 1024
NAME = re.compile(r"(?:request|response)-[0-9a-f]{32}\.(?:json|tmp)$")


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def call(directory, token, path, payload=None, timeout=5):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    request = directory / f"request-{identifier}.json"
    response = directory / f"response-{identifier}.json"
    started = time.monotonic()
    value = {"token": token, "path": path, "payload": payload, "expires_at": time.time() + timeout}
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST:
        raise ValueError("本地请求过大")
    atomic_json(request, value)
    try:
        while time.monotonic() - started < timeout:
            if response.is_file():
                if response.stat().st_size > MAX_RESPONSE:
                    raise ValueError("本地响应过大")
                value = json.loads(response.read_text(encoding="utf-8"))
                if not value.get("ok"):
                    raise RuntimeError(value.get("error", "本地操作失败"))
                return value["result"]
            time.sleep(.04)
        raise TimeoutError("本地回顾服务暂时没有响应")
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


class FileServer:
    def __init__(self, directory, token, dispatch):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.token, self.dispatch = token, dispatch
        self.closed = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._serve, daemon=True, name="recap-local-messages")
        self.thread.start()

    def _serve(self):
        cleaned_at = 0
        while not self.closed.is_set():
            requests = list(self.directory.glob("request-*.json"))[:128]
            for request in requests:
                if self.closed.is_set():
                    break
                self._handle(request)
            now = time.time()
            if now - cleaned_at > 60:
                for item in self.directory.iterdir():
                    try:
                        if NAME.fullmatch(item.name) and item.is_file() and now - item.stat().st_mtime > 60:
                            item.unlink(missing_ok=True)
                    except OSError:
                        pass
                cleaned_at = now
            self.closed.wait(.1)

    def _handle(self, request):
        if not NAME.fullmatch(request.name) or request.is_symlink():
            return
        response = request.with_name(request.name.replace("request-", "response-", 1))
        try:
            if request.stat().st_size > MAX_REQUEST:
                return
            value = json.loads(request.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("token"), str):
                return
            if not secrets.compare_digest(value["token"].encode("utf-8"), self.token.encode("utf-8")):
                return
            expiry = value.get("expires_at")
            if not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or not time.time() < expiry <= time.time() + 30:
                return
            if not isinstance(value.get("path"), str):
                return
            result = self.dispatch(value["path"], value.get("payload"))
            packet = {"ok": True, "result": result}
            if len(json.dumps(packet, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE:
                raise ValueError("本地响应过大")
            atomic_json(response, packet)
        except Exception:
            try:
                atomic_json(response, {"ok": False, "error": "本地操作暂时未完成"})
            except OSError:
                pass
        finally:
            try:
                request.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self):
        self.closed.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
