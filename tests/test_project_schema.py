import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.project import ProjectData, load_project, save_project
from model_builder.validation import validate_project


def valid_project_dict():
    return {
        "schema_version": "1.0",
        "project": {"name": "示例办公楼", "building_type": "Office"},
        "site": {
            "latitude": 34.75,
            "longitude": 113.62,
            "time_zone": 8.0,
            "elevation_m": 110.0,
            "north_axis_deg": 0.0,
        },
        "floors": [
            {"id": "F01", "elevation_m": 0.0, "height_m": 3.6}
        ],
        "zones": [
            {
                "id": "Z001",
                "floor_id": "F01",
                "name": "办公室101",
                "polygon_xy": [[0, 0], [8, 0], [8, 6], [0, 6]],
                "conditioned": True,
                "space_type": "Office",
            }
        ],
        "windows": [],
        "materials": [],
        "constructions": [],
        "schedules": [],
        "loads": [],
        "thermostats": [],
        "hvac": {"mode": "ideal_loads"},
        "coating": {"solar_reflectance": 0.92, "thermal_emissivity": 0.95},
        "data_quality": {},
    }


class ProjectSchemaTests(unittest.TestCase):
    def test_valid_minimum_project_has_no_errors(self):
        issues = validate_project(valid_project_dict())
        self.assertFalse([item for item in issues if item.severity == "error"])

    def test_missing_section_is_reported(self):
        data = valid_project_dict()
        del data["materials"]
        issues = validate_project(data)
        self.assertTrue(
            any(item.path == "materials" and item.severity == "error" for item in issues)
        )

    def test_unknown_floor_and_invalid_polygon_are_reported(self):
        data = valid_project_dict()
        data["zones"][0]["floor_id"] = "F99"
        data["zones"][0]["polygon_xy"] = [[0, 0], [1, 1]]
        issues = validate_project(data)
        paths = {item.path for item in issues if item.severity == "error"}
        self.assertIn("zones[0].floor_id", paths)
        self.assertIn("zones[0].polygon_xy", paths)

    def test_project_json_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "项目.json"
            project = ProjectData.from_dict(valid_project_dict())
            save_project(project, output)
            loaded = load_project(output)
            self.assertEqual(loaded.data["project"]["name"], "示例办公楼")
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["schema_version"],
                "1.0",
            )


if __name__ == "__main__":
    unittest.main()
