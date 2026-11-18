"""End-to-end fabrication through independent CLI invocations."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
import trimesh
from PIL import Image

from personal_3d_fabricator.adapters.cadquery_adapter import CadQueryUnavailable, load_cadquery
from personal_3d_fabricator.adapters.step_adapter import export_step
from personal_3d_fabricator.projects import record_artifact
from test_cli import run_cli


def command(*args):
    result = run_cli(*args, "--json")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def assert_turntable(paths):
    assert len(paths) == 4
    digests = []
    for path in map(Path, paths):
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.width >= 800 and image.height >= 700
            color_counts = image.convert("RGB").getcolors(maxcolors=100_000)
            assert color_counts is not None and len(color_counts) > 20
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    # Opposing cameras can agree exactly on a symmetric model.
    assert len(set(digests)) > 1
    return digests


def assert_tampered_view_is_rejected(approval, project, view):
    path = Path(view)
    original = path.read_bytes()
    path.write_bytes(original + b"changed")
    result = run_cli(approval, project)
    assert result.returncode == 2
    assert "image" in result.stderr
    path.write_bytes(original)


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "220 mm profile.json"
    path.write_text(json.dumps({
        "build_volume_mm": [220, 220, 220],
        "toolhead_count": 1,
        "nozzle_diameters_mm": [0.4],
        "enclosed": False,
        "heated_chamber": False,
        "supported_materials": ["PLA"],
        "slicer": "prusaslicer",
    }), encoding="utf-8")
    return path


def test_box_plans_three_segments_and_delivers_the_approved_parts(tmp_path, profile):
    project = tmp_path / "box prop"
    mesh_path = tmp_path / "500 mm box.stl"
    trimesh.creation.box(extents=(500, 90, 30)).export(mesh_path)

    created = command("init", tmp_path, project.name, "--scale-reference-mm", 500)
    assert created["approval_state"] == "draft"
    assert run_cli("approve-overall", project, "--mesh", mesh_path).returncode == 4
    inspected = command("inspect", mesh_path, "--project", project, "--assumed-unit", "mm")
    assert inspected["bounds_mm"] == [500, 90, 30]
    overall_views = assert_turntable(inspected["views"])
    assert inspected["approval_state"] == "draft"
    other_mesh = tmp_path / "unreviewed.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(other_mesh)
    replacement = run_cli("approve-overall", project, "--mesh", other_mesh, "--assumed-unit", "mm")
    assert replacement.returncode == 2
    assert "differs" in replacement.stderr
    assert_tampered_view_is_rejected("approve-overall", project, inspected["views"][0])
    overall = command("approve-overall", project, "--mesh", mesh_path, "--assumed-unit", "mm")
    assert overall["approval_state"] == "overall_approved"
    assert overall["overall_preview"]["views_sha256"] == overall_views
    snapshot, = (project / "source").rglob("*.stl")
    assert snapshot.read_bytes() == mesh_path.read_bytes()

    # Changes to the caller's file cannot change the approved source.
    trimesh.creation.box(extents=(10, 10, 10)).export(mesh_path)
    proposed = command("plan-split", project, "--profile", profile)
    assert proposed["cut"]["axis"] == "x"
    assert proposed["cut"]["segment_count"] == 3
    preview = Path(proposed["preview"])
    assert preview.is_file()
    split_views = assert_turntable(proposed["views"])
    assert split_views != overall_views
    assert not list((project / "printable").iterdir())
    assert run_cli("deliver", project).returncode == 4

    assert_tampered_view_is_rejected("approve-split", project, proposed["views"][0])
    split = command("approve-split", project)
    assert split["approval_state"] == "split_approved"
    delivered = command("deliver", project)
    assert command("deliver", project) == delivered
    ids = ["part-001", "part-002", "part-003"]
    assert [Path(part).stem for part in delivered["parts"]] == ids
    with ZipFile(preview) as bundle:
        assert [hashlib.sha256(bundle.read(Path(path).name)).hexdigest()
                for path in proposed["views"]] == split_views
        for filename in delivered["parts"]:
            path = Path(filename)
            assert path.parent == project / "printable"
            assert path.read_bytes() == bundle.read(path.name)
            mesh = trimesh.load_mesh(path)
            assert mesh.is_volume
            assert mesh.extents == pytest.approx([500 / 3, 90, 30])
    plan = json.loads(Path(delivered["print_plan_json"]).read_text(encoding="utf-8"))
    assert plan["approval_state"] == "split_approved"
    assert plan["inspection"]["bounds_mm"] == [500, 90, 30]
    assert [part["part_id"] for part in plan["parts"]] == ids
    assembly = Path(delivered["assembly_md"]).read_text(encoding="utf-8")
    assert "装配顺序" in assembly
    assert all(part_id.replace("-", "\\-") in assembly for part_id in ids)


def test_dimensioned_bracket_archives_editable_source_and_delivers(tmp_path, profile):
    cadquery = load_cadquery()
    if isinstance(cadquery, CadQueryUnavailable):
        pytest.skip(cadquery.reason)

    project = tmp_path / "dimensioned bracket"
    command("init", tmp_path, project.name, "--scale-reference-mm", 60)
    model = (
        cadquery.Workplane("XY").box(60, 25, 5)
        .union(cadquery.Workplane("XY").box(5, 25, 20).translate((-27.5, 0, 7.5)))
    )
    step = tmp_path / "bracket.step"
    assert export_step(model, step) == step
    archived = record_artifact(project, step, "source")
    assert archived.read_bytes() == step.read_bytes()
    mesh_path = tmp_path / "bracket.stl"
    cadquery.exporters.export(model, str(mesh_path))
    inspected = command("inspect", mesh_path, "--project", project, "--assumed-unit", "mm")
    assert inspected["bounds_mm"] == pytest.approx([60, 25, 20])
    assert_turntable(inspected["views"])

    command("approve-overall", project, "--mesh", mesh_path, "--assumed-unit", "mm")
    proposed = command("plan-split", project, "--profile", profile)
    assert proposed["cut"] is None
    assert_turntable(proposed["views"])
    command("approve-split", project)
    delivered = command("deliver", project)
    assert [Path(part).name for part in delivered["parts"]] == ["part-001.stl"]
    assert Path(delivered["assembly_md"]).is_file()


def test_delivery_uses_the_verified_bytes_when_preview_changes_during_read(
    tmp_path, profile, monkeypatch, capsys
):
    from io import BytesIO

    from personal_3d_fabricator import workflow
    from personal_3d_fabricator.cli import main

    project = tmp_path / "fixture"
    mesh_path = tmp_path / "fixture.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(mesh_path)
    command("init", tmp_path, project.name, "--scale-reference-mm", 30)
    command("inspect", mesh_path, "--project", project, "--assumed-unit", "mm")
    command("approve-overall", project, "--mesh", mesh_path, "--assumed-unit", "mm")
    proposed = command("plan-split", project, "--profile", profile)
    command("approve-split", project)

    path = Path(proposed["preview"])
    changed = BytesIO()
    with ZipFile(path) as original, ZipFile(changed, "w") as replacement:
        original_part = original.read("part-001.stl")
        for name in original.namelist():
            contents = original_part[::-1] if name == "part-001.stl" else original.read(name)
            replacement.writestr(name, contents)

    def mutate_archive_after_digest(*args, **kwargs):
        path.write_bytes(changed.getvalue())
        return ZipFile(*args, **kwargs)

    monkeypatch.setattr(workflow, "ZipFile", mutate_archive_after_digest)
    assert main(["deliver", str(project), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["parts"][0]).read_bytes() == original_part


def test_modified_preview_cannot_receive_split_approval(tmp_path, profile):
    project = tmp_path / "fixture"
    mesh_path = tmp_path / "fixture.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(mesh_path)
    command("init", tmp_path, project.name, "--scale-reference-mm", 30)
    command("inspect", mesh_path, "--project", project, "--assumed-unit", "mm")
    command("approve-overall", project, "--mesh", mesh_path, "--assumed-unit", "mm")
    proposed = command("plan-split", project, "--profile", profile)
    with ZipFile(proposed["preview"], "a") as bundle:
        bundle.writestr("unexpected.txt", b"content changed after planning")

    approved = run_cli("approve-split", project)
    assert approved.returncode == 2
    assert "preview" in approved.stderr.lower()
    persisted = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert persisted["approval_state"] == "overall_approved"
    assert run_cli("deliver", project).returncode == 4


def prepare_split(tmp_path, source, profile):
    project = tmp_path / "fixture"
    command("init", tmp_path, project.name, "--scale-reference-mm", 20)
    command("inspect", source, "--project", project, "--assumed-unit", "mm")
    command("approve-overall", project, "--mesh", source, "--assumed-unit", "mm")
    return project, command("plan-split", project, "--profile", profile)


def prepare_box_split(tmp_path, profile):
    source = tmp_path / "box.stl"
    trimesh.creation.box(extents=(500, 20, 10)).export(source)
    return prepare_split(tmp_path, source, profile)


def change_printer_profile(project):
    path = project / "project.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["printer_profile"]["build_volume_mm"][0] = 80
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_obj_far_from_origin_keeps_volume_in_the_exported_stl(tmp_path, profile):
    source = tmp_path / "distant.obj"
    fixture = trimesh.creation.box(extents=(20, 10, 5))
    fixture.apply_translation((1e9, 1e9, 1e9))
    fixture.export(source)
    project, _ = prepare_split(tmp_path, source, profile)
    command("approve-split", project)
    result = command("deliver", project)
    printed = trimesh.load_mesh(result["parts"][0])
    assert printed.is_volume
    assert printed.extents == pytest.approx((20, 10, 5))
    assert printed.volume == pytest.approx(1000)
    assert printed.bounds[0] == pytest.approx((0, 0, 0))
    assert "999999990" in Path(result["assembly_md"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("command_name", ["approve-split", "deliver"])
def test_changed_project_printer_profile_blocks_approval_and_delivery(tmp_path, profile, command_name):
    project, _ = prepare_box_split(tmp_path, profile)
    if command_name == "deliver":
        command("approve-split", project)
    change_printer_profile(project)
    result = run_cli(command_name, project)
    assert result.returncode == 2
    assert "profile" in result.stderr.lower()
    assert not list((project / "printable").iterdir())


def test_deliver_rejects_assembly_removed_steps_with_intact_checksums(tmp_path, profile):
    project, _ = prepare_box_split(tmp_path, profile)
    command("approve-split", project)
    result = command("deliver", project)
    assembly_path = Path(result["assembly_md"])
    assembly = assembly_path.read_text(encoding="utf-8")
    altered = "\n".join(line for line in assembly.splitlines() if "按切割轴顺序摆放" not in line) + "\n"
    assert altered != assembly and altered.count("SHA-256") == 2
    assembly_path.write_text(altered, encoding="utf-8")

    retry = run_cli("deliver", project)
    assert retry.returncode == 2
    assert "assembly.md" in retry.stderr


def test_missing_printable_directory_is_an_input_error(tmp_path, profile):
    project, _ = prepare_box_split(tmp_path, profile)
    command("approve-split", project)
    (project / "printable").rmdir()
    result = run_cli("deliver", project)
    assert result.returncode == 2
    assert "printable" in result.stderr
    assert not (project / "assembly.md").exists()


def test_exported_stl_is_reinspected_before_publishing(tmp_path, profile, monkeypatch, capsys):
    from personal_3d_fabricator.cli import main

    project = tmp_path / "fixture"
    source = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 10, 5)).export(source)
    command("init", tmp_path, project.name, "--scale-reference-mm", 20)
    command("inspect", source, "--project", project, "--assumed-unit", "mm")
    command("approve-overall", project, "--mesh", source, "--assumed-unit", "mm")
    wrong_shape = trimesh.creation.box(extents=(12, 10, 5)).export(file_type="stl")
    monkeypatch.setattr(trimesh.Trimesh, "export", lambda self, **kwargs: wrong_shape)

    assert main(["plan-split", str(project), "--profile", str(profile)]) == 5
    assert "STL" in capsys.readouterr().err
    assert not (project / "previews" / "split-preview.zip").exists()


@pytest.mark.parametrize("phase", ["approve-overall", "plan-split"])
def test_changed_scale_requires_a_new_overall_review(tmp_path, profile, phase):
    project = tmp_path / "fixture"
    source = tmp_path / "500 mm box.stl"
    trimesh.creation.box(extents=(500, 20, 10)).export(source)
    command("init", tmp_path, project.name, "--scale-reference-mm", 500)
    inspected = command("inspect", source, "--project", project, "--assumed-unit", "mm")
    args = [phase, project]
    if phase == "plan-split":
        command("approve-overall", project)
        args.extend(["--profile", profile])

    path = project / "project.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scale_reference_mm"] = 50
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    views = {filename: Path(filename).read_bytes() for filename in inspected["views"]}

    result = run_cli(*args)
    assert result.returncode == 4, result.stderr
    assert "scale_reference_mm" in result.stderr
    assert "overall approval" in result.stderr.lower()
    assert path.read_bytes() == before
    assert {filename: Path(filename).read_bytes() for filename in views} == views
    assert not (project / "previews" / "split-preview.zip").exists()
    assert inspected["scale_reference_mm"] == 500
    assert manifest["overall_preview"]["scale_reference_mm"] == 500
