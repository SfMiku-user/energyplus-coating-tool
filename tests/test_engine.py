import tempfile
import unittest
from pathlib import Path
import sqlite3
from contextlib import closing

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    RunResult,
    _read_annual_results_from_sql,
    compare_results,
    parse_idf,
    prepare_models,
)


SAMPLE_IDF = """
Version, 26.1;

Material,
  Roof Finish,
  Rough,
  0.01,
  0.2,
  1200,
  1000,
  0.90,
  0.70,
  0.70;

Material,
  Insulation,
  Rough,
  0.10,
  0.04,
  30,
  1400,
  0.90,
  0.70,
  0.70;

Construction,
  Roof Construction,
  Roof Finish,
  Insulation;

BuildingSurface:Detailed,
  Main Roof,
  Roof,
  Roof Construction,
  Main Zone,
  ,
  Outdoors,
  ,
  SunExposed,
  WindExposed,
  Autocalculate,
  4,
  0, 0, 3,
  0, 10, 3,
  10, 10, 3,
  10, 0, 3;
"""


class EngineTests(unittest.TestCase):
    def test_prepare_models_clones_only_coating_construction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.idf"
            source.write_text(SAMPLE_IDF, encoding="utf-8")
            baseline_path, coating_path, details = prepare_models(
                source,
                root / "generated",
                solar_reflectance=0.95,
                thermal_emissivity=0.94,
                target_roof=True,
                target_wall=False,
            )
            baseline = parse_idf(baseline_path)
            coating = parse_idf(coating_path)
            self.assertEqual(details["target_surface_count"], 1)
            self.assertGreater(len(coating), len(baseline))
            self.assertTrue(
                any(obj.object_type.casefold() == "output:sqlite" for obj in coating)
            )

            cloned = [
                obj
                for obj in coating
                if obj.object_type.casefold() == "material"
                and obj.fields[0].startswith("RC_Coating_")
            ]
            self.assertEqual(len(cloned), 1)
            self.assertAlmostEqual(float(cloned[0].fields[6]), 0.94)
            self.assertAlmostEqual(float(cloned[0].fields[7]), 0.05)

    def test_compare_results(self):
        baseline = RunResult(
            Path("base"),
            {
                "Cooling:Electricity": 100.0,
                "Fans:Electricity": 20.0,
                "Pumps:Electricity": 10.0,
                "HeatRejection:Electricity": 5.0,
                "Heating:Electricity": 30.0,
                "Electricity:Facility": 400.0,
            },
            20.0,
            0,
            0,
        )
        coating = RunResult(
            Path("coat"),
            {
                "Cooling:Electricity": 80.0,
                "Fans:Electricity": 18.0,
                "Pumps:Electricity": 9.0,
                "HeatRejection:Electricity": 4.0,
                "Heating:Electricity": 33.0,
                "Electricity:Facility": 375.0,
            },
            16.0,
            0,
            0,
        )
        result = compare_results(baseline, coating, {"target_surface_count": 1})
        cooling = result["metrics"]["cooling_electricity_kwh"]
        self.assertAlmostEqual(cooling["saving_percent"], 20.0)
        heating = result["metrics"]["heating_electricity_kwh"]
        self.assertAlmostEqual(heating["change_percent"], 10.0)

    def test_read_annual_sql_results_uses_weather_run_period(self):
        with tempfile.TemporaryDirectory() as temp:
            sql_path = Path(temp) / "eplusout.sql"
            with closing(sqlite3.connect(sql_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE TabularDataWithStrings (
                        ReportName TEXT, TableName TEXT, RowName TEXT,
                        ColumnName TEXT, Value REAL, Units TEXT
                    );
                    CREATE TABLE ReportDataDictionary (
                        ReportDataDictionaryIndex INTEGER,
                        Name TEXT, ReportingFrequency TEXT, Units TEXT
                    );
                    CREATE TABLE EnvironmentPeriods (
                        EnvironmentPeriodIndex INTEGER, EnvironmentType INTEGER
                    );
                    CREATE TABLE Time (
                        TimeIndex INTEGER, EnvironmentPeriodIndex INTEGER
                    );
                    CREATE TABLE ReportData (
                        ReportDataDictionaryIndex INTEGER,
                        TimeIndex INTEGER, Value REAL
                    );
                    """
                )
                for row_name, value in (
                    ("Cooling", 0.36),
                    ("Fans", 0.036),
                    ("Pumps", 0.018),
                    ("Heat Rejection", 0.009),
                    ("Heating", 0.072),
                    ("Total End Uses", 1.8),
                ):
                    connection.execute(
                        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?)",
                        (
                            "AnnualBuildingUtilityPerformanceSummary",
                            "End Uses",
                            row_name,
                            "Electricity",
                            value,
                            "GJ",
                        ),
                    )
                connection.execute(
                    "INSERT INTO ReportDataDictionary VALUES (1,?,?,?)",
                    ("Cooling:Electricity", "Hourly", "J"),
                )
                connection.execute("INSERT INTO EnvironmentPeriods VALUES (1,3)")
                connection.execute("INSERT INTO EnvironmentPeriods VALUES (2,1)")
                connection.execute("INSERT INTO Time VALUES (1,1)")
                connection.execute("INSERT INTO Time VALUES (2,1)")
                connection.execute("INSERT INTO Time VALUES (3,2)")
                connection.execute(
                    "INSERT INTO ReportData VALUES (1,1,180123456)"
                )
                connection.execute(
                    "INSERT INTO ReportData VALUES (1,2,179876544)"
                )
                connection.execute(
                    "INSERT INTO ReportData VALUES (1,3,999999999)"
                )
                connection.commit()

            meters, peak, counts = _read_annual_results_from_sql(sql_path)
            self.assertAlmostEqual(meters["Cooling:Electricity"], 100.0)
            self.assertAlmostEqual(meters["Electricity:Facility"], 500.0)
            self.assertAlmostEqual(peak, 180123456 / 3_600_000)
            self.assertEqual(counts["Cooling:Electricity"], 2)


if __name__ == "__main__":
    unittest.main()
