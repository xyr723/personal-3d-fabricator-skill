"""Connect project approvals to the exact archived mesh and reviewed print parts."""

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from io import BytesIO
from itertools import groupby
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZipInfo

import numpy as np

from .adapters._output import publish_output
from .approved_source import (
    _approved_overall_preview, _archived_source, _components, _format, open_approved_source, read_approved_mesh,
)
from .connectors import PinSocketSpec, apply_pin_connections, require_pin_backend, verify_pin_connections
from .inspection import (
    GeometryError, SUPPORTED_FORMATS, inspect_mesh_stream, require_usable_inspection, scene_mesh_mm,
)
from .models import (
    ApprovalState, ApprovedMesh, OverallPreview, PrinterProfile, PrintPart, PrintPlan, SplitConnector,
)
from .planning import estimate_print_plan_plates
from .profiles import load_printer_profile, supported_material
from .projects import (
    ApprovalError,
    ProjectDirectory,
    _with_archive_index,
    advance_approval,
    archive_review_input,
    bind_overall_preview,
    bind_split_preview,
    committed_manifest,
    open_project_file,
    project_lock,
    read_project_manifest,
    open_review_input,
    require_overall_preview,
    stable_input,
)
from .splitting import material_components, split_to_fit

_PREVIEW_NAME = "split-preview.zip"


def _source_to_review(project: ProjectDirectory, mesh: Path | None) -> Path:
    if mesh is None:
        manifest = committed_manifest(project)
        candidates = [row for row in manifest.artifacts
                      if row.role == "source" and _format(Path(row.filename)) in SUPPORTED_FORMATS]
        if len(candidates) != 1:
            raise ValueError("Provide one mesh or commit exactly one mesh under source/")
        record = candidates[0]
        _components(record.archive_version, record.filename)
        return project.path / "source" / record.archive_version / record.filename
    if _format(mesh) not in SUPPORTED_FORMATS:
        raise ValueError("An input mesh must use STL, OBJ or 3MF")
    project.verify()
    archived = archive_review_input(project, mesh)
    project.verify()
    return archived


def _render_views(meshes, labels, prefix):
    from .preview import render_turntable

    with TemporaryDirectory(prefix="fabricator-views-") as directory:
        images = render_turntable(meshes, Path(directory), labels)
        return {f"{prefix}-{image.name}": image.read_bytes() for image in images}


def _overall_views(previews: ProjectDirectory, evidence: OverallPreview) -> list[Path]:
    paths = []
    for index, expected in enumerate(evidence.views_sha256, 1):
        path = previews.path / f"overall-view-{index:02d}.png"
        with open_project_file(path, previews) as view:
            if hashlib.file_digest(view, "sha256").hexdigest() != expected:
                raise ValueError(f"Overall preview differs from the recorded image: {path.name}")
        paths.append(path)
    previews.verify()
    return paths


def _verify_overall_source(project, approval, mesh, assumed_unit, *, records=None):
    if assumed_unit is not None and assumed_unit != approval.assumed_unit:
        raise ValueError("Requested units differ from the recorded overall preview")
    if mesh is not None:
        with open_review_input(project, mesh) as (candidate, initial), stable_input(candidate, initial):
            digest = hashlib.file_digest(candidate, "sha256").hexdigest()
        if _format(mesh) != _format(Path(approval.filename)) or digest != approval.sha256:
            raise ValueError("Requested mesh differs from the recorded overall preview")
    with _archived_source(project, approval.archive_version, approval.filename, records=records) as (_, digest):
        if digest != approval.sha256:
            raise ValueError("Archived mesh differs from the recorded overall preview")


