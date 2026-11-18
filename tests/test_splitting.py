from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import trimesh

from personal_3d_fabricator import workflow
from personal_3d_fabricator.models import ApprovalState
from personal_3d_fabricator.planning import CutCandidate
from personal_3d_fabricator.projects import advance_approval, create_project
from personal_3d_fabricator.splitting import split_mesh, split_project_mesh


CUT = CutCandidate("x", 3, (4., 6., 8.), (), 0., 0.)
APPROVED = ApprovalState.overall_approved


def box():
    mesh = trimesh.creation.box(extents=(12, 6, 8))
    mesh.apply_translation([30, 2, -7])
    return mesh


def test_split_uses_parallel_planes_from_bounds_minimum():
    mesh = box()
    original = mesh.vertices.copy()
    result = split_mesh(mesh, CUT, approval_state=APPROVED)
    assert len(result.parts) == 3
    assert all(part.is_volume for part in result.parts)
    assert [part.bounds[0, 0] for part in result.parts] == pytest.approx([24, 28, 32])
    assert sum(part.volume for part in result.parts) == pytest.approx(mesh.volume, rel=1e-4)
    assert np.array_equal(original, mesh.vertices)


def test_cut_requires_approval():
    with pytest.raises(ValueError, match="overall_approved"):
        split_mesh(box(), CUT, approval_state=ApprovalState.draft)


def test_rejects_open_mesh():
    mesh = box()
    mesh.update_faces(np.arange(len(mesh.faces) - 1))
    with pytest.raises(ValueError, match="watertight"):
        split_mesh(mesh, CUT, approval_state=APPROVED)


def test_rejects_empty_cross_section_between_disjoint_boxes():
    left, right = box(), box()
    right.apply_translation([20, 0, 0])
    with pytest.raises(ValueError, match="intersect"):
        split_mesh(left + right, replace(CUT, segment_count=2, segment_bounds_mm=(16, 6, 8)), approval_state=APPROVED)


@pytest.mark.parametrize("kwargs", [{"min_fragment_volume_mm3": 200}, {"min_fragment_volume_mm3": float("nan")},
                                    {"volume_tolerance": float("inf")}, {"volume_tolerance": -1}])
def test_rejects_small_parts_or_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        split_mesh(box(), CUT, approval_state=APPROVED, **kwargs)


def test_rejects_nonfinite_cut():
    with pytest.raises(ValueError):
        split_mesh(box(), replace(CUT, segment_bounds_mm=(float("nan"), 6, 8)), approval_state=APPROVED)


def test_rejects_volume_loss(monkeypatch):
    slice_plane = trimesh.Trimesh.slice_plane

    def lose_volume(self, *args, **kwargs):
        piece = slice_plane(self, *args, **kwargs)
        piece.apply_scale(0.95)
        return piece

    monkeypatch.setattr(trimesh.Trimesh, "slice_plane", lose_volume)
    with pytest.raises(ValueError, match="volume"):
        split_mesh(box(), replace(CUT, segment_count=2, segment_bounds_mm=(6, 6, 8)), approval_state=APPROVED)


def test_capped_hollow_section_preserves_through_hole():
    ring = trimesh.creation.annulus(r_min=2, r_max=4, height=6)
    cut = CutCandidate("z", 2, (8, 8, 3), (), 0, 0)
    result = split_mesh(ring, cut, approval_state=ApprovalState.split_approved)
    for slab in result.parts:
        assert slab.is_volume
        assert slab.euler_number == 0
        assert slab.volume == pytest.approx(ring.volume / 2)


def test_rejects_small_disconnected_fragment_in_large_slab():
    speck = trimesh.creation.box(extents=[0.1] * 3)
    speck.apply_translation([26, 6, -7])
    mesh = box() + speck
    dims = mesh.extents.copy()
    dims[0] /= CUT.segment_count
    with pytest.raises(ValueError, match="fragment"):
        split_mesh(mesh, replace(CUT, segment_bounds_mm=tuple(dims)), approval_state=APPROVED)


def test_rejects_plane_touching_only_outer_face_of_disjoint_solid():
    right = trimesh.creation.box(extents=(6, 1, 1))
    left = trimesh.creation.box(extents=(3, 1, 1))
    right.apply_translation([3, 0, 0])
    left.apply_translation([-4.5, 0, 0])
    cut = CutCandidate("x", 2, (6, 1, 1), (), 0, 0)
    with pytest.raises(ValueError, match="intersect"):
        split_mesh(left + right, cut, approval_state=APPROVED)


def reverse_box(side, center):
    shell = trimesh.creation.box(extents=[side] * 3)
    shell.apply_translation(center)
    shell.invert()
    return shell


def cut_in_three(mesh):
    dims = mesh.extents.copy()
    dims[0] /= 3
    return replace(CUT, segment_bounds_mm=tuple(dims))


def test_hollow_fragment_uses_net_material_volume():
    center = [26, 9, -7]
    outer = trimesh.creation.box(extents=[2] * 3)
    outer.apply_translation(center)
    mesh = box() + outer + reverse_box(1.98, center)
    with pytest.raises(ValueError, match="fragment"):
        split_mesh(mesh, cut_in_three(mesh), approval_state=APPROVED)
    result = split_mesh(mesh, cut_in_three(mesh), approval_state=APPROVED, min_fragment_volume_mm3=0.2)
    assert sum(p.volume for p in result.parts) == pytest.approx(mesh.volume)


