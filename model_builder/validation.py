"""建筑项目数据的第一层结构和几何校验。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project import ProjectData
from .schema import (
    LIST_SECTIONS,
    OBJECT_SECTIONS,
    PROJECT_SECTIONS,
    REQUIRED_FLOOR_FIELDS,
    REQUIRED_PROJECT_FIELDS,
    REQUIRED_SITE_FIELDS,
    REQUIRED_ZONE_FIELDS,
    SCHEMA_VERSION,
    SOURCE_TYPES,
)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    path: str
    message: str

    def __str__(self) -> str:
        labels = {"error": "错误", "warning": "警告", "info": "提示"}
        label = labels.get(self.severity, self.severity)
        return f"{label}：{self.path}：{self.message}"


def _issue(
    issues: list[ValidationIssue], severity: str, path: str, message: str
) -> None:
    issues.append(ValidationIssue(severity, path, message))


def _check_required_fields(
    value: dict[str, Any],
    requirements: dict[str, object],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    for field, expected in requirements.items():
        field_path = f"{path}.{field}"
        if field not in value or value[field] in (None, ""):
            _issue(issues, "error", field_path, "缺少必填参数。")
            continue
        if not isinstance(value[field], expected):
            _issue(issues, "error", field_path, "数据类型不正确。")


def _valid_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _polygon_area(points: list[object]) -> float | None:
    if len(points) < 3:
        return None
    normalized: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        if not _valid_number(point[0]) or not _valid_number(point[1]):
            return None
        normalized.append((float(point[0]), float(point[1])))
    area = 0.0
    for index, (x1, y1) in enumerate(normalized):
        x2, y2 = normalized[(index + 1) % len(normalized)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _check_source(
    value: object, path: str, issues: list[ValidationIssue]
) -> None:
    if value not in (None, "") and value not in SOURCE_TYPES:
        _issue(
            issues,
            "error",
            path,
            "数据来源类型不在允许列表中。",
        )


def _check_numeric_range(
    value: object,
    path: str,
    issues: list[ValidationIssue],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
    maximum_inclusive: bool = True,
    required: bool = True,
) -> bool:
    if value in (None, ""):
        if required:
            _issue(issues, "error", path, "缺少必填参数。")
        return False
    if not _valid_number(value):
        _issue(issues, "error", path, "必须是有限数字。")
        return False
    number = float(value)
    if minimum is not None:
        invalid = number < minimum if minimum_inclusive else number <= minimum
        if invalid:
            operator = "大于或等于" if minimum_inclusive else "大于"
            _issue(issues, "error", path, f"必须{operator}{minimum:g}。")
            return False
    if maximum is not None:
        invalid = number > maximum if maximum_inclusive else number >= maximum
        if invalid:
            operator = "小于或等于" if maximum_inclusive else "小于"
            _issue(issues, "error", path, f"必须{operator}{maximum:g}。")
            return False
    return True


def _check_unique_id(
    value: object,
    path: str,
    seen: set[str],
    issues: list[ValidationIssue],
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _issue(issues, "error", path, "编号必须是非空文本。")
        return None
    normalized = value.strip()
    if normalized in seen:
        _issue(issues, "error", path, "编号重复。")
    seen.add(normalized)
    return normalized


def _validate_materials(
    materials: list[object], issues: list[ValidationIssue]
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    ids: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    supported = {"Material", "Material:NoMass", "Material:AirGap"}
    absorptance_fields = (
        "thermal_absorptance",
        "solar_absorptance",
        "visible_absorptance",
    )
    for index, item in enumerate(materials):
        path = f"materials[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "材料记录必须是对象。")
            continue
        material_id = _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        if material_id:
            records[material_id] = item
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            _issue(issues, "error", f"{path}.name", "缺少材料名称。")
        material_type = item.get("energyplus_type")
        if material_type not in supported:
            _issue(
                issues,
                "error",
                f"{path}.energyplus_type",
                "当前只支持 Material、Material:NoMass 或 Material:AirGap。",
            )
        if material_type == "Material":
            if item.get("roughness") in (None, ""):
                _issue(issues, "error", f"{path}.roughness", "普通材料必须填写粗糙度。")
            for field in (
                "thickness_m",
                "conductivity_W_mK",
                "density_kg_m3",
                "specific_heat_J_kgK",
            ):
                _check_numeric_range(
                    item.get(field),
                    f"{path}.{field}",
                    issues,
                    minimum=0,
                    minimum_inclusive=False,
                )
            for field in absorptance_fields:
                _check_numeric_range(
                    item.get(field),
                    f"{path}.{field}",
                    issues,
                    minimum=0,
                    maximum=1,
                )
        elif material_type in {"Material:NoMass", "Material:AirGap"}:
            _check_numeric_range(
                item.get("thermal_resistance_m2K_W"),
                f"{path}.thermal_resistance_m2K_W",
                issues,
                minimum=0,
                minimum_inclusive=False,
            )
        _check_source(item.get("source"), f"{path}.source", issues)
    return ids, records


def _validate_constructions(
    constructions: list[object],
    material_ids: set[str],
    issues: list[ValidationIssue],
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    ids: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(constructions):
        path = f"constructions[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "构造记录必须是对象。")
            continue
        construction_id = _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        if construction_id:
            records[construction_id] = item
        kind = item.get("kind", "opaque")
        if kind == "opaque":
            layers = item.get("layer_ids")
            if not isinstance(layers, list) or not layers:
                _issue(issues, "error", f"{path}.layer_ids", "实体构造至少需要一个材料层。")
            else:
                for layer_index, material_id in enumerate(layers):
                    if material_id not in material_ids:
                        _issue(
                            issues,
                            "error",
                            f"{path}.layer_ids[{layer_index}]",
                            f"引用的材料“{material_id}”不存在。",
                        )
                outside = item.get("outside_material_id")
                if outside not in material_ids:
                    _issue(
                        issues,
                        "error",
                        f"{path}.outside_material_id",
                        "外表面材料编号不存在。",
                    )
                elif outside != layers[0]:
                    _issue(
                        issues,
                        "error",
                        f"{path}.outside_material_id",
                        "必须与材料层列表的第一层一致。",
                    )
        elif kind == "window":
            _check_numeric_range(
                item.get("u_factor_W_m2K"),
                f"{path}.u_factor_W_m2K",
                issues,
                minimum=0,
                minimum_inclusive=False,
            )
            for field in ("shgc", "visible_transmittance"):
                _check_numeric_range(
                    item.get(field),
                    f"{path}.{field}",
                    issues,
                    minimum=0,
                    maximum=1,
                )
        else:
            _issue(issues, "error", f"{path}.kind", "构造类型必须是 opaque 或 window。")
        _check_source(item.get("source"), f"{path}.source", issues)
    return ids, records


def _validate_windows(
    windows: list[object],
    zone_ids: set[str],
    constructions: dict[str, dict[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    ids: set[str] = set()
    orientations = {"North", "East", "South", "West"}
    for index, item in enumerate(windows):
        path = f"windows[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "门窗记录必须是对象。")
            continue
        _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        if item.get("zone_id") not in zone_ids:
            _issue(issues, "error", f"{path}.zone_id", "引用的热区不存在。")
        construction = constructions.get(str(item.get("construction_id", "")))
        if construction is None:
            _issue(issues, "error", f"{path}.construction_id", "引用的窗构造不存在。")
        elif construction.get("kind") != "window":
            _issue(issues, "error", f"{path}.construction_id", "引用的构造不是窗构造。")
        if item.get("orientation") not in orientations:
            _issue(issues, "error", f"{path}.orientation", "方向必须是 North、East、South 或 West。")
        mode = item.get("input_mode")
        if mode == "窗墙比":
            _check_numeric_range(
                item.get("window_to_wall_ratio"),
                f"{path}.window_to_wall_ratio",
                issues,
                minimum=0,
                maximum=1,
                minimum_inclusive=False,
                maximum_inclusive=False,
            )
        elif mode == "明确尺寸":
            for field in ("width_m", "height_m"):
                _check_numeric_range(
                    item.get(field),
                    f"{path}.{field}",
                    issues,
                    minimum=0,
                    minimum_inclusive=False,
                )
            _check_numeric_range(
                item.get("sill_height_m"),
                f"{path}.sill_height_m",
                issues,
                minimum=0,
            )
        else:
            _issue(issues, "error", f"{path}.input_mode", "输入方式必须是“窗墙比”或“明确尺寸”。")
        _check_source(item.get("source"), f"{path}.source", issues)


def _validate_schedules(
    schedules: list[object], issues: list[ValidationIssue]
) -> set[str]:
    ids: set[str] = set()
    for index, item in enumerate(schedules):
        path = f"schedules[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "时间表记录必须是对象。")
            continue
        _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        if item.get("type") not in {"Fraction", "OnOff", "Temperature"}:
            _issue(issues, "error", f"{path}.type", "时间表类型不受支持。")
        start_ok = _check_numeric_range(
            item.get("weekday_start"),
            f"{path}.weekday_start",
            issues,
            minimum=0,
            maximum=1,
        )
        end_ok = _check_numeric_range(
            item.get("weekday_end"),
            f"{path}.weekday_end",
            issues,
            minimum=0,
            maximum=1,
        )
        if start_ok and end_ok and item["weekday_end"] <= item["weekday_start"]:
            _issue(issues, "error", f"{path}.weekday_end", "结束时间必须晚于起始时间。")
        _check_numeric_range(
            item.get("peak_fraction"),
            f"{path}.peak_fraction",
            issues,
            minimum=0,
            maximum=1,
        )
        _check_source(item.get("source"), f"{path}.source", issues)
    return ids


def _validate_loads(
    loads: list[object],
    zone_ids: set[str],
    schedule_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    seen_zones: set[str] = set()
    numeric_fields = (
        "people_density_person_m2",
        "lighting_power_density_W_m2",
        "equipment_power_density_W_m2",
        "infiltration_ach",
        "outdoor_air_L_s_person",
    )
    for index, item in enumerate(loads):
        path = f"loads[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "人员与设备记录必须是对象。")
            continue
        zone_id = item.get("zone_id")
        if zone_id not in zone_ids:
            _issue(issues, "error", f"{path}.zone_id", "引用的热区不存在。")
        elif zone_id in seen_zones:
            _issue(issues, "error", f"{path}.zone_id", "同一热区存在重复负荷记录。")
        else:
            seen_zones.add(zone_id)
        if item.get("schedule_id") not in schedule_ids:
            _issue(issues, "error", f"{path}.schedule_id", "引用的时间表不存在。")
        for field in numeric_fields:
            _check_numeric_range(item.get(field), f"{path}.{field}", issues, minimum=0)
        _check_source(item.get("source"), f"{path}.source", issues)


def _validate_thermostats(
    thermostats: list[object],
    zone_ids: set[str],
    schedule_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    seen_zones: set[str] = set()
    for index, item in enumerate(thermostats):
        path = f"thermostats[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "温控记录必须是对象。")
            continue
        zone_id = item.get("zone_id")
        if zone_id not in zone_ids:
            _issue(issues, "error", f"{path}.zone_id", "引用的热区不存在。")
        elif zone_id in seen_zones:
            _issue(issues, "error", f"{path}.zone_id", "同一热区存在重复温控记录。")
        else:
            seen_zones.add(zone_id)
        if item.get("schedule_id") not in schedule_ids:
            _issue(issues, "error", f"{path}.schedule_id", "引用的时间表不存在。")
        cooling_ok = _check_numeric_range(
            item.get("cooling_setpoint_C"),
            f"{path}.cooling_setpoint_C",
            issues,
            minimum=-50,
            maximum=60,
        )
        heating_ok = _check_numeric_range(
            item.get("heating_setpoint_C"),
            f"{path}.heating_setpoint_C",
            issues,
            minimum=-50,
            maximum=60,
        )
        if cooling_ok and heating_ok and item["cooling_setpoint_C"] <= item["heating_setpoint_C"]:
            _issue(issues, "error", f"{path}.cooling_setpoint_C", "制冷设定温度必须高于采暖设定温度。")
        _check_source(item.get("source"), f"{path}.source", issues)


def _validate_hvac(
    hvac: dict[str, Any],
    zone_ids: set[str],
    conditioned_zone_ids: set[str],
    schedule_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    systems = hvac.get("systems")
    mode = hvac.get("mode")
    if systems is None:
        if mode not in {"ideal_loads", None}:
            _issue(issues, "error", "hvac.systems", "当前 HVAC 模式必须提供系统列表。")
        return
    if not isinstance(systems, list):
        _issue(issues, "error", "hvac.systems", "系统列表格式错误。")
        return
    if not systems and mode != "ideal_loads":
        _issue(issues, "error", "hvac.systems", "至少需要一套 HVAC 系统。")
        return
    ids: set[str] = set()
    served: set[str] = set()
    for index, item in enumerate(systems):
        path = f"hvac.systems[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "error", path, "HVAC 系统记录必须是对象。")
            continue
        _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        system_zones = item.get("zone_ids")
        if not isinstance(system_zones, list) or not system_zones:
            _issue(issues, "error", f"{path}.zone_ids", "至少需要一个服务热区。")
        else:
            for zone_index, zone_id in enumerate(system_zones):
                if zone_id not in zone_ids:
                    _issue(
                        issues,
                        "error",
                        f"{path}.zone_ids[{zone_index}]",
                        f"引用的热区“{zone_id}”不存在。",
                    )
                else:
                    served.add(zone_id)
        for field in ("cooling_cop", "heating_cop"):
            _check_numeric_range(
                item.get(field),
                f"{path}.{field}",
                issues,
                minimum=0,
                minimum_inclusive=False,
            )
        _check_numeric_range(
            item.get("rated_cooling_capacity_kW"),
            f"{path}.rated_cooling_capacity_kW",
            issues,
            minimum=0,
            minimum_inclusive=False,
            required=False,
        )
        for field in ("supply_fan_power_W", "pump_power_W"):
            _check_numeric_range(
                item.get(field), f"{path}.{field}", issues, minimum=0, required=False
            )
        if item.get("availability_schedule_id") not in schedule_ids:
            _issue(issues, "error", f"{path}.availability_schedule_id", "引用的运行时间表不存在。")
        _check_source(item.get("source"), f"{path}.source", issues)
    for zone_id in sorted(conditioned_zone_ids - served):
        _issue(issues, "warning", "hvac.systems", f"空调热区“{zone_id}”未被任何 HVAC 系统服务。")


def _validate_coating(
    coating: dict[str, Any],
    constructions: dict[str, dict[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    scenarios = coating.get("scenarios")
    if isinstance(scenarios, list):
        records = scenarios
        prefix = "coating.scenarios"
    elif coating:
        records = [coating]
        prefix = "coating"
    else:
        return
    ids: set[str] = set()
    for index, item in enumerate(records):
        path = f"{prefix}[{index}]" if prefix.endswith("scenarios") else prefix
        if not isinstance(item, dict):
            _issue(issues, "error", path, "涂料方案必须是对象。")
            continue
        if "id" in item:
            _check_unique_id(item.get("id"), f"{path}.id", ids, issues)
        for field in ("solar_reflectance", "thermal_emissivity"):
            _check_numeric_range(
                item.get(field), f"{path}.{field}", issues, minimum=0, maximum=1
            )
        if "coverage_fraction" in item:
            _check_numeric_range(
                item.get("coverage_fraction"),
                f"{path}.coverage_fraction",
                issues,
                minimum=0,
                maximum=1,
                minimum_inclusive=False,
            )
        for field in ("thickness_m", "conductivity_W_mK"):
            if field in item:
                _check_numeric_range(
                    item.get(field),
                    f"{path}.{field}",
                    issues,
                    minimum=0,
                    minimum_inclusive=False,
                )
        if "age_years" in item:
            _check_numeric_range(item.get("age_years"), f"{path}.age_years", issues, minimum=0)
        targets = item.get("target_construction_ids")
        if targets is not None:
            if not isinstance(targets, list) or not targets:
                _issue(issues, "error", f"{path}.target_construction_ids", "至少需要一个目标构造。")
            else:
                for target_index, construction_id in enumerate(targets):
                    construction = constructions.get(str(construction_id))
                    if construction is None:
                        _issue(
                            issues,
                            "error",
                            f"{path}.target_construction_ids[{target_index}]",
                            f"目标构造“{construction_id}”不存在。",
                        )
                    elif construction.get("kind", "opaque") != "opaque":
                        _issue(
                            issues,
                            "error",
                            f"{path}.target_construction_ids[{target_index}]",
                            "涂料目标必须是实体围护构造。",
                        )
        _check_source(item.get("source"), f"{path}.source", issues)


def _validate_data_quality(
    data_quality: dict[str, Any], issues: list[ValidationIssue]
) -> None:
    for group_name in ("project_field_sources", "records"):
        records = data_quality.get(group_name, [])
        if not isinstance(records, list):
            _issue(issues, "error", f"data_quality.{group_name}", "数据质量记录必须是列表。")
            continue
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                _issue(issues, "error", f"data_quality.{group_name}[{index}]", "记录必须是对象。")
                continue
            _check_source(
                item.get("source"),
                f"data_quality.{group_name}[{index}].source",
                issues,
            )


def format_validation_report(issues: list[ValidationIssue]) -> str:
    """把校验结果整理成可直接展示或保存的中文报告。"""

    errors = sum(item.severity == "error" for item in issues)
    warnings = sum(item.severity == "warning" for item in issues)
    infos = sum(item.severity == "info" for item in issues)
    lines = [
        "建筑参数校验报告",
        "=" * 20,
        f"错误：{errors}；警告：{warnings}；提示：{infos}",
        "",
    ]
    if not issues:
        lines.append("校验通过：未发现错误、警告或提示。")
    else:
        order = {"error": 0, "warning": 1, "info": 2}
        labels = {"error": "错误", "warning": "警告", "info": "提示"}
        for item in sorted(issues, key=lambda value: (order.get(value.severity, 9), value.path)):
            lines.append(f"[{labels.get(item.severity, item.severity)}] {item.path}：{item.message}")
    return "\n".join(lines) + "\n"


def save_validation_report(
    issues: list[ValidationIssue], path: str | Path
) -> Path:
    """以 UTF-8 文本保存中文校验报告并返回输出路径。"""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(format_validation_report(issues), encoding="utf-8")
    return output_path


def validate_project(project: ProjectData | dict[str, Any]) -> list[ValidationIssue]:
    """返回项目中的错误、警告和提示，不直接修改输入数据。"""

    data = project.data if isinstance(project, ProjectData) else project
    issues: list[ValidationIssue] = []
    if not isinstance(data, dict):
        return [ValidationIssue("error", "$", "项目数据的根节点必须是对象。")]

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        _issue(
            issues,
            "error",
            "schema_version",
            f"当前只支持数据规范版本{SCHEMA_VERSION}。",
        )

    for section in PROJECT_SECTIONS:
        if section not in data:
            _issue(issues, "error", section, "缺少项目章节。")
            continue
        if section in OBJECT_SECTIONS and not isinstance(data[section], dict):
            _issue(issues, "error", section, "该章节必须是对象。")
        if section in LIST_SECTIONS and not isinstance(data[section], list):
            _issue(issues, "error", section, "该章节必须是列表。")

    project_info = data.get("project")
    if isinstance(project_info, dict):
        _check_required_fields(
            project_info, REQUIRED_PROJECT_FIELDS, "project", issues
        )

    site = data.get("site")
    if isinstance(site, dict):
        _check_required_fields(site, REQUIRED_SITE_FIELDS, "site", issues)
        if _valid_number(site.get("latitude")) and not -90 <= site["latitude"] <= 90:
            _issue(issues, "error", "site.latitude", "纬度必须在-90～90之间。")
        if _valid_number(site.get("longitude")) and not -180 <= site["longitude"] <= 180:
            _issue(issues, "error", "site.longitude", "经度必须在-180～180之间。")
        _check_numeric_range(
            site.get("time_zone"), "site.time_zone", issues, minimum=-12, maximum=14
        )
        _check_numeric_range(
            site.get("elevation_m"),
            "site.elevation_m",
            issues,
            minimum=-500,
            maximum=10000,
        )
        _check_numeric_range(
            site.get("north_axis_deg"),
            "site.north_axis_deg",
            issues,
            minimum=0,
            maximum=360,
            maximum_inclusive=False,
        )

    floors = data.get("floors")
    floor_ids: set[str] = set()
    if isinstance(floors, list):
        if not floors:
            _issue(issues, "error", "floors", "至少需要一个楼层。")
        for index, floor in enumerate(floors):
            path = f"floors[{index}]"
            if not isinstance(floor, dict):
                _issue(issues, "error", path, "楼层记录必须是对象。")
                continue
            _check_required_fields(floor, REQUIRED_FLOOR_FIELDS, path, issues)
            floor_id = floor.get("id")
            if isinstance(floor_id, str):
                if floor_id in floor_ids:
                    _issue(issues, "error", f"{path}.id", "楼层编号重复。")
                floor_ids.add(floor_id)
            if _valid_number(floor.get("height_m")) and floor["height_m"] <= 0:
                _issue(issues, "error", f"{path}.height_m", "层高必须大于0。")
            _check_numeric_range(
                floor.get("multiplier", 1),
                f"{path}.multiplier",
                issues,
                minimum=1,
            )
            _check_source(floor.get("source"), f"{path}.source", issues)

    zones = data.get("zones")
    zone_ids: set[str] = set()
    if isinstance(zones, list):
        if not zones:
            _issue(issues, "error", "zones", "至少需要一个热区。")
        for index, zone in enumerate(zones):
            path = f"zones[{index}]"
            if not isinstance(zone, dict):
                _issue(issues, "error", path, "热区记录必须是对象。")
                continue
            _check_required_fields(zone, REQUIRED_ZONE_FIELDS, path, issues)
            zone_id = zone.get("id")
            if isinstance(zone_id, str):
                if zone_id in zone_ids:
                    _issue(issues, "error", f"{path}.id", "热区编号重复。")
                zone_ids.add(zone_id)
            floor_id = zone.get("floor_id")
            if isinstance(floor_id, str) and floor_id not in floor_ids:
                _issue(issues, "error", f"{path}.floor_id", "引用的楼层不存在。")
            polygon = zone.get("polygon_xy")
            if isinstance(polygon, list):
                area = _polygon_area(polygon)
                if area is None:
                    _issue(
                        issues,
                        "error",
                        f"{path}.polygon_xy",
                        "必须包含至少三个有效的二维坐标点。",
                    )
                elif area <= 1e-8:
                    _issue(
                        issues,
                        "error",
                        f"{path}.polygon_xy",
                        "多边形面积必须大于0。",
                    )
            _check_numeric_range(
                zone.get("multiplier", 1),
                f"{path}.multiplier",
                issues,
                minimum=1,
            )
            _check_source(zone.get("source"), f"{path}.source", issues)

    conditioned_zone_ids = {
        str(zone.get("id"))
        for zone in zones or []
        if isinstance(zone, dict)
        and isinstance(zone.get("id"), str)
        and zone.get("conditioned") is True
    } if isinstance(zones, list) else set()

    materials = data.get("materials")
    material_ids: set[str] = set()
    if isinstance(materials, list):
        material_ids, _ = _validate_materials(materials, issues)

    constructions = data.get("constructions")
    construction_records: dict[str, dict[str, Any]] = {}
    if isinstance(constructions, list):
        _, construction_records = _validate_constructions(
            constructions, material_ids, issues
        )

    windows = data.get("windows")
    if isinstance(windows, list):
        _validate_windows(windows, zone_ids, construction_records, issues)

    schedules = data.get("schedules")
    schedule_ids: set[str] = set()
    if isinstance(schedules, list):
        schedule_ids = _validate_schedules(schedules, issues)

    loads = data.get("loads")
    if isinstance(loads, list):
        _validate_loads(loads, zone_ids, schedule_ids, issues)

    thermostats = data.get("thermostats")
    if isinstance(thermostats, list):
        _validate_thermostats(thermostats, zone_ids, schedule_ids, issues)

    hvac = data.get("hvac")
    if isinstance(hvac, dict) and hvac:
        _validate_hvac(
            hvac, zone_ids, conditioned_zone_ids, schedule_ids, issues
        )

    coating = data.get("coating")
    if isinstance(coating, dict) and coating:
        _validate_coating(coating, construction_records, issues)

    data_quality = data.get("data_quality")
    if isinstance(data_quality, dict):
        _validate_data_quality(data_quality, issues)

    if isinstance(hvac, dict) and not hvac:
        _issue(
            issues,
            "warning",
            "hvac",
            "尚未填写HVAC，当前只能准备建筑数据，不能计算实际制冷用电。",
        )
    if isinstance(coating, dict) and not coating:
        _issue(issues, "info", "coating", "尚未填写辐射制冷涂料参数。")

    return issues
