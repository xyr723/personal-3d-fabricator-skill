"""Physical split and alignment evidence through the regular project workflow."""

import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from markdown_it import MarkdownIt

import numpy as np
import pytest
import trimesh

from personal_3d_fabricator import workflow
from personal_3d_fabricator.cli import main
from personal_3d_fabricator.projects import create_project

@pytest.fixture
def profile(tmp_path):
    template = Path(__file__).resolve().parents[1] / "profiles/generic-single-nozzle.json"
    data = json.loads(template.read_bytes())
    data["build_volume_mm"] = [220, 220, 220]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    return path


def review(tmp_path, mesh):
    create_project(tmp_path, "fixture", 20)
    project = tmp_path / "fixture"
    source = tmp_path / "source.stl"
    mesh.export(source)
    workflow.prepare_overall_preview(project, source, "mm")
    workflow.approve_overall(project, None, None)
    return project


@pytest.mark.parametrize(("side", "offset"), [(20, 10), (5, 0)])
def test_uncut_overlapping_or_same_winding_nested_solids_are_rejected(tmp_path, profile, side, offset):
    outer = trimesh.creation.box(extents=[20] * 3)
    other = trimesh.creation.box(extents=[side] * 3)
    other.apply_translation([offset, 0, 0])
    project = review(tmp_path, outer + other)
    with pytest.raises(workflow.GeometryError, match="shell"):
        workflow.plan_project_split(project, profile)
    assert not (project / "previews/split-preview.zip").exists()


def test_uncut_disjoint_shells_retain_both_volumes(tmp_path, profile):
    left = trimesh.creation.box(extents=[20] * 3)
    right = left.copy()
    right.apply_translation([30, 0, 0])
    project = review(tmp_path, left + right)
    result = workflow.plan_project_split(project, profile)
    with ZipFile(result["preview"]) as bundle:
        mesh = trimesh.load_mesh(BytesIO(bundle.read("part-001.stl")), file_type="stl")
    assert sorted(part.volume for part in mesh.split()) == pytest.approx([8000, 8000])


@pytest.mark.parametrize(("extents", "count"), [((300, 300, 20), 4), ((300, 300, 300), 8)])
def test_recursive_split_fits_each_printed_part_and_conserves_material(tmp_path, profile, extents, count):
    project = review(tmp_path, trimesh.creation.box(extents=extents))
    proposed = workflow.plan_project_split(project, profile)
    assert workflow.plan_project_split(project, profile)["preview_sha256"] == proposed["preview_sha256"]
    workflow.approve_split(project)
    delivery = workflow.deliver_project(project)
    assert [Path(p).stem for p in delivery["parts"]] == [f"part-{n:03}" for n in range(1, count + 1)]
    meshes = [trimesh.load_mesh(p) for p in delivery["parts"]]
    assert all(m.is_volume and np.all(m.extents <= 218) for m in meshes)
    assert sum(m.volume for m in meshes) == pytest.approx(np.prod(extents), rel=1e-4)


def test_explicit_flat_cli_and_unavailable_pin_backend(tmp_path, profile, capsys):
    from importlib.util import find_spec
    project = review(tmp_path, trimesh.creation.box(extents=(300, 60, 20)))
    args = ["plan-split", str(project), "--profile", str(profile), "--connector"]
    if find_spec("cadquery") is None:
        assert main([*args, "pin", "--json"]) == 3
        assert "cadquery" in capsys.readouterr().err.lower()
        assert not (project / "previews/split-preview.zip").exists()
    assert main([*args, "flat", "--json"]) == 0


