import copy
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.excel_reader import load_project_from_excel
from model_builder.validation import (
    ValidationIssue,
    format_validation_report,
    save_validation_report,
    validate_project,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "建筑参数输入模板.xlsx"


def template_data():
    return copy.deepcopy(load_project_from_excel(TEMPLATE_PATH).data)


class FullValidationTests(unittest.TestCase):
    def test_real_template_passes_full_validation(self):
        issues = validate_project(template_data())
        self.assertFalse([item for item in issues if item.severity == "error"])

    def test_material_construction_and_window_errors_are_reported(self):
        data = template_data()
        del data["materials"][0]["conductivity_W_mK"]
        data["constructions"][0]["layer_ids"][1] = "MAT_MISSING"
        data["windows"][0]["zone_id"] = "Z999"
        data["windows"][0]["window_to_wall_ratio"] = 1.2

        paths = {
            item.path for item in validate_project(data) if item.severity == "error"
        }
        self.assertIn("materials[0].conductivity_W_mK", paths)
        self.assertIn("constructions[0].layer_ids[1]", paths)
        self.assertIn("windows[0].zone_id", paths)
        self.assertIn("windows[0].window_to_wall_ratio", paths)

    def test_schedule_hvac_and_coating_errors_are_reported(self):
        data = template_data()
        data["schedules"][0]["weekday_end"] = data["schedules"][0]["weekday_start"]
        data["hvac"]["systems"][0]["cooling_cop"] = 0
        data["hvac"]["systems"][0]["availability_schedule_id"] = "SCH_MISSING"
        data["coating"]["scenarios"][0]["solar_reflectance"] = 1.2
        data["coating"]["scenarios"][0]["target_construction_ids"] = [
            "CON_MISSING"
        ]

        paths = {
            item.path for item in validate_project(data) if item.severity == "error"
        }
        self.assertIn("schedules[0].weekday_end", paths)
        self.assertIn("hvac.systems[0].cooling_cop", paths)
        self.assertIn("hvac.systems[0].availability_schedule_id", paths)
        self.assertIn("coating.scenarios[0].solar_reflectance", paths)
        self.assertIn("coating.scenarios[0].target_construction_ids[0]", paths)

    def test_chinese_report_can_be_formatted_and_saved(self):
        issues = [
            ValidationIssue("error", "windows[0].zone_id", "引用的热区不存在。"),
            ValidationIssue("warning", "hvac.systems", "一个空调热区未被服务。"),
        ]
        report = format_validation_report(issues)
        self.assertIn("错误：1；警告：1；提示：0", report)
        self.assertIn("[错误] windows[0].zone_id", report)

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "校验报告.txt"
            result = save_validation_report(issues, output)
            self.assertEqual(result, output)
            self.assertEqual(output.read_text(encoding="utf-8"), report)


if __name__ == "__main__":
    unittest.main()