def prepare_overall_preview(project_dir: Path, mesh: Path | None, assumed_unit: str | None):
    """Archive the source and publish the overall geometry for human review."""
    with project_lock(project_dir) as project:
        manifest = read_project_manifest(project.path, project=project)
        if manifest.approval_state is not ApprovalState.draft:
            raise ApprovalError("An overall preview requires a draft project")
        with project.subdirectory("previews") as previews:
            evidence = manifest.overall_preview
            if evidence is None:
                archived = _source_to_review(project, mesh)
                with _archived_source(project, archived.parent.name, archived.name) as (source, digest):
                    report, scene = inspect_mesh_stream(source, _format(archived), assumed_unit)
                require_usable_inspection(report)
                try:
                    geometry = scene_mesh_mm(scene, report.format, assumed_unit)
                    images = _render_views([geometry], ["Overall model"], "overall")
                except (ValueError, RuntimeError, ArithmeticError) as error:
                    raise GeometryError(str(error)) from error
                for filename, contents in images.items():
                    _ensure_file(previews, filename, contents)
                evidence = OverallPreview(
                    mesh=ApprovedMesh(
                        archive_version=archived.parent.name, filename=archived.name,
                        sha256=digest, assumed_unit=assumed_unit, inspection=report,
                    ),
                    scale_reference_mm=manifest.scale_reference_mm,
                    views_sha256=tuple(hashlib.sha256(image).hexdigest() for image in images.values()),
                )
                bind_overall_preview(project, evidence)
            else:
                evidence = require_overall_preview(manifest)
                _verify_overall_source(project, evidence.mesh, mesh, assumed_unit)
            return evidence.mesh.inspection.model_dump(mode="json") | {
                "approval_state": manifest.approval_state,
                "scale_reference_mm": evidence.scale_reference_mm,
                "views": _overall_views(previews, evidence),
            }


def approve_overall(project_dir: Path, mesh: Path | None, assumed_unit: str | None):
    """Confirm the archive and images already presented for overall approval."""
    return advance_approval(
        project_dir, ApprovalState.overall_approved, source_mesh=mesh, assumed_unit=assumed_unit
    )


@contextmanager
def _stage(directory: ProjectDirectory):
    path = directory.path / f".fabricator-{uuid4().hex}"
    try:
        with directory.open(path, "x+b") as output:
            yield path, output
    finally:
        try:
            os.unlink(directory.entry(path), dir_fd=directory.fd)
        except FileNotFoundError:
            pass


def _publish(directory: ProjectDirectory, source: Path, destination: Path):
    directory.verify()
    publish_output(source, destination, directory=directory.path, directory_fd=directory.fd)
    directory.verify()


def _reload_print_part(data):
    """Check printable material using the coordinates actually represented by the STL."""
    report, scene = inspect_mesh_stream(BytesIO(data), "stl", "mm")
    if any(issue.severity == "error" for issue in report.issues):
        raise GeometryError("Exported STL failed geometry inspection")
    printed = scene_mesh_mm(scene, "stl", "mm")
    if not printed.is_volume:
        raise GeometryError("STL does not represent an oriented watertight volume")
    try:
        material_components(printed)
    except ValueError as error:
        raise GeometryError(f"Invalid STL shell geometry: {error}") from error
    return report, printed


def _export_checked_stl(mesh):
    """Reinspect the serialized STL and confirm it preserves the part geometry."""
    data = mesh.export(file_type="stl")
    report, printed = _reload_print_part(data)
    if (
        not np.allclose(printed.bounds, mesh.bounds, rtol=1e-6, atol=1e-6)
        or not isclose(printed.volume, mesh.volume, rel_tol=1e-4)
    ):
        raise GeometryError("Exported STL changed the part position, dimensions or volume")
    return data, report, printed


def _source_split(mesh, profile, state):
    origin = mesh.bounds[0].copy()
    mesh.apply_translation(-origin)
    if not mesh.is_volume:
        raise GeometryError("The approved source is not a consistently oriented volume")
    parts, steps, seams = split_to_fit(mesh, profile, state)
    cut = None
    if steps:
        cut = asdict(steps[0][1])
        cut["steps"] = [{"source_path": path, **asdict(step)} for path, step in steps]
    return mesh, origin, parts, cut, seams


