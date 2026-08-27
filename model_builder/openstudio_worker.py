"""由 OpenStudio 自带 Python 执行的 JSON→OSM/IDF 建模脚本。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import openstudio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_quality import assess_geometry


def _contains(value: object, *keywords: str) -> bool:
    text = str(value).casefold()
    return any(keyword.casefold() in text for keyword in keywords)


def _clockwise_polygon(raw_points: list[list[float]], elevation: float):
    points = [(float(item[0]), float(item[1])) for item in raw_points]
    signed_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )
    if signed_area > 0:
        points.reverse()
    result = openstudio.Point3dVector()
    for x, y in points:
        result.append(openstudio.Point3d(x, y, elevation))
    return result


def _make_materials(model, records: list[dict[str, object]]):
    materials = {}
    for record in records:
        material_id = str(record["id"])
        material_type = str(record["energyplus_type"]).casefold()
        if material_type == "material":
            material = openstudio.model.StandardOpaqueMaterial(model)
            material.setRoughness(str(record["roughness"]))
            material.setThickness(float(record["thickness_m"]))
            material.setConductivity(float(record["conductivity_W_mK"]))
            material.setDensity(float(record["density_kg_m3"]))
            material.setSpecificHeat(float(record["specific_heat_J_kgK"]))
            material.setThermalAbsorptance(float(record["thermal_absorptance"]))
            material.setSolarAbsorptance(float(record["solar_absorptance"]))
            material.setVisibleAbsorptance(float(record["visible_absorptance"]))
        elif material_type == "material:nomass":
            material = openstudio.model.MasslessOpaqueMaterial(model)
            material.setRoughness(str(record["roughness"]))
            material.setThermalResistance(
                float(record["thermal_resistance_m2K_W"])
            )
            material.setThermalAbsorptance(float(record["thermal_absorptance"]))
            material.setSolarAbsorptance(float(record["solar_absorptance"]))
            material.setVisibleAbsorptance(float(record["visible_absorptance"]))
        else:
            raise ValueError(f"暂不支持材料类型：{record['energyplus_type']}")
        material.setName(material_id)
        material.setComment(str(record.get("name", material_id)))
        materials[material_id] = material
    return materials


def _make_constructions(model, records, materials):
    constructions = {}
    uses = {}
    for record in records:
        construction_id = str(record["id"])
        construction = openstudio.model.Construction(model)
        construction.setName(construction_id)
        construction.setComment(str(record.get("name", construction_id)))
        if str(record.get("kind", "opaque")).casefold() == "window":
            glazing = openstudio.model.SimpleGlazing(model)
            glazing.setName(f"{construction_id}_GLAZING")
            glazing.setUFactor(float(record["u_factor_W_m2K"]))
            glazing.setSolarHeatGainCoefficient(float(record["shgc"]))
            if record.get("visible_transmittance") is not None:
                glazing.setVisibleTransmittance(
                    float(record["visible_transmittance"])
                )
            layers = openstudio.model.MaterialVector()
            layers.append(glazing)
        else:
            layers = openstudio.model.MaterialVector()
            for material_id in record["layer_ids"]:
                if str(material_id) not in materials:
                    raise ValueError(
                        f"构造 {construction_id} 引用了不存在的材料 {material_id}。"
                    )
                layers.append(materials[str(material_id)])
        if not construction.setLayers(layers):
            raise ValueError(f"构造 {construction_id} 的材料层无法写入 OpenStudio。")
        constructions[construction_id] = construction
        uses[construction_id] = str(record.get("use", ""))
    return constructions, uses


def _construction_for_use(constructions, uses, *keywords):
    for construction_id, use in uses.items():
        if _contains(use, *keywords):
            return constructions[construction_id]
    return None


def _orientation_difference(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _polygon_area(raw_points: list[list[float]]) -> float:
    points = [(float(item[0]), float(item[1])) for item in raw_points]
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
        )
    ) / 2.0


def _time_from_fraction(value: object):
    total_minutes = int(round(float(value) * 24.0 * 60.0))
    total_minutes = max(0, min(24 * 60, total_minutes))
    return openstudio.Time(0, total_minutes // 60, total_minutes % 60, 0)


def _write_day_profile(day, start: object, end: object, peak: object) -> None:
    day.clearValues()
    day.addValue(_time_from_fraction(start), 0.0)
    day.addValue(_time_from_fraction(end), float(peak))
    day.addValue(openstudio.Time(0, 24, 0, 0), 0.0)


def _make_schedules(model, records: list[dict[str, object]]):
    schedules = {}
    for record in records:
        schedule_id = str(record["id"])
        schedule = openstudio.model.ScheduleRuleset(model)
        schedule.setName(schedule_id)
        schedule.defaultDaySchedule().setName(f"{schedule_id}_OFF")
        schedule.defaultDaySchedule().addValue(openstudio.Time(0, 24, 0, 0), 0.0)

        weekday = openstudio.model.ScheduleRule(schedule)
        weekday.setName(f"{schedule_id}_WEEKDAY")
        for method in (
            weekday.setApplyMonday,
            weekday.setApplyTuesday,
            weekday.setApplyWednesday,
            weekday.setApplyThursday,
            weekday.setApplyFriday,
        ):
            method(True)
        _write_day_profile(
            weekday.daySchedule(),
            record["weekday_start"],
            record["weekday_end"],
            record["peak_fraction"],
        )
        if bool(record.get("saturday_enabled")):
            saturday = openstudio.model.ScheduleRule(schedule)
            saturday.setName(f"{schedule_id}_SATURDAY")
            saturday.setApplySaturday(True)
            _write_day_profile(
                saturday.daySchedule(),
                record["weekday_start"],
                record["weekday_end"],
                record["peak_fraction"],
            )
        if bool(record.get("sunday_enabled")):
            sunday = openstudio.model.ScheduleRule(schedule)
            sunday.setName(f"{schedule_id}_SUNDAY")
            sunday.setApplySunday(True)
            _write_day_profile(
                sunday.daySchedule(),
                record["weekday_start"],
                record["weekday_end"],
                record["peak_fraction"],
            )
        schedules[schedule_id] = schedule
    return schedules


def _add_internal_loads(model, records, zone_spaces, schedules):
    activity = openstudio.model.ScheduleConstant(model)
    activity.setName("PEOPLE_ACTIVITY_120W")
    activity.setValue(120.0)
    always_on = model.alwaysOnDiscreteSchedule()
    for record in records:
        zone_id = str(record["zone_id"])
        space = zone_spaces[zone_id]
        schedule_id = str(record["schedule_id"])
        if schedule_id not in schedules:
            raise ValueError(f"热区 {zone_id} 的负荷引用了未知时间表 {schedule_id}。")
        schedule = schedules[schedule_id]

        people_definition = openstudio.model.PeopleDefinition(model)
        people_definition.setName(f"PEOPLE_DEF_{zone_id}")
        people_definition.setPeopleperSpaceFloorArea(
            float(record["people_density_person_m2"])
        )
        people = openstudio.model.People(people_definition)
        people.setName(f"PEOPLE_{zone_id}")
        people.setSpace(space)
        people.setNumberofPeopleSchedule(schedule)
        people.setActivityLevelSchedule(activity)

        lights_definition = openstudio.model.LightsDefinition(model)
        lights_definition.setName(f"LIGHTS_DEF_{zone_id}")
        lights_definition.setWattsperSpaceFloorArea(
            float(record["lighting_power_density_W_m2"])
        )
        lights = openstudio.model.Lights(lights_definition)
        lights.setName(f"LIGHTS_{zone_id}")
        lights.setSpace(space)
        lights.setSchedule(schedule)

        equipment_definition = openstudio.model.ElectricEquipmentDefinition(model)
        equipment_definition.setName(f"EQUIPMENT_DEF_{zone_id}")
        equipment_definition.setWattsperSpaceFloorArea(
            float(record["equipment_power_density_W_m2"])
        )
        equipment = openstudio.model.ElectricEquipment(equipment_definition)
        equipment.setName(f"EQUIPMENT_{zone_id}")
        equipment.setSpace(space)
        equipment.setSchedule(schedule)

        infiltration = openstudio.model.SpaceInfiltrationDesignFlowRate(model)
        infiltration.setName(f"INFILTRATION_{zone_id}")
        infiltration.setSpace(space)
        infiltration.setAirChangesperHour(float(record["infiltration_ach"]))
        infiltration.setSchedule(always_on)

        outdoor_air = openstudio.model.DesignSpecificationOutdoorAir(model)
        outdoor_air.setName(f"OUTDOOR_AIR_{zone_id}")
        outdoor_air.setOutdoorAirMethod("Sum")
        outdoor_air.setOutdoorAirFlowperPerson(
            float(record["outdoor_air_L_s_person"]) / 1000.0
        )
        space.setDesignSpecificationOutdoorAir(outdoor_air)


def _add_thermostats(model, records, thermal_zones):
    for record in records:
        zone_id = str(record["zone_id"])
        heating = openstudio.model.ScheduleConstant(model)
        heating.setName(f"HEATING_SETPOINT_{zone_id}")
        heating.setValue(float(record["heating_setpoint_C"]))
        cooling = openstudio.model.ScheduleConstant(model)
        cooling.setName(f"COOLING_SETPOINT_{zone_id}")
        cooling.setValue(float(record["cooling_setpoint_C"]))
        thermostat = openstudio.model.ThermostatSetpointDualSetpoint(model)
        thermostat.setName(f"THERMOSTAT_{zone_id}")
        thermostat.setHeatingSetpointTemperatureSchedule(heating)
        thermostat.setCoolingSetpointTemperatureSchedule(cooling)
        thermal_zones[zone_id].setThermostatSetpointDualSetpoint(thermostat)


def _add_hvac(model, hvac, thermal_zones, schedules, warnings):
    systems = {
        str(record["id"]): record for record in hvac.get("systems", [])
    }
    active_id = str(hvac.get("active_system_id") or "")
    if active_id not in systems:
        raise ValueError(f"找不到启用的 HVAC 系统：{active_id}")
    system = systems[active_id]
    zone_ids = [str(item) for item in system["zone_ids"]]
    schedule_id = str(system["availability_schedule_id"])
    if schedule_id not in schedules:
        raise ValueError(f"HVAC 系统引用了未知运行时间表：{schedule_id}")
    availability = schedules[schedule_id]
    mode = str(hvac.get("mode", "constant_cop")).casefold()

    if mode == "ideal_loads" or _contains(system.get("system_type"), "idealloads"):
        for zone_id in zone_ids:
            ideal = openstudio.model.ZoneHVACIdealLoadsAirSystem(model)
            ideal.setName(f"{active_id}_{zone_id}")
            ideal.setAvailabilitySchedule(availability)
            ideal.addToThermalZone(thermal_zones[zone_id])
        return

    zone_count = max(1, len(zone_ids))
    fan_power = float(system.get("supply_fan_power_W") or 0.0) / zone_count
    rated_capacity = system.get("rated_cooling_capacity_kW")
    if float(system.get("pump_power_W") or 0.0) > 0:
        warnings.append(
            "当前 constant_cop 封装使用分区热泵，不包含独立水泵；"
            "输入的水泵功率尚未计入，水系统需在后续设备级模型中实现。"
        )
    for zone_id in zone_ids:
        fan = openstudio.model.FanSystemModel(model)
        fan.setName(f"FAN_{active_id}_{zone_id}")
        if fan_power > 0:
            fan.setDesignElectricPowerConsumption(fan_power)
        heating_coil = openstudio.model.CoilHeatingDXSingleSpeed(model)
        heating_coil.setName(f"HEATING_COIL_{active_id}_{zone_id}")
        heating_coil.setRatedCOP(float(system["heating_cop"]))
        cooling_coil = openstudio.model.CoilCoolingDXSingleSpeed(model)
        cooling_coil.setName(f"COOLING_COIL_{active_id}_{zone_id}")
        cooling_coil.setRatedCOP(float(system["cooling_cop"]))
        if rated_capacity is not None:
            cooling_coil.setRatedTotalCoolingCapacity(
                float(rated_capacity) * 1000.0 / zone_count
            )
        supplemental = openstudio.model.CoilHeatingElectric(model)
        supplemental.setName(f"SUPPLEMENTAL_HEAT_{active_id}_{zone_id}")
        unit = openstudio.model.ZoneHVACPackagedTerminalHeatPump(
            model,
            availability,
            fan,
            heating_coil,
            cooling_coil,
            supplemental,
        )
        unit.setName(f"PTHP_{active_id}_{zone_id}")
        unit.addToThermalZone(thermal_zones[zone_id])

    served = set(zone_ids)
    missing = sorted(set(thermal_zones) - served)
    if missing:
        warnings.append("以下热区未连接启用的 HVAC 系统：" + ";".join(missing))


def _add_sizing_and_outputs(model, thermal_zones):
    simulation = model.getSimulationControl()
    simulation.setDoZoneSizingCalculation(True)
    simulation.setDoSystemSizingCalculation(False)
    simulation.setDoPlantSizingCalculation(False)
    simulation.setRunSimulationforSizingPeriods(False)
    simulation.setRunSimulationforWeatherFileRunPeriods(True)

    for zone_id, zone in thermal_zones.items():
        sizing = zone.sizingZone()
        sizing.setName(f"SIZING_{zone_id}")
        sizing.setZoneCoolingDesignSupplyAirTemperatureInputMethod(
            "SupplyAirTemperature"
        )
        sizing.setZoneCoolingDesignSupplyAirTemperature(12.8)
        sizing.setZoneHeatingDesignSupplyAirTemperatureInputMethod(
            "SupplyAirTemperature"
        )
        sizing.setZoneHeatingDesignSupplyAirTemperature(40.0)
        sizing.setZoneCoolingSizingFactor(1.15)
        sizing.setZoneHeatingSizingFactor(1.25)

    summer = openstudio.model.DesignDay(model)
    summer.setName("SUMMER_DESIGN_DAY")
    summer.setMonth(7)
    summer.setDayOfMonth(21)
    summer.setDayType("SummerDesignDay")
    summer.setMaximumDryBulbTemperature(35.0)
    summer.setDailyDryBulbTemperatureRange(10.0)
    summer.setHumidityConditionType("Wetbulb")
    summer.setWetBulbOrDewPointAtMaximumDryBulb(26.0)
    summer.setBarometricPressure(100000.0)
    summer.setWindSpeed(3.0)
    summer.setWindDirection(180.0)
    summer.setRainIndicator(False)
    summer.setSnowIndicator(False)
    summer.setSolarModelIndicator("ASHRAEClearSky")
    summer.setSkyClearness(1.0)

    winter = openstudio.model.DesignDay(model)
    winter.setName("WINTER_DESIGN_DAY")
    winter.setMonth(1)
    winter.setDayOfMonth(21)
    winter.setDayType("WinterDesignDay")
    winter.setMaximumDryBulbTemperature(-5.0)
    winter.setDailyDryBulbTemperatureRange(0.0)
    winter.setHumidityConditionType("Wetbulb")
    winter.setWetBulbOrDewPointAtMaximumDryBulb(-5.0)
    winter.setBarometricPressure(100000.0)
    winter.setWindSpeed(3.8)
    winter.setWindDirection(340.0)
    winter.setRainIndicator(False)
    winter.setSnowIndicator(False)
    winter.setSolarModelIndicator("ASHRAEClearSky")
    winter.setSkyClearness(0.0)

    for variable_name in (
        "Zone Air System Sensible Cooling Energy",
        "Zone Air System Sensible Cooling Rate",
    ):
        variable = openstudio.model.OutputVariable(variable_name, model)
        variable.setKeyValue("*")
        variable.setReportingFrequency("Hourly")
    for meter_name in (
        "Cooling:Electricity",
        "Electricity:Facility",
    ):
        meter = openstudio.model.OutputMeter(model)
        meter.setName(meter_name)
        meter.setReportingFrequency("Hourly")
    model.getOutputSQLite().setOptionType("SimpleAndTabular")


def _vertices(item) -> list[list[float]]:
    return [[float(point.x()), float(point.y()), float(point.z())] for point in item.vertices()]


def _extract_geometry_records(model):
    spaces = []
    for space in model.getSpaces():
        floor_vertices = [
            point
            for surface in space.surfaces()
            if surface.surfaceType() == "Floor"
            for point in _vertices(surface)
        ]
        zone = space.thermalZone()
        spaces.append(
            {
                "name": space.nameString(),
                "floor_area_m2": float(space.floorArea()),
                "space_multiplier": float(space.multiplier()),
                "zone_multiplier": (
                    float(zone.get().multiplier()) if zone.is_initialized() else 1.0
                ),
                "floor_vertices": floor_vertices,
            }
        )

    surfaces = []
    for surface in model.getSurfaces():
        adjacent = surface.adjacentSurface()
        space = surface.space()
        surfaces.append(
            {
                "name": surface.nameString(),
                "space_name": (
                    space.get().nameString() if space.is_initialized() else ""
                ),
                "surface_type": surface.surfaceType(),
                "outside_boundary_condition": surface.outsideBoundaryCondition(),
                "adjacent_surface_name": (
                    adjacent.get().nameString() if adjacent.is_initialized() else None
                ),
                "gross_area_m2": float(surface.grossArea()),
                "net_area_m2": float(surface.netArea()),
                "vertices": _vertices(surface),
            }
        )

    subsurfaces = []
    for subsurface in model.getSubSurfaces():
        parent = subsurface.surface()
        subsurfaces.append(
            {
                "name": subsurface.nameString(),
                "parent_surface_name": (
                    parent.get().nameString() if parent.is_initialized() else ""
                ),
                "gross_area_m2": float(subsurface.grossArea()),
                "vertices": _vertices(subsurface),
            }
        )
    return spaces, surfaces, subsurfaces


def build(
    project_path: Path,
    output_dir: Path,
    floor_area_tolerance_m2: float = 0.01,
    coordinate_tolerance_m: float = 1e-5,
) -> dict[str, object]:
    project = json.loads(project_path.read_text(encoding="utf-8-sig"))
    output_dir.mkdir(parents=True, exist_ok=True)
    model = openstudio.model.Model()
    warnings: list[str] = []

    building = model.getBuilding()
    building.setName(str(project["project"]["name"]))
    building.setNorthAxis(float(project["site"]["north_axis_deg"]))
    site = model.getSite()
    site.setLatitude(float(project["site"]["latitude"]))
    site.setLongitude(float(project["site"]["longitude"]))
    site.setTimeZone(float(project["site"]["time_zone"]))
    site.setElevation(float(project["site"]["elevation_m"]))

    materials = _make_materials(model, project["materials"])
    constructions, construction_uses = _make_constructions(
        model, project["constructions"], materials
    )
    opaque = [
        constructions[str(record["id"])]
        for record in project["constructions"]
        if str(record.get("kind", "opaque")).casefold() != "window"
    ]
    roof_construction = _construction_for_use(
        constructions, construction_uses, "屋顶", "roof"
    )
    wall_construction = _construction_for_use(
        constructions, construction_uses, "外墙", "wall"
    )
    floor_construction = _construction_for_use(
        constructions, construction_uses, "地面", "楼板", "floor", "slab"
    )
    if roof_construction is None or wall_construction is None:
        raise ValueError("构造数据必须至少包含屋顶构造和外墙构造。")
    if floor_construction is None:
        if not opaque:
            raise ValueError("没有可用于地面的不透明构造。")
        floor_construction = opaque[0]
        warnings.append(
            "未提供地面/楼板构造，第一版模型暂用首个不透明构造；"
            "进入正式计算前必须在输入模板中补充实际地面构造。"
        )

    floors = {str(item["id"]): item for item in project["floors"]}
    stories = {}
    for floor_id, record in floors.items():
        story = openstudio.model.BuildingStory(model)
        story.setName(floor_id)
        story.setNominalZCoordinate(float(record["elevation_m"]))
        story.setNominalFloortoFloorHeight(float(record["height_m"]))
        stories[floor_id] = story

    space_types = {}
    zone_spaces = {}
    thermal_zones = {}
    for record in project["zones"]:
        zone_id = str(record["id"])
        floor_id = str(record["floor_id"])
        floor = floors[floor_id]
        points = _clockwise_polygon(
            record["polygon_xy"], float(floor["elevation_m"])
        )
        optional_space = openstudio.model.Space.fromFloorPrint(
            points, float(floor["height_m"]), model
        )
        if not optional_space.is_initialized():
            raise ValueError(f"热区 {zone_id} 的地面多边形无法生成空间。")
        space = optional_space.get()
        space.setName(f"SPACE_{zone_id}")
        space.setBuildingStory(stories[floor_id])
        space.setPartofTotalFloorArea(True)
        space_type_name = str(record.get("space_type", "未分类"))
        # 每个热区使用独立 SpaceType，避免 OpenStudio 按随机句柄重排
        # 同一 SpaceList 中的成员，从而保证重复生成的 IDF 字节一致。
        space_type_key = f"{space_type_name}_{zone_id}"
        if space_type_key not in space_types:
            space_type = openstudio.model.SpaceType(model)
            space_type.setName(space_type_key)
            space_types[space_type_key] = space_type
        space.setSpaceType(space_types[space_type_key])

        thermal_zone = openstudio.model.ThermalZone(model)
        thermal_zone.setName(zone_id)
        multiplier = float(record.get("multiplier", 1)) * float(
            floor.get("multiplier", 1)
        )
        thermal_zone.setMultiplier(int(round(multiplier)))
        space.setThermalZone(thermal_zone)
        zone_spaces[zone_id] = space
        thermal_zones[zone_id] = thermal_zone

    spaces = openstudio.model.SpaceVector()
    for space in model.getSpaces():
        spaces.append(space)
    openstudio.model.intersectSurfaces(spaces)
    openstudio.model.matchSurfaces(spaces)

    for surface in model.getSurfaces():
        surface_type = surface.surfaceType()
        boundary = surface.outsideBoundaryCondition()
        if boundary == "Surface" and surface_type in {"Floor", "RoofCeiling"}:
            surface.setConstruction(floor_construction)
        elif surface_type == "RoofCeiling":
            surface.setConstruction(roof_construction)
        elif surface_type == "Wall":
            surface.setConstruction(wall_construction)
        elif surface_type == "Floor":
            surface.setConstruction(floor_construction)

    directions = {"north": 0.0, "east": 90.0, "south": 180.0, "west": 270.0}
    for record in project["windows"]:
        zone_id = str(record["zone_id"])
        space = zone_spaces[zone_id]
        orientation = str(record["orientation"]).casefold()
        if orientation not in directions:
            raise ValueError(
                f"门窗 {record['id']} 的方向必须为 North/East/South/West。"
            )
        candidates = [
            surface
            for surface in space.surfaces()
            if surface.surfaceType() == "Wall"
            and surface.outsideBoundaryCondition() == "Outdoors"
        ]
        if not candidates:
            raise ValueError(f"热区 {zone_id} 没有可设置门窗的室外墙。")
        target = directions[orientation]
        wall = min(
            candidates,
            key=lambda item: _orientation_difference(
                math.degrees(item.azimuth()), target
            ),
        )
        if _orientation_difference(math.degrees(wall.azimuth()), target) > 45.0:
            raise ValueError(f"热区 {zone_id} 没有朝向 {orientation} 的外墙。")
        ratio = record.get("window_to_wall_ratio")
        if ratio is None:
            raise ValueError(f"门窗 {record['id']} 当前只支持窗墙比输入方式。")
        optional_window = wall.setWindowToWallRatio(float(ratio))
        if not optional_window.is_initialized():
            raise ValueError(f"门窗 {record['id']} 无法按窗墙比生成。")
        window = optional_window.get()
        window.setName(str(record["id"]))
        construction_id = str(record["construction_id"])
        if construction_id not in constructions:
            raise ValueError(f"门窗 {record['id']} 引用了未知窗构造 {construction_id}。")
        window.setConstruction(constructions[construction_id])

    schedules = _make_schedules(model, project["schedules"])
    _add_internal_loads(model, project["loads"], zone_spaces, schedules)
    _add_thermostats(model, project["thermostats"], thermal_zones)
    _add_hvac(model, project["hvac"], thermal_zones, schedules, warnings)
    _add_sizing_and_outputs(model, thermal_zones)

    input_floor_area = sum(
        _polygon_area(record["polygon_xy"])
        * float(record.get("multiplier", 1))
        * float(floors[str(record["floor_id"])].get("multiplier", 1))
        for record in project["zones"]
    )
    spaces_geometry, surfaces_geometry, subsurfaces_geometry = (
        _extract_geometry_records(model)
    )
    declared_floor_area = project["project"].get("total_floor_area_m2")
    geometry_validation = assess_geometry(
        declared_floor_area_m2=(
            float(declared_floor_area) if declared_floor_area is not None else None
        ),
        input_floor_area_m2=input_floor_area,
        spaces=spaces_geometry,
        surfaces=surfaces_geometry,
        subsurfaces=subsurfaces_geometry,
        floor_area_tolerance_m2=floor_area_tolerance_m2,
        coordinate_tolerance_m=coordinate_tolerance_m,
    )
    geometry_validation_path = output_dir / "geometry_validation.json"
    geometry_validation_path.write_text(
        json.dumps(geometry_validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not geometry_validation["all_passed"]:
        failed = [
            name
            for name, check in geometry_validation["checks"].items()
            if not check["passed"]
        ]
        raise ValueError("阶段六几何验收失败：" + ";".join(failed))
    model_floor_area = float(geometry_validation["areas"]["model_floor_area_m2"])

    osm_path = output_dir / "building.osm"
    idf_path = output_dir / "building_v25_2.idf"
    if not model.save(openstudio.path(str(osm_path)), True):
        raise RuntimeError(f"无法保存 OSM：{osm_path}")
    translator = openstudio.energyplus.ForwardTranslator()
    workspace = translator.translateModel(model)
    translator_errors = [str(item.logMessage()) for item in translator.errors()]
    translator_warnings = [str(item.logMessage()) for item in translator.warnings()]
    if translator_errors:
        raise RuntimeError("OpenStudio 正向转换失败：" + "；".join(translator_errors))
    if not workspace.save(openstudio.path(str(idf_path)), True):
        raise RuntimeError(f"无法保存 IDF：{idf_path}")

    manifest = {
        "openstudio_version": openstudio.openStudioVersion(),
        "energyplus_schema_version": "25.2",
        "project_name": str(project["project"]["name"]),
        "osm_path": str(osm_path.resolve()),
        "idf_25_2_path": str(idf_path.resolve()),
        "story_count": len(model.getBuildingStorys()),
        "space_count": len(model.getSpaces()),
        "thermal_zone_count": len(model.getThermalZones()),
        "input_floor_area_m2": input_floor_area,
        "model_floor_area_m2": model_floor_area,
        "floor_area_matches_input": True,
        "geometry_validation_status": geometry_validation["status"],
        "geometry_validation_path": str(geometry_validation_path.resolve()),
        "surface_count": len(model.getSurfaces()),
        "window_count": len(model.getSubSurfaces()),
        "material_count": len(materials),
        "construction_count": len(constructions),
        "schedule_count": len(schedules),
        "people_count": len(model.getPeoples()),
        "lights_count": len(model.getLightss()),
        "electric_equipment_count": len(model.getElectricEquipments()),
        "thermostat_count": len(model.getThermostatSetpointDualSetpoints()),
        "ideal_loads_system_count": len(model.getZoneHVACIdealLoadsAirSystems()),
        "packaged_heat_pump_count": len(
            model.getZoneHVACPackagedTerminalHeatPumps()
        ),
        "design_day_count": len(model.getDesignDays()),
        "output_variable_count": len(model.getOutputVariables()),
        "output_meter_count": len(model.getOutputMeters()),
        "warnings": warnings + translator_warnings,
    }
    manifest_path = output_dir / "build_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    if len(sys.argv) not in {3, 5}:
        raise SystemExit(
            "用法：openstudio_worker.py 项目.json 输出目录 "
            "[楼面面积容差_m2 坐标容差_m]"
        )
    area_tolerance = float(sys.argv[3]) if len(sys.argv) == 5 else 0.01
    coordinate_tolerance = float(sys.argv[4]) if len(sys.argv) == 5 else 1e-5
    manifest = build(
        Path(sys.argv[1]).resolve(),
        Path(sys.argv[2]).resolve(),
        area_tolerance,
        coordinate_tolerance,
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
