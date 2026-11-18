import builtins
import hashlib
import io
import os
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
import trimesh

from personal_3d_fabricator import projects, workflow
from personal_3d_fabricator.models import ApprovalState, ProjectManifest
from personal_3d_fabricator.projects import advance_approval, create_project, record_artifact


def prepare_overall(project):
    source = project.parent / "review.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(source)
    workflow.prepare_overall_preview(project, source, "mm")
    return projects.read_project_manifest(project)


def read_manifest(project: Path) -> ProjectManifest:
    return ProjectManifest.model_validate_json((project / "project.json").read_bytes())


def test_create_project_persists_manifest_and_workspace_directories(tmp_path: Path):
    root = tmp_path / "work"
    manifest = create_project(root, "台灯支架", 100.0)
    project = root / "台灯支架"

    assert read_manifest(project) == manifest
    assert manifest.name == "台灯支架"
    assert manifest.scale_reference_mm == 100.0
    for directory in ("source", "editable", "printable", "previews", "versions"):
        assert (project / directory).is_dir()


def test_recreating_project_preserves_existing_project(tmp_path: Path):
    manifest = create_project(tmp_path, "lamp-bracket", 100.0)

    with pytest.raises(FileExistsError):
        create_project(tmp_path, "lamp-bracket", 230.0)

    assert read_manifest(tmp_path / "lamp-bracket") == manifest


@pytest.mark.parametrize(
    "name", ["", "..", "../escape", "nested\\lamp", "C:lamp", "NUL.txt", "COM¹", "lamp.", "lamp "]
)
def test_create_project_rejects_nonportable_names(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match="name"):
        create_project(tmp_path, name, 100.0)

    assert list(tmp_path.iterdir()) == []


def test_invalid_scale_does_not_leave_project_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="scale_reference_mm"):
        create_project(tmp_path, "lamp-bracket", 0)

    assert not (tmp_path / "lamp-bracket").exists()


def test_cannot_approve_split_before_overall(tmp_path: Path):
    manifest = create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"

    with pytest.raises(ValueError, match="overall_approved"):
        advance_approval(project, ApprovalState.split_approved)

    assert read_manifest(project) == manifest


def test_missing_scale_reference_remains_draft(tmp_path: Path):
    manifest = create_project(tmp_path, "photo-prop", None)
    project = tmp_path / "photo-prop"

    assert manifest.approval_state is ApprovalState.draft
    assert manifest.scale_reference_mm is None
    with pytest.raises(ValueError, match="scale_reference_mm"):
        advance_approval(project, ApprovalState.overall_approved)

    assert read_manifest(project) == manifest


