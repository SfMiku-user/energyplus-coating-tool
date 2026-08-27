"""阶段七：由项目 JSON 生成并验收基准/辐射制冷涂层成对模型。"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine import (
    IDFObject,
    ToolError,
    _normal,
    _object_name,
    _surface_construction_field,
    parse_idf,
    prepare_models,
    run_energyplus,
)

from .idf_versioning import read_energyplus_version, read_idf_version
from .project import load_project
from .validation import format_validation_report, validate_project


class CoatingBuildError(RuntimeError):
    """阶段七输入、模型生成或验收失败。"""


@dataclass(frozen=True)
class CoatingBuildResult:
    output_dir: Path
    baseline_idf_path: Path
    coating_idf_path: Path
    validation_path: Path
    manifest_path: Path
    validation: dict[str, Any]
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _select_active_scenario(data: dict[str, Any]) -> dict[str, Any]:
    coating = data.get("coating")
    if not isinstance(coating, dict) or not coating:
        raise CoatingBuildError("项目 JSON 中没有可用的涂料方案。")
    scenarios = coating.get("scenarios")
    if not isinstance(scenarios, list):
        return coating
    active_id = str(
        coating.get("active_scenario_id") or coating.get("id") or ""
    ).strip()
    for scenario in scenarios:
        if isinstance(scenario, dict) and str(scenario.get("id", "")) == active_id:
            return scenario
    raise CoatingBuildError(f"找不到启用的涂料方案：{active_id or '未填写'}。")


def _target_flags(
    scenario: dict[str, Any], constructions: list[dict[str, Any]]
) -> tuple[bool, bool]:
    target_text = str(scenario.get("target_type", "")).casefold()
    target_roof = "屋顶" in target_text or "roof" in target_text
    target_wall = "外墙" in target_text or "wall" in target_text
    if target_roof or target_wall:
        return target_roof, target_wall
    uses = {
        str(item.get("id", "")): str(item.get("use", "")).casefold()
        for item in constructions
        if isinstance(item, dict)
    }
    for construction_id in scenario.get("target_construction_ids", []):
        use = uses.get(str(construction_id), "")
        target_roof = target_roof or "屋顶" in use or "roof" in use
        target_wall = target_wall or "外墙" in use or "wall" in use
    if not target_roof and not target_wall:
        raise CoatingBuildError("无法从涂料方案判断涂覆屋顶还是外墙。")
    return target_roof, target_wall


def _material_optical_values(material: IDFObject) -> tuple[float, float] | None:
    material_type = _normal(material.object_type)
    try:
        if material_type == "material":
            return float(material.fields[6]), float(material.fields[7])
        if material_type == "material:nomass":
            return float(material.fields[3]), float(material.fields[4])
    except (IndexError, ValueError):
        return None
    return None


def validate_coating_pair(
    baseline_path: Path,
    coating_path: Path,
    details: dict[str, Any],
    *,
    expected_reflectance: float,
    expected_emissivity: float,
    source_hash_before: str,
    source_hash_after: str,
) -> dict[str, Any]:
    baseline = parse_idf(baseline_path)
    coating = parse_idf(coating_path)
    target_names = {_normal(name) for name in details["target_surface_names"]}

    unexpected_changes: list[dict[str, Any]] = []
    changed_target_surfaces: list[str] = []
    geometry_mismatches: list[str] = []
    for index, base_object in enumerate(baseline):
        if index >= len(coating):
            unexpected_changes.append({"index": index, "reason": "涂层模型缺少对象"})
            continue
        coated_object = coating[index]
        if base_object.object_type != coated_object.object_type:
            unexpected_changes.append({"index": index, "reason": "对象类型改变"})
            continue
        if base_object.fields == coated_object.fields:
            continue
        is_surface = _normal(base_object.object_type) in {
            "buildingsurface:detailed",
            "roofceiling:detailed",
            "wall:detailed",
        }
        is_target = is_surface and _normal(_object_name(base_object)) in target_names
        if not is_target:
            unexpected_changes.append(
                {"index": index, "object_name": _object_name(base_object)}
            )
            continue
        construction_index = _surface_construction_field(base_object)
        base_without_construction = list(base_object.fields)
        coat_without_construction = list(coated_object.fields)
        base_without_construction[construction_index] = ""
        coat_without_construction[construction_index] = ""
        if base_without_construction != coat_without_construction:
            geometry_mismatches.append(_object_name(base_object))
        else:
            changed_target_surfaces.append(_object_name(base_object))

    extra_objects = coating[len(baseline) :]
    allowed_extra_types = {"material", "material:nomass", "construction"}
    invalid_extra_objects = [
        _object_name(item)
        for item in extra_objects
        if _normal(item.object_type) not in allowed_extra_types
    ]
    expected_extra_count = int(details["modified_construction_count"]) + int(
        details["modified_material_count"]
    )

    coating_materials = {
        _normal(_object_name(item)): item
        for item in coating
        if _normal(item.object_type) in {"material", "material:nomass"}
    }
    invalid_materials: list[str] = []
    for new_name in details["cloned_materials"].values():
        material = coating_materials.get(_normal(str(new_name)))
        values = _material_optical_values(material) if material else None
        if values is None or not (
            math.isclose(values[0], expected_emissivity, abs_tol=5e-7)
            and math.isclose(values[1], 1.0 - expected_reflectance, abs_tol=5e-7)
        ):
            invalid_materials.append(str(new_name))

    construction_names = [
        _normal(_object_name(item))
        for item in coating
        if _normal(item.object_type) == "construction"
    ]
    material_names = [
        _normal(_object_name(item))
        for item in coating
        if _normal(item.object_type) in {"material", "material:nomass"}
    ]
    duplicate_names = sorted(
        {
            name
            for names in (construction_names, material_names)
            for name in names
            if names.count(name) > 1
        }
    )

    checks = {
        "source_baseline_unchanged": {
            "passed": source_hash_before == source_hash_after,
            "sha256_before": source_hash_before,
            "sha256_after": source_hash_after,
        },
        "target_surfaces_found": {
            "passed": int(details["target_surface_count"]) > 0,
            "count": int(details["target_surface_count"]),
        },
        "only_target_surface_constructions_changed": {
            "passed": not unexpected_changes
            and len(changed_target_surfaces) == int(details["target_surface_count"]),
            "changed_count": len(changed_target_surfaces),
        },
        "geometry_unchanged": {
            "passed": not geometry_mismatches,
            "mismatch_count": len(geometry_mismatches),
        },
        "coating_optical_properties": {
            "passed": not invalid_materials,
            "invalid_material_count": len(invalid_materials),
        },
        "only_expected_objects_added": {
            "passed": len(extra_objects) == expected_extra_count
            and not invalid_extra_objects,
            "added_count": len(extra_objects),
            "expected_added_count": expected_extra_count,
        },
        "unique_material_and_construction_names": {
            "passed": not duplicate_names,
            "duplicate_count": len(duplicate_names),
        },
        "full_surface_coverage": {
            "passed": math.isclose(
                float(details["coverage_fraction"]), 1.0, abs_tol=1e-9
            ),
            "coverage_fraction": float(details["coverage_fraction"]),
        },
    }
    passed = all(bool(item["passed"]) for item in checks.values())
    return {
        "schema_version": "1.0",
        "stage": 7,
        "status": "passed" if passed else "failed",
        "all_passed": passed,
        "checks": checks,
        "target_surface_names": details["target_surface_names"],
        "coated_surface_area_m2": details["coated_surface_area_m2"],
        "details": {
            "unexpected_changes": unexpected_changes,
            "geometry_mismatches": geometry_mismatches,
            "invalid_materials": invalid_materials,
            "invalid_extra_objects": invalid_extra_objects,
            "duplicate_names": duplicate_names,
        },
    }


def _run_pair_validation(
    energyplus: Path,
    weather: Path,
    baseline: Path,
    coating: Path,
    destination: Path,
) -> dict[str, Any]:
    drive = energyplus.drive or Path.cwd().drive or "C:"
    staging_parent = Path(f"{drive}\\OpenStudio-Coating-Temp")
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="stage7_", dir=staging_parent))
    try:
        staging_baseline = staging_root / "baseline.idf"
        staging_coating = staging_root / "coating.idf"
        staging_weather = staging_root / "weather.epw"
        shutil.copy2(baseline, staging_baseline)
        shutil.copy2(coating, staging_coating)
        shutil.copy2(weather, staging_weather)
        baseline_result = run_energyplus(
            energyplus,
            staging_weather,
            staging_baseline,
            staging_root / "baseline_run",
        )
        coating_result = run_energyplus(
            energyplus,
            staging_weather,
            staging_coating,
            staging_root / "coating_run",
        )
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copytree(staging_root / "baseline_run", destination / "baseline")
        shutil.copytree(staging_root / "coating_run", destination / "coating")
        return {
            "baseline": {
                "warnings": baseline_result.warnings,
                "severe_errors": baseline_result.severe_errors,
                "meters_kwh": baseline_result.meters_kwh,
                "peak_cooling_kw": baseline_result.peak_cooling_kw,
                "hourly_record_counts": baseline_result.hourly_record_counts,
            },
            "coating": {
                "warnings": coating_result.warnings,
                "severe_errors": coating_result.severe_errors,
                "meters_kwh": coating_result.meters_kwh,
                "peak_cooling_kw": coating_result.peak_cooling_kw,
                "hourly_record_counts": coating_result.hourly_record_counts,
            },
        }
    except ToolError as exc:
        raise CoatingBuildError(str(exc)) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def build_coating_scenario(
    project_json: Path | str,
    baseline_idf: Path | str,
    output_dir: Path | str,
    *,
    energyplus_executable: Path | str | None = None,
    weather_file: Path | str | None = None,
) -> CoatingBuildResult:
    """生成阶段七成对模型；提供 EnergyPlus 和 EPW 时同时执行双模型验收。"""

    project_path = Path(project_json)
    source_baseline = Path(baseline_idf)
    destination = Path(output_dir)
    if not source_baseline.is_file():
        raise CoatingBuildError(f"找不到阶段六基准 IDF：{source_baseline}")
    if destination.exists() and any(destination.iterdir()):
        raise CoatingBuildError(f"阶段七输出目录必须为空：{destination}")
    destination.mkdir(parents=True, exist_ok=True)

    phase6_validation = None
    phase6_validation_path = source_baseline.parent / "geometry_validation.json"
    if phase6_validation_path.is_file():
        try:
            phase6_validation = json.loads(
                phase6_validation_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise CoatingBuildError("无法读取阶段六几何验收报告。") from exc
        if phase6_validation.get("all_passed") is not True:
            raise CoatingBuildError("阶段六几何验收未通过，禁止生成涂层模型。")

    project = load_project(project_path)
    errors = [
        issue for issue in validate_project(project) if issue.severity == "error"
    ]
    if errors:
        raise CoatingBuildError(
            "项目数据校验失败，未生成涂层模型。\n"
            + format_validation_report(errors)
        )
    data = project.data
    scenario = _select_active_scenario(data)
    target_ids = [str(item) for item in scenario.get("target_construction_ids", [])]
    if not target_ids:
        raise CoatingBuildError("启用的涂料方案没有目标构造编号。")
    target_roof, target_wall = _target_flags(scenario, data["constructions"])

    source_hash_before = _sha256(source_baseline)
    try:
        baseline_path, coating_path, details = prepare_models(
            source_baseline,
            destination,
            solar_reflectance=float(scenario["solar_reflectance"]),
            thermal_emissivity=float(scenario["thermal_emissivity"]),
            target_roof=target_roof,
            target_wall=target_wall,
            target_construction_ids=target_ids,
            coverage_fraction=float(scenario.get("coverage_fraction", 1.0)),
        )
    except ToolError as exc:
        raise CoatingBuildError(str(exc)) from exc
    source_hash_after = _sha256(source_baseline)
    validation = validate_coating_pair(
        baseline_path,
        coating_path,
        details,
        expected_reflectance=float(scenario["solar_reflectance"]),
        expected_emissivity=float(scenario["thermal_emissivity"]),
        source_hash_before=source_hash_before,
        source_hash_after=source_hash_after,
    )
    validation["checks"]["phase6_geometry_validation"] = {
        "passed": phase6_validation is None
        or phase6_validation.get("all_passed") is True,
        "available": phase6_validation is not None,
        "status": (
            phase6_validation.get("status")
            if phase6_validation is not None
            else "not_available"
        ),
    }
    validation["all_passed"] = all(
        bool(item["passed"]) for item in validation["checks"].values()
    )
    validation["status"] = "passed" if validation["all_passed"] else "failed"
    validation_path = destination / "coating_validation.json"
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not validation["all_passed"]:
        failed = [
            name
            for name, check in validation["checks"].items()
            if not check["passed"]
        ]
        raise CoatingBuildError("阶段七模型验收失败：" + ";".join(failed))

    energyplus_validation = None
    if (energyplus_executable is None) != (weather_file is None):
        raise CoatingBuildError("EnergyPlus 程序和 EPW 必须同时提供或同时省略。")
    if energyplus_executable is not None and weather_file is not None:
        energyplus = Path(energyplus_executable)
        weather = Path(weather_file)
        if not energyplus.is_file() or not weather.is_file():
            raise CoatingBuildError("EnergyPlus 程序或 EPW 文件不存在。")
        idf_version = read_idf_version(baseline_path)
        energyplus_version = read_energyplus_version(energyplus)
        if (idf_version.major, idf_version.minor) != (
            energyplus_version.major,
            energyplus_version.minor,
        ):
            raise CoatingBuildError(
                f"阶段七 IDF 为 {idf_version.idf_text}，"
                f"EnergyPlus 为 {energyplus_version.idf_text}。"
            )
        try:
            energyplus_validation = _run_pair_validation(
                energyplus,
                weather,
                baseline_path,
                coating_path,
                destination / "smoke_test",
            )
        except CoatingBuildError as exc:
            validation["checks"]["energyplus_pair_validation"] = {
                "passed": False,
                "message": str(exc),
            }
            validation["all_passed"] = False
            validation["status"] = "failed"
            validation_path.write_text(
                json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            raise
        validation["checks"]["energyplus_pair_validation"] = {
            "passed": energyplus_validation["baseline"]["severe_errors"] == 0
            and energyplus_validation["coating"]["severe_errors"] == 0,
            "baseline_severe_errors": energyplus_validation["baseline"][
                "severe_errors"
            ],
            "coating_severe_errors": energyplus_validation["coating"][
                "severe_errors"
            ],
        }
        validation["all_passed"] = all(
            bool(item["passed"]) for item in validation["checks"].values()
        )
        validation["status"] = (
            "passed" if validation["all_passed"] else "failed"
        )
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    project_copy = destination / "project.json"
    shutil.copy2(project_path, project_copy)
    manifest = {
        "schema_version": "1.0",
        "stage": 7,
        "status": "passed",
        "project_name": data["project"]["name"],
        "scenario": scenario,
        "baseline_idf_path": str(baseline_path.resolve()),
        "coating_idf_path": str(coating_path.resolve()),
        "validation_path": str(validation_path.resolve()),
        "project_json_path": str(project_copy.resolve()),
        "phase6_geometry_validation": (
            {
                "status": phase6_validation.get("status"),
                "all_passed": phase6_validation.get("all_passed"),
                "source_path": str(phase6_validation_path.resolve()),
            }
            if phase6_validation is not None
            else None
        ),
        "model_details": details,
        "energyplus_validation": energyplus_validation,
    }
    manifest_path = destination / "stage7_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return CoatingBuildResult(
        output_dir=destination.resolve(),
        baseline_idf_path=baseline_path.resolve(),
        coating_idf_path=coating_path.resolve(),
        validation_path=validation_path.resolve(),
        manifest_path=manifest_path.resolve(),
        validation=validation,
        manifest=manifest,
    )
