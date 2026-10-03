import base64
import json
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from recap import runtime


class BackgroundLaunchTests(unittest.TestCase):
    def test_default_data_directory_is_shared_outside_redirected_appdata(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(runtime.Path, "home", return_value=Path(directory)), \
             patch.dict(runtime.os.environ, {}, clear=True):
            runtime.os.environ["LOCALAPPDATA"] = str(Path(directory) / "packaged")
            packaged = runtime.data_path()
            runtime.os.environ["LOCALAPPDATA"] = str(Path(directory) / "desktop")
            desktop = runtime.data_path()
            self.assertEqual(packaged, desktop)
            self.assertEqual(desktop, Path(directory) / ".codex/work-recap-data")
            runtime.os.environ["WORK_RECAP_DATA"] = str(Path(directory) / "override")
            self.assertEqual(runtime.data_path(), Path(directory) / "override")

    def test_job_member_uses_explorer_instead_of_inheriting_caller_job(self):
        with patch.object(runtime, "_process_in_job", return_value=True), \
             patch.object(runtime, "_start_via_explorer", return_value={"ok": True}) as broker, \
             patch.object(runtime, "_popen_background") as direct:
            self.assertEqual(runtime._start_background(["pythonw", "-m", "recap", "--daemon"]), {"ok": True})
            broker.assert_called_once()
            direct.assert_not_called()

    def test_unknown_job_membership_does_not_claim_independence(self):
        with patch.object(runtime, "_process_in_job", side_effect=OSError("query unavailable")), \
             patch.object(runtime, "_start_via_explorer") as broker, \
             patch.object(runtime, "_popen_background") as direct:
            runtime._start_background(["pythonw", "-m", "recap", "--widget"])
            broker.assert_called_once()
            direct.assert_not_called()

    def test_process_outside_job_can_start_a_hidden_child_directly(self):
        with patch.object(runtime, "_process_in_job", return_value=False), \
             patch.object(runtime, "_start_via_explorer") as broker, \
             patch.object(runtime, "_popen_background", return_value="process") as direct:
            self.assertEqual(runtime._start_background(["pythonw", "-m", "recap", "--widget"]), "process")
            direct.assert_called_once()
            broker.assert_not_called()

    def test_explorer_failure_never_falls_back_to_a_caller_job_child(self):
        with patch.object(runtime, "_process_in_job", return_value=True), \
             patch.object(runtime, "_start_via_explorer", side_effect=RuntimeError("desktop unavailable")), \
             patch.object(runtime, "_popen_background") as direct:
            with self.assertRaisesRegex(RuntimeError, "desktop unavailable"):
                runtime._start_background(["pythonw", "-m", "recap", "--daemon"])
            direct.assert_not_called()

    def _payload(self, directory):
        value = {"command": ["pythonw", "-m", "recap", "--daemon"], "cwd": directory,
                 "environment": {"WORK_RECAP_DATA": directory},
                 "acknowledgement": str(Path(directory) / "started.json")}
        return value, base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")

    def test_desktop_parent_is_required_before_starting_business_process(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, encoded = self._payload(directory)
            with patch.object(runtime, "_desktop_process_id", return_value=123), \
                 patch.object(runtime.os, "getppid", return_value=999), \
                 patch.object(runtime, "_popen_background") as launch:
                runtime._run_explorer_child(encoded)
                launch.assert_not_called()
            self.assertFalse(json.loads(Path(payload["acknowledgement"]).read_text(encoding="utf-8"))["ok"])

    def test_explorer_system_job_is_recorded_without_rejecting_valid_parentage(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, encoded = self._payload(directory)
            child = Mock(pid=456, _handle=789)
            with patch.object(runtime, "_desktop_process_id", return_value=123), \
                 patch.object(runtime.os, "getppid", return_value=123), \
                 patch.object(runtime, "_process_in_job", return_value=True) as membership, \
                 patch.object(runtime, "_popen_background", return_value=child) as launch:
                runtime._run_explorer_child(encoded)
                membership.assert_called_once_with(789)
                child.terminate.assert_not_called()
                self.assertEqual(launch.call_args.kwargs["env"]["WORK_RECAP_DATA"], directory)
            result = json.loads(Path(payload["acknowledgement"]).read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertTrue(result["in_job"])
            self.assertTrue(result["independent_from_caller"])
            self.assertEqual(result["bootstrap_parent_pid"], result["desktop_pid"])

    def test_broker_uses_desktop_object_preserves_needed_config_and_cleans_handshake(self):
        captured = {}

        def shell(command, **kwargs):
            script = base64.b64decode(command[-1]).decode("utf-16-le")
            self.assertIn("FindWindowSW(0,0,8", script)
            self.assertIn("$desktop.Document.Application.ShellExecute", script)
            self.assertIn("Hidden", command)
            launch_blob = re.search(r"FromBase64String\('([^']+)'\)", script).group(1)
            launch = json.loads(base64.b64decode(launch_blob))
            payload_blob = re.search(r"_run_explorer_child\('([^']+)'\)", launch["arguments"]).group(1)
            payload = json.loads(base64.b64decode(payload_blob))
            captured.update(payload)
            Path(payload["acknowledgement"]).write_text(json.dumps({"ok": True, "pid": 456}), encoding="utf-8")
            return SimpleNamespace(returncode=0)

        command = ["C:/Test Files/pythonw.exe", "-m", "recap", "--daemon"]
        with patch.object(runtime.subprocess, "run", side_effect=shell), \
             patch.object(runtime, "_windowless_python", return_value="C:/Test Files/pythonw.exe"), \
             patch.dict(runtime.os.environ, {"WORK_RECAP_DATA": "C:/Test Folder/回顾", "OPENAI_API_KEY": "test-only-not-forwarded"}):
            result = runtime._start_via_explorer(command)
        self.assertEqual(result["pid"], 456)
        self.assertEqual(captured["command"], command)
        self.assertEqual(captured["environment"]["WORK_RECAP_DATA"], "C:/Test Folder/回顾")
        self.assertNotIn("OPENAI_API_KEY", captured["environment"])
        self.assertFalse(Path(captured["acknowledgement"]).parent.exists())

    def test_unacknowledged_broker_launch_times_out(self):
        with patch.object(runtime.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(runtime.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(RuntimeError, "未完成独立启动检查"):
                runtime._start_via_explorer(["pythonw", "-m", "recap", "--daemon"])

    def test_healthy_existing_service_is_reused_without_a_new_process(self):
        with patch.object(runtime, "request", return_value={"ok": True}), \
             patch.object(runtime, "read_runtime", return_value={"pid": 123}), \
             patch.object(runtime, "_start_background") as start:
            self.assertEqual(runtime.ensure_service(), {"pid": 123})
            start.assert_not_called()

    def test_service_and_widget_keep_existing_modes_through_shared_background_launcher(self):
        with patch.object(runtime, "request", side_effect=[OSError("not running"), {"ok": True}]), \
             patch.object(runtime, "read_runtime", return_value={"pid": 123}), \
             patch.object(runtime, "_windowless_python", return_value="pythonw"), \
             patch.object(runtime.time, "sleep"), \
             patch.object(runtime, "_start_background") as start:
            runtime.ensure_service()
            self.assertEqual(start.call_args.args[0], ["pythonw", "-m", "recap", "--daemon"])
            runtime.start_widget()
            self.assertEqual(start.call_args.args[0], ["pythonw", "-m", "recap", "--widget"])


if __name__ == "__main__":
    unittest.main()
