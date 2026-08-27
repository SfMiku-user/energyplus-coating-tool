"""第一版建筑项目数据规范。

本文件只描述与输入数据结构有关的稳定约定。OpenStudio 和 EnergyPlus
对象的生成规则将在后续模块中实现。
"""

from __future__ import annotations


SCHEMA_VERSION = "1.0"

PROJECT_SECTIONS = (
    "project",
    "site",
    "floors",
    "zones",
    "windows",
    "materials",
    "constructions",
    "schedules",
    "loads",
    "thermostats",
    "hvac",
    "coating",
    "data_quality",
)

OBJECT_SECTIONS = {
    "project",
    "site",
    "hvac",
    "coating",
    "data_quality",
}

LIST_SECTIONS = set(PROJECT_SECTIONS) - OBJECT_SECTIONS

SOURCE_TYPES = {
    "measured",
    "drawing",
    "design_drawing",
    "equipment_manual",
    "operation_record",
    "standard_default",
    "assumption",
    "calibrated",
}

REQUIRED_PROJECT_FIELDS = {
    "name": str,
    "building_type": str,
}

REQUIRED_SITE_FIELDS = {
    "latitude": (int, float),
    "longitude": (int, float),
    "time_zone": (int, float),
    "elevation_m": (int, float),
    "north_axis_deg": (int, float),
}

REQUIRED_FLOOR_FIELDS = {
    "id": str,
    "elevation_m": (int, float),
    "height_m": (int, float),
}

REQUIRED_ZONE_FIELDS = {
    "id": str,
    "floor_id": str,
    "name": str,
    "polygon_xy": list,
    "conditioned": bool,
    "space_type": str,
}


def new_project_dict() -> dict[str, object]:
    """返回一个包含全部固定章节的空项目。"""

    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
    }
    for section in PROJECT_SECTIONS:
        data[section] = {} if section in OBJECT_SECTIONS else []
    return data