def test_approval_advances_in_order_and_persists(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"

    prepare_overall(project)
    for target in (ApprovalState.overall_approved, ApprovalState.split_approved):
        if target is ApprovalState.split_approved:
            workflow.plan_project_split(
                project, Path(__file__).resolve().parents[1] / "profiles" / "generic-single-nozzle.json"
            )
        approved = advance_approval(project, target)

        assert approved.approval_state is target
        assert approved.scale_reference_mm == 100.0
        assert read_manifest(project) == approved

    for target in ApprovalState:
        with pytest.raises(ValueError, match="split_approved"):
            advance_approval(project, target)
        assert read_manifest(project) == approved


def test_failed_manifest_replacement_keeps_previous_state_and_can_retry(tmp_path: Path, monkeypatch):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = prepare_overall(project)
    temporary_files = []

    def fail_replace(path, destination, **kwargs):
        assert Path(path).parent == Path(destination).parent
        temporary_files.append(project / Path(path).name)
        raise OSError("simulated replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="simulated replace failure"):
            advance_approval(project, ApprovalState.overall_approved)

    assert temporary_files and not temporary_files[0].exists()
    assert read_manifest(project) == original
    assert advance_approval(project, ApprovalState.overall_approved).approval_state is (
        ApprovalState.overall_approved
    )


@pytest.mark.parametrize(
    ("role", "directory"),
    [
        ("source", "source"),
        ("editable", "versions"),
        ("printable", "versions"),
        ("preview", "versions"),
    ],
)
def test_record_artifact_routes_copies_and_checksums(tmp_path: Path, role: str, directory: str):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = tmp_path / "原型 part.STL"
    data = b"original mesh\x00"
    original.write_bytes(data)

    artifact = record_artifact(project, original, role)

    assert artifact.is_relative_to(project / directory)
    assert re.match(r"^\d{8}T\d{6}", artifact.name)
    assert artifact.suffix.lower() == ".stl"
    assert artifact.read_bytes() == original.read_bytes() == data
    checksum = artifact.with_suffix(artifact.suffix + ".sha256")
    assert checksum.read_text(encoding="utf-8").split()[0] == hashlib.sha256(data).hexdigest()


def test_record_artifact_keeps_prior_version(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")
    first = record_artifact(project, original, "printable")

    original.write_bytes(b"revised mesh")
    second = record_artifact(project, original, "printable")

    assert first != second
    assert first.read_bytes() == b"original mesh"
    assert second.read_bytes() == original.read_bytes() == b"revised mesh"


@pytest.mark.parametrize("directory", ["editable", "printable", "previews", "versions"])
def test_record_artifact_rejects_project_outputs(tmp_path: Path, directory: str):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    output = project / directory / "part.stl"
    output.write_bytes(b"project output")

    with pytest.raises(ValueError, match="source"):
        record_artifact(project, output, "source")

    assert output.read_bytes() == b"project output"
    assert list((project / "source").iterdir()) == []


def test_record_artifact_rejects_an_alias_to_project_output(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    output = project / "printable" / "part.stl"
    output.write_bytes(b"project output")
    alias = tmp_path / "alias.stl"
    try:
        alias.symlink_to(output)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symbolic links unavailable: {error}")

    with pytest.raises(ValueError, match="source"):
        record_artifact(project, alias, "source")

    assert list((project / "source").iterdir()) == []


def test_record_artifact_rejects_unsafe_role(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")

    with pytest.raises(ValueError, match="role"):
        record_artifact(project, original, "../printable")

    assert list((project / "versions").iterdir()) == []


def test_record_artifact_requires_initialized_project(tmp_path: Path):
    project = tmp_path / "incomplete"
    archive = project / "source"
    archive.mkdir(parents=True)
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")

    with pytest.raises(FileNotFoundError, match="project.json"):
        record_artifact(project, original, "source")

    assert list(archive.iterdir()) == []


def test_approval_does_not_modify_uninitialized_project(tmp_path: Path):
    project = tmp_path / "incomplete"
    project.mkdir()

    with pytest.raises(FileNotFoundError, match="project.json"):
        advance_approval(project, ApprovalState.overall_approved)

    assert list(project.iterdir()) == []


def test_record_artifact_rejects_case_alias_on_case_insensitive_volume(tmp_path: Path):
    create_project(tmp_path, "Lamp-Bracket", 100.0)
    project = tmp_path / "Lamp-Bracket"
    output = project / "printable" / "part.stl"
    output.write_bytes(b"project output")
    alias = tmp_path / "lamp-bracket" / "printable" / "part.stl"
    if not alias.exists():
        pytest.skip("Test volume uses case-sensitive filenames")

    assert alias.samefile(output)
    with pytest.raises(ValueError, match="source"):
        record_artifact(project, alias, "source")

    assert list((project / "source").iterdir()) == []


def test_record_artifact_rejects_project_ancestor_by_stat_identity(tmp_path: Path, monkeypatch):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    alias = tmp_path / "mount-alias"
    alias.mkdir()
    source = alias / "part.stl"
    source.write_bytes(b"project output through a filesystem alias")
    real_stat = Path.stat

    def stat_with_alias(path, *args, **kwargs):
        return real_stat(project if path == alias else path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_alias)
    with pytest.raises(ValueError, match="source"):
        record_artifact(project, source, "source")

    assert list((project / "source").iterdir()) == []


def test_record_artifact_rejects_source_replaced_between_stat_and_open(tmp_path: Path, monkeypatch):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")
    replacement = tmp_path / "replacement.stl"
    replacement.write_bytes(b"swapped mesh")

    def open_with_swap(real_open):
        def open_file(file, *args, **kwargs):
            if file in (original, str(original)) and replacement.exists():
                replacement.replace(original)
            return real_open(file, *args, **kwargs)

        return open_file

    monkeypatch.setattr(builtins, "open", open_with_swap(builtins.open))
    monkeypatch.setattr(io, "open", open_with_swap(io.open))
    with pytest.raises(ValueError, match="source.*changed"):
        record_artifact(project, original, "source")

    assert original.read_bytes() == b"swapped mesh"
    assert list((project / "source").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles can prevent directory renames")
@pytest.mark.parametrize("directory", ["project", "archive"])
def test_record_artifact_rolls_back_if_destination_changes_before_publish(
    tmp_path: Path, monkeypatch, directory: str
):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    archive = project / "versions"
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")
    moving = project if directory == "project" else archive
    displaced = tmp_path / "displaced"
    displaced_archive = displaced / "versions" if directory == "project" else displaced
    real_digest = hashlib.file_digest

    def digest_and_swap(file, algorithm):
        digest = real_digest(file, algorithm)
        moving.rename(displaced)
        archive.mkdir(parents=True)
        return digest

    monkeypatch.setattr(hashlib, "file_digest", digest_and_swap)
    with pytest.raises(ValueError, match="directory.*changed"):
        record_artifact(project, original, "printable")

    assert list(archive.iterdir()) == []
    assert list(displaced_archive.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles can prevent directory renames")
def test_record_artifact_rolls_back_if_archive_moves_immediately_after_publish(
    tmp_path: Path, monkeypatch
):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    archive = project / "versions"
    original = tmp_path / "part.stl"
    original.write_bytes(b"original mesh")
    displaced = tmp_path / "displaced"
    real_rename = os.rename

    def rename_and_swap(source, destination, *args, **kwargs):
        real_rename(source, destination, *args, **kwargs)
        if Path(source).name.startswith(".artifact-"):
            real_rename(archive, displaced)
            archive.mkdir()

    monkeypatch.setattr(os, "rename", rename_and_swap)
    with pytest.raises(ValueError, match="directory.*changed"):
        record_artifact(project, original, "printable")

    assert list(archive.iterdir()) == []
    assert list(displaced.iterdir()) == []


def test_manifest_write_failure_closes_before_cleanup_and_preserves_original_error(
    tmp_path: Path, monkeypatch
):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    original = prepare_overall(project)
    original_error = OSError("simulated manifest write failure")
    real_open = projects.ProjectDirectory.open
    real_unlink = os.unlink
    opened = {}

    def fail_write(anchor, path, mode):
        stream = real_open(anchor, path, mode)
        if not path.name.startswith(".project-"):
            return stream
        opened[path.name] = stream

        class PartialWriter:
            def write(self, data):
                stream.write(data[:10])
                raise original_error

            def close(self):
                stream.close()

        return PartialWriter()

    def reject_unlink_while_open(path, *args, **kwargs):
        stream = opened.get(Path(path).name)
        if stream is not None and not stream.closed:
            raise PermissionError("cannot delete an open temporary file")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(projects.ProjectDirectory, "open", fail_write)
        patch.setattr(os, "unlink", reject_unlink_while_open)
        with pytest.raises(OSError) as failure:
            advance_approval(project, ApprovalState.overall_approved)

    assert failure.value is original_error
    assert list(project.glob(".project-*")) == []
    assert read_manifest(project) == original


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles can prevent directory renames")
def test_record_artifact_rejects_replaced_project_when_alias_still_resolves_to_original(
    tmp_path: Path, monkeypatch
):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    displaced = tmp_path / "displaced"
    source = tmp_path / "part.stl"
    source.write_bytes(b"original mesh")
    real_open = builtins.open

    def open_and_swap(file, *args, **kwargs):
        if file in (source, str(source)) and not displaced.exists():
            project.rename(displaced)
            alias.unlink()
            alias.symlink_to(displaced, target_is_directory=True)
            create_project(tmp_path, "lamp-bracket", 100.0)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_and_swap)
    with pytest.raises(ValueError, match="directory.*changed"):
        record_artifact(alias, source, "printable")

    assert list((project / "versions").iterdir()) == []
    assert list((displaced / "versions").iterdir()) == []


@pytest.mark.parametrize(("role", "directory"), [("source", "source"), ("printable", "versions")])
def test_record_artifact_rejects_archive_swapped_at_source_open(
    tmp_path: Path, monkeypatch, role: str, directory: str
):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    archive = project / directory
    source = tmp_path / "part.stl"
    source.write_bytes(b"original mesh")
    displaced = tmp_path / "displaced"
    real_open = builtins.open

    def open_and_swap(file, *args, **kwargs):
        if file in (source, str(source)) and not displaced.exists():
            archive.rename(displaced)
            archive.mkdir()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_and_swap)
    with pytest.raises(ValueError, match="directory.*changed"):
        record_artifact(project, source, role)

    assert list(archive.iterdir()) == []
    assert list(displaced.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="File creation mode bits are POSIX permissions")
def test_record_artifact_creates_artifact_and_checksum_without_execute_bits(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    project = tmp_path / "lamp-bracket"
    source = tmp_path / "part.stl"
    source.write_bytes(b"original mesh")
    previous_umask = os.umask(0o022)
    try:
        artifact = record_artifact(project, source, "printable")
    finally:
        os.umask(previous_umask)

    checksum = artifact.with_name(f"{artifact.name}.sha256")
    for path in (artifact, checksum):
        assert path.stat().st_mode & 0o111 == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows pin prevents the injected rename")
def test_approval_rejects_replacement_project_after_manifest_read(tmp_path, monkeypatch):
    create_project(tmp_path, "project", 30)
    project = tmp_path / "project"
    displaced = tmp_path / "displaced"
    same_name = tmp_path / "candidate"
    create_project(same_name, "project", 50)
    replacement = same_name / "project"
    original_bytes = (project / "project.json").read_bytes()
    replacement_bytes = (replacement / "project.json").read_bytes()
    read_manifest = projects.read_project_manifest

    def move_after_read(*args, **kwargs):
        manifest = read_manifest(*args, **kwargs)
        project.rename(displaced)
        replacement.rename(project)
        return manifest

    monkeypatch.setattr(projects, "read_project_manifest", move_after_read)
    with pytest.raises(ValueError, match="directory.*changed"):
        advance_approval(project, ApprovalState.overall_approved)

    assert (project / "project.json").read_bytes() == replacement_bytes
    assert (displaced / "project.json").read_bytes() == original_bytes
    assert not list(displaced.glob(".project-*"))


@pytest.mark.skipif(os.name == "nt", reason="Windows pins the project directory")
def test_approval_persists_relative_to_held_directory(tmp_path, monkeypatch):
    create_project(tmp_path, "project", 30)
    project = tmp_path / "project"
    prepare_overall(project)
    identity = project.stat()
    replace = os.replace
    writes = []

    def require_directory_descriptor(source, destination, **kwargs):
        assert not Path(source).is_absolute()
        assert not Path(destination).is_absolute()
        fd = kwargs["dst_dir_fd"]
        assert fd == kwargs["src_dir_fd"]
        assert os.path.samestat(os.fstat(fd), identity)
        writes.append(destination)
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", require_directory_descriptor)
    manifest = advance_approval(project, ApprovalState.overall_approved)
    assert manifest.approval_state is ApprovalState.overall_approved
    assert [Path(path).name for path in writes] == ["project.json"]


def test_approval_uses_pinned_project_path_when_descriptor_is_unavailable(tmp_path, monkeypatch):
    create_project(tmp_path, "project", 30)
    project = (tmp_path / "project").resolve()
    prepare_overall(project)
    pinned = set()
    replace = os.replace
    writes = []

    @contextmanager
    def pin_without_descriptor(path, identity):
        assert path.is_relative_to(project) and os.path.samestat(path.stat(), identity)
        pinned.add(path)
        try:
            yield None
        finally:
            pinned.remove(path)

    def replace_while_pinned(source, destination, **kwargs):
        assert project in pinned
        assert destination == project / "project.json" and source.parent == project
        assert not kwargs
        writes.append(destination)
        return replace(source, destination)

    monkeypatch.setattr(projects, "_anchor_directory", pin_without_descriptor)
    monkeypatch.setattr(os, "replace", replace_while_pinned)
    advance_approval(project, ApprovalState.overall_approved)
    assert writes == [project / "project.json"]
    assert not pinned
    assert read_manifest(project).approval_state is ApprovalState.overall_approved