def _plan_mesh(project, manifest, profile, connector):
    with open_approved_source(project, manifest) as (source, approval):
        report, approved = read_approved_mesh(source, approval)
    try:
        mesh, origin, meshes, cut, seams = _source_split(approved, profile, manifest.approval_state)
        joints = []
        pin = connector.pin
        if pin is not None:
            if not seams:
                raise GeometryError("The model already fits and has no seam for a pin")
            joints = apply_pin_connections(meshes, seams, pin, max(profile.nozzle_diameters_mm))

        exported = [_export_checked_stl(part) for part in meshes]
        parts = tuple(
            PrintPart(
                part_id=f"part-{index:03d}", bounds_mm=part_report.bounds_mm,
                orientation_deg=(0, 0, 0),
            )
            for index, (_, part_report, _) in enumerate(exported, 1)
        )
        assembly_order = [f"按切割轴顺序摆放 {part.part_id}" for part in parts] if cut else []
        connections, hardware = [], []
        risks = ("平面拆件未添加连接结构，需单独验证固定方式。",) if cut else ()
        if joints:
            connections = [
                f"一体定位销 × {len(joints)}，直径 {pin.pin_diameter_mm:g} mm，"
                f"销长与孔深 {pin.pin_length_mm:g} mm，径向间隙 {pin.clearance_mm:g} mm。"
            ]
            hardware = ["定位销和配合孔已成形于编号打印件中，无需另购五金。"]
            risks = ("先用相同材料和切片参数试印销孔，确认装配间隙和孔底余量。",)
            assembly_order = []
            for _, group in groupby(joints, key=lambda joint: joint["assembly_step"]):
                matches = []
                for joint in group:
                    xyz = ", ".join(f"{v:.9g}" for v in joint["center_mm"])
                    matches.append(
                        f'{joint["pin_part"]} 的定位销沿 {joint["axis"]} 对准 {joint["socket_part"]} '
                        f'在 STL 坐标 XYZ ({xyz}) mm 的配合孔'
                    )
                action = "同时插合本组销孔" if len(matches) > 1 else "插合销孔"
                assembly_order.append("。".join(matches) + f"。{action}，先试装再固定。")
        if origin.any():
            displacement = ", ".join(f"{value:.17g}" for value in origin)
            assembly_order.append(f"如需恢复源坐标，将所有 STL 共同平移 XYZ ({displacement}) mm。")
        plan = PrintPlan(
            inspection=report,
            material=manifest.material,
            approval_state=manifest.approval_state,
            parts=parts,
            assembly_order=tuple(assembly_order),
            connectors=connections, hardware=hardware, risks=risks,
        )
        plan = estimate_print_plan_plates(plan, profile)
        visible = [printed.copy() for _, _, printed in exported]
        if joints:
            # Explode around the assembled centre so that mating faces are visible.
            for printed in visible:
                printed.apply_translation(0.45 * (printed.bounds.mean(axis=0) - mesh.bounds.mean(axis=0)))
        images = _render_views(visible, [part.part_id for part in parts], "split")
    except GeometryError:
        raise
    except (ValueError, RuntimeError, ArithmeticError) as error:
        raise GeometryError(str(error)) from error
    return cut, plan, [data for data, _, _ in exported], images, joints


def _write_preview_bundle(output, documents):
    """Reproducible archive bytes support verification after interrupted publication."""
    with ZipFile(output, "w") as bundle:
        for filename, data in documents.items():
            bundle.writestr(ZipInfo(filename), data)
    output.seek(0)
    return hashlib.file_digest(output, "sha256").hexdigest()


def _legacy_documents(documents, cut):
    old = {name: data for name, data in documents.items()
           if name not in {"connector.json", "joints.json"}}
    if cut is not None:
        old["cut.json"] = json.dumps(
            {key: value for key, value in cut.items() if key != "steps"}, indent=2, allow_nan=False
        )
    return old


