import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import RunResult
from model_builder import ResultsBuildError, build_energy_comparison


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAGE7_DIR = PROJECT_ROOT / "stage7_coating_model"


class ResultsBuilderTests(unittest.TestCase):
    def test_real_paired_sql_outputs_produce_precise_annual_results(self):
        with tempfile.TemporaryDirectory() as temp:
            result = build_energy_comparison(STAGE7_DIR, Path(temp) / "stage8")
            self.assertTrue(result.validation["all_passed"])
            self.assertTrue(result.json_path.is_file())
            self.assertTrue(result.csv_path.is_file())
            self.assertTrue(result.hourly_csv_path.is_file())
            cooling = result.result["metrics"]["cooling_electricity_kwh"]
            expected = (
                (cooling["baseline"] - cooling["coating"])
                / cooling["baseline"]
                * 100
            )
            self.assertAlmostEqual(cooling["saving_percent"], expected, places=12)
            self.assertEqual(
                result.result["hourly_record_counts"]["baseline"][
                    "Cooling:Electricity"
                ],
                8760,
            )

    def test_incomplete_hourly_results_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage7 = root / "stage7"
            stage7.mkdir()
            (stage7 / "stage7_manifest.json").write_text(
                json.dumps({"project_name": "test", "model_details": {}}),
                encoding="utf-8",
            )
            (stage7 / "coating_validation.json").write_text(
                json.dumps({"all_passed": True}), encoding="utf-8"
            )
            incomplete = RunResult(
                root,
                {
                    "Cooling:Electricity": 10.0,
                    "Fans:Electricity": 0.0,
                    "Pumps:Electricity": 0.0,
                    "HeatRejection:Electricity": 0.0,
                    "Heating:Electricity": 0.0,
                    "Electricity:Facility": 20.0,
                },
                1.0,
                0,
                0,
                {"Cooling:Electricity": 10, "Electricity:Facility": 10},
            )
            series = [((2009, 1, 1, hour, 0), 1.0) for hour in range(1, 11)]
            with patch(
                "model_builder.results_builder._run_result",
                return_value=incomplete,
            ), patch(
                "model_builder.results_builder._read_hourly_meter_series",
                return_value=series,
            ):
                with self.assertRaisesRegex(
                    ResultsBuildError, "annual_hourly_records_complete"
                ):
                    build_energy_comparison(stage7, root / "stage8")


if __name__ == "__main__":
    unittest.main()
