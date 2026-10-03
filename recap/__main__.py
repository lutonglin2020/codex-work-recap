import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="Codex 工作回顾助手")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--mcp", action="store_true")
    mode.add_argument("--widget", action="store_true")
    mode.add_argument("--show", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--url", action="store_true")
    mode.add_argument("--refresh", action="store_true")
    mode.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.daemon:
        from .service import run
        run()
    elif args.mcp:
        from .mcp import run
        run()
    elif args.widget:
        from .widget import run
        run()
    elif args.show:
        import time
        from .runtime import ensure_service, start_widget, data_path
        from .ipc import atomic_json
        ensure_service()
        atomic_json(data_path() / "island-reveal.json", {"requested_at": time.time()})
        start_widget()
    else:
        from .runtime import request, ensure_service, preview_url
        if args.url:
            print(preview_url())
            return
        if not args.stop:
            ensure_service()
        value = request("/api/shutdown", {}) if args.stop else request("/api/action", {"action": "refresh"}) if args.refresh else request("/api/state")
        print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
