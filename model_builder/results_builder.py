"""阶段八：从成对 EnergyPlus SQLite 精确生成节电率结果。"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine import (
    RunResult,
    ToolError,
    _energy_to_kwh,
    _parse_error_file,
    _read_annual_results_from_sql,
    compare_results,
    write_result_files,
)


class ResultsBuildError(RuntimeError):
    """阶段八输入结果不完整、不一致或无法计算。"""


@dataclass(frozen=True)
class ResultsBuildResult:
    output_dir: Path
    json_path: Path
    csv_path: Path
    hourly_csv_path: Path
    validation_path: Path
    result: dict[str, Any]
    validation: dict[str, Any]


def _read_hourly_meter_series(
    sql_path: Path, meter_name: str
) -> list[tuple[tuple[int, int, int, int, int], float]]:
    try:
        with closing(sqlite3.connect(sql_path)) as connection:
            rows = connection.execute(
                """
                SELECT t.Year, t.Month, t.Day, t.Hour, t.Minute,
                       rd.Value, rdd.Units
                FROM ReportData AS rd
                JOIN ReportDataDictionary AS rdd
                  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
                JOIN Time AS t ON rd.TimeIndex = t.TimeIndex
                JOIN EnvironmentPeriods AS ep
                  ON t.EnvironmentPeriodIndex = ep.EnvironmentPeriodIndex
                WHERE rdd.Name = ?
                  AND rdd.ReportingFrequency = 'Hourly'
                  AND ep.EnvironmentType = 3
                ORDER BY t.TimeIndex
                """,
                (meter_name,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ResultsBuildError(f"无法读取逐时结果 {sql_path}：{exc}") from exc
    return [
        (
            (int(year), int(month), int(day), int(hour), int(minute)),
            _energy_to_kwh(float(value), str(units)),
        )
        for year, month, day, hour, minute, value, units in rows
    ]


def _run_result(run_dir: Path) -> RunResult:
    sql_path = run_dir / "eplusout.sql"
    error_path = run_dir / "eplusout.err"
    if not sql_path.is_file() or not error_path.is_file():
        raise ResultsBuildError(f"EnergyPlus 结果不完整：{run_dir}")
    warnings, severe = _parse_error_file(error_path)
    if severe:
        raise ResultsBuildError(f"EnergyPlus 结果包含 Severe/Fatal：{error_path}")
    try:
        meters, peak, counts = _read_annual_results_from_sql(sql_path)
    except ToolError as exc:
        raise ResultsBuildError(str(exc)) from exc
    return RunResult(run_dir, meters, peak, warnings, severe, counts)


def build_energy_comparison(
    stage7_dir: Path | str,
    output_dir: Path | str,
) -> ResultsBuildResult:
    """使用逐时天气运行期记录计算阶段八精确节电率。"""

    source = Path(stage7_dir)
    destination = Path(output_dir)
    manifest_path = source / "stage7_manifest.json"
    stage7_validation_path = source / "coating_validation.json"
    if not manifest_path.is_file() or not stage7_validation_path.is_file():
        raise ResultsBuildError("找不到阶段七清单或涂层验收报告。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stage7_validation = json.loads(
        stage7_validation_path.read_text(encoding="utf-8")
    )
    if stage7_validation.get("all_passed") is not True:
        raise ResultsBuildError("阶段七验收未通过，禁止计算节电率。")
    if destination.exists() and any(destination.iterdir()):
        raise ResultsBuildError(f"阶段八输出目录必须为空：{destination}")
    destination.mkdir(parents=True, exist_ok=True)

    baseline_dir = source / "smoke_test" / "baseline"
    coating_dir = source / "smoke_test" / "coating"
    baseline = _run_result(baseline_dir)
    coating = _run_result(coating_dir)
    baseline_series = _read_hourly_meter_series(
        baseline_dir / "eplusout.sql", "Cooling:Electricity"
    )
    coating_series = _read_hourly_meter_series(
        coating_dir / "eplusout.sql", "Cooling:Electricity"
    )
    baseline_times = [item[0] for item in baseline_series]
    coating_times = [item[0] for item in coating_series]
    accepted_annual_counts = {8760, 8784}
    required_meters = ("Cooling:Electricity", "Electricity:Facility")
    counts_complete = all(
        baseline.hourly_record_counts.get(name) in accepted_annual_counts
        and coating.hourly_record_counts.get(name)
        == baseline.hourly_record_counts.get(name)
        for name in required_meters
    )
    series_aligned = baseline_times == coating_times and bool(baseline_times)
    nonnegative = all(value >= 0 for _, value in baseline_series + coating_series)
    baseline_series_sum = sum(value for _, value in baseline_series)
    coating_series_sum = sum(value for _, value in coating_series)
    sums_reconcile = math.isclose(
        baseline_series_sum,
        baseline.meters_kwh["Cooling:Electricity"],
        rel_tol=1e-10,
        abs_tol=1e-7,
    ) and math.isclose(
        coating_series_sum,
        coating.meters_kwh["Cooling:Electricity"],
        rel_tol=1e-10,
        abs_tol=1e-7,
    )

    details = dict(manifest.get("model_details") or {})
    details["scenario"] = manifest.get("scenario")
    result = compare_results(baseline, coating, details)
    result.update(
        {
            "schema_version": "1.0",
            "stage": 8,
            "status": "passed",
            "project_name": manifest.get("project_name"),
            "calculation": {
                "annual_energy_source": (
                    "EnergyPlus SQLite ReportData hourly weather run period"
                ),
                "saving_percent_formula": (
                    "(baseline - coating) / baseline * 100"
                ),
                "weather_environment_type": 3,
            },
        }
    )
    cooling_metric = result["metrics"]["cooling_electricity_kwh"]
    finite_cooling_result = all(
        math.isfinite(float(cooling_metric[key]))
        for key in ("baseline", "coating", "saving_percent")
        if cooling_metric[key] is not None
    ) and cooling_metric["saving_percent"] is not None

    checks = {
        "stage7_validation_passed": {
            "passed": True,
        },
        "paired_simulations_no_severe_or_fatal": {
            "passed": baseline.severe_errors == 0 and coating.severe_errors == 0,
            "baseline": baseline.severe_errors,
            "coating": coating.severe_errors,
        },
        "annual_hourly_records_complete": {
            "passed": counts_complete,
            "baseline": baseline.hourly_record_counts,
            "coating": coating.hourly_record_counts,
        },
        "paired_timestamps_aligned": {
            "passed": series_aligned,
            "baseline_count": len(baseline_times),
            "coating_count": len(coating_times),
        },
        "cooling_values_nonnegative": {
            "passed": nonnegative,
        },
        "hourly_sums_reconcile": {
            "passed": sums_reconcile,
            "baseline_sum_kwh": baseline_series_sum,
            "coating_sum_kwh": coating_series_sum,
        },
        "cooling_saving_percent_defined": {
            "passed": finite_cooling_result,
            "baseline_cooling_kwh": cooling_metric["baseline"],
            "coating_cooling_kwh": cooling_metric["coating"],
            "saving_percent": cooling_metric["saving_percent"],
        },
    }
    all_passed = all(bool(item["passed"]) for item in checks.values())
    validation = {
        "schema_version": "1.0",
        "stage": 8,
        "status": "passed" if all_passed else "failed",
        "all_passed": all_passed,
        "checks": checks,
    }
    if not all_passed:
        validation_path = destination / "stage8_validation.json"
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        failed = [name for name, item in checks.items() if not item["passed"]]
        raise ResultsBuildError("阶段八结果验收失败：" + ";".join(failed))

    json_path, csv_path = write_result_files(result, destination)
    hourly_csv_path = destination / "hourly_cooling_comparison.csv"
    with hourly_csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "year",
                "month",
                "day",
                "hour",
                "minute",
                "baseline_cooling_kwh",
                "coating_cooling_kwh",
                "saving_kwh",
            ]
        )
        for (timestamp, baseline_value), (_, coating_value) in zip(
            baseline_series, coating_series
        ):
            writer.writerow(
                [
                    *timestamp,
                    f"{baseline_value:.12f}",
                    f"{coating_value:.12f}",
                    f"{baseline_value - coating_value:.12f}",
                ]
            )
    validation_path = destination / "stage8_validation.json"
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return ResultsBuildResult(
        output_dir=destination.resolve(),
        json_path=json_path.resolve(),
        csv_path=csv_path.resolve(),
        hourly_csv_path=hourly_csv_path.resolve(),
        validation_path=validation_path.resolve(),
        result=result,
        validation=validation,
    )
