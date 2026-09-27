"""Exercise a separately installed skill, without a checkout or MCP server."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from install_skill import install


class DirectSkillTests(unittest.TestCase):
    def test_installed_script_and_error_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "standalone-skill"
            install(target)
            script = target / "scripts" / "factiva_api.py"
            env = {key: value for key, value in os.environ.items() if not key.startswith("FACTIVA_")}
            for action, request, code in (
                ("prepare", {"query": "Test question with $() and `literal text`", "limit": 2}, 0),
                ("search", {"query": "test"}, 1),
                ("prepare", {"query": "test", "limit": 999}, 2),
            ):
                with self.subTest(action=action, code=code):
                    result = subprocess.run([sys.executable, "-I", str(script), action],
                        input=json.dumps(request), text=True, capture_output=True,
                        cwd=directory, env=env, timeout=20)
                    self.assertEqual(result.returncode, code, result.stderr)
                    if code == 0:
                        data = json.loads(result.stdout)
                        self.assertEqual(data["mode"], "preview_only")
                        self.assertEqual(data["payload"]["data"]["attributes"]["query"]["value"], request["query"])
                        self.assertEqual(result.stderr, "")
                    else:
                        self.assertEqual(result.stdout, "")
                        self.assertIn("error", json.loads(result.stderr))
                        self.assertNotIn("Traceback", result.stderr)

    def test_install_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.mkdir()
            marker = target / "SKILL.md"
            marker.write_text("keep this user's skill")
            with self.assertRaises(FileExistsError):
                install(target)
            self.assertEqual(marker.read_text(), "keep this user's skill")
            self.assertFalse((target / "scripts").exists())


if __name__ == "__main__":
    unittest.main()
