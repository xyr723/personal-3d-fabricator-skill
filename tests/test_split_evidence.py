"""Revalidate geometry carried by split evidence, including historical flat projects."""

import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
import trimesh

from personal_3d_fabricator import workflow
from personal_3d_fabricator.projects import create_project
from test_cli import run_cli
from test_geometry_workflow import profile, review


def rewrite_bundle(project, *, legacy=False, change=None, bind=True):
    path = project / "previews/split-preview.zip"
    updated = BytesIO()
    with ZipFile(path) as original, ZipFile(updated, "w") as bundle:
        for entry in original.infolist():
            name = entry.filename
            data = original.read(entry)
            if legacy:
                if name in {"connector.json", "joints.json"}:
                    continue
                if name == "cut.json":
                    cut = json.loads(data)
                    cut.pop("steps")
                    data = json.dumps(cut, indent=2).encode()
            bundle.writestr(entry, change(name, data) if change else data)
    contents = updated.getvalue()
    path.write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    manifest_path = project / "project.json"
    manifest = json.loads(manifest_path.read_bytes())
    if legacy:
        manifest.pop("split_connector", None)
    manifest["split_preview_sha256"] = digest if bind else None
    if manifest["approval_state"] == "split_approved":
        manifest["approved_split_sha256"] = digest
    if not bind:
        manifest["printer_profile"] = None
    manifest_path.write_text(json.dumps(manifest))
    return digest


def crossing_shells():
    left = trimesh.creation.box(extents=(20, 20, 20))
    right = left.copy()
    right.apply_translation((10, 0, 0))
    return left + right


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("phase", ["approve", "deliver"])
def test_recorded_preview_rejects_intersecting_stl_even_with_matching_dimensions(tmp_path, profile, legacy, phase):
    project = review(tmp_path, trimesh.creation.box(extents=(30, 20, 20)))
    workflow.plan_project_split(project, profile)
    if phase == "deliver":
        workflow.approve_split(project)
    wrong = crossing_shells().export(file_type="stl")
    rewrite_bundle(project, legacy=legacy, change=lambda n, b: wrong if n.endswith(".stl") else b)
    with pytest.raises(ValueError, match="shell"):
        (workflow.approve_split if phase == "approve" else workflow.deliver_project)(project)
    assert not list((project / "printable").iterdir())


@pytest.mark.parametrize("wrong", ["bounds", "oversize", "open", "plate"])
def test_numbered_print_parts_are_verified_against_plan_and_profile(tmp_path, profile, wrong):
    project = review(tmp_path, trimesh.creation.box(extents=(30, 20, 20)))
    workflow.plan_project_split(project, profile)
    mesh = trimesh.creation.box(extents=(300 if wrong == "oversize" else 30, 20, 20))
    if wrong == "open":
        mesh.update_faces(range(11))

    def change(name, data):
        if name.endswith(".stl"):
            return mesh.export(file_type="stl")
        if name == "print-plan.json":
            plan = json.loads(data)
            if wrong == "bounds":
                plan["parts"][0]["bounds_mm"][1] = 25
            if wrong == "oversize":
                plan["parts"][0]["bounds_mm"][0] = 300
            if wrong == "plate":
                plan["plates"] = []
            return json.dumps(plan).encode()
        return data

    rewrite_bundle(project, change=change)
    with pytest.raises(ValueError):
        workflow.approve_split(project)


def test_precision_loss_contact_at_stl_export_is_rejected_during_planning(tmp_path, profile):
    left = trimesh.creation.box(extents=(150, 20, 20))
    left.apply_translation((75, 10, 10))
    right = trimesh.creation.box(extents=(20, 10, 10))
    right.apply_translation((160.000002, 10, 10))
    source = tmp_path / "gap.obj"
    source.write_text((left + right).export(file_type="obj", digits=12))
    create_project(tmp_path, "fixture", 150)
    project = tmp_path / "fixture"
    workflow.prepare_overall_preview(project, source, "mm")
    workflow.approve_overall(project, None, None)
    with pytest.raises(workflow.GeometryError, match="shell"):
        workflow.plan_project_split(project, profile)
    assert not (project / "previews/split-preview.zip").exists()


