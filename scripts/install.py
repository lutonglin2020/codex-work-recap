"""Stage and install through the official Codex plugin CLI, preserving other plugins."""
from __future__ import annotations
import argparse
import contextlib
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextlib.contextmanager
def stopped_instance_locks(directory, timeout=45):
    """Wait for our daemon/widget locks, and prevent their restart during update.

    Health going away is not proof that their cwd handles have been released.
    Only these two owned app-data files are inspected; no unrelated processes
    are enumerated or terminated. Lock acquisition does not append probe bytes.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handles = []
    busy_name = "service.lock"
    while True:
        for name in ("service.lock", "widget.lock"):
            handle = open(directory / name, "a+b")
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                for held in handles:
                    held.close()
                handles.clear()
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise RuntimeError(f"无法确认本插件退出锁 {name}；已中止更新，未强制结束任何进程。") from error
                busy_name = name
                break
            handles.append(handle)
        else:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"工作回顾进程尚未完全退出（{busy_name} 仍被占用）；已请求停止并等待{timeout:g}秒，未强杀。请稍后重试安装。")
        time.sleep(min(.1, remaining))
    try:
        # A normal interpreter exits immediately after releasing its final lock.
        # This short settle also allows the old native window to finish disposal.
        time.sleep(.25)
        yield
    finally:
        for handle in handles:
            handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from recap.runtime import read_runtime, request, data_path
    (data_path() / "widget.stop").write_text("stop", encoding="utf-8")
    try:
        active = read_runtime()
        request("/api/health", timeout=1)
    except (OSError, ValueError, KeyError, RuntimeError):
        active = None
    if active:
        try:
            request("/api/shutdown", {}, timeout=3)
        except (OSError, ValueError, KeyError, RuntimeError):
            # The old version may already be in its shutdown path. Its owned
            # locks, rather than a now-unreachable endpoint, decide readiness.
            pass
    with stopped_instance_locks(data_path()):
        install_package(args.codex)


def install_package(codex):
    stage = ROOT / "dist/work-recap"
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("recap", "ui", "hooks", "skills"):
        shutil.copytree(ROOT / name, stage / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "plugin.json", stage / "plugin.json")
    config = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
    config["mcpServers"]["work-recap"]["command"] = Path(sys.executable).name
    config["mcpServers"]["work-recap"]["env"] = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    }
    write_json(stage / "mcp.json", config)
    # The source package stays portable; the local build selects the verified interpreter.
    hooks = json.loads((stage / "hooks/hooks.json").read_text(encoding="utf-8"))
    for matchers in hooks["hooks"].values():
        for matcher in matchers:
            for hook in matcher["hooks"]:
                hook["commandWindows"] = 'call "' + sys.executable.replace("\\", "/") + '" "%PLUGIN_ROOT%/hooks/record_event.py"'
    write_json(stage / "hooks/hooks.json", hooks)
    home = Path.home()
    marketplace_file = home / ".agents/plugins/marketplace.json"
    if marketplace_file.exists():
        marketplace = json.loads(marketplace_file.read_text(encoding="utf-8-sig"))
    else:
        marketplace = {"name": "lutong-local", "interface": {"displayName": "我的本地插件"}, "plugins": []}
    entry = {"name": "work-recap", "source": {"source": "local", "path": "./" + stage.relative_to(home).as_posix()},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}
    existing = [p for p in marketplace["plugins"] if p.get("name") != "work-recap"]
    marketplace["plugins"] = existing + [entry]
    write_json(marketplace_file, marketplace)
    command = [codex, "plugin", "add", "work-recap@" + marketplace["name"], "--json"]
    completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    print(completed.stdout)
    if completed.returncode:
        print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)
    installed = json.loads(completed.stdout)
    write_json(ROOT / "dist/install-result.json", {"marketplace": marketplace["name"],
               "plugin": "work-recap", "interpreter": sys.executable,
               "cache": installed["installedPath"],
               "cli_result": installed})


if __name__ == "__main__":
    main()