def test_orphan_inward_shell_is_invalid():
    mesh = box() + reverse_box(1.98, [26, 9, -7])
    with pytest.raises(ValueError, match="shell"):
        split_mesh(mesh, cut_in_three(mesh), approval_state=APPROVED)


def test_string_approval_is_rejected():
    with pytest.raises(ValueError, match="overall_approved"):
        split_mesh(box(), CUT, approval_state="overall_approved")


@pytest.fixture
def reviewed(tmp_path):
    create_project(tmp_path, "reviewed", 12)
    project = tmp_path / "reviewed"
    source = tmp_path / "box.stl"
    box().export(source)
    workflow.prepare_overall_preview(project, source, "mm")
    advance_approval(project, APPROVED)
    return project


def test_project_split_reads_only_selected_project(tmp_path, reviewed):
    create_project(tmp_path, "draft", 12)
    with pytest.raises(ValueError, match="overall_approved"):
        split_project_mesh(tmp_path / "draft", CUT)
    with pytest.raises(FileNotFoundError):
        split_project_mesh(tmp_path / "missing", CUT)
    with pytest.raises(TypeError):
        split_project_mesh(tmp_path / "draft", CUT, approval_state=APPROVED)
    assert len(split_project_mesh(tmp_path / "reviewed", CUT).parts) == 3
    profile = Path(__file__).resolve().parents[1] / "profiles" / "generic-single-nozzle.json"
    workflow.plan_project_split(reviewed, profile)
    advance_approval(reviewed, ApprovalState.split_approved)
    assert len(split_project_mesh(tmp_path / "reviewed", CUT).parts) == 3


def test_project_split_rejects_another_projects_manifest(tmp_path, reviewed):
    create_project(tmp_path, "draft", 12)
    (tmp_path / "draft/project.json").write_bytes((reviewed / "project.json").read_bytes())
    with pytest.raises(ValueError, match="project"):
        split_project_mesh(tmp_path / "draft", CUT)


def test_concave_solid_with_crossing_cavity_faces_is_invalid():
    ring = trimesh.creation.annulus(r_min=2, r_max=4, height=6)
    cavity = trimesh.creation.box(extents=[4.5, 4.5, 1])
    assert ring.contains(cavity.vertices).all()
    cavity.invert()
    mesh = ring + cavity
    cut = CutCandidate("z", 2, (8, 8, 3), (), 0, 0)
    with pytest.raises(ValueError, match="shell surfaces intersect"):
        split_mesh(mesh, cut, approval_state=APPROVED)


def nested_boxes(sides):
    shells = []
    for index, side in enumerate(sides):
        shell = trimesh.creation.box(extents=[side] * 3)
        if index % 2:
            shell.invert()
        shells.append(shell)
    return trimesh.util.concatenate(shells)


@pytest.mark.parametrize("minimum", [0.0, 1.0])
def test_cut_through_closed_cavity(minimum):
    mesh = nested_boxes([10, 8])
    result = split_mesh(mesh, CutCandidate("x", 2, (5, 10, 10), (), 0, 0),
                        approval_state=APPROVED, min_fragment_volume_mm3=minimum)
    for part in result.parts:
        assert part.is_watertight and part.is_winding_consistent
        assert part.volume == pytest.approx(244)
        assert part.body_count == 1
        cap = np.all(np.isclose(part.triangles[:, :, 0], 0), axis=1)
        assert part.area_faces[cap].sum() == pytest.approx(36)


def test_cut_through_four_alternating_shells():
    mesh = nested_boxes([10, 8, 6, 4])
    result = split_mesh(mesh, CutCandidate("x", 2, (5, 10, 10), (), 0, 0),
                        approval_state=APPROVED)
    for slab in result.parts:
        bodies = slab.split(only_watertight=False, repair=False)
        assert all(body.is_volume for body in bodies)
        assert sorted(body.volume for body in bodies) == pytest.approx([76, 244])


def test_cut_through_two_separate_hollow_solids():
    left, right = nested_boxes([10, 8]), nested_boxes([6, 4])
    right.apply_translation([0, 12, 0])
    result = split_mesh(left + right, CutCandidate("x", 2, (5, 20, 10), (), 0, 0),
                        approval_state=APPROVED)
    for slab in result.parts:
        bodies = slab.split(only_watertight=False, repair=False)
        assert all(body.is_volume for body in bodies)
        assert sorted(body.volume for body in bodies) == pytest.approx([76, 244])


def test_old_geos_reports_required_version_and_recovery(monkeypatch):
    import shapely

    with monkeypatch.context() as patch:
        patch.setattr(shapely, "geos_version", (3, 9, 4))
        with pytest.raises(RuntimeError, match=r"GEOS >= 3\.10\.0.*3\.9\.4.*Shapely.*wheel"):
            split_mesh(box(), CUT, approval_state=APPROVED)
    assert shapely.geos_version >= (3, 10, 0)
    assert all(part.is_volume for part in split_mesh(box(), CUT, approval_state=APPROVED).parts)
