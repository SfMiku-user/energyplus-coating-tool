from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


METERS = (
    "Cooling:Electricity",
    "Fans:Electricity",
    "Pumps:Electricity",
    "HeatRejection:Electricity",
    "Heating:Electricity",
    "Electricity:Facility",
)


class ToolError(RuntimeError):
    """An error that can be shown directly to the user."""


@dataclass
class IDFObject:
    object_type: str
    fields: list[str]

    def clone(self) -> "IDFObject":
        return IDFObject(self.object_type, list(self.fields))


@dataclass
class RunResult:
    output_dir: Path
    meters_kwh: dict[str, float]
    peak_cooling_kw: float
    warnings: int
    severe_errors: int
    hourly_record_counts: dict[str, int] = field(default_factory=dict)


def _strip_comments(text: str) -> str:
    result: list[str] = []
    in_quote = False
    quote_char = ""
    for line in text.splitlines():
        kept: list[str] = []
        index = 0
        while index < len(line):
            char = line[index]
            if char in {'"', "'"}:
                if in_quote and char == quote_char:
                    in_quote = False
                elif not in_quote:
                    in_quote = True
                    quote_char = char
                kept.append(char)
            elif char == "!" and not in_quote:
                break
            else:
                kept.append(char)
            index += 1
        result.append("".join(kept))
    return "\n".join(result)


def _split_unquoted(text: str, separators: set[str]) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    quote_char = ""
    for char in text:
        if char in {'"', "'"}:
            if in_quote and char == quote_char:
                in_quote = False
            elif not in_quote:
                in_quote = True
                quote_char = char
            current.append(char)
        elif char in separators and not in_quote:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts


def parse_idf(path: Path) -> list[IDFObject]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    clean = _strip_comments(text)
    raw_objects = _split_unquoted(clean, {";"})
    objects: list[IDFObject] = []
    for raw in raw_objects:
        if not raw.strip():
            continue
        parts = _split_unquoted(raw, {","})
        if not parts or not parts[0]:
            continue
        objects.append(IDFObject(parts[0].strip(), [part.strip() for part in parts[1:]]))
    if not objects:
        raise ToolError("没有从 IDF 中解析到任何对象，请检查模型文件。")
    return objects


def write_idf(objects: Iterable[IDFObject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for obj in objects:
        if obj.fields:
            lines = [f"{obj.object_type},"]
            for index, value in enumerate(obj.fields):
                suffix = ";" if index == len(obj.fields) - 1 else ","
                lines.append(f"  {value}{suffix}")
            blocks.append("\n".join(lines))
        else:
            blocks.append(f"{obj.object_type};")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _normal(value: str) -> str:
    return value.strip().strip('"').strip("'").casefold()


def _object_name(obj: IDFObject) -> str:
    return obj.fields[0].strip() if obj.fields else ""


def _unique_name(base: str, used_names: set[str]) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff ]+", "_", base).strip()
    candidate = cleaned
    counter = 2
    while _normal(candidate) in used_names:
        candidate = f"{cleaned}_{counter}"
        counter += 1
    used_names.add(_normal(candidate))
    return candidate


def _ensure_length(fields: list[str], length: int) -> None:
    while len(fields) < length:
        fields.append("")


def _apply_optical_properties(
    material: IDFObject,
    solar_reflectance: float,
    thermal_emissivity: float,
) -> None:
    material_type = _normal(material.object_type)
    solar_absorptance = 1.0 - solar_reflectance
    visible_absorptance = solar_absorptance

    if material_type == "material":
        _ensure_length(material.fields, 9)
        material.fields[6] = f"{thermal_emissivity:.6f}"
        material.fields[7] = f"{solar_absorptance:.6f}"
        material.fields[8] = f"{visible_absorptance:.6f}"
    elif material_type == "material:nomass":
        _ensure_length(material.fields, 6)
        material.fields[3] = f"{thermal_emissivity:.6f}"
        material.fields[4] = f"{solar_absorptance:.6f}"
        material.fields[5] = f"{visible_absorptance:.6f}"
    else:
        raise ToolError(
            f"外层材料“{_object_name(material)}”的类型为 {material.object_type}，"
            "当前最小工具仅支持 Material 和 Material:NoMass。"
        )


def _is_target_surface(obj: IDFObject, target_roof: bool, target_wall: bool) -> bool:
    obj_type = _normal(obj.object_type)
    normalized_fields = {_normal(field) for field in obj.fields}

    if "outdoors" not in normalized_fields:
        return False
    if "nosun" in normalized_fields:
        return False

    if obj_type == "buildingsurface:detailed":
        if len(obj.fields) < 3:
            return False
        surface_type = _normal(obj.fields[1])
        return (target_roof and surface_type in {"roof", "roofceiling"}) or (
            target_wall and surface_type == "wall"
        )
    if target_roof and obj_type == "roofceiling:detailed":
        return True
    if target_wall and obj_type == "wall:detailed":
        return True
    return False


def _surface_construction_field(obj: IDFObject) -> int:
    if _normal(obj.object_type) == "buildingsurface:detailed":
        return 2
    return 1


def _detailed_surface_area_m2(obj: IDFObject) -> float | None:
    if _normal(obj.object_type) != "buildingsurface:detailed" or len(obj.fields) < 14:
        return None
    try:
        declared_count = int(float(obj.fields[10])) if obj.fields[10].strip() else 0
        raw_coordinates = obj.fields[11:]
        if declared_count >= 3 and len(raw_coordinates) >= 3 * declared_count:
            raw_coordinates = raw_coordinates[: 3 * declared_count]
        elif len(raw_coordinates) % 3 != 0:
            return None
        coordinates = [float(value) for value in raw_coordinates]
    except (ValueError, TypeError):
        return None
    vertex_count = len(coordinates) // 3
    if vertex_count < 3 or len(coordinates) != 3 * vertex_count:
        return None
    vertices = [
        coordinates[index : index + 3]
        for index in range(0, len(coordinates), 3)
    ]
    nx = ny = nz = 0.0
    for first, second in zip(vertices, vertices[1:] + vertices[:1]):
        nx += (first[1] - second[1]) * (first[2] + second[2])
        ny += (first[2] - second[2]) * (first[0] + second[0])
        nz += (first[0] - second[0]) * (first[1] + second[1])
    return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)


