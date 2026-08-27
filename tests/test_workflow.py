import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder import WorkflowError, run_project_workflow


class WorkflowTests(unittest.TestCase):
    def test_one_click_workflow_connects_all_stages_on_chinese_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            excel = root / "建筑参数.xlsx"
            weather = root / "郑州气象.epw"
            openstudio = root / "openstudio.exe"
            energyplus = root / "energyplus.exe"
            for path in (excel, weather, openstudio, energyplus):
                path.write_text("test", encoding="utf-8")
            progress = []

            def fake_excel(source, target):
                Path(target).write_text("{}", encoding="utf-8")
                return Path(target)

            def fake_model(project, os_exe, ep_exe, output, **kwargs):
                output = Path(output)
                output.mkdir()
                baseline = output / "baseline.idf"
                baseline.write_text("Version,26.1;", encoding="utf-8")
                return SimpleNamespace(
                    compatible_idf_path=baseline,
                    output_dir=output.resolve(),
                    manifest={"geometry_validation_status": "passed"},
                )

            def fake_coating(project, baseline, output, **kwargs):
                output = Path(output)
                output.mkdir()
                return SimpleNamespace(
                    output_dir=output.resolve(),
                    validation={"status": "passed"},
                )

            def fake_results(source, output):
                output = Path(output)
                output.mkdir()
                result = {
                    "metrics": {
                        "cooling_electricity_kwh": {
                            "baseline": 100.0,
                            "coating": 90.0,
                            "saving_percent": 10.0,
                        }
                    }
                }
                paths = []
                for name in (
                    "comparison_results.json",
                    "comparison_results.csv",
                    "hourly_cooling_comparison.csv",
                ):
                    path = output / name
                    path.write_text("test", encoding="utf-8")
                    paths.append(path.resolve())
                return SimpleNamespace(
                    output_dir=output.resolve(),
                    json_path=paths[0],
                    csv_path=paths[1],
                    hourly_csv_path=paths[2],
                    validation={"status": "passed"},
                    result=result,
                )

            with patch(
                "model_builder.workflow.convert_excel_to_json",
                side_effect=fake_excel,
            ), patch(
                "model_builder.workflow.build_openstudio_model",
                side_effect=fake_model,
            ), patch(
                "model_builder.workflow.build_coating_scenario",
                side_effect=fake_coating,
            ), patch(
                "model_builder.workflow.build_energy_comparison",
                side_effect=fake_results,
            ):
                result = run_project_workflow(
                    excel,
                    weather,
                    openstudio,
                    energyplus,
                    root / "中文结果",
                    run_name="测试项目",
                    progress_callback=lambda message, percent: progress.append(
                        (message, percent)
                    ),
                )

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["release_stage"], "testable")
            self.assertEqual(progress[-1][1], 100)
            self.assertEqual(
                result.result["metrics"]["cooling_electricity_kwh"][
                    "saving_percent"
                ],
                10.0,
            )

    def test_failure_keeps_a_readable_error_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / name for name in ("a.xlsx", "a.epw", "o.exe", "e.exe")]
            for path in paths:
                path.touch()
            with patch(
                "model_builder.workflow.convert_excel_to_json",
                side_effect=ValueError("测试错误"),
            ):
                with self.assertRaisesRegex(WorkflowError, "测试错误"):
                    run_project_workflow(
                        *paths,
                        root / "output",
                        run_name="failed_run",
                    )
            error = root / "output" / "failed_run" / "workflow_error.json"
            self.assertTrue(error.is_file())
            self.assertIn("测试错误", error.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
