import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class AppPortabilityTests(unittest.TestCase):
    def test_environment_variable_executable_is_preferred(self):
        with tempfile.TemporaryDirectory(prefix="程序路径_") as temp_dir:
            executable = Path(temp_dir) / "energyplus.exe"
            executable.write_bytes(b"")
            with patch.dict(os.environ, {"TEST_EPLUS_EXE": str(executable)}):
                found = app._discover_executable(
                    "TEST_EPLUS_EXE",
                    ("missing-energyplus-command",),
                    (),
                )
            self.assertEqual(found, str(executable))

    def test_missing_executable_returns_empty_string(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            app.shutil, "which", return_value=None
        ):
            found = app._discover_executable(
                "TEST_MISSING_EXE",
                ("missing-command",),
                ("Z:/not-installed/tool.exe",),
            )
        self.assertEqual(found, "")


if __name__ == "__main__":
    unittest.main()
