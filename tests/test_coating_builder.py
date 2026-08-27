import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder import CoatingBuildError, build_coating_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JSON = PROJECT_ROOT / "sample_projects" / "示例办公楼.json"
BASELINE_IDF = PROJECT_ROOT / "generated_model" / "baseline.idf"


class CoatingBuilderTests(unittest.TestCase):
    def test_active_scenario_generates_a_valid_pair_without_changing_source(self):
        original = BASELINE_IDF.read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            result = build_coating_scenario(
                SAMPLE_JSON,
                BASELINE_IDF,
                Path(temp) / "stage7",
            )
            self.assertTrue(result.baseline_idf_path.is_file())
            self.assertTrue(result.coating_idf_path.is_file())
            self.assertTrue(result.validation_path.is_file())
            self.assertTrue(result.manifest_path.is_file())
            self.assertTrue(result.validation["all_passed"])
            self.assertEqual(
                result.validation["checks"]["target_surfaces_found"]["count"], 1
            )
            self.assertAlmostEqual(
                result.validation["coated_surface_area_m2"], 48.0
            )
        self.assertEqual(BASELINE_IDF.read_bytes(), original)

    def test_partial_coverage_is_rejected_instead_of_silently_approximated(self):
        data = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))
        data["coating"]["scenarios"][0]["coverage_fraction"] = 0.5
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project.json"
            project.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(CoatingBuildError, "100% 涂覆"):
                build_coating_scenario(
                    project,
                    BASELINE_IDF,
                    root / "stage7",
                )


if __name__ == "__main__":
    unittest.main()
