"""读取建筑参数 Excel 模板并转换为统一项目数据。

实现只依赖 Python 标准库，直接解析 XLSX 压缩包中的 Open XML 文件，
从而保持桌面工具无需额外安装 openpyxl 等第三方依赖。
"""

from __future__ import annotations

import math
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .project import ProjectData
from .schema import SCHEMA_VERSION, new_project_dict
from .validation import ValidationIssue, format_validation_report, validate_project


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF_RE = re.compile(r"([A-Z]+)(\d+)")


class ExcelReadError(ValueError):
    """工作簿不存在、结构不符或数据不能转换。"""


class ExcelValidationError(ExcelReadError):
    """工作簿可以读取，但完整项目校验未通过。"""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__(
            "Excel 数据校验失败，未生成 JSON。\n"
            + format_validation_report(issues)
        )


@dataclass(frozen=True)
class _TableRow:
    sheet: str
    excel_row: int
    values: dict[str, object]


def _qualified(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _column_index(cell_reference: str) -> int:
    match = _CELL_REF_RE.fullmatch(cell_reference)
    if not match:
        raise ExcelReadError(f"无法识别单元格地址：{cell_reference}")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


def _xml_texts(element: ElementTree.Element) -> str:
    return "".join(
        node.text or ""
        for node in element.iter(_qualified(_MAIN_NS, "t"))
    )


def _parse_scalar(raw: str) -> object:
    try:
        number = float(raw)
    except ValueError:
        return raw
    if not math.isfinite(number):
        return raw
    if number.is_integer():
        return int(number)
    return number


class _XlsxReader:
    def __init__(self, path: Path):
        self.path = path
        self.shared_strings: list[str] = []

    def read(self) -> dict[str, list[list[object | None]]]:
        if not self.path.is_file():
            raise ExcelReadError(f"找不到 Excel 文件：{self.path}")
        try:
            with zipfile.ZipFile(self.path) as archive:
                self.shared_strings = self._read_shared_strings(archive)
                sheet_paths = self._read_sheet_paths(archive)
                return {
                    name: self._read_sheet(archive, sheet_path)
                    for name, sheet_path in sheet_paths
                }
        except zipfile.BadZipFile as exc:
            raise ExcelReadError("文件不是有效的 XLSX 工作簿。") from exc
        except KeyError as exc:
            raise ExcelReadError(f"XLSX 内部结构不完整：{exc}") from exc
        except ElementTree.ParseError as exc:
            raise ExcelReadError("XLSX 内部 XML 无法解析。") from exc

    @staticmethod
    def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
        try:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        return [_xml_texts(item) for item in root.findall(_qualified(_MAIN_NS, "si"))]

    @staticmethod
    def _read_sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        relationships = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in rels_root.findall(_qualified(_PACKAGE_REL_NS, "Relationship"))
        }
        result: list[tuple[str, str]] = []
        for sheet in workbook_root.iter(_qualified(_MAIN_NS, "sheet")):
            name = sheet.attrib.get("name", "").strip()
            relation_id = sheet.attrib.get(_qualified(_REL_NS, "id"))
            if not name or not relation_id or relation_id not in relationships:
                continue
            target = relationships[relation_id].replace("\\", "/")
            if target.startswith("/"):
                sheet_path = target.lstrip("/")
            else:
                sheet_path = posixpath.normpath(posixpath.join("xl", target))
            result.append((name, sheet_path))
        return result

    def _read_cell(self, cell: ElementTree.Element) -> object | None:
        cell_type = cell.attrib.get("t")
        if cell_type == "inlineStr":
            return _xml_texts(cell)
        value_node = cell.find(_qualified(_MAIN_NS, "v"))
        if value_node is None or value_node.text is None:
            return None
        raw = value_node.text
        if cell_type == "s":
            try:
                return self.shared_strings[int(raw)]
            except (ValueError, IndexError) as exc:
                raise ExcelReadError("共享字符串索引无效。") from exc
        if cell_type == "b":
            return raw == "1"
        if cell_type in {"str", "e", "d"}:
            return raw
        return _parse_scalar(raw)

    def _read_sheet(
        self, archive: zipfile.ZipFile, sheet_path: str
    ) -> list[list[object | None]]:
        root = ElementTree.fromstring(archive.read(sheet_path))
        row_values: dict[int, dict[int, object | None]] = {}
        max_column = -1
        for row in root.iter(_qualified(_MAIN_NS, "row")):
            row_number = int(row.attrib.get("r", "0"))
            if row_number <= 0:
                continue
            current: dict[int, object | None] = {}
            for cell in row.findall(_qualified(_MAIN_NS, "c")):
                reference = cell.attrib.get("r")
                if not reference:
                    continue
                column = _column_index(reference)
                current[column] = self._read_cell(cell)
                max_column = max(max_column, column)
            row_values[row_number] = current
        if not row_values or max_column < 0:
            return []
        max_row = max(row_values)
        return [
            [row_values.get(row, {}).get(column) for column in range(max_column + 1)]
            for row in range(1, max_row + 1)
        ]