def _add_output_meters(objects: list[IDFObject]) -> None:
    existing = {
        (_normal(obj.fields[0]), _normal(obj.fields[1]) if len(obj.fields) > 1 else "")
        for obj in objects
        if _normal(obj.object_type) == "output:meter" and obj.fields
    }
    for meter in METERS:
        key = (_normal(meter), "hourly")
        if key not in existing:
            objects.append(IDFObject("Output:Meter", [meter, "Hourly"]))

    summary_objects = [
        obj
        for obj in objects
        if _normal(obj.object_type) == "output:table:summaryreports"
    ]
    summary_names = {
        _normal(field) for obj in summary_objects for field in obj.fields if field.strip()
    }
    if not summary_objects:
        objects.append(
            IDFObject(
                "Output:Table:SummaryReports",
                ["AnnualBuildingUtilityPerformanceSummary"],
            )
        )
    elif not summary_names.intersection(
        {"annualbuildingutilityperformancesummary", "allsummary"}
    ):
        summary_objects[0].fields.append("AnnualBuildingUtilityPerformanceSummary")

    sqlite_objects = [
        obj for obj in objects if _normal(obj.object_type) == "output:sqlite"
    ]
    if sqlite_objects:
        _ensure_length(sqlite_objects[0].fields, 1)
        sqlite_objects[0].fields[0] = "SimpleAndTabular"
    else:
        objects.append(IDFObject("Output:SQLite", ["SimpleAndTabular"]))


