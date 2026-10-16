"""Package checks use an isolated home and never invoke the real Codex CLI."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import install


class PackageDocumentationTests(unittest.TestCase):
    def test_installed_package_preserves_license_and_readme(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "source"
            root.mkdir()
            for name in ("recap", "ui", "hooks", "skills"):
                (root / name).mkdir()
            for name in ("plugin.json", "mcp.json", "LICENSE", "README.md"):
                shutil.copy2(source / name, root / name)
            shutil.copy2(source / "hooks/hooks.json", root / "hooks/hooks.json")
            result = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "installedPath": str(home / "plugin-cache"),
            }))
            with patch.object(install, "ROOT", root), \
                 patch.object(install.Path, "home", return_value=home), \
                 patch.object(install.subprocess, "run", return_value=result) as cli, \
                 contextlib.redirect_stdout(io.StringIO()):
                install.install_package("synthetic-codex")

            stage = root / "dist/work-recap"
            for name in ("LICENSE", "README.md"):
                self.assertEqual((stage / name).read_bytes(), (source / name).read_bytes())
            self.assertIn("GNU GENERAL PUBLIC LICENSE", (stage / "LICENSE").read_text(encoding="utf-8"))
            cli.assert_called_once()
            self.assertEqual(cli.call_args.args[0][:3], ["synthetic-codex", "plugin", "add"])


if __name__ == "__main__":
    unittest.main()