def test_pin_socket_stl_preview_approval_and_assembly(tmp_path, profile, capsys):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 60, 20)))
    args = ["plan-split", str(project), "--profile", str(profile), "--connector", "pin",
            "--pin-diameter-mm", "6", "--pin-length-mm", "7", "--pin-clearance-mm", "0.3"]
    assert main([*args, "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["plan"]["connectors"] and preview["plan"]["hardware"]
    assert len(preview["views"]) >= 4
    assert main([*args, "--pin-clearance-mm", "0.5"]) == 2
    assert hashlib.sha256(Path(preview["preview"]).read_bytes()).hexdigest() == preview["preview_sha256"]
    approved = workflow.approve_split(project)
    assert approved.approved_split_sha256 == preview["preview_sha256"]
    delivery = workflow.deliver_project(project)
    male, female = [trimesh.load_mesh(p) for p in delivery["parts"]]
    assert male.is_volume and female.is_volume
    assert male.bounds[1, 0] == pytest.approx(157)
    flat_volume = 150 * 60 * 20
    assert male.volume - flat_volume == pytest.approx(np.pi * 3**2 * 7, rel=0.02)
    assert flat_volume - female.volume == pytest.approx(np.pi * 3.3**2 * 7, rel=0.02)
    # Probe the delivered material and mating bore in the same coordinate system.
    assert male.contains([[153, 30, 10]])[0]
    assert not female.contains([[153, 30, 10]])[0]
    assert female.contains([[153, 34, 10]])[0]
    assembly = MarkdownIt().render(Path(delivery["assembly_md"]).read_text())
    assert all(token in assembly for token in ["定位销", "0.3", "150", "30", "10", "+X"])


@pytest.mark.parametrize("stage", ["approve", "deliver"])
def test_connector_selection_is_bound_to_the_manifest(tmp_path, profile, stage):
    project = review(tmp_path, trimesh.creation.box(extents=(300, 60, 20)))
    workflow.plan_project_split(project, profile)
    if stage == "deliver":
        workflow.approve_split(project)
    manifest_path = project / "project.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["split_connector"] == {"mode": "flat", "pin": None}
    manifest["split_connector"] = {"mode": "pin", "pin": {
        "pin_diameter_mm": 4, "pin_length_mm": 6, "clearance_mm": 0.2, "count": 1}}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="connector"):
        (workflow.approve_split if stage == "approve" else workflow.deliver_project)(project)
    assert not list((project / "printable").iterdir())


def test_pin_socket_cannot_break_through_far_wall(tmp_path, profile, capsys):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 60, 20)))
    assert main(["plan-split", str(project), "--profile", str(profile), "--connector", "pin",
                 "--pin-length-mm", "155"]) == 5
    assert not (project / "previews/split-preview.zip").exists()


def test_panel_assembly_joins_rows_then_engages_both_parallel_pins_together(tmp_path, profile):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 300, 20)))
    proposed = workflow.plan_project_split(project, profile, connector="pin")
    joints = proposed["joints"]
    assert [(j["axis"], j["assembly_step"]) for j in joints] == [
        ("+Y", 1), ("+Y", 2), ("+X", 3), ("+X", 3)]
    assert "同时" in proposed["plan"]["assembly_order"][2]
    assert len(proposed["plan"]["assembly_order"]) == 4  # Three joins and the source offset.
    assert workflow.plan_project_split(project, profile, connector="pin")["preview_sha256"] == proposed["preview_sha256"]
    workflow.approve_split(project)
    parts = [trimesh.load_mesh(p) for p in workflow.deliver_project(project)["parts"]]
    assert all(p.is_volume and p.body_count == 1 for p in parts)
    for j in joints:
        pin = parts[int(j["pin_part"][-3:]) - 1]
        socket = parts[int(j["socket_part"][-3:]) - 1]
        probe = np.array(j["center_mm"])
        probe["XYZ".index(j["axis"][1])] += 3
        assert pin.contains([probe])[0] and not socket.contains([probe])[0]


def test_pending_flat_preview_from_previous_release_can_be_reopened(tmp_path, profile):
    """A prior flat bundle has no connector or joint documents and no new manifest field."""
    project = review(tmp_path, trimesh.creation.box(extents=(200, 60, 20)))
    result = workflow.plan_project_split(project, profile)
    path = Path(result["preview"])
    old = BytesIO()
    with ZipFile(path) as current, ZipFile(old, "w") as previous:
        for member in current.infolist():
            if member.filename not in {"connector.json", "joints.json"}:
                previous.writestr(member, current.read(member))
    path.write_bytes(old.getvalue())
    digest = hashlib.sha256(old.getvalue()).hexdigest()
    manifest_path = project / "project.json"
    manifest = json.loads(manifest_path.read_bytes())
    del manifest["split_connector"]
    manifest["split_preview_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    assert workflow.plan_project_split(project, profile)["preview_sha256"] == digest
    workflow.approve_split(project)
    assert len(workflow.deliver_project(project)["parts"]) == 1
