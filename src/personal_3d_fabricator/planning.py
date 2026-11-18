"""Conservative planning from fixed-orientation bounding boxes in millimeters."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat, TypeAdapter

from .profiles import supported_material
from .models import BuildVolume, NonEmptyText, NonNegativeMillimeters, PrinterProfile, PrintPlan

Axis = Literal["x", "y", "z"]
_AXES: tuple[Axis, Axis, Axis] = ("x", "y", "z")
_DEFAULT_CLEARANCE_MM = 2.0
_DEFAULT_SPACING_MM = 2.0
_BOUNDS = TypeAdapter(BuildVolume)
_MARGIN = TypeAdapter(NonNegativeMillimeters)
_SCORES = TypeAdapter(dict[Axis, FiniteFloat])


@dataclass(frozen=True)
class CutCandidate:
    """Equal intervals along one axis, measured from the bounding-box minimum."""

    axis: Axis
    segment_count: int
    segment_bounds_mm: BuildVolume
    remaining_oversize_axes: tuple[Axis, ...]
    visible_seam_penalty: float
    connector_accessibility: float


class PartEnvelope(BaseModel):
    """A part ID and its box extents in the intended print orientation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    part_id: NonEmptyText
    bounds_mm: BuildVolume


@dataclass(frozen=True)
class PlatePlan:
    """Estimated part membership based on rectangular footprints."""

    part_ids: tuple[str, ...]


def _dimensions(bounds_mm: BuildVolume) -> tuple[Fraction, Fraction, Fraction]:
    x, y, z = _BOUNDS.validate_python(bounds_mm)
    # Use the supplied decimal precision so equality keeps the reserved clearance.
    return Fraction(str(x)), Fraction(str(y)), Fraction(str(z))


def _usable_volume(
    profile: PrinterProfile, clearance_mm: float
) -> tuple[Fraction, Fraction, Fraction]:
    clearance = Fraction(str(_MARGIN.validate_python(clearance_mm)))
    x, y, z = _dimensions(profile.build_volume_mm)
    return x - clearance, y - clearance, z - clearance


def fits_build_volume(
    bounds_mm: BuildVolume, profile: PrinterProfile, clearance_mm: float = _DEFAULT_CLEARANCE_MM
) -> bool:
    """Check the given XYZ orientation with total clearance reserved on each axis."""
    return all(
        extent <= capacity
        for extent, capacity in zip(
            _dimensions(bounds_mm), _usable_volume(profile, clearance_mm)
        )
    )


def propose_axis_cuts(
    bounds_mm: BuildVolume,
    profile: PrinterProfile,
    *,
    visible_seam_penalties: Mapping[Axis, float] | None = None,
    connector_accessibility: Mapping[Axis, float] | None = None,
) -> list[CutCandidate]:
    """Propose the minimum number of equal segments for each oversize axis.

    A candidate describes parallel planes perpendicular to its axis. Each plane
    is at a successive multiple of the segment length from the box minimum.
    Any axes that still exceed capacity are recorded on the candidate. Segment
    bounds are approximate float dimensions for preview. Missing caller scores
    default to zero. Lower seam penalty and higher accessibility rank first
    after segment count.
    """
    bounds = _dimensions(bounds_mm)
    usable = _usable_volume(profile, _DEFAULT_CLEARANCE_MM)
    penalties = _SCORES.validate_python(visible_seam_penalties or {})
    accessibility = _SCORES.validate_python(connector_accessibility or {})
    if min(usable) <= 0:
        return []

    oversize = [index for index, extent in enumerate(bounds) if extent > usable[index]]
    candidates = []
    for index in oversize:
        axis = _AXES[index]
        segment_count = ceil(bounds[index] / usable[index])
        segment_bounds = list(bounds)
        segment_bounds[index] /= segment_count
        x, y, z = (float(extent) for extent in segment_bounds)
        candidates.append(
            CutCandidate(
                axis=axis,
                segment_count=segment_count,
                segment_bounds_mm=(x, y, z),
                remaining_oversize_axes=tuple(_AXES[other] for other in oversize if other != index),
                visible_seam_penalty=penalties.get(axis, 0.0),
                connector_accessibility=accessibility.get(axis, 0.0),
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.segment_count,
            candidate.visible_seam_penalty,
            -candidate.connector_accessibility,
            candidate.axis,
        ),
    )