def prepare_models(
    source_idf: Path,
    work_dir: Path,
    solar_reflectance: float,
    thermal_emissivity: float,
    target_roof: bool,
    target_wall: bool,
    target_construction_ids: Iterable[str] | None = None,
    coverage_fraction: float = 1.0,
) -> tuple[Path, Path, dict[str, object]]:
    if not 0.0 <= solar_reflectance <= 1.0:
        raise ToolError("太阳反射率必须在 0～1 之间。")
    if not 0.0 <= thermal_emissivity <= 1.0:
        raise ToolError("长波发射率必须在 0～1 之间。")
    if not target_roof and not target_wall:
        raise ToolError("请至少选择屋顶或外墙中的一种涂覆对象。")
    if not 0.0 < coverage_fraction <= 1.0:
        raise ToolError("涂覆比例必须大于 0 且不超过 1。")
    if not math.isclose(coverage_fraction, 1.0, abs_tol=1e-9):
        raise ToolError(
            "阶段七当前只允许 100% 涂覆；部分涂覆需要拆分表面后才能保持物理含义。"
        )

    original = parse_idf(source_idf)
    baseline = [obj.clone() for obj in original]
    coating = [obj.clone() for obj in original]
    _add_output_meters(baseline)
    _add_output_meters(coating)

    target_id_values = (
        [str(item) for item in target_construction_ids]
        if target_construction_ids is not None
        else None
    )
    target_ids = (
        {_normal(item) for item in target_id_values}
        if target_id_values is not None
        else None
    )
    target_surfaces = []
    for obj in coating:
        if not _is_target_surface(obj, target_roof, target_wall):
            continue
        construction_index = _surface_construction_field(obj)
        if len(obj.fields) <= construction_index:
            continue
        if target_ids is not None and _normal(obj.fields[construction_index]) not in target_ids:
            continue
        target_surfaces.append(obj)
    if not target_surfaces:
        raise ToolError("模型中没有找到符合条件的室外日照屋顶或外墙。")

    constructions = {
        _normal(_object_name(obj)): obj
        for obj in coating
        if _normal(obj.object_type) == "construction" and obj.fields
    }
    materials = {
        _normal(_object_name(obj)): obj
        for obj in coating
        if _normal(obj.object_type) in {"material", "material:nomass"} and obj.fields
    }
    used_names = {
        _normal(_object_name(obj)) for obj in coating if _object_name(obj)
    }

    new_objects: list[IDFObject] = []
    cloned_constructions: dict[str, str] = {}
    cloned_materials: dict[str, str] = {}
    target_surface_names: list[str] = []
    coated_surface_area_m2 = 0.0

    for surface in target_surfaces:
        target_surface_names.append(_object_name(surface))
        area = _detailed_surface_area_m2(surface)
        if area is not None:
            coated_surface_area_m2 += area
        construction_index = _surface_construction_field(surface)
        if len(surface.fields) <= construction_index:
            raise ToolError(f"表面“{_object_name(surface)}”缺少构造名称。")
        old_construction_name = surface.fields[construction_index].strip()
        construction_key = _normal(old_construction_name)

        if construction_key not in constructions:
            raise ToolError(f"找不到构造“{old_construction_name}”。")

        if construction_key not in cloned_constructions:
            old_construction = constructions[construction_key]
            if len(old_construction.fields) < 2:
                raise ToolError(f"构造“{old_construction_name}”没有外层材料。")
            old_material_name = old_construction.fields[1].strip()
            material_key = _normal(old_material_name)
            if material_key not in materials:
                raise ToolError(
                    f"构造“{old_construction_name}”的外层材料“{old_material_name}”"
                    "不是可修改的 Material 或 Material:NoMass。"
                )

            if material_key not in cloned_materials:
                new_material = materials[material_key].clone()
                new_material_name = _unique_name(
                    f"RC_Coating_{old_material_name}", used_names
                )
                new_material.fields[0] = new_material_name
                _apply_optical_properties(
                    new_material, solar_reflectance, thermal_emissivity
                )
                new_objects.append(new_material)
                cloned_materials[material_key] = new_material_name

            new_construction = old_construction.clone()
            new_construction_name = _unique_name(
                f"RC_{old_construction_name}", used_names
            )
            new_construction.fields[0] = new_construction_name
            new_construction.fields[1] = cloned_materials[material_key]
            new_objects.append(new_construction)
            cloned_constructions[construction_key] = new_construction_name

        surface.fields[construction_index] = cloned_constructions[construction_key]

    coating.extend(new_objects)
    work_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = work_dir / "baseline.idf"
    coating_path = work_dir / "coating.idf"
    write_idf(baseline, baseline_path)
    write_idf(coating, coating_path)

    details: dict[str, object] = {
        "target_surface_count": len(target_surfaces),
        "modified_construction_count": len(cloned_constructions),
        "modified_material_count": len(cloned_materials),
        "target_surface_names": target_surface_names,
        "coated_surface_area_m2": coated_surface_area_m2,
        "target_construction_ids": sorted(target_id_values or []),
        "cloned_constructions": dict(sorted(cloned_constructions.items())),
        "cloned_materials": dict(sorted(cloned_materials.items())),
        "solar_reflectance": solar_reflectance,
        "solar_absorptance": round(1.0 - solar_reflectance, 6),
        "thermal_emissivity": thermal_emissivity,
        "target_roof": target_roof,
        "target_wall": target_wall,
        "coverage_fraction": coverage_fraction,
        "representation": "thin_film_optical_properties",
    }
    return baseline_path, coating_path, details


