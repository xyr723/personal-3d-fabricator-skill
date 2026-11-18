"""Decisions and delivered geometry follow the same approved project inputs."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest
import trimesh

from personal_3d_fabricator import workflow
from personal_3d_fabricator.cli import main
from personal_3d_fabricator.models import PrintPlan
from personal_3d_fabricator.planning import estimate_print_plan_plates
from personal_3d_fabricator.profiles import load_printer_profile
from personal_3d_fabricator.projects import read_project_manifest
from personal_3d_fabricator.splitting import split_project_mesh
from test_geometry_workflow import profile, review
from test_splitting import CUT, box, reviewed


def approved_file(project):
    mesh = read_project_manifest(project).approved_mesh
    return project / "source" / mesh.archive_version / mesh.filename


def outputs(project):
    files = [project / "project.json", *project.glob("*.md"), *project.glob("*.json"),
             *(project / "printable").iterdir(), *(project / "previews").iterdir()]
    return {p.relative_to(project): p.read_bytes() for p in files}


def edit_material(project, value):
    path = project / "project.json"
    doc = json.loads(path.read_bytes())
    doc["material"] = value
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("phase", ["approve", "deliver", "retry"])
@pytest.mark.parametrize("problem", ["missing", "same_size", "sidecar", "unregistered"])
def test_flat_source_required_before_any_publication(tmp_path, profile, phase, problem):
    project = review(tmp_path, box())
    workflow.plan_project_split(project, profile)
    if phase != "approve":
        workflow.approve_split(project)
    if phase == "retry":
        workflow.deliver_project(project)
    source = approved_file(project)
    if problem == "missing":
        source.rename(source.with_suffix(".unavailable"))
    elif problem == "same_size":
        data = source.read_bytes()
        source.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    elif problem == "sidecar":
        source.with_suffix(".stl.sha256").write_text("unavailable")
    else:
        updated = read_project_manifest(project).with_artifacts(())
        (project / "project.json").write_text(updated.model_dump_json())
    before = outputs(project)
    command = "approve-split" if phase == "approve" else "deliver"
    assert main([command, str(project)]) in {2, 5}
    assert outputs(project) == before


@pytest.mark.parametrize("phase", ["approve", "deliver", "retry"])
def test_pin_source_required_before_any_publication(tmp_path, profile, phase):
    pytest.importorskip("cadquery")
    project = review(tmp_path, trimesh.creation.box(extents=(300, 60, 20)))
    workflow.plan_project_split(project, profile, connector="pin")
    if phase != "approve":
        workflow.approve_split(project)
    if phase == "retry":
        workflow.deliver_project(project)
    source = approved_file(project)
    original = source.read_bytes()
    source.write_bytes(original[::-1])
    before = outputs(project)
    command = "approve-split" if phase == "approve" else "deliver"
    assert main([command, str(project)]) in {2, 5}
    assert outputs(project) == before


@pytest.mark.parametrize("state", ["overall", "split"])
def test_project_cut_uses_reviewed_mesh_only(reviewed, profile, state):
    if state == "split":
        workflow.plan_project_split(reviewed, profile)
        workflow.approve_split(reviewed)
    different = box()
    different.apply_translation([100, 0, 0])
    with pytest.raises(TypeError):
        split_project_mesh(reviewed, different, CUT)
    result = split_project_mesh(reviewed, CUT)
    assert [part.bounds[0, 0] for part in result.parts] == pytest.approx([24, 28, 32])
    assert sum(part.volume for part in result.parts) == pytest.approx(12 * 6 * 8)


@pytest.mark.parametrize("problem", ["missing", "changed"])
def test_project_cut_rejects_unavailable_approved_geometry(reviewed, problem):
    source = approved_file(reviewed)
    if problem == "missing":
        source.rename(source.with_suffix(".unavailable"))
    else:
        data = source.read_bytes()
        source.write_bytes(data[::-1])
    with pytest.raises((FileNotFoundError, ValueError)):
        split_project_mesh(reviewed, CUT)


def test_unsupported_material_returns_input_error_before_preview(tmp_path, profile, capsys):
    project = review(tmp_path, box())
    edit_material(project, "Nylon")
    before = outputs(project)
    assert main(["plan-split", str(project), "--profile", str(profile)]) == 2
    error = capsys.readouterr().err
    assert all(name in error for name in ("Nylon", "PLA", "PETG"))
    assert outputs(project) == before


@pytest.mark.parametrize("selected", ["  pEtG ", None])
def test_project_material_is_bound_to_saved_plan(tmp_path, profile, selected):
    project = review(tmp_path, box())
    edit_material(project, selected)
    plan = workflow.plan_project_split(project, profile)
    expected = "PETG" if selected else None
    with ZipFile(plan["preview"]) as bundle:
        assert json.loads(bundle.read("print-plan.json")).get("material") == expected
    candidate = PrintPlan.model_validate(plan["plan"]).model_copy(update={"material": "Nylon"})
    with pytest.raises(ValueError, match="Nylon"):
        estimate_print_plan_plates(candidate, load_printer_profile(profile))
    edit_material(project, "petg" if selected else None)
    workflow.approve_split(project)
    delivered = workflow.deliver_project(project)
    assert json.loads(Path(delivered["print_plan_json"]).read_bytes()).get("material") == expected


@pytest.mark.parametrize("phase", ["approve", "deliver", "retry"])
def test_material_change_requires_new_review(tmp_path, profile, phase):
    project = review(tmp_path, box())
    edit_material(project, "PETG")
    workflow.plan_project_split(project, profile)
    if phase != "approve":
        workflow.approve_split(project)
    if phase == "retry":
        workflow.deliver_project(project)
    edit_material(project, "PLA")
    before = outputs(project)
    command = "approve-split" if phase == "approve" else "deliver"
    assert main([command, str(project)]) == 2
    assert outputs(project) == before
