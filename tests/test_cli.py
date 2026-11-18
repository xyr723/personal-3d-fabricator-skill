"""Command-line contracts and approval gate regressions."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import trimesh

from personal_3d_fabricator.projects import create_project

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    """Exercise the same entry point in a fresh process on every invocation."""
    return subprocess.run(
        [sys.executable, "-m", "personal_3d_fabricator.cli", *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


@pytest.mark.parametrize(("command", "state"), [
    ("plan-split", "overall_approved"),
    ("approve-split", "overall_approved"),
    ("deliver", "split_approved"),
])
def test_commands_gate_on_the_persisted_approval(tmp_path, command, state):
    create_project(tmp_path, "draft", 500)
    result = run_cli(command, tmp_path / "draft")
    assert result.returncode == 4, result.stderr
    assert state in result.stderr
    assert result.stdout == ""


def test_environment_report_is_json():
    result = run_cli("check-environment", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["python"]["available"] is True


def test_main_returns_exit_codes_instead_of_exiting(capsys):
    from personal_3d_fabricator.cli import main

    assert main(["--help"]) == 0
    assert main(["inspect", "--unknown"]) == 2
    assert "usage:" in capsys.readouterr().err
    assert main(["inspect"]) == 2
    assert "--project" in capsys.readouterr().err


def test_missing_required_tool_has_its_own_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_3D_FABRICATOR_BLENDER", str(tmp_path / "missing"))
    result = run_cli("check-environment", "--require", "blender", "--json")
    assert result.returncode == 3
    assert json.loads(result.stdout)["blender"]["available"] is False
    assert "blender" in result.stderr


def test_exported_schemas_match_repository(tmp_path):
    result = run_cli("export-schemas", tmp_path, "--json")
    assert result.returncode == 0, result.stderr
    paths = [Path(path) for path in json.loads(result.stdout)["schemas"]]
    assert {path.name for path in paths} == {
        "inspection.schema.json", "printer-profile.schema.json",
        "print-plan.schema.json", "project.schema.json",
    }
    for path in paths:
        assert path.read_bytes() == (ROOT / "schemas" / path.name).read_bytes()


def test_inspect_rejects_bad_geometry_with_json_diagnostic(tmp_path):
    source = tmp_path / "broken.stl"
    source.write_bytes(b"invalid STL")
    result = run_cli("inspect", source, "--assumed-unit", "mm", "--json")
    assert result.returncode == 5
    assert json.loads(result.stdout)["issues"][0]["code"] == "parse_error"
    assert "parse_error" in result.stderr


def test_inspect_missing_file_is_input_error(tmp_path):
    result = run_cli("inspect", tmp_path / "missing.stl", "--json")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr


def test_mismatched_manifest_name_cannot_authorize_other_directory(tmp_path):
    create_project(tmp_path, "draft", 500)
    project = tmp_path / "draft"
    project.rename(tmp_path / "other")
    result = run_cli("approve-overall", tmp_path / "other")
    assert result.returncode == 2
    assert "different project" in result.stderr


def test_approval_uses_unique_archived_mesh_without_importing_new_source(tmp_path):
    from personal_3d_fabricator.projects import record_artifact

    create_project(tmp_path, "fixture", 30)
    project = tmp_path / "fixture"
    source = tmp_path / "fixture.stl"
    trimesh.creation.box(extents=(30, 12, 5)).export(source)
    archived = record_artifact(project, source, "source")
    inspected = run_cli("inspect", "--project", project, "--assumed-unit", "mm")
    assert inspected.returncode == 0, inspected.stderr
    result = run_cli("approve-overall", project, "--assumed-unit", "mm", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["approval_state"] == "overall_approved"
    assert list((project / "source").rglob("*.stl")) == [archived]


def test_split_requires_a_preview(tmp_path):
    create_project(tmp_path, "fixture", 30)
    project = tmp_path / "fixture"
    source = tmp_path / "fixture.stl"
    trimesh.creation.box(extents=(30, 12, 5)).export(source)
    inspected = run_cli("inspect", source, "--project", project, "--assumed-unit", "mm")
    assert inspected.returncode == 0, inspected.stderr
    approved = run_cli("approve-overall", project, "--mesh", source, "--assumed-unit", "mm")
    assert approved.returncode == 0, approved.stderr
    result = run_cli("approve-split", project)
    assert result.returncode == 4
    assert "plan-split" in result.stderr


@pytest.mark.parametrize("command", ["help", "environment", "inspect"])
def test_non_rendering_cli_commands_do_not_import_matplotlib(tmp_path, command):
    source = tmp_path / "mesh.stl"
    if command == "inspect":
        trimesh.creation.box(extents=(20, 10, 5)).export(source)
    commands = {
        "help": ["--help"],
        "environment": ["check-environment", "--json"],
        "inspect": ["inspect", str(source), "--assumed-unit", "mm", "--json"],
    }
    probe = """
import importlib.abc
import sys

def rendering_module(name):
    return name == "personal_3d_fabricator.preview" or name.partition(".")[0] == "matplotlib"

class RejectRenderingImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if rendering_module(fullname):
            raise RuntimeError(f"Unexpected rendering import: {fullname}")

sys.meta_path.insert(0, RejectRenderingImport())
from personal_3d_fabricator.cli import main
code = main(sys.argv[1:])
assert not any(rendering_module(name) for name in sys.modules)
raise SystemExit(code)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, *commands[command]],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if command == "help":
        assert "usage:" in result.stdout
    elif command == "environment":
        assert json.loads(result.stdout)["python"]["available"] is True
    else:
        assert json.loads(result.stdout)["bounds_mm"] == [20, 10, 5]