def _check_pin_diameter(selection, profile):
    minimum = 2 * min(profile.nozzle_diameters_mm)
    if selection and selection.pin and selection.pin.pin_diameter_mm < minimum:
        raise GeometryError(
            f"Set --pin-diameter-mm to at least {minimum:g} mm (twice the smallest profile "
            "nozzle diameter), select a suitable nozzle profile, or use --connector flat."
        )


def plan_project_split(project_dir: Path, profile_path: Path | None, *, connector="flat",
                       pin_diameter_mm=None, pin_length_mm=None, pin_clearance_mm=None):
    """Create one immutable preview bundle from the approved source."""
    with project_lock(project_dir) as project:
        manifest = read_project_manifest(project.path, project=project)
        if manifest.approval_state is not ApprovalState.overall_approved:
            raise ApprovalError("plan-split requires overall_approved")
        if profile_path is None and manifest.printer_profile is None:
            raise ValueError("Provide --profile or set a printer profile in project.json")
        profile = load_printer_profile(profile_path) if profile_path is not None else manifest.printer_profile
        supported_material(manifest.material, profile)
        pin = None
        if connector == "pin":
            pin = PinSocketSpec(4.0 if pin_diameter_mm is None else pin_diameter_mm,
                                6.0 if pin_length_mm is None else pin_length_mm,
                                0.2 if pin_clearance_mm is None else pin_clearance_mm, 1)
        elif any(value is not None for value in (pin_diameter_mm, pin_length_mm, pin_clearance_mm)):
            raise ValueError("Pin parameters require --connector pin")
        selection = SplitConnector(mode=connector, pin=pin)
        if manifest.split_connector is not None and manifest.split_connector != selection:
            raise ValueError("Selected connector differs from the recorded split preview")
        legacy_flat = manifest.split_preview_sha256 is not None and manifest.split_connector is None
        if legacy_flat and pin is not None:
            raise ValueError("Selected connector differs from the recorded flat preview")
        if pin is not None:
            require_pin_backend()
            _check_pin_diameter(selection, profile)
        with project.subdirectory("previews") as previews:
            destination = previews.path / _PREVIEW_NAME
            # Keep a legacy index in memory until the verified proposal is recorded.
            manifest = _with_archive_index(project, manifest)
            cut, plan, geometry, images, joints = _plan_mesh(project, manifest, profile, selection)
            documents = {
                "print-plan.json": plan.model_dump_json(indent=2),
                "overall-approval.json": _source_binding(manifest),
                "printer-profile.json": profile.model_dump_json(indent=2),
                "connector.json": selection.model_dump_json(),
                "joints.json": json.dumps(joints, allow_nan=False),
            }
            if cut is not None:
                documents["cut.json"] = json.dumps(cut, indent=2, allow_nan=False)
            documents.update({
                f"{part.part_id}.stl": data for part, data in zip(plan.parts, geometry, strict=True)
            })
            documents.update(images)
            if legacy_flat:
                documents = _legacy_documents(documents, cut)
            with _stage(previews) as (staged, output):
                # A stable archive lets retries recover from a publication that
                # finished before its digest was recorded in project.json.
                with output:
                    digest = _write_preview_bundle(output, documents)
                    # Read back the private ZIP before publishing any part of this proposal.
                    try:
                        with ZipFile(output) as candidate:
                            _verify_preview_geometry(
                                project, manifest, candidate, profile, None if legacy_flat else selection
                            )
                    except GeometryError:
                        raise
                    except (BadZipFile, KeyError, ValueError) as error:
                        raise GeometryError(f"Invalid proposed split preview: {error}") from error
                if manifest.split_preview_sha256 not in (None, digest):
                    raise ValueError("A different split preview is already recorded for this project")
                try:
                    _publish(previews, staged, destination)
                except FileExistsError:
                    with open_project_file(destination, previews) as saved, stable_input(saved):
                        saved_digest = hashlib.file_digest(saved, "sha256").hexdigest()
                    if saved_digest != digest:
                        # Older releases could publish the ZIP before recording its digest.
                        # Rebuild their exact content from today's validated source, geometry,
                        # profile and images; never trust metadata from the orphan itself.
                        if (manifest.split_preview_sha256 is not None or pin is not None
                                or (cut and len(cut["steps"]) != 1)):
                            raise ValueError("Existing split preview differs from the proposed plan") from None
                        with _stage(previews) as (_, alternate):
                            old_digest = _write_preview_bundle(alternate, _legacy_documents(documents, cut))
                        if saved_digest != old_digest:
                            raise ValueError("Existing split preview differs from the proposed plan") from None
                        legacy_flat = True
                        digest = saved_digest
                views = [_ensure_file(previews, filename, contents) for filename, contents in images.items()]
                bind_split_preview(project, profile, digest, None if legacy_flat else selection)
        return {
            "preview": destination,
            "preview_sha256": digest,
            "views": views,
            "cut": cut,
            "connector": selection.model_dump(mode="json"),
            "joints": joints,
            "plan": plan.model_dump(mode="json"),
        }


