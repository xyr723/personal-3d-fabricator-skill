"""Parametric connector tools and local preview placements in millimetres."""

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

import fcl
import numpy as np
import shapely
import trimesh
from pydantic.dataclasses import dataclass as validated

from .adapters.cadquery_adapter import CadQueryUnavailable, load_cadquery
from .models import PinSocketSpec, PositiveMillimeters

if TYPE_CHECKING:
    from cadquery import Solid


@validated(frozen=True)
class MagnetPocketSpec:
    diameter_mm: PositiveMillimeters
    depth_mm: PositiveMillimeters


@validated(frozen=True)
class ScrewInsertSpec:
    """Plain cylindrical bore sized to the caller's insert."""

    diameter_mm: PositiveMillimeters
    depth_mm: PositiveMillimeters


Row = tuple[float, float, float, float]
Transform = tuple[Row, Row, Row, Row]


def _placement(x: float = 0.0) -> Transform:
    return ((1, 0, 0, x), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))


@dataclass(frozen=True)
class ConnectorPair:
    pin: "Solid"
    socket: "Solid"
    socket_diameter_mm: float
    placement_transforms: tuple[Transform, ...]


@dataclass(frozen=True)
class PocketTool:
    solid: "Solid"
    placement_transform: Transform = _placement()


def _cylinder(diameter: float, depth: float) -> "Solid":
    try:
        from cadquery import Solid
    except ModuleNotFoundError as error:
        if error.name != "cadquery":
            raise
        raise ModuleNotFoundError("cadquery extra not installed") from error
    return Solid.makeCylinder(diameter / 2, depth)


def build_pin_socket_pair(spec: PinSocketSpec) -> ConnectorPair:
    """Preview tools along local +Z with two socket diameters between centres.

    Each placement maps both tools into the same local seam coordinate system.
    The caller chooses the seam frame and confirms positions on the real parts.
    """
    diameter = spec.pin_diameter_mm + 2 * spec.clearance_mm
    if not isfinite(diameter) or not isfinite((spec.count - 1) * 2 * diameter):
        raise ValueError("derived connector dimensions must be finite")
    return ConnectorPair(
        pin=_cylinder(spec.pin_diameter_mm, spec.pin_length_mm),
        socket=_cylinder(diameter, spec.pin_length_mm),
        socket_diameter_mm=diameter,
        placement_transforms=tuple(_placement(i * 2 * diameter) for i in range(spec.count)),
    )


def build_magnet_pocket(spec: MagnetPocketSpec) -> PocketTool:
    return PocketTool(_cylinder(spec.diameter_mm, spec.depth_mm))


def build_screw_insert(spec: ScrewInsertSpec) -> PocketTool:
    return PocketTool(_cylinder(spec.diameter_mm, spec.depth_mm))


class ConnectorBackendUnavailable(RuntimeError):
    """The selected geometry needs the optional CAD backend."""


def require_pin_backend():
    backend = load_cadquery()
    if isinstance(backend, CadQueryUnavailable):
        raise ConnectorBackendUnavailable(
            f"{backend.reason}. Install with python -m pip install 'personal-3d-fabricator[cadquery]'"
        )


def _cap(mesh, axis, plane, sign):
    """Extract the oriented planar patch, retaining its triangulation boundary nodes."""

    uv = [(axis + 1) % 3, (axis + 2) % 3]
    mask = np.all(np.isclose(mesh.triangles[:, :, axis], plane, rtol=0, atol=1e-8), axis=1)
    mask &= mesh.face_normals[:, axis] * sign > 0.999999
    if not mask.any():
        return mask, shapely.Polygon()
    faces = mesh.faces[mask]
    edges = np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    edges, counts = np.unique(np.sort(edges, axis=1), axis=0, return_counts=True)
    boundary = mesh.vertices[:, uv][edges[counts == 1]]
    return mask, shapely.build_area(shapely.multilinestrings(shapely.linestrings(boundary)))


def _tool_mesh(solid, axis, position):

    points, faces = solid.tessellate(0.02, 0.12)
    vertices = np.roll([p.toTuple() for p in points], axis + 1, axis=1) + position
    return trimesh.Trimesh(vertices=vertices, faces=faces)