def _parse_error_file(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    text = path.read_text(encoding="utf-8", errors="replace")
    warnings = len(re.findall(r"\*\* Warning \*\*", text, flags=re.IGNORECASE))
    severe = len(re.findall(r"\*\* Severe  \*\*", text, flags=re.IGNORECASE))
    severe += len(re.findall(r"\*\*  Fatal  \*\*", text, flags=re.IGNORECASE))
    return warnings, severe


def _find_meter_column(fieldnames: list[str], meter_name: str) -> str | None:
    wanted = meter_name.casefold()
    for field in fieldnames:
        normalized = field.strip().casefold()
        if normalized.startswith(wanted) and "[j]" in normalized:
            return field
    return None


def _read_peak_from_meter_csv(path: Path) -> float:
    if not path.exists():
        raise ToolError(
            f"没有生成 {path.name}。请确认模型包含 Output:Meter，且运行时使用了 -r。"
        )
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames or not rows:
        raise ToolError(f"结果文件为空：{path}")

    column = _find_meter_column(fieldnames, "Cooling:Electricity")
    if column is None:
        return 0.0
    values_j: list[float] = []
    for row in rows:
        raw = (row.get(column) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            values_j.append(value)
    return 0.0 if not values_j else max(values_j) / 3_600_000.0


def _energy_to_kwh(value: float, units: str) -> float:
    normalized = units.strip().casefold().replace(" ", "")
    factors = {
        "j": 1.0 / 3_600_000.0,
        "kj": 1.0 / 3_600.0,
        "mj": 1.0 / 3.6,
        "gj": 1_000.0 / 3.6,
        "wh": 0.001,
        "kwh": 1.0,
        "mwh": 1_000.0,
        "kbtu": 0.29307107,
    }
    if normalized not in factors:
        raise ToolError(f"暂不支持的年度能耗单位：{units}")
    return value * factors[normalized]


def _read_annual_results_from_sql(
    path: Path,
) -> tuple[dict[str, float], float, dict[str, int]]:
    if not path.exists():
        raise ToolError(f"没有生成 SQLite 结果文件：{path}")
    row_to_meter = {
        "cooling": "Cooling:Electricity",
        "fans": "Fans:Electricity",
        "pumps": "Pumps:Electricity",
        "heat rejection": "HeatRejection:Electricity",
        "heating": "Heating:Electricity",
        "total end uses": "Electricity:Facility",
    }
    meters = {name: 0.0 for name in METERS}
    try:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute(
                """
                SELECT RowName, Value, Units
                FROM TabularDataWithStrings
                WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
                  AND TableName = 'End Uses'
                  AND ColumnName = 'Electricity'
                """
            ).fetchall()
            if not rows:
                raise ToolError("SQLite 中没有找到年度 End Uses 电力汇总表。")
            for row_name, value, units in rows:
                meter = row_to_meter.get(str(row_name).strip().casefold())
                if meter is not None and value is not None:
                    meters[meter] = _energy_to_kwh(float(value), str(units))

            precise_rows = connection.execute(
                """
                SELECT rdd.Name, rdd.Units, COUNT(rd.Value),
                       SUM(rd.Value), MAX(rd.Value)
                FROM ReportData AS rd
                JOIN ReportDataDictionary AS rdd
                  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
                JOIN Time AS t ON rd.TimeIndex = t.TimeIndex
                JOIN EnvironmentPeriods AS ep
                  ON t.EnvironmentPeriodIndex = ep.EnvironmentPeriodIndex
                WHERE rdd.Name IN (
                    'Cooling:Electricity',
                    'Fans:Electricity',
                    'Pumps:Electricity',
                    'HeatRejection:Electricity',
                    'Heating:Electricity',
                    'Electricity:Facility'
                )
                  AND rdd.ReportingFrequency = 'Hourly'
                  AND ep.EnvironmentType = 3
                GROUP BY rdd.Name, rdd.Units
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise ToolError(f"读取 EnergyPlus SQLite 结果失败：{exc}") from exc

    peak_kw = 0.0
    hourly_counts: dict[str, int] = {}
    for name, units, count, total, maximum in precise_rows:
        meter_name = str(name)
        hourly_counts[meter_name] = int(count)
        if total is not None:
            meters[meter_name] = _energy_to_kwh(float(total), str(units))
        if meter_name == "Cooling:Electricity" and maximum is not None:
            peak_kw = _energy_to_kwh(float(maximum), str(units))
    return meters, peak_kw, hourly_counts


def run_energyplus(
    executable: Path,
    weather: Path,
    model: Path,
    output_dir: Path,
) -> RunResult:
    if not executable.exists():
        raise ToolError(f"找不到 EnergyPlus 程序：{executable}")
    if not weather.exists():
        raise ToolError(f"找不到气象文件：{weather}")
    if not model.exists():
        raise ToolError(f"找不到模型文件：{model}")

    if output_dir.exists():
        raise ToolError(f"本次运行目录已经存在，请更换运行目录：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    command = [
        str(executable),
        "-w",
        str(weather),
        "-d",
        str(output_dir),
        "-r",
        str(model),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        cwd=model.parent,
    )
    error_file = output_dir / "eplusout.err"
    warnings, severe = _parse_error_file(error_file)
    if completed.returncode != 0 or severe:
        error_excerpt = ""
        if error_file.exists():
            lines = error_file.read_text(encoding="utf-8", errors="replace").splitlines()
            error_excerpt = "\n".join(lines[-30:])
        raise ToolError(
            f"EnergyPlus 运行失败（返回码 {completed.returncode}）。\n"
            f"{error_excerpt or completed.stderr or completed.stdout}"
        )

    meters, peak, hourly_counts = _read_annual_results_from_sql(
        output_dir / "eplusout.sql"
    )
    if peak <= 0:
        peak = _read_peak_from_meter_csv(output_dir / "eplusmtr.csv")
    return RunResult(
        output_dir,
        meters,
        peak,
        warnings,
        severe,
        hourly_counts,
    )


def _saving_percent(baseline: float, coating: float) -> float | None:
    if baseline <= 0:
        return None
    return (baseline - coating) / baseline * 100.0


def compare_results(
    baseline: RunResult,
    coating: RunResult,
    details: dict[str, object],
) -> dict[str, object]:
    base = baseline.meters_kwh
    coat = coating.meters_kwh

    auxiliary_names = (
        "Cooling:Electricity",
        "Fans:Electricity",
        "Pumps:Electricity",
        "HeatRejection:Electricity",
    )
    baseline_system = sum(base.get(name, 0.0) for name in auxiliary_names)
    coating_system = sum(coat.get(name, 0.0) for name in auxiliary_names)

    metrics = {
        "cooling_electricity_kwh": {
            "baseline": base.get("Cooling:Electricity", 0.0),
            "coating": coat.get("Cooling:Electricity", 0.0),
            "saving_percent": _saving_percent(
                base.get("Cooling:Electricity", 0.0),
                coat.get("Cooling:Electricity", 0.0),
            ),
        },
        "cooling_system_electricity_kwh": {
            "baseline": baseline_system,
            "coating": coating_system,
            "saving_percent": _saving_percent(baseline_system, coating_system),
        },
        "facility_electricity_kwh": {
            "baseline": base.get("Electricity:Facility", 0.0),
            "coating": coat.get("Electricity:Facility", 0.0),
            "saving_percent": _saving_percent(
                base.get("Electricity:Facility", 0.0),
                coat.get("Electricity:Facility", 0.0),
            ),
        },
        "heating_electricity_kwh": {
            "baseline": base.get("Heating:Electricity", 0.0),
            "coating": coat.get("Heating:Electricity", 0.0),
            "change_percent": (
                None
                if base.get("Heating:Electricity", 0.0) <= 0
                else (
                    coat.get("Heating:Electricity", 0.0)
                    - base.get("Heating:Electricity", 0.0)
                )
                / base.get("Heating:Electricity", 0.0)
                * 100.0
            ),
        },
        "peak_cooling_kw": {
            "baseline": baseline.peak_cooling_kw,
            "coating": coating.peak_cooling_kw,
            "saving_percent": _saving_percent(
                baseline.peak_cooling_kw, coating.peak_cooling_kw
            ),
        },
    }
    return {
        "model_details": details,
        "metrics": metrics,
        "hourly_record_counts": {
            "baseline": baseline.hourly_record_counts,
            "coating": coating.hourly_record_counts,
        },
        "warnings": {
            "baseline": baseline.warnings,
            "coating": coating.warnings,
        },
    }


def write_result_files(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "comparison_results.json"
    csv_path = output_dir / "comparison_results.csv"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    labels = {
        "cooling_electricity_kwh": "制冷主设备用电量 (kWh)",
        "cooling_system_electricity_kwh": "制冷系统综合用电量 (kWh)",
        "facility_electricity_kwh": "建筑总用电量 (kWh)",
        "heating_electricity_kwh": "供暖用电量 (kWh)",
        "peak_cooling_kw": "制冷峰值功率 (kW)",
    }
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["指标", "基准工况", "涂层工况", "变化率 (%)"])
        metrics = result["metrics"]
        assert isinstance(metrics, dict)
        for key, values in metrics.items():
            assert isinstance(values, dict)
            percent = values.get("saving_percent", values.get("change_percent"))
            writer.writerow(
                [
                    labels.get(key, key),
                    f"{float(values['baseline']):.6f}",
                    f"{float(values['coating']):.6f}",
                    "" if percent is None else f"{float(percent):.6f}",
                ]
            )
    return json_path, csv_path
