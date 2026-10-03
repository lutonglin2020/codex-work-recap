"""One local service shared by every plugin instance; no Codex database writes."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent.parent


def _windowless_python() -> str:
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return str(windowless if os.name == "nt" and windowless.exists() else executable)


def _process_in_job(handle=None) -> bool:
    """Unknown membership is an error, never evidence of independence."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    inside = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess() if handle is None else handle, None, ctypes.byref(inside)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(inside.value)


def _desktop_process_id() -> int:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetShellWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    process_id = wintypes.DWORD()
    desktop = user.GetShellWindow()
    if not desktop or not user.GetWindowThreadProcessId(desktop, ctypes.byref(process_id)):
        raise RuntimeError("未找到可用于独立启动的 Windows 桌面进程。")
    return process_id.value


def _popen_background(command, *, cwd=ROOT, env=None):
    return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            start_new_session=os.name != "nt")


def _run_explorer_child(encoded: str) -> None:
    """Verify Explorer parentage before starting work outside the caller tree."""
    payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    acknowledgement = Path(payload["acknowledgement"])
    try:
        desktop_pid, parent_pid = _desktop_process_id(), os.getppid()
        if parent_pid != desktop_pid:
            raise RuntimeError("启动进程的父进程不是 Windows 桌面，未启动回顾服务。")
        environment = {**os.environ, **payload["environment"]}
        process = _popen_background(payload["command"], cwd=payload["cwd"], env=environment)
        try:
            child_in_job = _process_in_job(int(process._handle))
        except OSError:
            process.terminate()
            process.wait(timeout=5)
            raise
        # Explorer itself may belong to a system/session Job. That does not
        # couple this child to Codex's Job or its process ancestry.
        result = {"ok": True, "pid": process.pid, "launcher": "explorer_desktop", "in_job": child_in_job,
                  "bootstrap_pid": os.getpid(), "bootstrap_parent_pid": parent_pid,
                  "desktop_pid": desktop_pid, "independent_from_caller": True}
    except Exception as error:
        result = {"ok": False, "error": str(error)[:500]}
    temporary = acknowledgement.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(acknowledgement)


def _start_via_explorer(command, *, cwd=ROOT) -> dict:
    # Getting the desktop's Application routes ShellExecute through Explorer;
    # creating Shell.Application alone does not establish that broker boundary.
    with tempfile.TemporaryDirectory(prefix="work-recap-launch-") as temporary:
        acknowledgement = Path(temporary) / "started.json"
        names = ("WORK_RECAP_DATA", "WORK_RECAP_NO_WIDGET", "CODEX_HOME", "CODEX_WORK_RECAP_BIN")
        payload = {"command": [str(part) for part in command], "cwd": str(cwd),
                   "environment": {name: os.environ[name] for name in names if name in os.environ},
                   "acknowledgement": str(acknowledgement)}
        if "WORK_RECAP_DATA" not in payload["environment"]:
            payload["environment"]["WORK_RECAP_DATA"] = str(data_path())
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        code = "from recap.runtime import _run_explorer_child; _run_explorer_child('" + encoded + "')"
        launch = {"executable": _windowless_python(), "arguments": subprocess.list2cmdline(["-c", code]), "cwd": str(ROOT)}
        launch_json = base64.b64encode(json.dumps(launch, ensure_ascii=False).encode("utf-8")).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop';"
            "$launch=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + launch_json + "')));"
            "$shell=New-Object -ComObject Shell.Application;$desktopHandle=0;"
            "$desktop=$shell.Windows().FindWindowSW(0,0,8,[ref]$desktopHandle,1);"
            "if($null -eq $desktop){throw 'Explorer desktop unavailable'};"
            "$desktop.Document.Application.ShellExecute([string]$launch.executable,[string]$launch.arguments,[string]$launch.cwd,'open',0)"
        )
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
                                    base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if completed.returncode:
            raise RuntimeError("无法通过 Explorer 独立启动回顾；请确认 Windows 桌面会话可用。")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if acknowledgement.exists():
                result = json.loads(acknowledgement.read_text(encoding="utf-8"))
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "后台独立启动检查失败。")
                return result
            time.sleep(.1)
        raise RuntimeError("Explorer 已接到启动请求，但后台进程未完成独立启动检查。")


def _start_background(command, *, cwd=ROOT):
    if os.name == "nt":
        try:
            in_job = _process_in_job()
        except OSError:
            in_job = True
        if in_job:
            return _start_via_explorer(command, cwd=cwd)
    return _popen_background(command, cwd=cwd,
                             env={**os.environ, "WORK_RECAP_DATA": str(data_path())})


def data_path() -> Path:
    base = os.environ.get("WORK_RECAP_DATA")
    # MSIX redirects LocalAppData for Codex-launched processes, while Explorer
    # sees its physical counterpart. A shared user-profile directory keeps
    # plugin, daemon and widget on the same database and IPC mailbox.
    path = Path(base) if base else Path.home() / ".codex" / "work-recap-data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_runtime() -> dict:
    value = json.loads((data_path() / "runtime.json").read_text(encoding="utf-8"))
    if value.get("transport") == "file":
        return value
    if not isinstance(value.get("port"), int) or not 0 < value["port"] < 65536:
        raise ValueError("Invalid service port")
    return value


def request(path: str, payload: dict | None = None, timeout: float = 5) -> dict:
    info = read_runtime()
    if info.get("transport") == "file":
        from .ipc import call
        return call(data_path() / "ipc", info["token"], path, payload, timeout)
    # Migration only: lets the installer stop a previous HTTP-based version.
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(f'http://127.0.0.1:{info["port"]}{path}', data=body,
                  headers={"Authorization": "Bearer " + info["token"], "Content-Type": "application/json"})
    # Loopback must never go through the user's corporate/network proxy.
    with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
        return json.load(response)


def ensure_service() -> dict:
    try:
        request("/api/health", timeout=1)
        return read_runtime()
    except (OSError, ValueError, KeyError):
        pass
    _start_background([_windowless_python(), "-m", "recap", "--daemon"])
    for _ in range(60):
        time.sleep(.15)
        try:
            request("/api/health", timeout=.5)
            return read_runtime()
        except (OSError, ValueError, KeyError):
            pass
    raise RuntimeError("工作回顾服务启动失败；请检查本地运行日志。")


def preview_url() -> str:
    info = ensure_service()
    if info.get("transport") == "file":
        raise RuntimeError("工作回顾已改为自动原生悬浮窗，无需打开网页；可运行 --widget。")
    return f'http://127.0.0.1:{info["port"]}/#token={info["token"]}'


def start_widget():
    return _start_background([_windowless_python(), "-m", "recap", "--widget"])