def _clean_text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()


def _is_blank(value: object | None) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _table_rows(
    sheets: dict[str, list[list[object | None]]], sheet_name: str
) -> list[_TableRow]:
    if sheet_name not in sheets:
        raise ExcelReadError(f"缺少工作表：{sheet_name}")
    rows = sheets[sheet_name]
    if not rows:
        raise ExcelReadError(f"工作表“{sheet_name}”为空。")
    headers = [_clean_text(value) for value in rows[0]]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or any(not header for header in headers):
        raise ExcelReadError(f"工作表“{sheet_name}”的第一行表头不完整。")
    result: list[_TableRow] = []
    for row_index, row in enumerate(rows[1:], start=2):
        values = list(row[: len(headers)])
        values.extend([None] * (len(headers) - len(values)))
        if all(_is_blank(value) for value in values):
            break
        result.append(
            _TableRow(
                sheet=sheet_name,
                excel_row=row_index,
                values=dict(zip(headers, values, strict=True)),
            )
        )
    return result


def _cell_label(row: _TableRow, field: str) -> str:
    return f"{row.sheet} 第{row.excel_row}行“{field}”"


def _required_text(row: _TableRow, field: str) -> str:
    value = _clean_text(row.values.get(field))
    if not value:
        raise ExcelReadError(f"{_cell_label(row, field)}不能为空。")
    return value


def _optional_text(row: _TableRow, field: str) -> str | None:
    value = _clean_text(row.values.get(field))
    return value or None


def _number(row: _TableRow, field: str, required: bool = True) -> float | int | None:
    value = row.values.get(field)
    if _is_blank(value):
        if required:
            raise ExcelReadError(f"{_cell_label(row, field)}不能为空。")
        return None
    if isinstance(value, bool):
        raise ExcelReadError(f"{_cell_label(row, field)}必须是数字。")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExcelReadError(f"{_cell_label(row, field)}必须是纯数字，不要附带单位。") from exc
    if not math.isfinite(number):
        raise ExcelReadError(f"{_cell_label(row, field)}必须是有限数字。")
    if number.is_integer():
        return int(number)
    return number


def _boolean(row: _TableRow, field: str) -> bool:
    value = row.values.get(field)
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if text in {"是", "true", "1", "yes", "y"}:
        return True
    if text in {"否", "false", "0", "no", "n"}:
        return False
    raise ExcelReadError(f"{_cell_label(row, field)}必须填写“是”或“否”。")


