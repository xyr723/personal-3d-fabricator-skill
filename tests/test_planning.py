from math import inf, nextafter
from pathlib import Path

import pytest

from personal_3d_fabricator.models import InspectionReport, PrintPlan
from personal_3d_fabricator.planning import (
    PartEnvelope,
    estimate_print_plan_plates,
    fits_build_volume,
    group_parts_on_plates,
    propose_axis_cuts,
)
from personal_3d_fabricator.profiles import load_printer_profile


@pytest.fixture
def single_nozzle():
    path = Path(__file__).resolve().parents[1] / "profiles" / "generic-single-nozzle.json"
    return load_printer_profile(path)


def test_exact_fit_reserves_clearance(single_nozzle):
    assert not fits_build_volume((220.0, 100.0, 100.0), single_nozzle)
    assert fits_build_volume((218.0, 218.0, 248.0), single_nozzle)
    assert fits_build_volume((220.0, 220.0, 250.0), single_nozzle, clearance_mm=0)


def test_fitting_preserves_the_callers_orientation(single_nozzle):
    assert not fits_build_volume((230.0, 100.0, 100.0), single_nozzle)
    assert fits_build_volume((100.0, 100.0, 230.0), single_nozzle)


def test_clearance_compares_decimal_edges_without_accepting_larger_bounds(single_nozzle):
    profile = single_nozzle.model_copy(update={"build_volume_mm": (0.3, 0.3, 0.3)})
    assert fits_build_volume((0.2, 0.2, 0.2), profile, clearance_mm=0.1)
    assert not fits_build_volume((nextafter(0.2, inf), 0.2, 0.2), profile, clearance_mm=0.1)


@pytest.mark.parametrize("size", [0, -1, inf, float("nan"), True, "20"])
def test_planning_rejects_invalid_extents(single_nozzle, size):
    bounds = (size, 10, 10)
    with pytest.raises(ValueError):
        fits_build_volume(bounds, single_nozzle)
    with pytest.raises(ValueError):
        propose_axis_cuts(bounds, single_nozzle)
    with pytest.raises(ValueError):
        PartEnvelope(part_id="A", bounds_mm=bounds)


@pytest.mark.parametrize("margin", [-1, inf, float("nan")])
def test_planning_rejects_invalid_clearance_and_spacing(single_nozzle, margin):
    with pytest.raises(ValueError):
        fits_build_volume((10, 10, 10), single_nozzle, clearance_mm=margin)
    with pytest.raises(ValueError):
        group_parts_on_plates([], single_nozzle, spacing_mm=margin)


def test_oversized_axis_gets_minimum_equal_segments(single_nozzle):
    cuts = propose_axis_cuts((500.0, 100.0, 100.0), single_nozzle)

    assert len(cuts) == 1
    best = cuts[0]
    assert best.axis == "x"
    assert best.segment_count == 3
    assert best.segment_bounds_mm == pytest.approx((500 / 3, 100, 100))
    assert best.remaining_oversize_axes == ()
    assert fits_build_volume(best.segment_bounds_mm, single_nozzle)


def test_exact_segment_boundary_uses_minimum_count(single_nozzle):
    profile = single_nozzle.model_copy(update={"build_volume_mm": (2.1, 10, 10)})
    assert propose_axis_cuts((0.3, 4, 4), profile)[0].segment_count == 3
    assert propose_axis_cuts((nextafter(0.3, inf), 4, 4), profile)[0].segment_count == 4


def test_cut_candidates_are_empty_when_clearance_already_fits(single_nozzle):
    assert propose_axis_cuts((218.0, 218.0, 248.0), single_nozzle) == []


def test_clearance_can_leave_no_usable_axis(single_nozzle):
    profile = single_nozzle.model_copy(update={"build_volume_mm": (2, 10, 10)})
    assert not fits_build_volume((1, 1, 1), profile)
    assert propose_axis_cuts((1, 1, 1), profile) == []


def test_cut_ranking_prioritizes_count_then_seam_and_accessibility(single_nozzle):
    bounds = (600, 300, 300)
    cuts = propose_axis_cuts(
        bounds,
        single_nozzle,
        visible_seam_penalties={"y": 2, "z": 1},
        connector_accessibility={"y": 9, "z": 1},
    )
    assert [cut.axis for cut in cuts] == ["z", "y", "x"]
    assert cuts[0].remaining_oversize_axes == ("x", "y")

    cuts = propose_axis_cuts(bounds, single_nozzle, connector_accessibility={"z": 1})
    assert [cut.axis for cut in cuts] == ["z", "y", "x"]
    assert [cut.axis for cut in propose_axis_cuts(bounds, single_nozzle)] == ["y", "z", "x"]


