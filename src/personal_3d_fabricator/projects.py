"""Create project workspaces, archive artifacts, and advance approvals."""

import errno
import hashlib
import os
import re
import stat
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import uuid4

from .models import ApprovalState, ApprovedMesh, ArchivedArtifact, OverallPreview, PrinterProfile, ProjectManifest

_PROJECT_DIRS = ("source", "editable", "printable", "previews", "versions")
_NEXT_APPROVAL = {
    ApprovalState.draft: ApprovalState.overall_approved,
    ApprovalState.overall_approved: ApprovalState.split_approved,
}
_INVALID_FILENAME_CHAR = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])")
_FILENAME_LIMIT = 255


def _validate_filename(value: str, field: str, max_bytes: int = _FILENAME_LIMIT) -> None:
    device_name = value.partition(".")[0].rstrip(" ").upper()
    if (
        not value
        or value != value.strip()
        or value.endswith(".")
        or _INVALID_FILENAME_CHAR.search(value)
        or _WINDOWS_DEVICE_NAME.fullmatch(device_name)
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ValueError(f"{field} must be a portable filename component")


def _manifest_path(project_dir: Path) -> Path:
    path = project_dir / "project.json"
    if not path.is_file():
        raise FileNotFoundError(f"Project manifest not found: {path}")
    return path


def _write_manifest(
    path: Path, manifest: ProjectManifest, *, project: "ProjectDirectory | None" = None
) -> None:
    contents = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    if project is None:
        temporary = NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".project-", delete=False
        )
        temporary_path = Path(temporary.name)
        temporary_entry = temporary_path
    else:
        project.verify()
        temporary_path = project.path / f".project-{uuid4().hex}"
        temporary_entry = project.entry(temporary_path)
        temporary = project.open(temporary_path, "xb")
    try:
        try:
            temporary.write(contents)
        except BaseException as write_error:
            try:
                temporary.close()
            except OSError as close_error:
                write_error.add_note(f"Closing temporary manifest failed: {close_error}")
            raise
        temporary.close()
        if project is not None:
            project.verify()
        if project is None or project.fd is None:
            os.replace(temporary_entry, path)
        else:
            os.replace(
                temporary_entry, project.entry(path), src_dir_fd=project.fd, dst_dir_fd=project.fd
            )
        if project is not None:
            project.verify()
    except BaseException as error:
        try:
            os.unlink(temporary_entry, dir_fd=None if project is None else project.fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            error.add_note(f"Removing temporary manifest failed: {cleanup_error}")
        raise


@dataclass(frozen=True)
class ProjectDirectory:
    path: Path
    identity: os.stat_result
    fd: int | None

    def verify(self) -> None:
        _require_directory_identity(self.path, self.identity, follow_symlinks=False)

    def entry(self, path: Path) -> Path:
        return path if self.fd is None else path.relative_to(self.path)

    def stat(self, path: Path) -> os.stat_result:
        return os.stat(self.entry(path), dir_fd=self.fd, follow_symlinks=False)

    def names(self) -> list[str]:
        self.verify()
        return os.listdir(self.path if self.fd is None else self.fd)

    def open(self, path: Path, mode: str) -> BinaryIO:
        def opener(name, flags):
            safe_flags = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            return os.open(name, flags | safe_flags, mode=0o600, dir_fd=self.fd)

        return open(self.entry(path), mode, opener=opener)


    @contextmanager
    def subdirectory(self, name: str):
        """Anchor a local child directory without following directory symlinks."""
        _validate_filename(name, "directory")
        path = self.path / name
        self.verify()
        identity = self.stat(path)
        if _is_reparse_point(identity) or not stat.S_ISDIR(identity.st_mode):
            raise ValueError(f"Project subdirectory must be local: {path}")
        if self.fd is None:
            with _anchor_directory(path, identity):
                yield ProjectDirectory(path, identity, None)
        else:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                if not os.path.samestat(os.fstat(fd), identity):
                    raise ValueError(f"Project subdirectory changed: {path}")
                yield ProjectDirectory(path, identity, fd)
            finally:
                os.close(fd)


@contextmanager
def project_lock(project_dir: Path):
    """Lock a verified project and hold its directory identity during operations."""
    directory = project_dir.resolve()
    identity = directory.lstat()
    with _anchor_directory(directory, identity) as fd:
        project = ProjectDirectory(directory, identity, fd)
        project.verify()
        manifest = directory / "project.json"
        try:
            manifest_identity = project.stat(manifest)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"Project manifest not found: {manifest}") from error
        if not stat.S_ISREG(manifest_identity.st_mode):
            raise ValueError(f"Project manifest must belong to this project: {manifest}")
        lock = directory / ".approval.lock"
        try:
            existing = project.stat(lock)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"Project lock must be a regular, local file: {lock}")
        with project.open(lock, "a+b") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"Project lock must be a regular, local file: {lock}")
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            project.verify()
            if not os.path.samestat(project.stat(lock), opened):
                raise ValueError(f"Project lock changed: {lock}")
            yield project


