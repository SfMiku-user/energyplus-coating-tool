"""阶段六几何质量检查；不依赖 OpenStudio，便于单元测试。"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def _quantize(value: float, tolerance: float) -> int:
    return int(round(float(value) / tolerance))


def _polygon_key(
    vertices: list[list[float]], coordinate_tolerance_m: float
) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        sorted(
            (
                _quantize(point[0], coordinate_tolerance_m),
                _quantize(point[1], coordinate_tolerance_m),
                _quantize(point[2], coordinate_tolerance_m),
            )
            for point in vertices
        )
    )


def _normal(vertices: list[list[float]]) -> tuple[float, float, float]:
    nx = ny = nz = 0.0
    for first, second in zip(vertices, vertices[1:] + vertices[:1]):
        nx += (first[1] - second[1]) * (first[2] + second[2])
        ny += (first[2] - second[2]) * (first[0] + second[0])
        nz += (first[0] - second[0]) * (first[1] + second[1])
    return nx, ny, nz


def _project(vertices: list[list[float]]) -> list[tuple[float, float]]:
    normal = _normal(vertices)
    drop_axis = max(range(3), key=lambda index: abs(normal[index]))
    axes = [index for index in range(3) if index != drop_axis]
    return [(float(point[axes[0]]), float(point[axes[1]])) for point in vertices]


def _point_on_segment(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
    tolerance: float,
) -> bool:
    px, py = point
    x1, y1 = first
    x2, y2 = second
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    length = math.hypot(x2 - x1, y2 - y1)
    if abs(cross) > tolerance * max(1.0, length):
        return False
    return (
        min(x1, x2) - tolerance <= px <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= py <= max(y1, y2) + tolerance
    )


def _point_in_polygon(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
    tolerance: float,
) -> bool:
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        if _point_on_segment(point, first, second, tolerance):
            return True
    inside = False
    px, py = point
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > py) != (y2 > py):
            crossing_x = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            if px < crossing_x:
                inside = not inside
        previous = current
    return inside


def assess_geometry(
    *,
    declared_floor_area_m2: float | None,
    input_floor_area_m2: float,
    spaces: list[dict[str, Any]],
    surfaces: list[dict[str, Any]],
    subsurfaces: list[dict[str, Any]],
    floor_area_tolerance_m2: float = 0.01,
    coordinate_tolerance_m: float = 1e-5,
) -> dict[str, Any]:
    """返回完整的阶段六验收报告。"""

    if floor_area_tolerance_m2 <= 0 or coordinate_tolerance_m <= 0:
        raise ValueError("几何验收容差必须大于 0。")

    model_floor_area = sum(
        float(space["floor_area_m2"])
        * float(space.get("space_multiplier", 1))
        * float(space.get("zone_multiplier", 1))
        for space in spaces
    )
    input_model_error = abs(model_floor_area - float(input_floor_area_m2))
    declared_model_error = (
        abs(model_floor_area - float(declared_floor_area_m2))
        if declared_floor_area_m2 is not None
        else None
    )

    surface_by_name = {str(item["name"]): item for item in surfaces}
    groups: dict[tuple[tuple[int, int, int], ...], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for surface in surfaces:
        groups[
            _polygon_key(surface["vertices"], coordinate_tolerance_m)
        ].append(surface)

    unmatched_internal: list[dict[str, Any]] = []
    for matching in groups.values():
        spaces_in_group = {str(item.get("space_name", "")) for item in matching}
        if len(matching) < 2 or len(spaces_in_group) < 2:
            continue
        unpaired = [
            item
            for item in matching
            if str(item.get("outside_boundary_condition", "")) != "Surface"
            or not item.get("adjacent_surface_name")
        ]
        if unpaired:
            unmatched_internal.append(
                {
                    "surface_names": sorted(str(item["name"]) for item in matching),
                    "space_names": sorted(spaces_in_group),
                }
            )

    duplicate_groups: list[dict[str, Any]] = []
    spaces_by_footprint: dict[
        tuple[tuple[int, int, int], ...], list[str]
    ] = defaultdict(list)
    for space in spaces:
        spaces_by_footprint[
            _polygon_key(space["floor_vertices"], coordinate_tolerance_m)
        ].append(str(space["name"]))
    for names in spaces_by_footprint.values():
        if len(names) > 1:
            duplicate_groups.append({"space_names": sorted(names)})
    duplicate_space_count = sum(
        len(item["space_names"]) - 1 for item in duplicate_groups
    )

    negative_surfaces = [
        {
            "surface_name": str(surface["name"]),
            "gross_area_m2": float(surface["gross_area_m2"]),
            "net_area_m2": float(surface["net_area_m2"]),
        }
        for surface in surfaces
        if float(surface["gross_area_m2"]) < -floor_area_tolerance_m2
        or float(surface["net_area_m2"]) < -floor_area_tolerance_m2
    ]

    windows_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for window in subsurfaces:
        windows_by_parent[str(window.get("parent_surface_name", ""))].append(window)
    invalid_windows: list[dict[str, Any]] = []
    for parent_name, windows in windows_by_parent.items():
        parent = surface_by_name.get(parent_name)
        if parent is None:
            invalid_windows.extend(
                {
                    "window_name": str(window["name"]),
                    "parent_surface_name": parent_name,
                    "reason": "找不到所属墙面",
                }
                for window in windows
            )
            continue
        parent_polygon = _project(parent["vertices"])
        total_window_area = sum(float(item["gross_area_m2"]) for item in windows)
        if total_window_area > float(parent["gross_area_m2"]) + floor_area_tolerance_m2:
            invalid_windows.append(
                {
                    "window_name": ";".join(str(item["name"]) for item in windows),
                    "parent_surface_name": parent_name,
                    "reason": "窗总面积超过墙面面积",
                }
            )
        for window in windows:
            projected = _project(window["vertices"])
            if not all(
                _point_in_polygon(point, parent_polygon, coordinate_tolerance_m)
                for point in projected
            ):
                invalid_windows.append(
                    {
                        "window_name": str(window["name"]),
                        "parent_surface_name": parent_name,
                        "reason": "窗顶点超出墙面边界",
                    }
                )

    floor_keys = {
        _polygon_key(item["vertices"], coordinate_tolerance_m)
        for item in surfaces
        if str(item.get("surface_type", "")) == "Floor"
    }
    intermediate_roofs = [
        {
            "surface_name": str(item["name"]),
            "space_name": str(item.get("space_name", "")),
        }
        for item in surfaces
        if str(item.get("surface_type", "")) == "RoofCeiling"
        and str(item.get("outside_boundary_condition", "")) == "Outdoors"
        and _polygon_key(item["vertices"], coordinate_tolerance_m) in floor_keys
    ]

    checks = {
        "unmatched_internal_surfaces": {
            "passed": not unmatched_internal,
            "count": len(unmatched_internal),
        },
        "total_floor_area": {
            "passed": input_model_error <= floor_area_tolerance_m2
            and (
                declared_model_error is None
                or declared_model_error <= floor_area_tolerance_m2
            ),
            "input_model_error_m2": input_model_error,
            "declared_model_error_m2": declared_model_error,
        },
        "duplicate_spaces": {
            "passed": duplicate_space_count == 0,
            "count": duplicate_space_count,
        },
        "negative_area_surfaces": {
            "passed": not negative_surfaces,
            "count": len(negative_surfaces),
        },
        "windows_within_walls": {
            "passed": not invalid_windows,
            "count": len(invalid_windows),
        },
        "intermediate_floors_not_exterior_roofs": {
            "passed": not intermediate_roofs,
            "count": len(intermediate_roofs),
        },
    }
    all_passed = all(bool(item["passed"]) for item in checks.values())
    return {
        "schema_version": "1.0",
        "stage": 6,
        "status": "passed" if all_passed else "failed",
        "all_passed": all_passed,
        "tolerances": {
            "floor_area_m2": floor_area_tolerance_m2,
            "coordinate_m": coordinate_tolerance_m,
        },
        "areas": {
            "declared_floor_area_m2": declared_floor_area_m2,
            "input_polygon_floor_area_m2": input_floor_area_m2,
            "model_floor_area_m2": model_floor_area,
        },
        "checks": checks,
        "details": {
            "unmatched_internal_surfaces": unmatched_internal,
            "duplicate_spaces": duplicate_groups,
            "negative_area_surfaces": negative_surfaces,
            "invalid_windows": invalid_windows,
            "intermediate_exterior_roofs": intermediate_roofs,
        },
    }
