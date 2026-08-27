import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.excel_reader import (
    ExcelReadError,
    ExcelValidationError,
    convert_excel_to_json,
    load_project_from_excel,
)
from model_builder.project import load_project
from model_builder.validation import validate_project


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "建筑参数输入模板.xlsx"
SAMPLE_XLSX_PATH = PROJECT_ROOT / "sample_projects" / "示例办公楼.xlsx"
SAMPLE_JSON_PATH = PROJECT_ROOT / "sample_projects" / "示例办公楼.json"

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _rewrite_xlsx(source: Path, destination: Path, changes: dict[str, bytes]) -> None:
    """复制工作簿，并替换指定的 Open XML 部件。"""

    with zipfile.ZipFile(source, "r") as input_archive:
        with zipfile.ZipFile(destination, "w") as output_archive:
            for item in input_archive.infolist():
                output_archive.writestr(
                    item,
                    changes.get(item.filename, input_archive.read(item.filename)),
                )


def _workbook_with_invalid_reflectance(source: Path, destination: Path) -> None:
    """把“涂料”工作表首条方案的太阳反射率改为 1.2。"""

    sheet_path = "xl/worksheets/sheet13.xml"
    with zipfile.ZipFile(source, "r") as archive:
        root = ElementTree.fromstring(archive.read(sheet_path))
    cell = root.find(f".//{{{_MAIN_NS}}}c[@r='E2']")
    if cell is None:
        raise AssertionError("示例工作簿中未找到涂料!E2。")
    cell.attrib.pop("t", None)
    value = cell.find(f"{{{_MAIN_NS}}}v")
    if value is None:
        value = ElementTree.SubElement(cell, f"{{{_MAIN_NS}}}v")
    value.text = "1.2"
    _rewrite_xlsx(
        source,
        destination,
        {sheet_path: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)},
    )


def _workbook_without_material_sheet(source: Path, destination: Path) -> None:
    """从工作簿目录中移除必填的“材料”工作表。"""

    workbook_path = "xl/workbook.xml"
    with zipfile.ZipFile(source, "r") as archive:
        root = ElementTree.fromstring(archive.read(workbook_path))
    sheets = root.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise AssertionError("示例工作簿缺少工作表目录。")
    material_sheet = next(
        (item for item in sheets if item.attrib.get("name") == "材料"),
        None,
    )
    if material_sheet is None:
        raise AssertionError("示例工作簿中未找到“材料”工作表。")
    sheets.remove(material_sheet)
    _rewrite_xlsx(
        source,
        destination,
        {
            workbook_path: ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True
            )
        },
    )


class ExcelReaderTests(unittest.TestCase):
    def test_real_template_is_converted_to_project_data(self):
        project = load_project_from_excel(TEMPLATE_PATH)
        data = project.data

        self.assertEqual(data["schema_version"], "1.0")
        self.assertEqual(data["project"]["name"], "郑州某办公楼")
        self.assertEqual(data["site"]["longitude"], 113.62)
        self.assertEqual(data["floors"][0]["id"], "F01")
        self.assertEqual(
            data["zones"][0]["polygon_xy"],
            [[0.0, 0.0], [8.0, 0.0], [8.0, 6.0], [0.0, 6.0]],
        )
        self.assertTrue(data["zones"][0]["conditioned"])
        self.assertAlmostEqual(data["windows"][0]["window_to_wall_ratio"], 0.35)
        self.assertEqual(
            data["constructions"][0]["layer_ids"],
            ["MAT_FINISH", "MAT_INS", "MAT_CONCRETE"],
        )
        self.assertEqual(data["hvac"]["systems"][0]["zone_ids"], ["Z001", "Z002"])
        self.assertAlmostEqual(data["coating"]["solar_reflectance"], 0.90)
        self.assertEqual(data["coating"]["target_construction_ids"], ["CON_ROOF01"])
        self.assertFalse(
            [issue for issue in validate_project(project) if issue.severity == "error"]
        )

    def test_invalid_xlsx_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as temp:
            invalid = Path(temp) / "invalid.xlsx"
            invalid.write_bytes(b"not an xlsx file")
            with self.assertRaisesRegex(ExcelReadError, "有效的 XLSX"):
                load_project_from_excel(invalid)

    def test_template_can_be_exported_as_project_json(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "project.json"
            result = convert_excel_to_json(TEMPLATE_PATH, output)
            self.assertEqual(result, output)
            self.assertEqual(load_project(output).data["project"]["name"], "郑州某办公楼")

    def test_missing_xlsx_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.xlsx"
            with self.assertRaisesRegex(ExcelReadError, "找不到 Excel 文件"):
                load_project_from_excel(missing)

    def test_sample_excel_converts_to_json(self):
        self.assertTrue(SAMPLE_XLSX_PATH.is_file())
        self.assertTrue(SAMPLE_JSON_PATH.is_file())
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "示例办公楼.json"
            convert_excel_to_json(SAMPLE_XLSX_PATH, output)
            self.assertEqual(load_project(output).data["project"]["name"], "郑州某办公楼")

    def test_invalid_field_stops_conversion_and_reports_exact_error(self):
        with tempfile.TemporaryDirectory() as temp:
            invalid = Path(temp) / "非法反射率.xlsx"
            output = Path(temp) / "不应生成.json"
            _workbook_with_invalid_reflectance(SAMPLE_XLSX_PATH, invalid)

            with self.assertRaises(ExcelValidationError) as caught:
                convert_excel_to_json(invalid, output)

            message = str(caught.exception)
            self.assertIn("coating.scenarios[0].solar_reflectance", message)
            self.assertIn("必须小于或等于1", message)
            self.assertFalse(output.exists())

    def test_chinese_path_and_project_name_are_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            chinese_folder = Path(temp) / "中文项目目录"
            chinese_folder.mkdir()
            output = chinese_folder / "郑州办公楼项目.json"

            convert_excel_to_json(SAMPLE_XLSX_PATH, output)

            self.assertTrue(output.is_file())
            self.assertEqual(load_project(output).data["project"]["name"], "郑州某办公楼")

    def test_missing_required_sheet_stops_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            incomplete = Path(temp) / "缺少材料工作表.xlsx"
            output = Path(temp) / "不应生成.json"
            _workbook_without_material_sheet(SAMPLE_XLSX_PATH, incomplete)

            with self.assertRaisesRegex(ExcelReadError, "缺少工作表：材料"):
                convert_excel_to_json(incomplete, output)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