def _apply_tool(mesh, tool, axis, plane, cap, material, socket, wall):
    """Splice an open cylinder onto the cut boundary, with reversed faces for the bore."""

    sign = -1 if socket else 1
    # Require an uninterrupted wall around and behind the tool, including cavities.
    # Narrow-phase solid-box vs triangle tests also catch surfaces wholly inside the box.
    low, high = tool.bounds.copy()
    low -= wall
    high += wall
    if socket:
        low[axis] = plane
    else:
        low[axis], high[axis] = plane - wall, plane
    other = mesh.submesh([~cap], append=True)
    surface = fcl.CollisionObject(trimesh.collision.mesh_to_BVH(other))
    envelope = fcl.CollisionObject(fcl.Box(*(high - low)), fcl.Transform((low + high) / 2))
    if fcl.collide(surface, envelope):
        raise ValueError("Insufficient uninterrupted material for pin/socket and nozzle wall")

    base, disk = _cap(tool, axis, plane, -1)
    if not material.contains_properly(disk):
        raise ValueError("Connector footprint crosses the seam boundary")
    ring = shapely.difference(material, disk)
    triangles = shapely.constrained_delaunay_triangles(ring)
    triangles = shapely.orient_polygons(triangles, exterior_cw=sign < 0)
    uv = shapely.get_coordinates(triangles).reshape(-1, 4, 2)[:, :3, :].reshape(-1, 2)
    vertices = np.roll(np.column_stack((uv, np.full(len(uv), plane))), axis + 1, axis=1)
    new_cap = trimesh.Trimesh(vertices=vertices, faces=np.arange(len(vertices)).reshape(-1, 3))
    sides = tool.submesh([~base], append=True)
    if socket:
        sides.invert()
    result = trimesh.util.concatenate([other, new_cap, sides])
    result.merge_vertices()
    if not result.is_volume or not np.isclose(
        result.volume, mesh.volume + sign * tool.volume, rtol=1e-8, atol=1e-7
    ):
        raise ValueError("Connector did not produce a closed pin/socket of the requested volume")
    return result


@dataclass(frozen=True)
class _PinSite:
    step: int
    male: int
    female: int
    axis: int
    center: np.ndarray

    def record(self):
        return {"assembly_step": self.step, "pin_part": f"part-{self.male + 1:03}",
                "socket_part": f"part-{self.female + 1:03}", "axis": "+" + "XYZ"[self.axis],
                "center_mm": self.center.tolist()}


def _pin_sites(parts, seams, spec, wall):
    """Derive every required interface from the flat material sections before adding pins."""
    sites = []
    socket_radius = spec.pin_diameter_mm / 2 + spec.clearance_mm
    for step, (axis, plane, left, right) in enumerate(seams, 1):
        previous = len(sites)
        for i in left:
            for j in right:
                _, male_face = _cap(parts[i], axis, plane, 1)
                _, female_face = _cap(parts[j], axis, plane, -1)
                shared = male_face.intersection(female_face)
                if shared.area == 0:
                    continue
                if shared.geom_type != "Polygon":
                    raise ValueError(
                        "The seam has separate material regions. Process bodies separately or use --connector flat."
                    )
                inset = shared.buffer(-(socket_radius + wall))
                if inset.is_empty:
                    raise ValueError("Seam is too narrow for the specified pin and clearance")
                middle = shared.centroid
                if not inset.covers(middle):
                    middle = inset.representative_point()
                sites.append(_PinSite(step, i, j, axis, np.roll([middle.x, middle.y, plane], axis + 1)))
        if len(sites) == previous:
            raise ValueError("No shared material for the requested pin connection")
    return sites


def apply_pin_connections(parts, seams, spec, wall):
    """Place one coaxial pin/socket pair between each pair of adjoining printed parts."""
    sites = _pin_sites(parts, seams, spec, wall)
    tools = build_pin_socket_pair(spec)
    for site in sites:
        axis = site.axis
        plane = site.center[axis]
        for index, solid, socket in ((site.male, tools.pin, False), (site.female, tools.socket, True)):
            cap, material = _cap(parts[index], axis, plane, -1 if socket else 1)
            tool = _tool_mesh(solid, axis, site.center)
            parts[index] = _apply_tool(parts[index], tool, axis, plane, cap, material, socket, wall)
    return [site.record() for site in sites]


