"""Approval and archive boundaries exercised via their public entry points."""

import builtins
import hashlib
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import trimesh

from personal_3d_fabricator import delivery, inspection, projects, workflow
from personal_3d_fabricator.models import ApprovalState, PrintPlan

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "generic-single-nozzle.json"


def new_project(root, name="fixture"):
    projects.create_project(root, name, 30)
    return root / name


def box(path, width=30):
    trimesh.creation.box(extents=(width, 20, 10)).export(path)
    return path


def prepare_split(project, source):
    workflow.prepare_overall_preview(project, source, "mm")
    workflow.approve_overall(project, source, "mm")
    return workflow.plan_project_split(project, PROFILE)


def test_checksum_bytes_and_legacy_windows_workflow(tmp_path, monkeypatch):
    project = new_project(tmp_path)
    source = box(tmp_path / "box.stl")
    real_open = builtins.open

    def windows_open(file, mode="r", *args, **kwargs):
        if str(file).endswith(".sha256") and "b" not in mode:
            kwargs.setdefault("newline", "\r\n")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", windows_open)
    saved = projects.record_artifact(project, source, "source")
    sidecar = saved.with_name(saved.name + ".sha256")
    line = f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {saved.name}".encode()
    assert sidecar.read_bytes() == line + b"\n"
    sidecar.write_bytes(line + b"\r\n")
    workflow.prepare_overall_preview(project, None, "mm")
    for bad in (line + b" ", line.upper() + b"\n", line + b"\r\r\n", line + b"\n\n"):
        sidecar.write_bytes(bad)
        with pytest.raises(ValueError, match="checksum"):
            workflow.approve_overall(project, None, None)
    sidecar.write_bytes(line + b"\r\n")
    assert workflow.approve_overall(project, None, None).approval_state == "overall_approved"


def test_direct_approval_requires_overall_review(tmp_path):
    project = new_project(tmp_path)
    with pytest.raises(projects.ApprovalError):
        projects.advance_approval(project, ApprovalState.overall_approved)
    assert projects.read_project_manifest(project).approval_state == "draft"


def test_direct_approval_verifies_overall_files(tmp_path):
    project = new_project(tmp_path)
    view = workflow.prepare_overall_preview(project, box(tmp_path / "box.stl"), "mm")["views"][0]
    evidence = projects.read_project_manifest(project).overall_preview
    view.write_bytes(b"unavailable")
    with pytest.raises(ValueError, match="image"):
        projects.advance_approval(project, ApprovalState.overall_approved, approved_mesh=evidence.mesh)


def test_direct_split_approval_requires_reviewed_package(tmp_path):
    project = new_project(tmp_path)
    source = box(tmp_path / "box.stl")
    workflow.prepare_overall_preview(project, source, "mm")
    workflow.approve_overall(project, None, None)
    with pytest.raises(projects.ApprovalError):
        projects.advance_approval(project, ApprovalState.split_approved)
    split = workflow.plan_project_split(project, PROFILE)
    split["views"][0].write_bytes(b"unavailable")
    with pytest.raises(ValueError, match="image"):
        projects.advance_approval(project, ApprovalState.split_approved,
                                  split_preview_sha256=split["preview_sha256"])


def test_delivery_requires_distinct_approved_digest_and_exact_plan(tmp_path):
    project = new_project(tmp_path)
    split = prepare_split(project, box(tmp_path / "box.stl"))
    pending = projects.read_project_manifest(project)
    assert pending.split_preview_sha256 == split["preview_sha256"]
    assert pending.model_dump().get("approved_split_sha256") is None
    approved = workflow.approve_split(project)
    assert approved.model_dump().get("approved_split_sha256") == pending.split_preview_sha256
    plan = PrintPlan.model_validate(split["plan"] | {"approval_state": "split_approved"})
    with pytest.raises(ValueError, match="preview"):
        delivery.write_delivery(project, plan.inspection, plan.model_copy(update={"risks": ("changed",)}))
    manifest = project / "project.json"
    manifest.write_text(json.dumps(approved.model_dump(mode="json") | {"approved_split_sha256": None}))
    for operation in (lambda: workflow.deliver_project(project),
                      lambda: delivery.write_delivery(project, plan.inspection, plan)):
        with pytest.raises(projects.ApprovalError):
            operation()
    assert not list((project / "printable").iterdir())
    assert not (project / "assembly.md").exists()


