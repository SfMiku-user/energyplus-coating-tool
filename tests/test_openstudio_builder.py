import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.openstudio_builder import (
    OpenStudioBuildError,
    _run_smoke_test,
    build_openstudio_model,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JSON = PROJECT_ROOT / "sample_projects" / "示例办公楼.json"


class OpenStudioBuilderTests(unittest.TestCase):
    def test_chinese_project_uses_ascii_staging_and_returns_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            openstudio = root / "OpenStudio" / "openstudio.exe"
            energyplus = root / "EnergyPlus" / "energyplus.exe"
            openstudio.parent.mkdir()
            energyplus.parent.mkdir()
            openstudio.touch()
            energyplus.touch()
            destination = root / "中文结果" / "示例办公楼"

            def fake_openstudio(command, **kwargs):
                self.assertEqual(command[1], "execute_python_script")
                for argument in command[2:]:
                    self.assertTrue(str(argument).isascii())
                staging_output = Path(command[4])
                staging_output.mkdir(parents=True)
                (staging_output / "building.osm").write_text("OSM", encoding="utf-8")
                (staging_output / "building_v25_2.idf").write_text(
                    "Version, 25.2;\n", encoding="utf-8"
                )
                (staging_output / "build_manifest.json").write_text(
                    json.dumps(
                        {
                            "project_name": "郑州某办公楼",
                            "osm_path": str(staging_output / "building.osm"),
                            "idf_25_2_path": str(staging_output / "building_v25_2.idf"),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                (staging_output / "geometry_validation.json").write_text(
                    json.dumps({"status": "passed", "all_passed": True}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "BUILD_OK", "")

            def fake_versioning(source, executable, work_dir):
                converted = Path(work_dir) / "source_v25_2_to_v26_1.idf"
                converted.parent.mkdir(parents=True)
                converted.write_text("Version, 26.1;\n", encoding="utf-8")
                (converted.parent / "transition.log").write_text(
                    "converted", encoding="utf-8"
                )
                return converted, {
                    "source_idf": str(Path(source).resolve()),
                    "source_version": "25.2.0",
                    "target_version": "26.1.0",
                    "converted": True,
                    "compatible_idf": str(converted.resolve()),
                    "transition_log": str((converted.parent / "transition.log").resolve()),
                }

            with patch(
                "model_builder.openstudio_builder.subprocess.run",
                side_effect=fake_openstudio,
            ), patch(
                "model_builder.openstudio_builder.prepare_idf_for_energyplus",
                side_effect=fake_versioning,
            ):
                result = build_openstudio_model(
                    SAMPLE_JSON, openstudio, energyplus, destination
                )

            self.assertTrue(result.osm_path.is_file())
            self.assertTrue(result.idf_25_2_path.is_file())
            self.assertTrue(result.compatible_idf_path.is_file())
            self.assertEqual(result.osm_path.name, "baseline.osm")
            self.assertEqual(result.idf_25_2_path.name, "baseline_25_2.idf")
            self.assertEqual(result.compatible_idf_path.name, "baseline.idf")
            self.assertTrue((destination / "project.json").is_file())
            self.assertTrue((destination / "openstudio.log").is_file())
            self.assertTrue((destination / "transition.log").is_file())
            self.assertTrue((destination / "geometry_validation.json").is_file())
            self.assertEqual(result.manifest["project_name"], "郑州某办公楼")
            self.assertTrue(result.version_details["converted"])

    def test_smoke_test_requires_cooling_output_and_accepts_zero_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            energyplus = root / "energyplus.exe"
            weather = root / "weather.epw"
            model = root / "baseline.idf"
            for path in (energyplus, weather, model):
                path.touch()
            output = root / "smoke_test"

            def fake_energyplus(command, **kwargs):
                (output / "eplusout.err").write_text(
                    "EnergyPlus Completed Successfully.\n", encoding="utf-8"
                )
                with closing(
                    sqlite3.connect(output / "eplusout.sql")
                ) as connection:
                    connection.execute(
                        "CREATE TABLE ReportDataDictionary ("
                        "ReportDataDictionaryIndex INTEGER, Name TEXT, Units TEXT)"
                    )
                    connection.execute(
                        "CREATE TABLE ReportData ("
                        "ReportDataDictionaryIndex INTEGER, Value REAL)"
                    )
                    connection.execute(
                        "INSERT INTO ReportDataDictionary VALUES (1, ?, 'J')",
                        ("Zone Air System Sensible Cooling Energy",),
                    )
                    connection.execute("INSERT INTO ReportData VALUES (1, 123.0)")
                    connection.commit()
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "model_builder.openstudio_builder.subprocess.run",
                side_effect=fake_energyplus,
            ):
                details = _run_smoke_test(
                    energyplus, weather, model, output
                )

            self.assertEqual(details["energyplus_exit_code"], 0)
            self.assertEqual(details["fatal_errors"], 0)
            self.assertEqual(
                details["cooling_outputs"][
                    "Zone Air System Sensible Cooling Energy"
                ]["sum"],
                123.0,
            )

    def test_nonempty_output_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            openstudio = root / "openstudio.exe"
            energyplus = root / "energyplus.exe"
            openstudio.touch()
            energyplus.touch()
            destination = root / "output"
            destination.mkdir()
            (destination / "existing.txt").touch()

            with self.assertRaisesRegex(OpenStudioBuildError, "必须为空"):
                build_openstudio_model(
                    SAMPLE_JSON, openstudio, energyplus, destination
                )


if __name__ == "__main__":
    unittest.main()
