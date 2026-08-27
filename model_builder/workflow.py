"""可测试版一键流程：Excel → 模型 → 涂层 → 仿真 → 精确结果。"""

from __future__ import annotations

import hashlib
import json
import shutil
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .coating_builder import build_coating_scenario
from .excel_reader import convert_excel_to_json
from .openstudio_builder import build_openstudio_model
from .results_builder import build_energy_comparison


class WorkflowError(RuntimeError):
    """一键流程失败；消息可直接显示给非技术用户。"""


@dataclass(frozen=True)
class WorkflowResult:
    run_dir: Path
    manifest_path: Path
    comparison_json_path: Path
    comparison_csv_path: Path
    hourly_csv_path: Path
    result: dict[str, Any]


ProgressCallback = Callable[[str, int], None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _notify(callback: ProgressCallback | None, message: str, percent: int) -> None:
    if callback is not None:
        callback(message, percent)


def run_project_workflow(
    excel_file: Path | str,
    weather_file: Path | str,
    openstudio_executable: Path | str,
    energyplus_executable: Path | str,
    output_root: Path | str,
    *,
    progress_callback: ProgressCallback | None = None,
    run_name: str | None = None,
) -> WorkflowResult:
    """从建筑参数 Excel 开始完成全部计算并返回最终结果。"""

    excel = Path(excel_file)
    weather = Path(weather_file)
    openstudio = Path(openstudio_executable)
    energyplus = Path(energyplus_executable)
    root = Path(output_root)
    for path, label in (
        (excel, "建筑参数 Excel"),
        (weather, "EPW 气象文件"),
        (openstudio, "OpenStudio 程序"),
        (energyplus, "EnergyPlus 程序"),
    ):
        if not path.is_file():
            raise WorkflowError(f"找不到{label}：{path}")
    root.mkdir(parents=True, exist_ok=True)
    name = run_name or datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    run_dir = root / name
    if run_dir.exists():
        raise WorkflowError(f"运行目录已经存在：{run_dir}")
    run_dir.mkdir(parents=True)
    started_at = datetime.now().astimezone().isoformat()
    try:
        _notify(progress_callback, "正在读取并校验建筑参数 Excel……", 5)
        input_dir = run_dir / "input"
        input_dir.mkdir()
        excel_copy = input_dir / "建筑参数输入.xlsx"
        project_json = input_dir / "project.json"
        shutil.copy2(excel, excel_copy)
        convert_excel_to_json(excel_copy, project_json)

        _notify(progress_callback, "正在生成并验收建筑几何模型……", 20)
        model_result = build_openstudio_model(
            project_json,
            openstudio,
            energyplus,
            run_dir / "generated_model",
            floor_area_tolerance_m2=0.01,
            coordinate_tolerance_m=1e-5,
        )

        _notify(progress_callback, "正在生成基准与辐射制冷涂层模型……", 45)
        coating_result = build_coating_scenario(
            project_json,
            model_result.compatible_idf_path,
            run_dir / "coating_model",
            energyplus_executable=energyplus,
            weather_file=weather,
        )

        _notify(progress_callback, "正在核对 8760 小时结果并计算节电率……", 85)
        comparison = build_energy_comparison(
            coating_result.output_dir,
            run_dir / "results",
        )

        completed_at = datetime.now().astimezone().isoformat()
        manifest = {
            "schema_version": "1.0",
            "release_stage": "testable",
            "status": "passed",
            "started_at": started_at,
            "completed_at": completed_at,
            "inputs": {
                "excel_source_path": str(excel.resolve()),
                "excel_copy_path": str(excel_copy.resolve()),
                "excel_sha256": _sha256(excel_copy),
                "weather_path": str(weather.resolve()),
                "weather_sha256": _sha256(weather),
                "openstudio_executable": str(openstudio.resolve()),
                "energyplus_executable": str(energyplus.resolve()),
            },
            "stages": {
                "excel_validation": "passed",
                "geometry_model": model_result.manifest.get(
                    "geometry_validation_status"
                ),
                "coating_model": coating_result.validation.get("status"),
                "energy_results": comparison.validation.get("status"),
            },
            "paths": {
                "project_json": str(project_json.resolve()),
                "generated_model": str(model_result.output_dir),
                "coating_model": str(coating_result.output_dir),
                "results": str(comparison.output_dir),
                "comparison_json": str(comparison.json_path),
                "comparison_csv": str(comparison.csv_path),
                "hourly_csv": str(comparison.hourly_csv_path),
            },
            "summary_metrics": comparison.result["metrics"],
        }
        manifest_path = run_dir / "workflow_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _notify(progress_callback, "计算完成，可以查看节电率结果。", 100)
        return WorkflowResult(
            run_dir=run_dir.resolve(),
            manifest_path=manifest_path.resolve(),
            comparison_json_path=comparison.json_path,
            comparison_csv_path=comparison.csv_path,
            hourly_csv_path=comparison.hourly_csv_path,
            result=comparison.result,
        )
    except Exception as exc:
        error_path = run_dir / "workflow_error.json"
        error_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "started_at": started_at,
                    "failed_at": datetime.now().astimezone().isoformat(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError(f"一键计算失败：{exc}\n错误记录：{error_path}") from exc
