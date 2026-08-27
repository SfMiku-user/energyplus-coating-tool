"""从统一项目 JSON 调用 OpenStudio，生成 OSM 与兼容的 IDF。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .idf_versioning import prepare_idf_for_energyplus
from .project import load_project
from .validation import format_validation_report, validate_project


class OpenStudioBuildError(RuntimeError):
    """OpenStudio 程序、项目数据或模型生成过程不可用。"""


@dataclass(frozen=True)
class OpenStudioBuildResult:
    output_dir: Path
    osm_path: Path
    idf_25_2_path: Path
    compatible_idf_path: Path
    manifest_path: Path
    geometry_validation_path: Path
    log_path: Path
    manifest: dict[str, object]
    version_details: dict[str, object]


def _run_smoke_test(
    energyplus: Path,
    weather: Path,
    model: Path,
    output_dir: Path,
) -> dict[str, object]:
    if not weather.is_file():
        raise OpenStudioBuildError(f"找不到烟雾测试气象文件：{weather}")
    output_dir.mkdir(parents=True, exist_ok=False)
    completed = subprocess.run(
        [
            str(energyplus),
            "-w",
            str(weather),
            "-d",
            str(output_dir),
            "-r",
            str(model),
        ],
        cwd=model.parent,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )
    error_path = output_dir / "eplusout.err"
    error_text = (
        error_path.read_text(encoding="utf-8", errors="replace")
        if error_path.is_file()
        else ""
    )
    if completed.returncode != 0 or "**  Fatal  **" in error_text:
        raise OpenStudioBuildError(
            "EnergyPlus 烟雾测试失败。\n"
            + (error_text[-5000:] or completed.stderr or completed.stdout)
        )

    sql_path = output_dir / "eplusout.sql"
    if not sql_path.is_file():
        raise OpenStudioBuildError("烟雾测试没有生成 eplusout.sql。")
    try:
        with closing(sqlite3.connect(sql_path)) as connection:
            rows = connection.execute(
                """
                SELECT rdd.Name, rdd.Units, COUNT(rd.Value), SUM(rd.Value)
                FROM ReportDataDictionary AS rdd
                JOIN ReportData AS rd
                  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
                WHERE rdd.Name IN (
                  'Zone Air System Sensible Cooling Energy',
                  'Zone Air System Sensible Cooling Rate',
                  'Cooling:Electricity'
                )
                GROUP BY rdd.Name, rdd.Units
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise OpenStudioBuildError(f"无法读取烟雾测试制冷输出：{exc}") from exc
    cooling_outputs = {
        str(name): {"units": str(units), "count": int(count), "sum": float(total)}
        for name, units, count, total in rows
        if count and total is not None
    }
    if "Zone Air System Sensible Cooling Energy" not in cooling_outputs:
        raise OpenStudioBuildError("烟雾测试未输出逐时制冷负荷。")
    return {
        "energyplus_exit_code": completed.returncode,
        "fatal_errors": 0,
        "eplusout_err": str(error_path.resolve()),
        "cooling_outputs": cooling_outputs,
    }