def _split_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _polygon(row: _TableRow, field: str) -> list[list[float]]:
    text = _required_text(row, field)
    points: list[list[float]] = []
    try:
        for item in text.split(";"):
            x_text, y_text = (part.strip() for part in item.split(",", 1))
            points.append([float(x_text), float(y_text)])
    except (TypeError, ValueError) as exc:
        raise ExcelReadError(
            f"{_cell_label(row, field)}格式错误，应为 x1,y1;x2,y2;x3,y3。"
        ) from exc
    if len(points) < 3:
        raise ExcelReadError(f"{_cell_label(row, field)}至少需要三个坐标点。")
    return points


def _copy_optional(target: dict[str, Any], key: str, value: object | None) -> None:
    if value is not None and value != "":
        target[key] = value


def _source_and_notes(row: _TableRow, target: dict[str, Any]) -> None:
    _copy_optional(target, "source", _optional_text(row, "数据来源类型"))
    _copy_optional(target, "notes", _optional_text(row, "备注"))


def _project_info(
    rows: Iterable[_TableRow], data: dict[str, Any]
) -> list[dict[str, object]]:
    field_sources: list[dict[str, object]] = []
    for row in rows:
        key = _required_text(row, "字段键")
        value = row.values.get("填写值")
        source = _optional_text(row, "数据来源类型")
        notes = _optional_text(row, "备注")
        if key == "schema_version":
            if isinstance(value, (int, float)):
                data["schema_version"] = f"{float(value):.1f}"
            else:
                data["schema_version"] = _clean_text(value)
            section = "schema"
        elif key in {"name", "building_type", "total_floor_area_m2", "year_built"}:
            data["project"][key] = value
            section = "project"
        elif key in {
            "epw_path",
            "latitude",
            "longitude",
            "time_zone",
            "elevation_m",
            "north_axis_deg",
        }:
            data["site"][key] = value
            section = "site"
        else:
            data["project"].setdefault("extra_fields", {})[key] = value
            section = "project"
        field_sources.append(
            {
                "section": section,
                "field": key,
                "source": source,
                "notes": notes,
            }
        )
    return field_sources


