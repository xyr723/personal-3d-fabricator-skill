"""Approved parallel plane cuts, in the source mesh's millimetre coordinates."""

from dataclasses import dataclass
from itertools import combinations
from math import fsum, isfinite
from pathlib import Path

import fcl
import numpy as np
from pydantic import TypeAdapter
import shapely
import trimesh

from .approved_source import open_approved_source, read_approved_mesh
from .models import ApprovalState, BuildVolume, NonNegativeMillimeters
from .planning import CutCandidate, fits_build_volume, propose_axis_cuts
from .projects import project_lock, read_project_manifest


@dataclass(frozen=True)
class SplitResult:
    """Capped slabs ordered from minimum to maximum along the candidate axis."""

    parts: tuple[trimesh.Trimesh, ...]


def split_project_mesh(
    project_dir: Path,
    cut: CutCandidate,
    *,
    min_fragment_volume_mm3: float = 1.0,
    volume_tolerance: float = 1e-4,
) -> SplitResult:
    """Cut the approved archive in its original millimetre coordinates."""
    with project_lock(project_dir) as project:
        manifest = read_project_manifest(project.path, project=project)
        with open_approved_source(project, manifest) as (source, approval):
            _, mesh = read_approved_mesh(source, approval)
        return split_mesh(
            mesh, cut, approval_state=manifest.approval_state,
            min_fragment_volume_mm3=min_fragment_volume_mm3, volume_tolerance=volume_tolerance,
        )


def material_components(mesh: trimesh.Trimesh) -> list[tuple[trimesh.Trimesh, float]]:
    """Associate each solid shell with its immediate cavity shells."""
    shells = mesh.split(only_watertight=False, repair=False)
    if not all(
        s.is_watertight and s.is_winding_consistent and isfinite(s.volume) and s.volume != 0
        for s in shells
    ):
        raise ValueError("invalid watertight shell")
    parents: list[int | None] = [None] * len(shells)
    surfaces = {}
    for i, j in combinations(range(len(shells)), 2):
        a, b = shells[i], shells[j]
        if (a.bounds[1] < b.bounds[0]).any() or (b.bounds[1] < a.bounds[0]).any():
            continue
        for index in (i, j):
            if index not in surfaces:
                surfaces[index] = fcl.CollisionObject(trimesh.collision.mesh_to_BVH(shells[index]))
        if fcl.collide(surfaces[i], surfaces[j]):
            raise ValueError("shell surfaces intersect or touch")

        # Disjoint connected surfaces cannot cross. One vertex classifies
        # containment; the closest enclosing shell has the smallest volume.
        outer, inner = (i, j) if abs(a.volume) > abs(b.volume) else (j, i)
        if shells[outer].contains(shells[inner].vertices[:1])[0]:
            parent = parents[inner]
            if parent is None or abs(shells[outer].volume) < abs(shells[parent].volume):
                parents[inner] = outer

    volumes = [[s.volume] for s in shells]
    for index, parent in enumerate(parents):
        positive = shells[index].volume > 0
        if positive != (parent is None or shells[parent].volume < 0):
            raise ValueError("shell winding conflicts with spatial containment")
        if not positive:
            volumes[parent].append(shells[index].volume)
    return [(s, fsum(v)) for s, v in zip(shells, volumes) if s.volume > 0]


def _capped_half(mesh: trimesh.Trimesh, origin, normal, axis: int) -> trimesh.Trimesh:
    """Triangulate the material section while preserving every cut boundary edge."""
    half = mesh.slice_plane(origin, normal, cap=False, process=True)
    edge_counts = np.bincount(half.edges_unique_inverse)
    boundary = half.edges_unique[edge_counts == 1]
    if not len(boundary):
        return half

    # Cyclic coordinate order gives the 2D plane the positive axis normal.
    xy = half.vertices[:, [(axis + 1) % 3, (axis + 2) % 3]]
    # build_area applies even/odd nesting to the closed boundary rings.
    material = shapely.build_area(shapely.multilinestrings(shapely.linestrings(xy[boundary])))
    triangles = shapely.constrained_delaunay_triangles(material)
    # Caps face outward, opposite the retained half-space normal.
    triangles = shapely.orient_polygons(triangles, exterior_cw=bool(normal[axis] > 0))
    coords = shapely.get_coordinates(triangles).reshape(-1, 4, 2)
    indices = {tuple(xy[i]): i for i in np.unique(boundary)}
    faces = [[indices[tuple(vertex)] for vertex in ring[:3]] for ring in coords]
    if not faces:
        raise ValueError("cut boundary could not be capped")
    half.faces = np.vstack((half.faces, faces))
    return half