@dataclass
class _Row:
    depth: Fraction
    used_width: Fraction


@dataclass
class _Plate:
    rows: list[_Row] = field(default_factory=list)
    used_depth: Fraction = Fraction(0)
    part_ids: list[str] = field(default_factory=list)

    def try_add(
        self,
        part_id: str,
        width: Fraction,
        depth: Fraction,
        plate_width: Fraction,
        plate_depth: Fraction,
        spacing: Fraction,
    ) -> bool:
        for row in self.rows:
            used_width = row.used_width + spacing + width
            if depth <= row.depth and used_width <= plate_width:
                row.used_width = used_width
                self.part_ids.append(part_id)
                return True

        used_depth = self.used_depth + (spacing if self.rows else 0) + depth
        if used_depth > plate_depth:
            return False
        self.rows.append(_Row(depth, width))
        self.used_depth = used_depth
        self.part_ids.append(part_id)
        return True


def group_parts_on_plates(
    parts: list[PartEnvelope],
    profile: PrinterProfile,
    *,
    spacing_mm: float = _DEFAULT_SPACING_MM,
    clearance_mm: float = _DEFAULT_CLEARANCE_MM,
) -> list[PlatePlan]:
    """Estimate plate membership using fixed-orientation rectangular rows.

    Sort by descending XY footprint area and then part ID. For each part,
    stop at the first plate whose first compatible row or new row has space.
    Spacing is reserved between parts within a row and between adjacent rows.
    The result contains part IDs for each plate estimate.
    """
    usable = _usable_volume(profile, clearance_mm)
    plate_width, plate_depth, _ = usable
    spacing = Fraction(str(_MARGIN.validate_python(spacing_mm)))
    seen = set()
    envelopes = []
    for part in parts:
        if part.part_id in seen:
            raise ValueError(f"Duplicate part ID: {part.part_id}")
        seen.add(part.part_id)
        dimensions = _dimensions(part.bounds_mm)
        if any(extent > capacity for extent, capacity in zip(dimensions, usable)):
            raise ValueError(f"Part {part.part_id!r} exceeds the usable build volume")
        width, depth, _ = dimensions
        envelopes.append((width, depth, part.part_id))

    envelopes.sort(key=lambda envelope: (-(envelope[0] * envelope[1]), envelope[2]))
    plates: list[_Plate] = []
    for width, depth, part_id in envelopes:
        for plate in plates:
            if plate.try_add(part_id, width, depth, plate_width, plate_depth, spacing):
                break
        else:
            plates.append(_Plate(rows=[_Row(depth, width)], used_depth=depth, part_ids=[part_id]))
    return [PlatePlan(part_ids=tuple(plate.part_ids)) for plate in plates]


def estimate_print_plan_plates(
    plan: PrintPlan,
    profile: PrinterProfile,
    *,
    spacing_mm: float = _DEFAULT_SPACING_MM,
    clearance_mm: float = _DEFAULT_CLEARANCE_MM,
) -> PrintPlan:
    """Revalidate a separate plan with estimated plates for its oriented parts."""
    material = supported_material(plan.material, profile)
    parts = []
    for part in plan.parts:
        if part.bounds_mm is None:
            raise ValueError(f"Part {part.part_id!r} requires measured XYZ bounds")
        parts.append(PartEnvelope(part_id=part.part_id, bounds_mm=part.bounds_mm))
    groups = group_parts_on_plates(parts, profile, spacing_mm=spacing_mm, clearance_mm=clearance_mm)
    data = plan.model_dump(exclude={"plates"})
    data["material"] = material
    data["plates"] = tuple(group.part_ids for group in groups)
    return PrintPlan.model_validate(data)
