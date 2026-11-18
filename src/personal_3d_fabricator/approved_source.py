"""Open a committed source archive and load its approved millimetre geometry."""

from contextlib import contextmanager
from pathlib import Path

from .inspection import SUPPORTED_FORMATS, inspect_mesh_stream, require_usable_inspection, scene_mesh_mm
from .models import ApprovalState, OverallPreview
from .projects import (
    ApprovalError, ProjectDirectory, _with_archive_index, checked_artifact, committed_manifest,
    require_overall_preview,
)


def _format(path: Path) -> str:
    return path.suffix.removeprefix(".").lower()


def _components(version: str, filename: str) -> None:
    for name in (version, filename):
        if not name or name in {".", ".."} or any(c in name for c in "/\\:\x00"):
            raise ValueError("Approved source has an invalid archive path")
    if _format(Path(filename)) not in SUPPORTED_FORMATS:
        raise ValueError("An archived mesh must use STL, OBJ or 3MF")


@contextmanager
def _archived_source(project: ProjectDirectory, version: str, filename: str, *, records=None):
    _components(version, filename)
    if records is None:
        records = committed_manifest(project).artifacts
    record = next((row for row in records
                   if row.role == "source" and row.archive_version == version and row.filename == filename), None)
    if record is None:
        raise ValueError("Archived mesh has no committed record")
    with project.subdirectory("source") as archive, archive.subdirectory(version) as folder:
        with checked_artifact(folder, filename, record.sha256, record.size_bytes) as (mesh, _):
            yield mesh, record.sha256


def _approved_overall_preview(manifest) -> OverallPreview:
    if manifest.approved_mesh is None or manifest.scale_reference_mm is None:
        raise ApprovalError("The overall_approved project requires an approved mesh and scale")
    preview = require_overall_preview(manifest)
    if preview.mesh != manifest.approved_mesh:
        raise ApprovalError("Approved mesh differs from the reviewed source. Repeat overall approval.")
    return preview


@contextmanager
def open_approved_source(project, manifest):
    if manifest.approval_state not in (ApprovalState.overall_approved, ApprovalState.split_approved):
        raise ApprovalError("overall_approved is required before splitting")
    approval = _approved_overall_preview(manifest).mesh
    indexed = _with_archive_index(project, manifest)
    with _archived_source(project, approval.archive_version, approval.filename,
                          records=indexed.artifacts) as (source, digest):
        if digest != approval.sha256:
            raise ValueError("Archived mesh differs from the overall approval")
        yield source, approval


def read_approved_mesh(source, approval):
    report, scene = inspect_mesh_stream(source, _format(Path(approval.filename)), approval.assumed_unit)
    require_usable_inspection(report)
    if report != approval.inspection or report.bounds_mm is None:
        raise ValueError("Archived mesh inspection differs from the overall approval")
    return report, scene_mesh_mm(scene, report.format, approval.assumed_unit)
