"""Resource bounds and identity checks at archive entry points."""

import contextlib
import io
import os
from zipfile import ZipFile

import pytest
import trimesh

from personal_3d_fabricator import inspection, projects, workflow


def package():
    return trimesh.Scene(trimesh.creation.box()).export(file_type="3mf")


def no_zip_parser(*args, **kwargs):
    pytest.fail("ZipFile received a package above the preflight limit")


@pytest.mark.parametrize("limit", ["_3MF_MAX_CONTAINER_BYTES", "_3MF_MAX_CENTRAL_BYTES",
                                   "_3MF_MAX_MEMBERS"])
def test_zip_limits_run_before_zipinfo_construction(monkeypatch, limit):
    source = io.BytesIO(package())
    monkeypatch.setattr(inspection, limit, 1, raising=False)
    monkeypatch.setattr(inspection, "ZipFile", no_zip_parser)
    report, scene = inspection.inspect_mesh_stream(source, "3mf")
    assert scene is None and report.issues[0].code == "parse_error"


def test_oversize_sparse_stream_rejected_without_copy_or_zip_parser(tmp_path, monkeypatch):
    path = tmp_path / "sparse.3mf"
    with path.open("wb") as stream:
        # POSIX truncate represents an oversized real input without allocating
        # its contents. Keep the same boundary test small on Windows volumes.
        limit = 1024 if os.name == "nt" else inspection._3MF_MAX_CONTAINER_BYTES
        stream.truncate(limit + 1)
    monkeypatch.setattr(inspection, "_3MF_MAX_CONTAINER_BYTES", limit)
    monkeypatch.setattr(inspection, "ZipFile", no_zip_parser)
    assert inspection.inspect_mesh(path).issues[0].code == "parse_error"


def test_real_zip_member_with_one_backslash_rejected_before_xml(monkeypatch):
    contents = io.BytesIO(package())
    # Set both ZIP name fields consistently, including on platforms where
    # ZipInfo normalizes backslashes while constructing a member.
    with ZipFile(contents, "a") as complete:
        complete.writestr("extra/label", b"canvas")
    raw = contents.getvalue()
    assert raw.count(b"extra/label") == 2
    source = io.BytesIO(raw.replace(b"extra/label", b"extra\\label"))
    with ZipFile(source) as actual:
        info, = (info for info in actual.infolist() if info.orig_filename == "extra\\label")
        assert actual.read(info) == b"canvas"
    source.seek(0)
    monkeypatch.setattr(inspection.etree, "parse", lambda *a: pytest.fail("XML received the package"))
    assert inspection.inspect_mesh_stream(source, "3mf")[0].issues[0].code == "parse_error"


def test_archive_hashing_has_no_unbounded_read(tmp_path, monkeypatch):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    model = tmp_path / "box.3mf"
    model.write_bytes(package())
    saved = projects.record_artifact(project, model, "source")
    real_open = projects.open_project_file

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024
            return self.stream.read(size)

    @contextlib.contextmanager
    def guarded(path, directory):
        with real_open(path, directory) as stream:
            yield Reader(stream) if path.suffix in {".3mf", ".sha256"} else stream

    monkeypatch.setattr(projects, "open_project_file", guarded)
    with projects.project_lock(project) as locked:
        with workflow._archived_source(locked, saved.parent.name, saved.name) as (stream, digest):
            assert len(digest) == 64 and stream.read(4) == b"PK\x03\x04"


def test_archiver_rejects_source_metadata_changed_while_copying(tmp_path, monkeypatch):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    source = tmp_path / "draft.obj"
    source.write_bytes(b"v 0 0 0\n")
    real_digest = projects.hashlib.file_digest

    def finish_digest(*args, **kwargs):
        result = real_digest(*args, **kwargs)
        timestamp = source.stat().st_mtime_ns + 2_000_000_000
        os.utime(source, ns=(timestamp, timestamp))
        return result

    monkeypatch.setattr(projects.hashlib, "file_digest", finish_digest)
    with pytest.raises(ValueError, match="changed"):
        projects.record_artifact(project, source, "source")
    assert not list((project / "source").iterdir())


def test_staging_files_are_opened_against_the_held_directory(tmp_path, monkeypatch):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    source = tmp_path / "draft.obj"
    source.write_bytes(b"v 0 0 0\n")
    real_open = projects.ProjectDirectory.open
    held = []

    def record_open(anchor, path, mode):
        if ".artifact-" in str(path) and "x" in mode:
            assert path.parent == anchor.path
            if anchor.fd is not None:
                assert os.path.samestat(os.fstat(anchor.fd), anchor.identity)
            held.append(path.name)
        return real_open(anchor, path, mode)

    monkeypatch.setattr(projects.ProjectDirectory, "open", record_open)
    saved = projects.record_artifact(project, source, "source")
    assert held == [saved.name, saved.name + ".sha256"]