def _convert_workbook(sheets: dict[str, list[list[object | None]]]) -> dict[str, Any]:
    data = new_project_dict()
    field_sources = _project_info(_table_rows(sheets, "项目信息"), data)

    data["floors"] = []
    for row in _table_rows(sheets, "楼层"):
        record = {
            "id": _required_text(row, "楼层编号"),
            "elevation_m": _number(row, "标高_m"),
            "height_m": _number(row, "层高_m"),
            "multiplier": _number(row, "倍数"),
        }
        _source_and_notes(row, record)
        data["floors"].append(record)

    data["zones"] = []
    for row in _table_rows(sheets, "热区"):
        record = {
            "id": _required_text(row, "热区编号"),
            "floor_id": _required_text(row, "楼层编号"),
            "name": _required_text(row, "热区名称"),
            "polygon_xy": _polygon(row, "地面多边形坐标_m"),
            "conditioned": _boolean(row, "是否空调"),
            "space_type": _required_text(row, "空间用途"),
            "multiplier": _number(row, "热区倍数"),
        }
        _source_and_notes(row, record)
        data["zones"].append(record)

    data["windows"] = []
    for row in _table_rows(sheets, "门窗"):
        record = {
            "id": _required_text(row, "门窗编号"),
            "zone_id": _required_text(row, "热区编号"),
            "input_mode": _required_text(row, "输入方式"),
            "orientation": _required_text(row, "方向"),
            "construction_id": _required_text(row, "窗构造编号"),
        }
        _copy_optional(record, "window_to_wall_ratio", _number(row, "窗墙比", False))
        _copy_optional(record, "width_m", _number(row, "宽度_m", False))
        _copy_optional(record, "height_m", _number(row, "高度_m", False))
        _copy_optional(record, "sill_height_m", _number(row, "窗台高_m", False))
        _copy_optional(record, "shading_type", _optional_text(row, "遮阳类型"))
        _source_and_notes(row, record)
        data["windows"].append(record)

    data["materials"] = []
    material_fields = [
        ("thickness_m", "厚度_m"),
        ("conductivity_W_mK", "导热系数_W_mK"),
        ("density_kg_m3", "密度_kg_m3"),
        ("specific_heat_J_kgK", "比热_J_kgK"),
        ("thermal_absorptance", "热吸收率"),
        ("solar_absorptance", "太阳吸收率"),
        ("visible_absorptance", "可见光吸收率"),
        ("thermal_resistance_m2K_W", "热阻_m2K_W"),
    ]
    for row in _table_rows(sheets, "材料"):
        record = {
            "id": _required_text(row, "材料编号"),
            "name": _required_text(row, "名称"),
            "energyplus_type": _required_text(row, "EnergyPlus类型"),
            "roughness": _required_text(row, "粗糙度"),
        }
        for key, header in material_fields:
            _copy_optional(record, key, _number(row, header, False))
        _source_and_notes(row, record)
        data["materials"].append(record)

    data["constructions"] = []
    for row in _table_rows(sheets, "构造"):
        record = {
            "id": _required_text(row, "构造编号"),
            "name": _required_text(row, "名称"),
            "kind": "opaque",
            "use": _required_text(row, "用途"),
            "layer_ids": _split_ids(_required_text(row, "从室外到室内材料层")),
            "outside_material_id": _required_text(row, "外表面材料编号"),
        }
        _source_and_notes(row, record)
        data["constructions"].append(record)
    for row in _table_rows(sheets, "窗构造"):
        record = {
            "id": _required_text(row, "窗构造编号"),
            "name": _required_text(row, "名称"),
            "kind": "window",
            "use": "窗",
            "energyplus_type": _required_text(row, "EnergyPlus类型"),
            "u_factor_W_m2K": _number(row, "整窗U值_W_m2K"),
            "shgc": _number(row, "太阳得热系数_SHGC"),
            "visible_transmittance": _number(row, "可见光透射率"),
        }
        _source_and_notes(row, record)
        data["constructions"].append(record)

    data["schedules"] = []
    for row in _table_rows(sheets, "时间表"):
        record = {
            "id": _required_text(row, "时间表编号"),
            "type": _required_text(row, "类型"),
            "weekday_start": _number(row, "工作日起始"),
            "weekday_end": _number(row, "工作日结束"),
            "saturday_enabled": _boolean(row, "周六启用"),
            "sunday_enabled": _boolean(row, "周日启用"),
            "peak_fraction": _number(row, "峰值比例"),
        }
        _source_and_notes(row, record)
        data["schedules"].append(record)

    data["loads"] = []
    for row in _table_rows(sheets, "人员与设备"):
        record = {
            "zone_id": _required_text(row, "热区编号"),
            "schedule_id": _required_text(row, "时间表编号"),
            "people_density_person_m2": _number(row, "人员密度_person_m2"),
            "lighting_power_density_W_m2": _number(row, "照明功率密度_W_m2"),
            "equipment_power_density_W_m2": _number(row, "设备功率密度_W_m2"),
            "infiltration_ach": _number(row, "渗透换气次数_ACH"),
            "outdoor_air_L_s_person": _number(row, "新风量_L_s_person"),
        }
        _source_and_notes(row, record)
        data["loads"].append(record)

    data["thermostats"] = []
    for row in _table_rows(sheets, "温控"):
        record = {
            "zone_id": _required_text(row, "热区编号"),
            "cooling_setpoint_C": _number(row, "制冷设定温度_C"),
            "heating_setpoint_C": _number(row, "采暖设定温度_C"),
            "schedule_id": _required_text(row, "时间表编号"),
        }
        _source_and_notes(row, record)
        data["thermostats"].append(record)

    systems: list[dict[str, Any]] = []
    for row in _table_rows(sheets, "HVAC"):
        system_type = _required_text(row, "系统类型")
        record = {
            "id": _required_text(row, "系统编号"),
            "system_type": system_type,
            "zone_ids": _split_ids(_required_text(row, "服务热区编号")),
            "cooling_cop": _number(row, "制冷COP"),
            "heating_cop": _number(row, "采暖COP"),
            "availability_schedule_id": _required_text(row, "运行时间表编号"),
        }
        _copy_optional(record, "rated_cooling_capacity_kW", _number(row, "额定制冷容量_kW", False))
        _copy_optional(record, "supply_fan_power_W", _number(row, "送风机功率_W", False))
        _copy_optional(record, "pump_power_W", _number(row, "水泵功率_W", False))
        _copy_optional(record, "part_load_curve", _optional_text(row, "部分负荷曲线"))
        _source_and_notes(row, record)
        systems.append(record)
    data["hvac"] = {
        "mode": "ideal_loads"
        if systems and systems[0]["system_type"] == "IdealLoads仅负荷"
        else "constant_cop",
        "active_system_id": systems[0]["id"] if systems else None,
        "systems": systems,
    }

    scenarios: list[dict[str, Any]] = []
    for row in _table_rows(sheets, "涂料"):
        record = {
            "id": _required_text(row, "方案编号"),
            "name": _required_text(row, "涂料名称"),
            "target_type": _required_text(row, "涂覆对象类型"),
            "target_construction_ids": _split_ids(_required_text(row, "目标构造编号")),
            "solar_reflectance": _number(row, "太阳反射率"),
            "thermal_emissivity": _number(row, "长波发射率"),
            "thickness_m": _number(row, "厚度_m"),
            "conductivity_W_mK": _number(row, "导热系数_W_mK"),
            "state": _required_text(row, "状态"),
            "coverage_fraction": _number(row, "涂覆面积比例"),
            "age_years": _number(row, "老化年数"),
        }
        _source_and_notes(row, record)
        scenarios.append(record)
    if scenarios:
        data["coating"] = dict(scenarios[0])
        data["coating"]["active_scenario_id"] = scenarios[0]["id"]
        data["coating"]["scenarios"] = scenarios
    else:
        data["coating"] = {"active_scenario_id": None, "scenarios": []}

    quality_records: list[dict[str, Any]] = []
    for row in _table_rows(sheets, "数据质量"):
        record = {
            "section": _required_text(row, "章节"),
            "record_id": _required_text(row, "记录编号"),
            "field": _required_text(row, "参数字段"),
            "source": _required_text(row, "数据来源类型"),
        }
        mappings = [
            ("source_file", "来源文件或设备"),
            ("uncertainty", "不确定度"),
            ("confidence", "置信度"),
            ("review_status", "复核状态"),
            ("notes", "备注"),
        ]
        for key, header in mappings:
            _copy_optional(record, key, _optional_text(row, header))
        quality_records.append(record)
    data["data_quality"] = {
        "project_field_sources": field_sources,
        "records": quality_records,
    }

    if data.get("schema_version") != SCHEMA_VERSION:
        raise ExcelReadError(
            f"模板数据规范版本为 {data.get('schema_version')!r}，当前只支持 {SCHEMA_VERSION}。"
        )
    return data


def load_project_from_excel(path: Path | str) -> ProjectData:
    """读取建筑参数 XLSX，并返回标准化的 :class:`ProjectData`。"""

    workbook_path = Path(path)
    sheets = _XlsxReader(workbook_path).read()
    project = ProjectData.from_dict(_convert_workbook(sheets))
    project.source_path = workbook_path.resolve()
    return project


def convert_excel_to_json(
    excel_path: Path | str, json_path: Path | str
) -> Path:
    """校验建筑参数工作簿并转换为项目 JSON。

    只要存在一个错误级别的校验问题，就停止转换且不创建 JSON。
    警告和提示会保留给界面展示，但不会阻止转换。
    """

    project = load_project_from_excel(excel_path)
    errors = [
        issue for issue in validate_project(project) if issue.severity == "error"
    ]
    if errors:
        raise ExcelValidationError(errors)
    return project.to_json(json_path)
