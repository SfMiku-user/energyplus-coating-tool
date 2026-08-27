import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.idf_versioning import (
    IDFVersion,
    IDFVersionError,
    find_transition_program,
    prepare_idf_for_energyplus,
    read_energyplus_version,
    read_idf_version,
)


class IDFVersioningTests(unittest.TestCase):
    def test_reads_idf_version_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "模型.idf"
            model.write_text(
                "! Version, 9.6;\nVersion,\n  25.2; ! current version\n",
                encoding="utf-8",
            )
            self.assertEqual(read_idf_version(model), IDFVersion(25, 2, 0))

    def test_reads_energyplus_version(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "energyplus.exe"
            executable.touch()
            completed = subprocess.CompletedProcess(
                [str(executable), "--version"],
                0,
                stdout="EnergyPlus, Version 26.1.0-6f2e40d102\n",
                stderr="",
            )
            with patch(
                "model_builder.idf_versioning.subprocess.run",
                return_value=completed,
            ):
                self.assertEqual(
                    read_energyplus_version(executable), IDFVersion(26, 1, 0)
                )

    def test_finds_exact_transition_and_required_idds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "energyplus.exe"
            executable.touch()
            updater = root / "PreProcess" / "IDFVersionUpdater"
            updater.mkdir(parents=True)
            transition = updater / "Transition-V25-2-0-to-V26-1-0.exe"
            transition.touch()
            (updater / "V25-2-0-Energy+.idd").touch()
            (updater / "V26-1-0-Energy+.idd").touch()

            self.assertEqual(
                find_transition_program(
                    executable, IDFVersion(25, 2), IDFVersion(26, 1)
                ),
                transition,
            )

    def test_conversion_uses_copy_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "原始模型.idf"
            source.write_text("Version, 25.2;\n", encoding="utf-8")
            executable = root / "EnergyPlus" / "energyplus.exe"
            executable.parent.mkdir()
            executable.touch()
            updater = executable.parent / "PreProcess" / "IDFVersionUpdater"
            updater.mkdir(parents=True)
            transition = updater / "Transition-V25-2-0-to-V26-1-0.exe"
            transition.touch()
            (updater / "V25-2-0-Energy+.idd").touch()
            (updater / "V26-1-0-Energy+.idd").touch()

            def fake_transition(command, **kwargs):
                self.assertEqual(Path(kwargs["cwd"]), updater)
                converted = Path(command[1])
                converted.write_text("Version, 26.1;\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "converted", "")

            with patch(
                "model_builder.idf_versioning.read_energyplus_version",
                return_value=IDFVersion(26, 1, 0),
            ), patch(
                "model_builder.idf_versioning.subprocess.run",
                side_effect=fake_transition,
            ):
                converted, details = prepare_idf_for_energyplus(
                    source, executable, root / "中文转换目录"
                )

            self.assertEqual(read_idf_version(source), IDFVersion(25, 2, 0))
            self.assertEqual(read_idf_version(converted), IDFVersion(26, 1, 0))
            self.assertTrue(details["converted"])
            self.assertTrue((converted.parent / "transition.log").is_file())

    def test_same_version_does_not_run_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "model.idf"
            source.write_text("Version, 26.1;\n", encoding="utf-8")
            executable = root / "energyplus.exe"
            executable.touch()
            work_dir = root / "unused"

            with patch(
                "model_builder.idf_versioning.read_energyplus_version",
                return_value=IDFVersion(26, 1, 0),
            ):
                compatible, details = prepare_idf_for_energyplus(
                    source, executable, work_dir
                )

            self.assertEqual(compatible, source.resolve())
            self.assertFalse(details["converted"])
            self.assertFalse(work_dir.exists())

    def test_missing_transition_reports_exact_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "energyplus.exe"
            executable.touch()
            with self.assertRaisesRegex(IDFVersionError, "25.2→26.1"):
                find_transition_program(
                    executable, IDFVersion(25, 2), IDFVersion(26, 1)
                )


if __name__ == "__main__":
    unittest.main()