@pytest.mark.parametrize("folder", ["editable", "versions"])
def test_internal_revision_delivers_its_own_geometry(tmp_path, folder):
    original_project = new_project(tmp_path, "original")
    original = box(tmp_path / "original.stl")
    project = new_project(tmp_path, "revision")
    preserved = projects.record_artifact(project, original, "source")
    revision_dir = project / folder / "v2"
    revision_dir.mkdir()
    revised = box(revision_dir / "part.stl", 42)
    for draft, source, width in ((original_project, original, 30), (project, revised, 42)):
        prepare_split(draft, source)
        if draft == project:
            assert len(list((draft / "source").rglob("*.stl"))) == 2
            assert preserved.read_bytes() == original.read_bytes()
            box(revised, 55)
        workflow.approve_split(draft)
        part, = workflow.deliver_project(draft)["parts"]
        assert trimesh.load_mesh(part).extents == pytest.approx([width, 20, 10])


@pytest.mark.parametrize("folder", ["source", "versions", "previews", "printable", "editable"])
def test_managed_directory_rejects_reparse_attribute(tmp_path, monkeypatch, folder):
    project = new_project(tmp_path)
    original_stat = projects.ProjectDirectory.stat

    def reported_stat(self, path):
        value = original_stat(self, path)
        if path.name == folder:
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return value

    with projects.project_lock(project) as locked:
        monkeypatch.setattr(projects.ProjectDirectory, "stat", reported_stat)
        with pytest.raises(ValueError, match="local"):
            with locked.subdirectory(folder):
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_managed_directory_rejects_windows_junction(tmp_path):
    project = new_project(tmp_path)
    local = project / "editable"
    destination = tmp_path / "outside"
    local.rename(destination)
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(local), str(destination)], check=True)
    with projects.project_lock(project) as locked, pytest.raises(ValueError, match="local"):
        with locked.subdirectory("editable"):
            pass


@pytest.mark.parametrize("model_name", ["3D/3dmodel.model", "3D/other.model"])
@pytest.mark.parametrize("limit", ["members", "member_bytes", "total_bytes", "ratio", "stream"])
def test_3mf_enforces_budget_before_xml_or_geometry(tmp_path, monkeypatch, model_name, limit):
    source = tmp_path / "shape.3mf"
    original = io.BytesIO(trimesh.Scene(trimesh.creation.box()).export(file_type="3mf"))
    with ZipFile(original) as scene, ZipFile(source, "w") as archive:
        for name in scene.namelist():
            data = scene.read(name)
            if name == "3D/3dmodel.model":
                name = model_name
            if name == "_rels/.rels":
                data = data.replace(b"/3D/3dmodel.model", ("/" + model_name).encode())
            archive.writestr(name, data)

    if limit == "stream":
        original_open = ZipFile.open

        def oversized_stream(archive, name, *args, **kwargs):
            if getattr(name, "filename", name) == "_rels/.rels":
                return io.BytesIO(b"x" * 1025)
            return original_open(archive, name, *args, **kwargs)

        monkeypatch.setattr(ZipFile, "open", oversized_stream)
    else:
        key = {"members": "_3MF_MAX_MEMBERS", "member_bytes": "_3MF_MAX_MEMBER_BYTES",
               "total_bytes": "_3MF_MAX_TOTAL_BYTES", "ratio": "_3MF_MAX_RATIO"}[limit]
        monkeypatch.setattr(inspection, key, 0.5 if limit == "ratio" else 1, raising=False)

    monkeypatch.setattr(inspection.etree, "parse", lambda *a, **k: pytest.fail("XML received data"))
    monkeypatch.setattr(trimesh, "load_scene", lambda *a, **k: pytest.fail("geometry received data"))
    assert inspection.inspect_mesh(source).issues[0].code == "parse_error"