def _source_binding(manifest) -> str:
    preview = _approved_overall_preview(manifest)
    return json.dumps({
        "project": manifest.name,
        "scale_reference_mm": preview.scale_reference_mm,
        "approved_mesh": preview.mesh.model_dump(mode="json"),
    }, sort_keys=True)


def _verify_preview_geometry(project, manifest, bundle, expected_profile, expected_connector):
    """Verify the serialized parts and plan at publication, approval and delivery."""
    if bundle.read("overall-approval.json").decode("utf-8") != _source_binding(manifest):
        raise ValueError("Split preview belongs to a different project or overall approval")
    # Legacy flat-only bundles predate the optional connector evidence.
    selection = (SplitConnector.model_validate_json(bundle.read("connector.json"))
                 if "connector.json" in bundle.namelist() else None)
    if selection != expected_connector:
        raise ValueError("Project connector differs from the split preview")
    profile = PrinterProfile.model_validate_json(bundle.read("printer-profile.json"))
    if profile != expected_profile:
        raise ValueError("Project printer profile differs from the split preview")
    _check_pin_diameter(selection, profile)
    plan = PrintPlan.model_validate_json(bundle.read("print-plan.json"))
    if plan.material != supported_material(manifest.material, profile):
        raise ValueError("Project material differs from the reviewed print plan. Plan a new revision.")
    if (
        plan.approval_state is not ApprovalState.overall_approved
        or plan.inspection != manifest.approved_mesh.inspection
        or not plan.parts
        or [part.part_id for part in plan.parts] != [
            f"part-{number:03d}" for number in range(1, len(plan.parts) + 1)
        ]
    ):
        raise ValueError("Split preview contains a mismatched print plan")
    members = bundle.namelist()
    filenames = [f"{part.part_id}.stl" for part in plan.parts]
    if sorted(name for name in members if name.lower().endswith(".stl")) != sorted(filenames):
        raise ValueError("Split preview does not contain its numbered parts")
    parts = {filename: bundle.read(filename) for filename in filenames}
    pin = selection.pin if selection else None
    with open_approved_source(project, manifest) as (source, approval):
        if pin is not None:
            _, approved = read_approved_mesh(source, approval)
    printed = []
    for part, data in zip(plan.parts, parts.values(), strict=True):
        report, mesh = _reload_print_part(data)
        if pin is not None:
            printed.append(mesh)
        if part.bounds_mm != report.bounds_mm or part.orientation_deg != (0, 0, 0):
            raise GeometryError(f"{part.part_id} STL differs from its planned dimensions or orientation")
    # Re-evaluate fixed-orientation fit and the plate assignments using measured bounds.
    if estimate_print_plan_plates(plan, profile).plates != plan.plates:
        raise ValueError("Split preview plate assignments differ from the printable parts")
    if pin is not None:
        try:
            _, _, flat, cut, seams = _source_split(approved, profile, manifest.approval_state)
            # JSON represents the cut candidate's tuples as arrays.
            if json.loads(bundle.read("cut.json")) != json.loads(json.dumps(cut)):
                raise ValueError("Cut record differs from the approved source intervals")
            verify_pin_connections(flat, seams, printed, json.loads(bundle.read("joints.json")),
                                   pin, max(profile.nozzle_diameters_mm))
        except (ValueError, KeyError, ArithmeticError, RuntimeError) as error:
            detail = str(error).rstrip(".")
            raise GeometryError(
                f"Cannot verify pin connections: {detail}. "
                "Check the connector parameters or select --connector flat when planning."
            ) from error
    return plan, parts


