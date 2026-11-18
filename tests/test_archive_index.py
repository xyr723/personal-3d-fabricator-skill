"""Source selection uses only archives committed by a verified publisher."""

import hashlib
import io
import json
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import trimesh

from personal_3d_fabricator import inspection, projects, workflow
from test_cli import run_cli


@pytest.fixture
def workspace(tmp_path):
    projects.create_project(tmp_path, "draft", 20)
    model = tmp_path / "box.stl"
    trimesh.creation.box(extents=(20, 12, 5)).export(model)
    return tmp_path / "draft", model


def manifest(project):
    return json.loads((project / "project.json").read_bytes())


def save_manifest(project, data):
    (project / "project.json").write_text(json.dumps(data), encoding="utf-8")


def test_failed_commit_leaves_no_selectable_source(workspace, monkeypatch):
    project, model = workspace
    def unavailable(*args, **kwargs):
        raise OSError("manifest unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(projects, "_write_manifest", unavailable)
        with pytest.raises(OSError, match="unavailable"):
            projects.record_artifact(project, model, "source")
    # A plausible uncommitted directory can survive an interrupted publication.
    leftover = project / "source" / "uncommitted"
    leftover.mkdir()
    target = leftover / "box.stl"
    target.write_bytes(model.read_bytes())
    (leftover / "box.stl.sha256").write_text(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  box.stl\n")
    with pytest.raises(ValueError, match="one mesh|committed"):
        workflow.prepare_overall_preview(project, None, "mm")
    assert target.exists() and manifest(project)["overall_preview"] is None


def test_concurrent_archive_commits_keep_every_record(workspace):
    project, model = workspace
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: projects.record_artifact(project, model, "source"), range(4)))
    rows = manifest(project).get("artifacts") or []
    assert {row["archive_version"] for row in rows} == {path.parent.name for path in paths}
    for row in rows:
        path = project / "source" / row["archive_version"] / row["filename"]
        assert path in paths
        assert row["size_bytes"] == model.stat().st_size
        assert row["sha256"] == hashlib.sha256(model.read_bytes()).hexdigest()
        assert datetime.fromisoformat(row["committed_at"]).tzinfo is not None


def test_legacy_review_migrates_only_its_verified_source(workspace):
    project, model = workspace
    workflow.prepare_overall_preview(project, model, "mm")
    old = manifest(project)
    old.pop("artifacts", None)
    old.pop("artifacts_sha256", None)
    save_manifest(project, old)
    other = project / "source" / "uncommitted"
    other.mkdir()
    (other / "other.stl").write_bytes(model.read_bytes())
    workflow.prepare_overall_preview(project, None, "mm")
    rows = manifest(project).get("artifacts") or []
    assert len(rows) == 1
    assert rows[0]["archive_version"] == old["overall_preview"]["mesh"]["archive_version"]


@pytest.mark.parametrize("change", ["size_bytes", "sha256", "missing", "sidecar"])
def test_registered_source_requires_current_files_and_digest(workspace, change):
    project, model = workspace
    path = projects.record_artifact(project, model, "source")
    data = manifest(project)
    rows = data.get("artifacts") or []
    assert len(rows) == 1, "Archive publication must persist its committed identity"
    if change == "missing":
        path.rename(path.with_suffix(".saved"))
    elif change == "sidecar":
        path.with_suffix(".stl.sha256").write_bytes(b"missing digest")
    else:
        rows[0][change] = 0 if change == "size_bytes" else "f" * 64
        save_manifest(project, data)
    with pytest.raises((ValueError, OSError)):
        workflow.prepare_overall_preview(project, None, "mm")
    assert manifest(project)["overall_preview"] is None


@pytest.mark.parametrize("new_size", [11, 40, 5])
def test_archive_copies_exactly_the_initial_fstat_size(workspace, monkeypatch, new_size):
    project, model = workspace
    source = model.with_suffix(".3mf")
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(inspection, "_3MF_MAX_CONTAINER_BYTES", 32)
    publish = projects._archive_artifact

    def save_during_setup(*args, **kwargs):
        source.write_bytes(b"y" * new_size)
        return publish(*args, **kwargs)

    monkeypatch.setattr(projects, "_archive_artifact", save_during_setup)
    with pytest.raises(ValueError, match="changed|size limit"):
        projects.record_artifact(project, source, "source")
    assert not list((project / "source").iterdir())
    assert not manifest(project).get("artifacts")


def test_zipped_member_must_start_on_local_disk_before_zipfile(monkeypatch):
    data = bytearray(trimesh.Scene(trimesh.creation.box()).export(file_type="3mf"))
    with ZipFile(io.BytesIO(data)) as valid:
        offset = valid.start_dir
    assert data[offset:offset + 4] == b"PK\x01\x02"
    struct.pack_into("<H", data, offset + 34, 1)
    monkeypatch.setattr(inspection, "ZipFile", lambda *a: pytest.fail("multi-disk member reached ZipFile"))
    assert inspection.inspect_mesh_stream(io.BytesIO(data), "3mf")[0].issues[0].code == "parse_error"



