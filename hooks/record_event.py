"""Best-effort event capture. Never blocks a turn or copies prompt/transcript text."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recap.runtime import request

try:
    incoming = json.loads(sys.stdin.read(65536))
    event = {key: incoming[key] for key in ("session_id", "turn_id", "hook_event_name", "source") if key in incoming}
    event["timestamp"] = time.time()
    # No service/model startup in a hook: polling provides catch-up after downtime.
    request("/api/action", {"action": "event", "payload": event}, timeout=.5)
except Exception:
    pass
print("{}")