def split_mesh(
    mesh: trimesh.Trimesh,
    cut: CutCandidate,
    *,
    approval_state: ApprovalState,
    min_fragment_volume_mm3: float = 1.0,
    volume_tolerance: float = 1e-4,
) -> SplitResult:
    """Preview a planned split after overall approval. Tolerance is relative."""
    if (
        approval_state is not ApprovalState.overall_approved
        and approval_state is not ApprovalState.split_approved
    ):
        raise ValueError("overall_approved is required before splitting")
    if shapely.geos_version < (3, 10, 0):
        version = ".".join(map(str, shapely.geos_version))
        raise RuntimeError(
            f"Mesh splitting requires GEOS >= 3.10.0 (found {version}). "
            "Install a Shapely 2.2+ binary wheel or upgrade the linked GEOS library."
        )
    if not np.isfinite(mesh.vertices).all() or not mesh.is_volume or not isfinite(mesh.volume):
        raise ValueError("source must be a finite, consistently wound watertight solid")
    limits = TypeAdapter(NonNegativeMillimeters)
    minimum = limits.validate_python(min_fragment_volume_mm3)
    tolerance = limits.validate_python(volume_tolerance)
    dimensions = TypeAdapter(BuildVolume).validate_python(cut.segment_bounds_mm)
    if type(cut.segment_count) is not int or cut.segment_count < 2:
        raise ValueError("segment_count must be an integer of at least two")
    if not all(map(isfinite, (cut.visible_seam_penalty, cut.connector_accessibility))):
        raise ValueError("cut scores must be finite")
    axis = ("x", "y", "z").index(cut.axis)
    expected = mesh.extents.copy()
    expected[axis] /= cut.segment_count
    if not np.allclose(expected, dimensions, rtol=1e-9, atol=0.0):
        raise ValueError("candidate segment bounds must match the source mesh")
    normal = np.eye(3)[axis]
    origin = mesh.bounds[0].copy()
    # A plane touching a separate component's end face has a nonempty
    # section, but no material actually crosses that plane.
    intervals = [shell.bounds[:, axis] for shell, _ in material_components(mesh)]
    remainder = mesh
    slabs = []
    for index in range(1, cut.segment_count):
        origin[axis] = mesh.bounds[0, axis] + index * dimensions[axis]
        if not any(low < origin[axis] < high for low, high in intervals):
            raise ValueError("cut plane does not intersect the solid")
        slabs.append(_capped_half(remainder, origin, -normal, axis))
        remainder = _capped_half(remainder, origin, normal, axis)
    slabs.append(remainder)

    for slab in slabs:
        if not slab.is_volume:
            raise ValueError("cut did not produce a watertight volume")
        if any(volume < minimum for _, volume in material_components(slab)):
            raise ValueError("fragment volume below configured minimum")
    combined = fsum(slab.volume for slab in slabs)
    if not isfinite(combined) or abs(combined - mesh.volume) / mesh.volume > tolerance:
        raise ValueError("combined volume outside relative tolerance")
    return SplitResult(tuple(slabs))


def split_to_fit(mesh, profile, approval_state):
    """Split oversize descendants in stable axis order and record mating groups bottom-up."""
    parts, steps, seams = [], [], []

    def visit(piece, path):
        if fits_build_volume(tuple(piece.extents), profile):
            parts.append(piece)
            return [len(parts) - 1]
        candidates = propose_axis_cuts(tuple(piece.extents), profile)
        if not candidates:
            raise ValueError("Printer profile has no usable build volume")
        cut = candidates[0]
        steps.append((path, cut))
        children = split_mesh(piece, cut, approval_state=approval_state).parts
        groups = [visit(child, (*path, index)) for index, child in enumerate(children)]
        axis = "xyz".index(cut.axis)
        for i in range(len(groups) - 1):
            plane = piece.bounds[0, axis] + (i + 1) * cut.segment_bounds_mm[axis]
            seams.append((axis, plane, groups[i], groups[i + 1]))
        return [index for group in groups for index in group]

    # split_mesh validates shell intersections and containment on every cut.
    if fits_build_volume(tuple(mesh.extents), profile):
        material_components(mesh)
    visit(mesh, ())
    if not np.isclose(fsum(p.volume for p in parts), mesh.volume, rtol=1e-4, atol=0):
        raise ValueError("Final split volume outside relative tolerance")
    return parts, steps, seams