def test_zip64_rejected_before_zip_parser(monkeypatch):
    import zipfile

    source = io.BytesIO()
    with monkeypatch.context() as patch:
        patch.setattr(zipfile, "ZIP64_LIMIT", 1)
        with ZipFile(source, "w") as actual:
            actual.writestr("label", b"paper")
    assert b"PK\x06\x07" in source.getvalue()
    source.seek(0)
    monkeypatch.setattr(inspection, "ZipFile", no_zip_parser)
    with pytest.raises(ValueError, match="ZIP64"):
        inspection._prepare_3mf(source)


def test_oversize_project_mesh_rejected_before_archiving(tmp_path, monkeypatch):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    source = tmp_path / "box.3mf"
    source.write_bytes(package())
    monkeypatch.setattr(inspection, "_3MF_MAX_CONTAINER_BYTES", 1, raising=False)
    monkeypatch.setattr(projects, "_archive_artifact", lambda *a: pytest.fail("archived oversized input"))
    with pytest.raises(ValueError, match="3MF"):
        workflow.prepare_overall_preview(project, source, None)
    assert not list((project / "source").iterdir())


def test_incomplete_staging_preserves_unowned_entries(tmp_path, monkeypatch):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    source = tmp_path / "draft.obj"
    source.write_bytes(b"v 0 0 0\\n")
    real_open = projects.ProjectDirectory.open
    notes = []

    def add_unrelated_file(anchor, path, mode):
        output = real_open(anchor, path, mode)
        if path.suffix == ".sha256":
            note = anchor.path / "concurrent-note.txt"
            note.write_text("work in progress")
            notes.append(note)
        return output

    monkeypatch.setattr(projects.ProjectDirectory, "open", add_unrelated_file)
    with pytest.raises(ValueError, match="changed"):
        projects.record_artifact(project, source, "source")
    assert len(notes) == 1 and notes[0].read_text() == "work in progress"
    assert not any(not path.name.startswith(".") for path in (project / "source").iterdir())


def test_archived_source_detects_changes_during_inspection(tmp_path):
    projects.create_project(tmp_path, "fixture", 5)
    project = tmp_path / "fixture"
    model = tmp_path / "box.3mf"
    model.write_bytes(package())
    saved = projects.record_artifact(project, model, "source")
    with projects.project_lock(project) as locked, pytest.raises(ValueError, match="changed"):
        with workflow._archived_source(locked, saved.parent.name, saved.name) as (stream, digest):
            assert stream.read(4) == b"PK\x03\x04" and len(digest) == 64
            stamp = saved.stat().st_mtime_ns + 2_000_000_000
            os.utime(saved, ns=(stamp, stamp))


def test_standalone_inspection_detects_concurrent_edits(tmp_path, monkeypatch):
    model = tmp_path / "box.3mf"
    model.write_bytes(package())
    inspect_stream = inspection.inspect_mesh_stream

    def read_then_edit(*args, **kwargs):
        result = inspect_stream(*args, **kwargs)
        stamp = model.stat().st_mtime_ns + 2_000_000_000
        os.utime(model, ns=(stamp, stamp))
        return result

    monkeypatch.setattr(inspection, "inspect_mesh_stream", read_then_edit)
    assert [issue.code for issue in inspection.inspect_mesh(model).issues] == ["parse_error"]


def test_staging_identity_failure_preserves_unrelated_directory_entry(tmp_path, monkeypatch):
    project = tmp_path / "fixture"
    projects.create_project(tmp_path, project.name, 5)
    model = tmp_path / "box.3mf"
    model.write_bytes(package())
    verify = projects.ProjectDirectory.verify
    notes = []

    def checked_directory(anchor):
        verify(anchor)
        if anchor.path.parent == project / "source" and anchor.path.name.startswith(".artifact-"):
            if list(anchor.path.glob("*/*.sha256")):
                note = anchor.path / "other-owner.txt"
                note.write_text("preserve")
                notes.append(note)
                raise ValueError("Directory identity changed")

    monkeypatch.setattr(projects.ProjectDirectory, "verify", checked_directory)
    with pytest.raises(ValueError, match="identity changed"):
        projects.record_artifact(project, model, "source")
    assert len(notes) == 1 and notes[0].read_text() == "preserve"
    assert not any(not path.name.startswith(".") for path in (project / "source").iterdir())