def build_openstudio_model(
    project_json: Path | str,
    openstudio_executable: Path | str,
    energyplus_executable: Path | str,
    output_dir: Path | str,
    weather_file: Path | str | None = None,
    floor_area_tolerance_m2: float = 0.01,
    coordinate_tolerance_m: float = 1e-5,
) -> OpenStudioBuildResult:
    """校验项目 JSON，并生成 OSM、25.2 IDF 和 EnergyPlus 兼容 IDF。"""

    project_path = Path(project_json)
    openstudio = Path(openstudio_executable)
    energyplus = Path(energyplus_executable)
    weather = Path(weather_file) if weather_file is not None else None
    destination = Path(output_dir)
    if not openstudio.is_file():
        raise OpenStudioBuildError(f"找不到 OpenStudio 程序：{openstudio}")
    if not energyplus.is_file():
        raise OpenStudioBuildError(f"找不到 EnergyPlus 程序：{energyplus}")
    if floor_area_tolerance_m2 <= 0 or coordinate_tolerance_m <= 0:
        raise OpenStudioBuildError("几何验收容差必须大于 0。")
    project = load_project(project_path)
    errors = [
        issue for issue in validate_project(project) if issue.severity == "error"
    ]
    if errors:
        raise OpenStudioBuildError(
            "项目数据校验失败，未生成模型。\n" + format_validation_report(errors)
        )
    if destination.exists() and any(destination.iterdir()):
        raise OpenStudioBuildError(f"模型输出目录必须为空：{destination}")
    destination.mkdir(parents=True, exist_ok=True)

    worker = Path(__file__).with_name("openstudio_worker.py")
    geometry_quality = Path(__file__).with_name("geometry_quality.py")
    drive = openstudio.drive or Path.cwd().drive or "C:"
    staging_parent = Path(f"{drive}\\OpenStudio-Coating-Temp")
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix="build_", dir=str(staging_parent))
    )
    staging_worker = staging_root / "openstudio_worker.py"
    staging_geometry_quality = staging_root / "geometry_quality.py"
    staging_project = staging_root / "project.json"
    staging_output = staging_root / "output"
    shutil.copy2(worker, staging_worker)
    shutil.copy2(geometry_quality, staging_geometry_quality)
    shutil.copy2(project_path, staging_project)
    command = [
        str(openstudio),
        "execute_python_script",
        str(staging_worker),
        str(staging_project),
        str(staging_output),
        str(floor_area_tolerance_m2),
        str(coordinate_tolerance_m),
    ]
    try:
        try:
            completed = subprocess.run(
                command,
                cwd=staging_root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OpenStudioBuildError(f"无法调用 OpenStudio：{openstudio}") from exc
        log_path = destination / "openstudio.log"
        log_path.write_text(
            "OpenStudio 使用 ASCII 临时工作区，以兼容中文项目路径。\n"
            "命令：" + " ".join(command) + "\n\n"
            + completed.stdout
            + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            failed_geometry_report = staging_output / "geometry_validation.json"
            if failed_geometry_report.is_file():
                shutil.copy2(
                    failed_geometry_report,
                    destination / "geometry_validation.json",
                )
            raise OpenStudioBuildError(
                f"OpenStudio 模型生成失败（返回码 {completed.returncode}）。"
                f"详见：{log_path}"
            )

        staging_osm = staging_output / "building.osm"
        staging_idf = staging_output / "building_v25_2.idf"
        staging_manifest = staging_output / "build_manifest.json"
        staging_geometry_report = staging_output / "geometry_validation.json"
        for path in (
            staging_osm,
            staging_idf,
            staging_manifest,
            staging_geometry_report,
        ):
            if not path.is_file():
                raise OpenStudioBuildError(f"OpenStudio 未生成预期文件：{path.name}")
        try:
            manifest = json.loads(staging_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenStudioBuildError("无法读取 OpenStudio 建模清单。") from exc

        staging_compatible, version_details = prepare_idf_for_energyplus(
            staging_idf,
            energyplus,
            staging_output / "version_conversion",
        )
        smoke_details: dict[str, object] | None = None
        if weather is not None:
            staging_weather = staging_root / "weather.epw"
            shutil.copy2(weather, staging_weather)
            smoke_details = _run_smoke_test(
                energyplus,
                staging_weather,
                staging_compatible,
                staging_output / "smoke_test",
            )

        project_copy = destination / "project.json"
        osm_path = destination / "baseline.osm"
        idf_25_2_path = destination / "baseline_25_2.idf"
        compatible_idf = destination / "baseline.idf"
        transition_log = destination / "transition.log"
        manifest_path = destination / "build_manifest.json"
        geometry_validation_path = destination / "geometry_validation.json"
        shutil.copy2(project_path, project_copy)
        shutil.copy2(staging_osm, osm_path)
        shutil.copy2(staging_idf, idf_25_2_path)
        shutil.copy2(staging_compatible, compatible_idf)
        shutil.copy2(staging_geometry_report, geometry_validation_path)
        shutil.copy2(
            staging_output / "version_conversion" / "transition.log",
            transition_log,
        )
        if smoke_details is not None:
            shutil.copytree(staging_output / "smoke_test", destination / "smoke_test")

        final_version_details = dict(version_details)
        final_version_details["source_idf"] = str(idf_25_2_path.resolve())
        final_version_details["compatible_idf"] = str(compatible_idf.resolve())
        final_version_details["transition_log"] = str(transition_log.resolve())
        if smoke_details is not None:
            smoke_details["eplusout_err"] = str(
                (destination / "smoke_test" / "eplusout.err").resolve()
            )
        manifest["project_json_path"] = str(project_copy.resolve())
        manifest["osm_path"] = str(osm_path.resolve())
        manifest["idf_25_2_path"] = str(idf_25_2_path.resolve())
        manifest["compatible_idf_path"] = str(compatible_idf.resolve())
        manifest["geometry_validation_path"] = str(
            geometry_validation_path.resolve()
        )
        manifest["idf_version"] = final_version_details
        manifest["smoke_test"] = smoke_details
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return OpenStudioBuildResult(
            output_dir=destination.resolve(),
            osm_path=osm_path.resolve(),
            idf_25_2_path=idf_25_2_path.resolve(),
            compatible_idf_path=compatible_idf.resolve(),
            manifest_path=manifest_path.resolve(),
            geometry_validation_path=geometry_validation_path.resolve(),
            log_path=log_path.resolve(),
            manifest=manifest,
            version_details=final_version_details,
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