def _has_cylindrical_feature(mesh, site, radius, length, sign):
    """Identify the complete oriented cylindrical wall, its closed end and its join to the seam."""
    axis = site.axis
    uv = [(axis + 1) % 3, (axis + 2) % 3]
    # STL stores absolute vertex coordinates as float32. Fail closed if this scale
    # cannot distinguish the root, end or diameter of the requested feature.
    tol = max(1e-8, 2 * float(np.spacing(np.float32(np.abs(site.center).max() + length + radius))))
    if tol >= min(radius, length) * 0.01:
        return False
    local = mesh.vertices - site.center
    axial = local[:, axis]
    radial = np.linalg.norm(local[:, uv], axis=1)
    base = (np.abs(axial) <= tol)[mesh.faces]
    tip = (np.abs(axial - length) <= tol)[mesh.faces]
    on_circle = (np.abs(radial - radius) <= tol)[mesh.faces]
    normals = mesh.face_normals
    direction = (mesh.triangles_center - site.center)[:, uv]
    outward = np.einsum("ij,ij->i", normals[:, uv], direction)
    # Our printable prisms have two equal rings joined by vertical facets.
    sides = (on_circle.all(axis=1) & (base | tip).all(axis=1) & base.any(axis=1) & tip.any(axis=1)
             & (np.abs(normals[:, axis]) < 0.001) & (outward * sign > radius * 0.99))
    lid = (tip.all(axis=1) & (radial[mesh.faces] <= radius + tol).all(axis=1)
           & (normals[:, axis] * sign > 0.999))
    feature = sides | lid
    if not sides.any() or not lid.any():
        return False

    # Every edge of the oriented wall and lid must pair, except for the complete
    # base ring. That ring must share edges with the actual outward seam surface.
    face_edges = mesh.edges_unique_inverse.reshape(-1, 3)
    counts = np.bincount(face_edges[feature].ravel())
    rim = np.flatnonzero(counts == 1)
    shoulder = base.all(axis=1) & (normals[:, axis] * sign > 0.999)
    if not len(rim) or not np.isin(rim, face_edges[shoulder]).all():
        return False
    segments = local[:, uv][mesh.edges_unique[rim]]
    disk = shapely.build_area(shapely.multilinestrings(shapely.linestrings(segments)))
    if disk.geom_type != "Polygon" or not disk.covers(shapely.Point(0, 0).buffer(radius * 0.99)):
        return False

    # A closed extra shell inside the bore or pin must not obstruct its working
    # volume. The planner already reserves at least one nozzle wall around it.
    low = site.center - radius
    high = site.center + radius
    low[axis] = site.center[axis] + tol
    high[axis] = site.center[axis] + length - tol
    candidates = list(mesh.triangles_tree.intersection(np.r_[low, high]))
    other = [index for index in candidates if not feature[index]]
    if other:
        surface = fcl.CollisionObject(trimesh.collision.mesh_to_BVH(mesh.submesh([other], append=True)))
        envelope = fcl.CollisionObject(fcl.Box(*(high - low)), fcl.Transform((low + high) / 2))
        if fcl.collide(surface, envelope):
            return False
    return True


def verify_pin_connections(flat, seams, printed, records, spec, wall):
    """Check saved part pairs and actual STL material against the approved source sections."""
    sites = _pin_sites(flat, seams, spec, wall)
    if not sites or len(flat) != len(printed) or not isinstance(records, list) or len(records) != len(sites):
        raise ValueError("Recorded pin pairs do not cover all source interfaces")
    for site, saved in zip(sites, records, strict=True):
        expected = site.record()
        if not isinstance(saved, dict) or saved.keys() != expected.keys():
            raise ValueError("Pin interface record is incomplete")
        center = saved["center_mm"]
        if (any(saved[k] != expected[k] for k in expected if k != "center_mm")
                or not isinstance(center, list) or len(center) != 3
                or not all(type(v) in (float, int) and isfinite(v) for v in center)
                or not np.allclose(center, site.center, rtol=1e-12, atol=1e-7)):
            raise ValueError("Pin record differs from the source interface")
        pin_radius = spec.pin_diameter_mm / 2
        for index, radius, sign in ((site.male, pin_radius, 1),
                                    (site.female, pin_radius + spec.clearance_mm, -1)):
            if not _has_cylindrical_feature(printed[index], site, radius, spec.pin_length_mm, sign):
                raise ValueError(f"part-{index + 1:03} STL does not contain the required mating geometry")