def create_project(root: Path, name: str, scale_reference_mm: float | None) -> ProjectManifest:
    """Create a new project and persist its initial draft manifest."""
    _validate_filename(name, "name")
    manifest = ProjectManifest(name=name, scale_reference_mm=scale_reference_mm).with_artifacts(())
    root.mkdir(parents=True, exist_ok=True)
    project_dir = root / name
    project_dir.mkdir()
    with ExitStack() as rollback:
        rollback.callback(project_dir.rmdir)
        for directory in _PROJECT_DIRS:
            path = project_dir / directory
            path.mkdir()
            rollback.callback(path.rmdir)
        _write_manifest(project_dir / "project.json", manifest)
        rollback.pop_all()
    return manifest


def _has_ancestor(path: Path, directory_identity: os.stat_result) -> bool:
    return any(os.path.samestat(parent.stat(), directory_identity) for parent in path.parents)


def _is_reparse_point(identity: os.stat_result) -> bool:
    return bool(getattr(identity, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _require_directory_identity(
    path: Path, expected: os.stat_result, *, follow_symlinks: bool = True
) -> None:
    try:
        actual = path.stat(follow_symlinks=follow_symlinks)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ValueError(f"Project or archive directory changed: {path}") from error
    if _is_reparse_point(actual) or not os.path.samestat(actual, expected):
        raise ValueError(f"Project or archive directory changed: {path}")


@contextmanager
def _pin_windows_directory(path: Path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # Omitting FILE_SHARE_DELETE prevents moving the directory while the handle is open.
    list_directory = 1
    share_read_and_write = 3
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        str(path), list_directory, share_read_and_write, None, open_existing, backup_semantics | open_reparse_point, None
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close_handle(handle)


@contextmanager
def _anchor_directory(directory: Path, identity: os.stat_result):
    if _is_reparse_point(identity) or not stat.S_ISDIR(identity.st_mode):
        raise ValueError(f"Project directory must be local: {directory}")
    if os.name == "nt":
        with _pin_windows_directory(directory):
            _require_directory_identity(directory, identity, follow_symlinks=False)
            yield None
        return

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not os.path.samestat(os.fstat(fd), identity):
            raise ValueError(f"Project or archive directory changed: {directory}")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _anchor_archive(project: Path, archive: Path, identity: os.stat_result):
    if os.name == "nt":
        with _pin_windows_directory(project), _anchor_directory(archive, identity) as fd:
            yield fd
    else:
        with _anchor_directory(archive, identity) as fd:
            yield fd


def _archive_artifact(
    project: Path,
    project_identity: os.stat_result,
    archive: Path,
    archive_identity: os.stat_result,
    source_file: BinaryIO,
    artifact_name: str,
    version_name: str,
    initial: os.stat_result,
):
    def verify_destination():
        _require_directory_identity(project, project_identity)
        _require_directory_identity(archive.parent, project_identity)
        _require_directory_identity(archive, archive_identity, follow_symlinks=False)

    with _anchor_archive(archive.parent, archive, archive_identity) as archive_fd:
        verify_destination()
        parent = ProjectDirectory(archive, archive_identity, archive_fd)
        staging_path = archive / f".artifact-{version_name}"
        version_path = archive / version_name
        os.mkdir(parent.entry(staging_path), mode=0o700, dir_fd=parent.fd)
        staging_identity = parent.stat(staging_path)
        try:
            with parent.subdirectory(staging_path.name) as staging:
                if not os.path.samestat(staging.identity, staging_identity):
                    raise ValueError("Artifact staging directory changed")
                # Rename a child *from the pinned staging directory*. Replacing
                # its public name cannot redirect the source of the publication.
                payload_path = staging.path / ".artifact-content"
                os.mkdir(staging.entry(payload_path), mode=0o700, dir_fd=staging.fd)
                with ExitStack() as pins:
                    payload = pins.enter_context(staging.subdirectory(payload_path.name))
                    files = {}
                    published = False
                    try:
                        digest = _write_artifact_files(payload, source_file, artifact_name, files, initial)
                        if set(payload.names()) != files.keys() or any(
                            not _owns_entry(payload, payload.path / name, identity)
                            for name, identity in files.items()
                        ):
                            raise ValueError("Artifact staging contents changed")
                        verify_destination()
                        staging.verify()
                        payload.verify()
                        if payload.fd is None:
                            # Windows denies renaming the child while pinned.
                            # Its parent stays pinned throughout the rename.
                            pins.close()
                        os.rename(
                            staging.entry(payload_path), parent.entry(version_path),
                            src_dir_fd=staging.fd, dst_dir_fd=parent.fd,
                        )
                        published = True
                        if not os.path.samestat(parent.stat(version_path), payload.identity):
                            raise ValueError("Published version directory changed")
                        verify_destination()
                        staging.verify()
                        return payload.identity, files, digest
                    except BaseException as error:
                        try:
                            location = version_path if published else payload_path
                            owner = parent if published else staging
                            if payload.fd is not None:
                                _remove_owned_files(payload, files)
                            elif _owns_entry(owner, location, payload.identity):
                                with _anchor_directory(location, payload.identity) as fd:
                                    _remove_owned_files(ProjectDirectory(location, payload.identity, fd), files)
                            pins.close()
                            if _owns_entry(owner, location, payload.identity):
                                os.rmdir(owner.entry(location), dir_fd=owner.fd)
                        except OSError as cleanup_error:
                            error.add_note(f"Cleaning owned artifact files failed: {cleanup_error}")
                        raise
        finally:
            if _owns_entry(parent, staging_path, staging_identity):
                try:
                    os.rmdir(parent.entry(staging_path), dir_fd=parent.fd)
                except OSError as cleanup_error:
                    # Preserve a concurrent writer's entries and the original failure.
                    if cleanup_error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise


def _write_artifact_files(payload, source_file, artifact_name, files, initial):
    with stable_input(source_file, initial) as size:
        copied = hashlib.sha256()
        with payload.open(payload.path / artifact_name, "x+b") as saved:
            files[artifact_name] = os.fstat(saved.fileno())
            remaining = size
            while remaining:
                chunk = source_file.read(min(remaining, 256 * 1024))
                if not chunk or len(chunk) > remaining:
                    raise ValueError("Mesh source changed while being copied")
                copied.update(chunk)
                saved.write(chunk)
                remaining -= len(chunk)
            if source_file.read(1):
                raise ValueError("Mesh source changed while being copied")
            saved.seek(0)
            digest = hashlib.file_digest(saved, "sha256").hexdigest()
            if digest != copied.hexdigest():
                raise ValueError("Staged archive differs from its source")
        checksum_name = f"{artifact_name}.sha256"
        with payload.open(payload.path / checksum_name, "xb") as checksum:
            files[checksum_name] = os.fstat(checksum.fileno())
            checksum.write(f"{digest}  {artifact_name}\n".encode("utf-8"))
    return digest


def _remove_owned_files(directory, files):
    for name, identity in files.items():
        path = directory.path / name
        if _owns_entry(directory, path, identity):
            os.unlink(directory.entry(path), dir_fd=directory.fd)


def _owns_entry(parent: ProjectDirectory, path: Path, identity: os.stat_result) -> bool:
    try:
        return os.path.samestat(parent.stat(path), identity)
    except FileNotFoundError:
        return False


@contextmanager
def stable_input(source: BinaryIO, initial: os.stat_result | None = None):
    """Detect in-place edits to a validated file across the read operation."""
    before = os.fstat(source.fileno())
    if initial is not None:
        _require_unchanged_file(initial, before)
    yield before.st_size
    _require_unchanged_file(before, os.fstat(source.fileno()))


def _require_unchanged_file(before, after):
    if (not os.path.samestat(before, after) or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns):
        raise ValueError("Mesh source changed while being read")


def _open_source(path, flags):
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


@contextmanager
def _pinned_file(path: Path, project: ProjectDirectory | None = None):
    """Return the opened file and the same descriptor metadata used to accept it."""
    identity = path.lstat() if project is None else project.stat(path)
    if _is_reparse_point(identity) or not stat.S_ISREG(identity.st_mode):
        raise ValueError(f"Project file must be a regular, local file: {path}")
    source = (
        open(path, "rb", opener=_open_source) if project is None else project.open(path, "rb")
    )
    with source:
        opened = os.fstat(source.fileno())
        if not os.path.samestat(opened, identity):
            raise ValueError(f"Project file changed between validation and opening: {path}")
        if path.suffix.lower() == ".3mf":
            from .inspection import check_3mf_size

            check_3mf_size(opened.st_size)
        yield source, opened


@contextmanager
def open_project_file(path: Path, project: ProjectDirectory | None = None):
    """Open a regular file and keep the validated descriptor alive for its reader."""
    with _pinned_file(path, project) as (source, _):
        yield source


def read_project_manifest(
    project_dir: Path, *, project: ProjectDirectory | None = None
) -> ProjectManifest:
    """Read the validated manifest belonging to the resolved project directory."""
    directory = project_dir.resolve() if project is None else project.path
    path = directory / "project.json"
    try:
        with open_project_file(path, project) as source:
            manifest_bytes = source.read()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Project manifest not found: {path}") from error
    except ValueError as error:
        raise ValueError(f"Project manifest must belong to this project: {path}") from error
    manifest = ProjectManifest.model_validate_json(manifest_bytes)
    if manifest.name != directory.name:
        raise ValueError("Project manifest belongs to a different project")
    return manifest


def record_artifact(project_dir: Path, source: Path, role: str) -> Path:
    """Copy an external file into the project with a version and SHA-256 checksum."""
    project_identity = project_dir.stat()
    project_path = project_dir.resolve()
    _require_directory_identity(project_path, project_identity)
    _manifest_path(project_dir)
    archive_name = "source" if role == "source" else "versions"
    archive_dir = project_path / archive_name
    archive_identity = archive_dir.stat(follow_symlinks=False)
    if (
        _is_reparse_point(archive_identity) or not stat.S_ISDIR(archive_identity.st_mode)
        or archive_dir.resolve() != archive_dir
    ):
        raise ValueError("artifact archive must be a directory inside the project")

    source_path = source.resolve()
    try:
        source_identity = source_path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError("source must be an existing file") from error
    if not stat.S_ISREG(source_identity.st_mode):
        raise ValueError("source must be an existing file")
    if _has_ancestor(source_path, project_identity) or _has_ancestor(
        Path(os.path.abspath(source)), project_identity
    ):
        raise ValueError("source must be outside the project workspace")

    with project_lock(project_dir) as locked, open(source_path, "rb", opener=_open_source) as source_file:
        initial = os.fstat(source_file.fileno())
        if not os.path.samestat(initial, source_identity):
            raise ValueError("source changed between validation and opening")
        return _record_open_artifact(
            locked, project_dir, project_identity, archive_dir, archive_identity, source_file,
            source.suffix, role, initial,
        )


@contextmanager
def checked_artifact(folder, filename, expected_digest, expected_size=None):
    """Read only the bytes that match the authoritative digest and the sidecar."""
    _validate_filename(filename, "archived filename")
    with open_project_file(folder.path / filename, folder) as stream, stable_input(stream) as size:
        if filename.lower().endswith(".3mf"):
            from .inspection import check_3mf_size

            check_3mf_size(size)
        if expected_size is not None and size != expected_size:
            raise ValueError("Archived file differs from its committed size")
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
            raise ValueError("Archived file differs from its recorded SHA-256 checksum")
        expected = f"{expected_digest}  {filename}".encode("utf-8")
        with open_project_file(folder.path / f"{filename}.sha256", folder) as checksum:
            if checksum.read(len(expected) + 3) not in (expected + b"\n", expected + b"\r\n"):
                raise ValueError("Archived file does not match its SHA-256 checksum")
        stream.seek(0)
        yield stream, size
        folder.verify()


def _with_archive_index(locked: ProjectDirectory, manifest: ProjectManifest) -> ProjectManifest:
    """Build a verified legacy index in memory for the caller's transaction."""
    if manifest.artifacts is not None:
        return manifest

    # Older releases only recorded artifact identities as part of a review.
    # Loose files cannot prove that their publication completed successfully.
    reviewed = manifest.overall_preview.mesh if manifest.overall_preview else None
    if reviewed and manifest.approved_mesh and reviewed != manifest.approved_mesh:
        raise ApprovalError("Legacy approved mesh differs from the recorded review")
    previous = reviewed or manifest.approved_mesh
    records = ()
    if previous is not None:
        with locked.subdirectory("source") as archive, archive.subdirectory(previous.archive_version) as version:
            with checked_artifact(version, previous.filename, previous.sha256) as (_, size):
                records = (ArchivedArtifact(
                    archive_version=previous.archive_version, filename=previous.filename, role="source",
                    sha256=previous.sha256, size_bytes=size, committed_at=datetime.now(UTC),
                ),)
    return manifest.with_artifacts(records)


def committed_manifest(locked: ProjectDirectory) -> ProjectManifest:
    manifest = read_project_manifest(locked.path, project=locked)
    updated = _with_archive_index(locked, manifest)
    if updated is not manifest:
        _write_manifest(locked.path / "project.json", updated, project=locked)
    return updated


def _record_open_artifact(
    locked, project_dir, project_identity, archive_dir, archive_identity, source_file, suffix, role, initial
):
    if suffix.lower() == ".3mf":
        from .inspection import check_3mf_size

        check_3mf_size(initial.st_size)
    manifest = committed_manifest(locked)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if not re.fullmatch(r"\.[a-zA-Z0-9]{1,16}", suffix):
        suffix = ""
    max_role_bytes = _FILENAME_LIMIT - len(f"{timestamp}-{suffix}.sha256")
    _validate_filename(role, "role", max_role_bytes)
    artifact_name = f"{timestamp}-{role}{suffix}"
    version_name = f"{timestamp}-{uuid4().hex}"
    identity, files, digest = _archive_artifact(
        project_dir, project_identity, archive_dir, archive_identity, source_file, artifact_name, version_name, initial,
    )
    with locked.subdirectory(archive_dir.name) as archive, archive.subdirectory(version_name) as version:
        if not os.path.samestat(archive.identity, archive_identity) or not os.path.samestat(version.identity, identity):
            raise ValueError("Published archive directory changed")
        with checked_artifact(version, artifact_name, digest, initial.st_size):
            pass
        if set(version.names()) != files.keys() or any(
            not _owns_entry(version, version.path / name, entry) for name, entry in files.items()
        ):
            raise ValueError("Published archive files changed before commit")
        archive.verify()
        version.verify()
        record = ArchivedArtifact(
            archive_version=version_name, filename=artifact_name, role=role, sha256=digest,
            size_bytes=initial.st_size, committed_at=datetime.now(UTC),
        )
        updated = manifest.with_artifacts((*manifest.artifacts, record))
        _write_manifest(locked.path / "project.json", updated, project=locked)
    return project_dir / archive_dir.name / version_name / artifact_name


@contextmanager
def open_review_input(project: ProjectDirectory, path: Path):
    """Read an external input or a pinned editable/version revision in this draft."""
    absolute = path.absolute()
    # Resolve system aliases above the project (for example macOS /var) only.
    root = next((parent for parent in reversed(absolute.parents) if parent.resolve() == project.path), None)
    if root is None:
        resolved = absolute.resolve()
        if _has_ancestor(resolved, project.identity):
            raise ValueError("Review source must not alias a project output")
        with _pinned_file(resolved) as opened:
            yield opened
        return

    if ".." in absolute.parts:
        raise ValueError("Revision source must not contain parent traversal")
    _require_directory_identity(root, project.identity, follow_symlinks=False)
    relative = absolute.relative_to(root)
    if len(relative.parts) < 2 or relative.parts[0] not in {"editable", "versions"}:
        raise ValueError("A project revision source must belong to editable/ or versions/")
    _validate_filename(relative.name, "revision filename")
    with ExitStack() as stack:
        directory = project
        for name in relative.parts[:-1]:
            directory = stack.enter_context(directory.subdirectory(name))
        with _pinned_file(directory.path / relative.name, directory) as opened:
            yield opened


def archive_review_input(project: ProjectDirectory, path: Path) -> Path:
    """Publish a fresh source archive from the pinned input descriptor."""
    with project.subdirectory("source") as archive, open_review_input(project, path) as (source, initial):
        return _record_open_artifact(
            project, project.path, project.identity, archive.path, archive.identity, source, path.suffix,
            "source", initial,
        )


class ApprovalError(ValueError):
    """The requested action is blocked by the persisted approval state."""


def require_overall_preview(manifest: ProjectManifest) -> OverallPreview:
    """Return the recorded overall preview only at the reviewed scale."""
    preview = manifest.overall_preview
    if preview is None:
        raise ApprovalError("Run inspect --project to prepare the overall preview first")
    if preview.scale_reference_mm != manifest.scale_reference_mm:
        raise ApprovalError(
            "scale_reference_mm differs from the recorded overall preview. "
            "Use a new draft project and repeat overall approval."
        )
    return preview


def bind_overall_preview(project: ProjectDirectory, preview: OverallPreview) -> None:
    """Record the preview staged for the first approval under the project lock."""
    manifest = read_project_manifest(project.path, project=project)
    if manifest.approval_state is not ApprovalState.draft:
        raise ApprovalError("An overall preview requires a draft project")
    if manifest.overall_preview is not None and manifest.overall_preview != preview:
        raise ValueError("A different overall preview is already recorded for this project")
    updated = ProjectManifest.model_validate(manifest.model_dump() | {"overall_preview": preview})
    require_overall_preview(updated)
    _write_manifest(project.path / "project.json", updated, project=project)


def bind_split_preview(
    project: ProjectDirectory, profile: PrinterProfile, digest: str, connector
) -> None:
    """Record the exact preview shown for approval under the existing project lock."""
    manifest = read_project_manifest(project.path, project=project)
    if manifest.approval_state is not ApprovalState.overall_approved:
        raise ApprovalError("A split preview requires overall_approved")
    require_overall_preview(manifest)
    if manifest.split_preview_sha256 is not None and manifest.split_preview_sha256 != digest:
        raise ValueError("A different split preview is already recorded for this project")
    if (manifest.artifacts is not None and manifest.split_preview_sha256 == digest
            and manifest.printer_profile == profile and manifest.split_connector == connector):
        return
    manifest = _with_archive_index(project, manifest)
    updated = ProjectManifest.model_validate(
        manifest.model_dump() | {"printer_profile": profile, "split_preview_sha256": digest, "split_connector": connector}
    )
    _write_manifest(project.path / "project.json", updated, project=project)


def advance_approval(
    project_dir: Path,
    target: ApprovalState,
    *,
    project: ProjectDirectory | None = None,
    approved_mesh: ApprovedMesh | None = None,
    split_preview_sha256: str | None = None,
    source_mesh: Path | None = None,
    assumed_unit: str | None = None,
) -> ProjectManifest:
    """Advance one approval step and atomically persist any supplied evidence."""
    from .workflow import _overall_views, _read_preview, _verify_overall_source

    target = ApprovalState(target)
    with project_lock(project_dir) if project is None else nullcontext(project) as locked:
        manifest = read_project_manifest(locked.path, project=locked)
        locked.verify()
        current = manifest.approval_state
        expected = _NEXT_APPROVAL.get(current)
        if expected is None:
            raise ApprovalError(f"Cannot advance approval from final state {current}")
        if target is not expected:
            raise ApprovalError(
                f"Cannot advance approval from {current} to {target}. Expected {expected}."
            )
        if target is ApprovalState.overall_approved:
            if manifest.scale_reference_mm is None:
                raise ApprovalError("scale_reference_mm is required before overall_approved")
            preview = require_overall_preview(manifest)
            if approved_mesh is not None and preview.mesh != approved_mesh:
                raise ApprovalError("Overall approval requires the mesh from the recorded preview")
            manifest = _with_archive_index(locked, manifest)
            _verify_overall_source(
                locked, preview.mesh, source_mesh, assumed_unit, records=manifest.artifacts
            )
            with locked.subdirectory("previews") as previews:
                _overall_views(previews, preview)
            manifest.approved_mesh = preview.mesh
        else:
            digest, _, _ = _read_preview(locked, manifest, manifest.split_preview_sha256)
            if split_preview_sha256 is not None and digest != split_preview_sha256:
                raise ApprovalError("Split approval requires the recorded preview digest")
            manifest.approved_split_sha256 = digest
        approved = ProjectManifest.model_validate(
            manifest.model_dump() | {"approval_state": target}
        )
        _write_manifest(locked.path / "project.json", approved, project=locked)
    return approved