@pytest.mark.parametrize("changed", [False, True])
def test_orphaned_legacy_flat_bundle_reconciles_only_exact_reviewed_geometry(tmp_path, profile, changed):
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    workflow.plan_project_split(project, profile)
    digest = rewrite_bundle(project, legacy=True, bind=False, change=(
        lambda n, b: crossing_shells().export(file_type="stl") if changed and n.endswith(".stl") else b))
    if changed:
        with pytest.raises(ValueError):
            workflow.plan_project_split(project, profile)
        assert json.loads((project / "project.json").read_bytes())["split_preview_sha256"] is None
    else:
        assert workflow.plan_project_split(project, profile)["preview_sha256"] == digest
        assert workflow.approve_split(project).approved_split_sha256 == digest
        assert len(workflow.deliver_project(project)["parts"]) == 2
    assert hashlib.sha256((project / "previews/split-preview.zip").read_bytes()).hexdigest() == digest


def test_pin_cli_rejects_seam_through_two_separate_bars(tmp_path, profile):
    pytest.importorskip("cadquery")
    bar = trimesh.creation.box(extents=(300, 20, 20))
    other = bar.copy()
    other.apply_translation((0, 40, 0))
    project = review(tmp_path, bar + other)
    result = run_cli("plan-split", project, "--profile", profile, "--connector", "pin")
    assert result.returncode == 5, result.stdout
    assert "flat" in result.stderr and "separate" in result.stderr
    assert not (project / "previews/split-preview.zip").exists()
    assert run_cli("plan-split", project, "--profile", profile, "--connector", "flat").returncode == 0


def test_pin_cli_requires_two_nozzle_widths(tmp_path, profile):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    args = ["plan-split", project, "--profile", profile, "--connector", "pin", "--pin-diameter-mm"]
    result = run_cli(*args, "0.01")
    assert result.returncode == 5
    assert "0.8" in result.stderr and "nozzle" in result.stderr
    assert not (project / "previews/split-preview.zip").exists()
    assert run_cli(*args, "0.8").returncode == 0