def _read_preview(project, manifest, expected_digest):
    if expected_digest is None:
        raise ApprovalError("Run plan-split to produce a split preview before approving")
    with project.subdirectory("previews") as previews:
        try:
            with open_project_file(previews.path / _PREVIEW_NAME, previews) as source:
                contents = source.read()
        except FileNotFoundError as error:
            raise ApprovalError("The recorded split preview is missing") from error
        digest = hashlib.sha256(contents).hexdigest()
        if digest != expected_digest:
            raise ValueError("Split preview differs from the split approval")

        try:
            with ZipFile(BytesIO(contents)) as bundle:
                plan, parts = _verify_preview_geometry(
                    project, manifest, bundle, manifest.printer_profile, manifest.split_connector
                )
                members = bundle.namelist()
                view_names = [name for name in members if name.startswith("split-view-")]
                if not view_names:
                    raise ValueError("Split preview does not contain turntable images")
                for index, name in enumerate(view_names, 1):
                    if name != f"split-view-{index:02d}.png":
                        raise ValueError("Split preview has invalid image names")
                    with open_project_file(previews.path / name, previews) as view:
                        if view.read() != bundle.read(name):
                            raise ValueError(f"Split preview differs from the recorded image: {name}")
        except (BadZipFile, KeyError) as error:
            raise ValueError("Split preview is invalid") from error
        previews.verify()
        return digest, plan, parts


def approve_split(project_dir: Path):
    """Bind the split approval to the saved preview bytes."""
    return advance_approval(project_dir, ApprovalState.split_approved)


def _ensure_file(directory, filename, contents):
    destination = directory.path / filename
    try:
        with open_project_file(destination, directory) as current:
            if current.read() != contents:
                raise ValueError(f"Existing output differs from the prepared bytes: {filename}")
    except FileNotFoundError:
        with _stage(directory) as (staged, output):
            with output:
                output.write(contents)
            _publish(directory, staged, destination)
    return destination


def _approved_delivery(project):
    manifest = read_project_manifest(project.path, project=project)
    if manifest.approval_state is not ApprovalState.split_approved or manifest.approved_split_sha256 is None:
        raise ApprovalError("deliver requires a project with a recorded split_approved preview")
    if manifest.approved_split_sha256 != manifest.split_preview_sha256:
        raise ApprovalError("Split approval differs from the recorded preview digest")
    _, saved_plan, geometry = _read_preview(project, manifest, manifest.approved_split_sha256)
    plan = PrintPlan.model_validate(
        saved_plan.model_dump() | {"approval_state": ApprovalState.split_approved}
    )
    return plan, geometry


def deliver_project(project_dir: Path):
    """Publish the approved preview bytes and the existing delivery documents."""
    from .delivery import _finish_delivery

    with project_lock(project_dir) as project:
        plan, geometry = _approved_delivery(project)
        paths, parts = _finish_delivery(project, plan, geometry, resume=True)
        return asdict(paths) | {"parts": parts}
