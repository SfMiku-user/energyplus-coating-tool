import unittest
from copy import deepcopy
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_builder.geometry_quality import assess_geometry


def _valid_geometry():
    lower_floor = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]
    middle = [[0, 0, 3], [10, 0, 3], [10, 10, 3], [0, 10, 3]]
    upper_roof = [[0, 0, 6], [10, 0, 6], [10, 10, 6], [0, 10, 6]]
    south_wall = [[0, 0, 0], [10, 0, 0], [10, 0, 3], [0, 0, 3]]
    spaces = [
        {
            "name": "SPACE_Z001",
            "floor_area_m2": 100,
            "space_multiplier": 1,
            "zone_multiplier": 1,
            "floor_vertices": lower_floor,
        },
        {
            "name": "SPACE_Z002",
            "floor_area_m2": 100,
            "space_multiplier": 1,
            "zone_multiplier": 1,
            "floor_vertices": middle,
        },
    ]
    surfaces = [
        {
            "name": "LOWER_FLOOR",
            "space_name": "SPACE_Z001",
            "surface_type": "Floor",
            "outside_boundary_condition": "Ground",
            "adjacent_surface_name": None,
            "gross_area_m2": 100,
            "net_area_m2": 100,
            "vertices": lower_floor,
        },
        {
            "name": "LOWER_ROOF",
            "space_name": "SPACE_Z001",
            "surface_type": "RoofCeiling",
            "outside_boundary_condition": "Surface",
            "adjacent_surface_name": "UPPER_FLOOR",
            "gross_area_m2": 100,
            "net_area_m2": 100,
            "vertices": middle,
        },
        {
            "name": "UPPER_FLOOR",
            "space_name": "SPACE_Z002",
            "surface_type": "Floor",
            "outside_boundary_condition": "Surface",
            "adjacent_surface_name": "LOWER_ROOF",
            "gross_area_m2": 100,
            "net_area_m2": 100,
            "vertices": list(reversed(middle)),
        },
        {
            "name": "UPPER_ROOF",
            "space_name": "SPACE_Z002",
            "surface_type": "RoofCeiling",
            "outside_boundary_condition": "Outdoors",
            "adjacent_surface_name": None,
            "gross_area_m2": 100,
            "net_area_m2": 100,
            "vertices": upper_roof,
        },
        {
            "name": "SOUTH_WALL",
            "space_name": "SPACE_Z001",
            "surface_type": "Wall",
            "outside_boundary_condition": "Outdoors",
            "adjacent_surface_name": None,
            "gross_area_m2": 30,
            "net_area_m2": 24,
            "vertices": south_wall,
        },
    ]
    subsurfaces = [
        {
            "name": "WINDOW",
            "parent_surface_name": "SOUTH_WALL",
            "gross_area_m2": 6,
            "vertices": [[2, 0, 1], [8, 0, 1], [8, 0, 2], [2, 0, 2]],
        }
    ]
    return spaces, surfaces, subsurfaces


class GeometryQualityTests(unittest.TestCase):
    def test_all_stage_six_checks_pass_for_valid_stacked_spaces(self):
        spaces, surfaces, subsurfaces = _valid_geometry()
        report = assess_geometry(
            declared_floor_area_m2=200,
            input_floor_area_m2=200,
            spaces=spaces,
            surfaces=surfaces,
            subsurfaces=subsurfaces,
        )
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(item["passed"] for item in report["checks"].values()))

    def test_unmatched_middle_surface_is_reported_as_internal_and_roof(self):
        spaces, surfaces, subsurfaces = _valid_geometry()
        surfaces[1]["outside_boundary_condition"] = "Outdoors"
        surfaces[1]["adjacent_surface_name"] = None
        report = assess_geometry(
            declared_floor_area_m2=200,
            input_floor_area_m2=200,
            spaces=spaces,
            surfaces=surfaces,
            subsurfaces=subsurfaces,
        )
        self.assertFalse(report["all_passed"])
        self.assertEqual(
            report["checks"]["unmatched_internal_surfaces"]["count"], 1
        )
        self.assertEqual(
            report["checks"][
                "intermediate_floors_not_exterior_roofs"
            ]["count"],
            1,
        )

    def test_area_duplicate_negative_surface_and_window_errors_are_reported(self):
        spaces, surfaces, subsurfaces = deepcopy(_valid_geometry())
        spaces[1]["floor_vertices"] = deepcopy(spaces[0]["floor_vertices"])
        surfaces[4]["net_area_m2"] = -1
        subsurfaces[0]["vertices"][1][0] = 11
        report = assess_geometry(
            declared_floor_area_m2=201,
            input_floor_area_m2=200,
            spaces=spaces,
            surfaces=surfaces,
            subsurfaces=subsurfaces,
            floor_area_tolerance_m2=0.01,
        )
        self.assertFalse(report["checks"]["total_floor_area"]["passed"])
        self.assertEqual(report["checks"]["duplicate_spaces"]["count"], 1)
        self.assertEqual(
            report["checks"]["negative_area_surfaces"]["count"], 1
        )
        self.assertGreaterEqual(
            report["checks"]["windows_within_walls"]["count"], 1
        )


if __name__ == "__main__":
    unittest.main()