@pytest.mark.parametrize("stage", ["approve", "deliver"])
def test_historical_single_pin_through_only_one_of_two_bars_is_rejected(tmp_path, profile, monkeypatch, stage):
    pytest.importorskip("cadquery")
    from personal_3d_fabricator.connectors import apply_pin_connections

    bar = trimesh.creation.box(extents=(300, 20, 20))
    other = bar.copy()
    other.apply_translation((0, 40, 0))
    project = review(tmp_path, bar + other)

    # Recreate the previous planner's actual geometry: only the low-Y bar has a pin and bore.
    def pin_first_bar(parts, seams, spec, wall):
        pairs = [sorted(p.split(), key=lambda m: m.centroid[1]) for p in parts]
        joined = [pair[0] for pair in pairs]
        joints = apply_pin_connections(joined, seams, spec, wall)
        parts[:] = [p + pair[1] for p, pair in zip(joined, pairs, strict=True)]
        return joints

    with monkeypatch.context() as patch:
        patch.setattr(workflow, "apply_pin_connections", pin_first_bar)
        # Historical planners published this fixture before the prepublication check existed.
        patch.setattr(workflow, "verify_pin_connections", lambda *args: None)
        created = workflow.plan_project_split(project, profile, connector="pin")
    assert len(created["joints"]) == 1
    if stage == "deliver":
        manifest = json.loads((project / "project.json").read_bytes())
        manifest.update(approval_state="split_approved", approved_split_sha256=created["preview_sha256"])
        (project / "project.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="flat"):
        (workflow.approve_split if stage == "approve" else workflow.deliver_project)(project)
    assert not list((project / "printable").iterdir())


@pytest.mark.parametrize("changed", ["male_position", "filled_bore", "short_bore", "obstructed_bore", "cut_axis", "joint_position"])
def test_pin_approval_verifies_actual_material_and_recorded_interface(tmp_path, profile, changed):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    workflow.plan_project_split(project, profile, connector="pin")
    if changed == "short_bore":
        separate = tmp_path / "another"
        separate.mkdir()
        second = review(separate, trimesh.creation.box(extents=(300, 20, 20)))
        shorter = workflow.plan_project_split(second, profile, connector="pin", pin_length_mm=3)
        with ZipFile(shorter["preview"]) as z:
            short = z.read("part-002.stl")

    def replace(name, data):
        if name == "part-001.stl" and changed == "male_position":
            mesh = trimesh.load_mesh(BytesIO(data), file_type="stl")
            mesh.apply_translation((0, 7, 0))
            return mesh.export(file_type="stl")
        if name == "part-002.stl" and changed == "filled_bore":
            solid = trimesh.creation.box(extents=(150, 20, 20))
            solid.apply_translation((225, 10, 10))
            return solid.export(file_type="stl")
        if name == "part-002.stl" and changed == "short_bore":
            return short
        if name == "part-002.stl" and changed == "obstructed_bore":
            mesh = trimesh.load_mesh(BytesIO(data), file_type="stl")
            speck = trimesh.creation.box(extents=(0.3, 0.3, 0.3))
            speck.apply_translation((153, 11, 10))
            return (mesh + speck).export(file_type="stl")
        if name == "cut.json" and changed == "cut_axis":
            cut = json.loads(data)
            cut["axis"] = "z"
            return json.dumps(cut).encode()
        if name == "joints.json" and changed == "joint_position":
            joints = json.loads(data)
            joints[0]["center_mm"][1] += 2
            return json.dumps(joints).encode()
        return data

    rewrite_bundle(project, change=replace)
    with pytest.raises(ValueError, match="flat"):
        workflow.approve_split(project)


@pytest.mark.parametrize("retry", ["pin", "flat"])
def test_unverifiable_short_pin_publishes_nothing_and_same_project_can_retry(tmp_path, profile, retry):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    manifest = project / "project.json"
    before = manifest.read_bytes()
    previews = project / "previews"
    images = {p.name: p.read_bytes() for p in previews.iterdir()}
    args = ["plan-split", project, "--profile", profile, "--connector"]
    result = run_cli(*args, "pin", "--pin-diameter-mm", 4, "--pin-length-mm", 0.003)
    assert result.returncode == 5
    assert manifest.read_bytes() == before
    assert json.loads(before)["approval_state"] == "overall_approved"
    assert json.loads(before)["split_preview_sha256"] is None
    assert {p.name: p.read_bytes() for p in previews.iterdir()} == images
    assert not list((project / "printable").iterdir())
    valid = [*args, retry]
    if retry == "pin":
        valid.extend(["--pin-diameter-mm", 4, "--pin-length-mm", 6])
    assert run_cli(*valid).returncode == 0
    assert run_cli("approve-split", project).returncode == 0


@pytest.mark.parametrize("bad", ["bore", "plates"])
def test_validates_serialized_staged_geometry_and_plan_before_publication(tmp_path, profile, monkeypatch, bad):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    before = (project / "project.json").read_bytes()
    write_bundle = workflow._write_preview_bundle

    def write_inconsistent_bundle(output, documents):
        altered = documents.copy()
        if bad == "bore":
            flat = trimesh.creation.box(extents=(150, 20, 20))
            flat.apply_translation((225, 10, 10))
            altered["part-002.stl"] = flat.export(file_type="stl")
        else:
            plan = json.loads(altered["print-plan.json"])
            plan["plates"] = []
            altered["print-plan.json"] = json.dumps(plan)
        return write_bundle(output, altered)

    monkeypatch.setattr(workflow, "_write_preview_bundle", write_inconsistent_bundle)
    with pytest.raises(workflow.GeometryError):
        workflow.plan_project_split(project, profile, connector="pin")
    assert (project / "project.json").read_bytes() == before
    assert not list((project / "previews").glob("split-*"))


def test_failed_geometry_does_not_commit_legacy_index_migration(tmp_path, profile):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 20, 20)))
    path = project / "project.json"
    legacy = json.loads(path.read_bytes())
    del legacy["artifacts"], legacy["artifacts_sha256"]
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    args = ["plan-split", project, "--profile", profile, "--connector", "pin", "--pin-length-mm"]
    assert run_cli(*args, 0.003).returncode == 5
    assert path.read_bytes() == before
    assert not list((project / "previews").glob("split-*"))
    assert run_cli(*args, 6).returncode == 0
    assert len(json.loads(path.read_bytes())["artifacts"]) == 1