def test_empty_parts_return_no_plate_groups(single_nozzle):
    assert group_parts_on_plates([], single_nozzle) == []


def test_grouping_sorts_by_footprint_then_id_and_uses_the_first_plate(single_nozzle):
    parts = [
        PartEnvelope(part_id="C", bounds_mm=(110, 100, 100)),
        PartEnvelope(part_id="B", bounds_mm=(100, 100, 100)),
        PartEnvelope(part_id="D", bounds_mm=(110, 100, 100)),
        PartEnvelope(part_id="A", bounds_mm=(218, 100, 100)),
    ]

    groups = group_parts_on_plates(parts, single_nozzle)
    assert [group.part_ids for group in groups] == [("A", "C", "B"), ("D",)]
    assert group_parts_on_plates(list(reversed(parts)), single_nozzle) == groups


@pytest.mark.parametrize("bounds", [(1, 218, 1), (218, 1, 1)])
def test_spacing_is_reserved_inside_and_between_rows(single_nozzle, bounds):
    parts = [PartEnvelope(part_id=part_id, bounds_mm=bounds) for part_id in ("A", "B")]

    assert len(group_parts_on_plates(parts, single_nozzle, spacing_mm=216)) == 1
    assert len(group_parts_on_plates(parts, single_nozzle, spacing_mm=nextafter(216, inf))) == 2


def test_grouping_does_not_accept_area_only_fit(single_nozzle):
    parts = [PartEnvelope(part_id=part_id, bounds_mm=(120, 120, 1)) for part_id in ("A", "B")]

    assert len(group_parts_on_plates(parts, single_nozzle)) == 2


@pytest.mark.parametrize("bounds", [(219, 1, 1), (1, 219, 1), (1, 1, 249)])
def test_grouping_rejects_parts_exceeding_any_printer_axis(single_nozzle, bounds):
    with pytest.raises(ValueError, match="oversized"):
        group_parts_on_plates([PartEnvelope(part_id="oversized", bounds_mm=bounds)], single_nozzle)


def test_grouping_rejects_duplicate_part_ids(single_nozzle):
    part = PartEnvelope(part_id="A", bounds_mm=(10, 10, 10))
    with pytest.raises(ValueError, match="A"):
        group_parts_on_plates([part, part], single_nozzle)


@pytest.fixture
def measured_plan():
    return PrintPlan(
        inspection=InspectionReport(
            format="stl",
            bounds_mm=(300, 100, 100),
            watertight=True,
            winding_consistent=True,
            component_count=1,
            face_count=12,
            degenerate_face_count=0,
            issues=[{"code": "mesh_note", "severity": "info", "message": "Measured"}],
        ),
        parts=[
            {"part_id": "A", "bounds_mm": (150, 100, 100)},
            {"part_id": "B", "bounds_mm": (150, 100, 100)},
        ],
    )


def test_print_plan_uses_estimated_part_groups_and_preserves_the_original(
    measured_plan, single_nozzle
):
    estimated = estimate_print_plan_plates(measured_plan, single_nozzle)

    assert estimated.plates == (("A", "B"),)
    assert estimated.parts == measured_plan.parts
    assert estimated.inspection == measured_plan.inspection
    assert measured_plan.plates == ()


def test_estimated_print_plan_is_independent_of_input_changes(measured_plan, single_nozzle):
    estimated = estimate_print_plan_plates(measured_plan, single_nozzle)

    measured_plan.parts[0].part_id = "new-id"
    measured_plan.inspection.issues[0].message = "Changed"

    assert estimated.parts[0].part_id == "A"
    assert estimated.plates == (("A", "B"),)
    assert estimated.inspection.issues[0].message == "Measured"


def test_estimated_print_plan_normalizes_ids_without_changing_input(measured_plan, single_nozzle):
    measured_plan.parts[0].part_id = " A "
    before = measured_plan.model_dump()

    estimated = estimate_print_plan_plates(measured_plan, single_nozzle)

    assert estimated.plates == (("A", "B"),)
    assert tuple(part.part_id for part in estimated.parts) == estimated.plates[0]
    assert measured_plan.model_dump() == before


def test_print_plan_rejects_unmeasured_parts(single_nozzle):
    plan = PrintPlan(
        inspection=InspectionReport(
            format="stl",
            bounds_mm=None,
            watertight=True,
            winding_consistent=True,
            component_count=1,
            face_count=12,
            degenerate_face_count=0,
        ),
        parts=[{"part_id": "A"}],
    )

    with pytest.raises(ValueError, match="A"):
        estimate_print_plan_plates(plan, single_nozzle)
