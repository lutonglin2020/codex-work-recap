"""Local recap coordinator and presentation snapshots, independent of chat focus."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import re
import threading
import time

from .runtime import ROOT, data_path


class InstanceLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        self.file.close()


class Coordinator:
    def __init__(self, engine):
        self.engine = engine
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recap")
        self.guard = threading.Lock()
        self.future = None
        self.stop = threading.Event()

    def refresh(self, force=False, period="auto"):
        with self.guard:
            if self.future is None or self.future.done():
                self.future = self.worker.submit(self._run, force, period)
        return self.engine.snapshot()

    def _run(self, force, period):
        try:
            if not installed_plugin_enabled():
                return self.engine.set_paused(True)
            idle = windows_idle_seconds()
            if idle is not None:
                self.engine.record_event({"type": "idle_hint", "user_idle_seconds": idle})
            return self.engine.tick(force=force, period=period)
        except Exception:
            logging.exception("recap tick failed")

    def schedule(self):
        # Collection is cheap and local; Engine gates every model invocation.
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(60)

    def action(self, value):
        action = value.get("action")
        if action == "refresh":
            period = value.get("period", "auto")
            if period not in ("auto", "today", "yesterday"):
                raise ValueError("不支持的回顾时间范围")
            return self.refresh(force=True, period=period)
        if action == "seen":
            summary_id = value.get("summary_id")
            if not isinstance(summary_id, str) or len(summary_id) > 200:
                raise ValueError("缺少回顾标识")
            return self.engine.mark_seen(summary_id)
        if action in ("pause", "resume"):
            return self.engine.set_paused(action == "pause")
        if action == "event":
            payload = value.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("无效事件")
            return self.engine.record_event(payload)
        raise ValueError("不支持的操作")


def installed_plugin_enabled():
    # Respect the official, documented plugin switch even though the service
    # is shared and remains available for reading its cached recaps.
    from pathlib import Path
    import tomllib
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        relative = ROOT.relative_to(codex_home / "plugins/cache")
    except ValueError:
        return True  # Development checkout.
    if not (ROOT / "plugin.json").exists():
        return False
    try:
        settings = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8-sig"))
        key = "work-recap@" + relative.parts[0]
        return settings.get("plugins", {}).get(key, {}).get("enabled", True) is not False
    except (OSError, ValueError, IndexError):
        return False


def windows_idle_seconds():
    """Only elapsed time since OS input; never records keys or app/window content."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        value = LastInput()
        value.cbSize = ctypes.sizeof(value)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(value)):
            return None
        elapsed = (ctypes.windll.kernel32.GetTickCount() - value.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0
    except (OSError, AttributeError):
        return None


def handler_class(coordinator, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log chat data, tokens, or URL fragments.

        def send(self, status, data, kind="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") == expected_host
                    and (origin is None or origin == "http://" + expected_host)
                    and secrets.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + token).encode("utf-8")))

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self.send(200, (ROOT / "ui/index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/favicon.ico":
                return self.send(204, b"")
            if not self.authorized():
                return self.send(403, {"error": "无效的本地访问凭据，请从插件重新打开。"})
            if self.path == "/api/health":
                return self.send(200, {"ok": True, "version": "0.1.0"})
            if self.path == "/api/state":
                return self.send(200, coordinator.engine.snapshot())
            return self.send(404, {"error": "Not found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                # Drain small request bodies before replying, including denials.
                # Otherwise Windows can reset the connection before 403 is read.
                if not 0 < length <= 65536:
                    raise ValueError("请求长度超限")
                self.connection.settimeout(3)
                body = self.rfile.read(length)
                if not self.authorized():
                    return self.send(403, {"error": "Forbidden"})
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("无效请求")
                if self.path == "/api/shutdown":
                    coordinator.stop.set()
                    self.send(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if self.path != "/api/action":
                    return self.send(404, {"error": "Not found"})
                self.send(200, coordinator.action(value))
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, {"error": str(error)})
            except TimeoutError:
                self.send(408, {"error": "请求未完整发送"})
            except Exception:
                logging.exception("API action failed")
                self.send(500, {"error": "操作失败，已保留现有回顾。"})
    return Handler


def run():
    path = data_path()
    try:
        lock = InstanceLock(path / "service.lock")
    except OSError:
        return  # Another installed/source instance already owns the service.
    log = RotatingFileHandler(path / "service.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(handlers=[log], level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .engine import Engine
    from .ipc import FileServer, atomic_json
    engine = Engine(path)
    coordinator = Coordinator(engine)
    token = secrets.token_urlsafe(32)

    def dispatch(endpoint, payload):
        if endpoint == "/api/health":
            return {"ok": True, "version": "0.3.13", "transport": "file"}
        if endpoint == "/api/state":
            return engine.snapshot()
        if endpoint == "/api/action" and isinstance(payload, dict):
            return coordinator.action(payload)
        if endpoint == "/api/shutdown":
            coordinator.stop.set()
            return {"ok": True}
        raise ValueError("无效本地操作")

    server = FileServer(path / "ipc", token, dispatch)
    runtime = {"pid": os.getpid(), "transport": "file", "token": token,
               "root": str(ROOT), "started_at": time.time(), "version": "0.3.13"}
    atomic_json(path / "runtime.json", runtime)
    server.start()
    threading.Thread(target=coordinator.schedule, daemon=True, name="recap-scheduler").start()
    last_display = None
    last_overview = None
    from .runtime import start_widget
    if os.environ.get("WORK_RECAP_NO_WIDGET") != "1" and not (path / "widget.disabled").exists():
        start_widget()
    try:
        while not coordinator.stop.is_set():
            state = engine.snapshot()
            summary = state.get("summary")
            display = {"state": state["state"], "paused": state["paused"],
                       "message": state["message"], "summary": None}
            if summary:
                display["summary"] = {key: summary.get(key) for key in ("id", "overview", "generated_at", "period_label", "display_after_idle_seconds")}
            if display != last_display:
                atomic_json(path / "display.json", display)
                last_display = display
            # A native Codex file tab can display this without any external window.
            overview = (summary.get("overview", "").strip() if summary else "") or "新的工作进展会自动整理在这里。"
            if overview != last_overview:
                draft = path / "recap-preview.tmp"
                draft.write_text(overview + "\n", encoding="utf-8")
                draft.replace(path / "工作回顾.md")
                last_overview = overview
            coordinator.stop.wait(.5)
    finally:
        coordinator.stop.set()
        (path / "widget.stop").write_text("stop", encoding="utf-8")
        server.close()
        engine.close()
        coordinator.worker.shutdown(wait=True, cancel_futures=True)
        lock.close()


def previous_endpoint(path):
    """Keep an already-open local panel connected across ordinary restarts."""
    try:
        previous = json.loads((path / "runtime.json").read_text(encoding="utf-8"))
        port, token = previous.get("port"), previous.get("token")
        if (isinstance(port, int) and not isinstance(port, bool) and 1024 <= port < 65536
                and isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
            return token, port
    except (OSError, ValueError, TypeError):
        pass
    return secrets.token_urlsafe(32), 0