def test_changed_published_bytes_never_commit(workspace, monkeypatch):
    project, model = workspace
    publish = projects._archive_artifact

    def damaged_output(*args):
        evidence = publish(*args)
        archive, filename, version = args[2], args[5], args[6]
        materialized = archive / version / filename
        materialized.write_bytes(b"." * materialized.stat().st_size)
        return evidence

    monkeypatch.setattr(projects, "_archive_artifact", damaged_output)
    with pytest.raises(ValueError, match="checksum"):
        projects.record_artifact(project, model, "source")
    assert not manifest(project)["artifacts"]
    assert list((project / "source").glob("*/*.stl"))
    with pytest.raises(ValueError, match="one mesh|committed"):
        workflow.prepare_overall_preview(project, None, "mm")


def test_unreviewed_legacy_files_do_not_migrate(workspace):
    project, model = workspace
    saved = projects.record_artifact(project, model, "source")
    former = manifest(project)
    former.pop("artifacts")
    former.pop("artifacts_sha256", None)
    save_manifest(project, former)
    with pytest.raises(ValueError, match="one mesh|committed"):
        workflow.prepare_overall_preview(project, None, "mm")
    assert saved.exists() and not manifest(project).get("artifacts")


def test_archive_index_metadata_change_is_detected(workspace):
    project, model = workspace
    projects.record_artifact(project, model, "source")
    data = manifest(project)
    data["artifacts"][0]["committed_at"] = "2001-01-01T00:00:00Z"
    save_manifest(project, data)
    with pytest.raises(ValueError, match="archive index"):
        workflow.prepare_overall_preview(project, None, "mm")


def test_project_review_checks_first_descriptor_size(workspace, monkeypatch):
    project, _ = workspace
    source = project / "editable" / "box.3mf"
    source.write_bytes(trimesh.Scene(trimesh.creation.box()).export(file_type="3mf"))
    identity = source.stat()
    actual_stat = os.fstat
    seen = False

    def first_observation(handle):
        nonlocal seen
        value = actual_stat(handle)
        if not seen and os.path.samestat(value, identity):
            seen = True
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_size=inspection._3MF_MAX_CONTAINER_BYTES + 1,
                                   st_ctime_ns=value.st_ctime_ns, st_mtime_ns=value.st_mtime_ns)
        return value

    monkeypatch.setattr(os, "fstat", first_observation)
    with pytest.raises(ValueError, match="size limit"):
        workflow.prepare_overall_preview(project, source, None)
    assert seen and not manifest(project).get("artifacts")


@pytest.fixture
def legacy_review(workspace):
    project, model = workspace
    workflow.prepare_overall_preview(project, model, "mm")
    old = manifest(project)
    del old["artifacts"], old["artifacts_sha256"]
    save_manifest(project, old)
    return project, model


@pytest.mark.parametrize("entry", ["library", "cli"])
def test_legacy_direct_approval_preserves_migrated_index(legacy_review, entry, monkeypatch):
    project, model = legacy_review
    if entry == "cli":
        result = run_cli("approve-overall", project, "--json")
        assert result.returncode == 0, result.stderr
        reported = json.loads(result.stdout)
    else:
        write = projects._write_manifest
        states = []

        def capture(path, updated, **kwargs):
            write(path, updated, **kwargs)
            states.append(updated.approval_state.value)

        monkeypatch.setattr(projects, "_write_manifest", capture)
        reported = projects.advance_approval(project, "overall_approved").model_dump(mode="json")
        assert states == ["overall_approved"]

    saved = manifest(project)
    assert reported == saved
    assert saved["approval_state"] == "overall_approved"
    assert saved["approved_mesh"] == saved["overall_preview"]["mesh"]
    records = saved["artifacts"] or []
    assert len(records) == 1 and len(saved["artifacts_sha256"]) == 64
    assert records[0]["archive_version"] == saved["approved_mesh"]["archive_version"]
    assert records[0]["sha256"] == hashlib.sha256(model.read_bytes()).hexdigest()


def test_rejected_legacy_approval_preserves_entire_manifest(legacy_review):
    project, _ = legacy_review
    original = (project / "project.json").read_bytes()
    (project / "previews" / "overall-view-01.png").write_bytes(b"incomplete preview")
    with pytest.raises(ValueError, match="image"):
        projects.advance_approval(project, "overall_approved")
    assert (project / "project.json").read_bytes() == original


def test_concurrent_legacy_approval_and_archive_keep_both_updates(legacy_review, monkeypatch):
    project, model = legacy_review
    reviewing = Event()
    registering = Event()
    verify_views = workflow._overall_views

    def pause_at_review(*args):
        reviewing.set()
        assert registering.wait(5)
        return verify_views(*args)

    def register():
        assert reviewing.wait(5)
        registering.set()
        return projects.record_artifact(project, model, "editable")

    monkeypatch.setattr(workflow, "_overall_views", pause_at_review)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(projects.advance_approval, project, "overall_approved")
        registration = pool.submit(register)
        approved = approval.result(timeout=10)
        saved = registration.result(timeout=10)
    final = projects.read_project_manifest(project)
    assert approved.artifacts and final.approved_mesh == approved.approved_mesh
    assert final.approval_state == "overall_approved"
    assert {row.archive_version for row in final.artifacts} == {
        approved.approved_mesh.archive_version, saved.parent.name
    }