@pytest.mark.parametrize("metadata", ["encrypted", "absolute", "parent", "backslash", "symlink", "duplicate"])
def test_3mf_rejects_unsafe_member_metadata_before_parsing(tmp_path, monkeypatch, metadata):
    import copy

    path = tmp_path / "box.3mf"
    path.write_bytes(trimesh.Scene(trimesh.creation.box()).export(file_type="3mf"))
    original = ZipFile.infolist

    def reported_members(archive):
        items = [copy.copy(info) for info in original(archive)]
        item = items[0]
        if metadata == "encrypted":
            item.flag_bits |= 1
        elif metadata == "symlink":
            item.create_system = 3
            item.external_attr = stat.S_IFLNK << 16
        elif metadata == "duplicate":
            items.append(copy.copy(item))
        else:
            item.orig_filename = item.filename = {"absolute": "/extra", "parent": "../extra",
                                                   "backslash": "folder\\extra"}[metadata]
        return items

    monkeypatch.setattr(ZipFile, "infolist", reported_members)
    monkeypatch.setattr(inspection.etree, "parse", lambda *a, **k: pytest.fail("XML received data"))
    assert inspection.inspect_mesh(path).issues[0].code == "parse_error"


def test_automatic_source_selection_checks_version_before_listing(tmp_path, monkeypatch):
    project = new_project(tmp_path)
    saved = projects.record_artifact(project, box(tmp_path / "box.stl"), "source")
    calls = []
    original = Path.glob

    def forbid_unanchored_recursive_glob(path, pattern, *args, **kwargs):
        if path.name == "source" and "/" in pattern:
            calls.append(pattern)
        return original(path, pattern, *args, **kwargs)

    real_stat = projects.ProjectDirectory.stat

    def flagged_version(self, path):
        value = real_stat(self, path)
        if path.name == saved.parent.name:
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return value

    monkeypatch.setattr(Path, "glob", forbid_unanchored_recursive_glob)
    monkeypatch.setattr(projects.ProjectDirectory, "stat", flagged_version)
    with pytest.raises(ValueError, match="local"):
        workflow.prepare_overall_preview(project, None, "mm")
    assert not calls


@pytest.mark.parametrize("kind", ["directory_link", "file_link", "traversal", "printable"])
def test_revision_input_rejects_indirect_paths(tmp_path, kind):
    project = new_project(tmp_path)
    source = box(project / "editable" / "box.stl")
    candidate = project / "editable" / "alias.stl"
    if kind == "traversal":
        candidate = project / "editable" / ".." / "editable" / "box.stl"
    elif kind == "printable":
        candidate = box(project / "printable" / "box.stl")
    else:
        try:
            candidate.symlink_to(source if kind == "file_link" else project,
                                 target_is_directory=kind == "directory_link")
        except OSError as error:
            pytest.skip(f"Symlinks unavailable: {error}")
        if kind == "directory_link":
            candidate = candidate / "editable" / "box.stl"
    with pytest.raises(ValueError):
        workflow.prepare_overall_preview(project, candidate, "mm")
    assert not list((project / "source").iterdir())


def test_review_checks_archive_identity_before_reading_input(tmp_path, monkeypatch):
    project = new_project(tmp_path)
    source = box(tmp_path / "box.stl")
    stat_entry = projects.ProjectDirectory.stat

    def archive_stat(self, path):
        value = stat_entry(self, path)
        if path.name == "source":
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return value

    monkeypatch.setattr(projects.ProjectDirectory, "stat", archive_stat)
    monkeypatch.setattr(projects, "open_review_input", lambda *args: pytest.fail("opened input"))
    with pytest.raises(ValueError, match="local"):
        workflow.prepare_overall_preview(project, source, "mm")
