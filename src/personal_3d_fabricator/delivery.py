"""Validate approved print plans and publish their delivery documents."""

import errno
import hashlib
import json
import os
import re
import shutil
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from string import punctuation
from uuid import uuid4

from .adapters._output import publish_output
from .models import ApprovalState, InspectionReport, PrintPlan
from .projects import ProjectDirectory, open_project_file, project_lock
from .workflow import _approved_delivery, _ensure_file

_CHECKSUM = re.compile(r"- `(inspection\.json|print-plan\.json)` SHA-256 `([0-9a-f]{64})`")
_MARKDOWN_ESCAPE = str.maketrans({character: "\\" + character for character in punctuation})


@dataclass(frozen=True)
class DeliveryPaths:
    inspection_json: Path
    print_plan_json: Path
    assembly_md: Path


def _plate_numbers(plan: PrintPlan) -> dict[str, int]:
    part_ids = {part.part_id for part in plan.parts}
    if not part_ids or len(part_ids) != len(plan.parts):
        raise ValueError("Delivery requires unique part IDs")

    numbers = {}
    for number, plate in enumerate(plan.plates, start=1):
        if not plate:
            raise ValueError("Delivery plates must not be empty")
        for part_id in plate:
            if part_id not in part_ids or part_id in numbers:
                raise ValueError(f"Unexpected or repeated part ID on delivery plates: {part_id}")
            numbers[part_id] = number
    if numbers.keys() != part_ids:
        raise ValueError("Delivery plates must include every part ID")
    return numbers


def _markdown_text(value: str) -> str:
    return " ".join(value.splitlines()).translate(_MARKDOWN_ESCAPE)


def _format_xyz(values: tuple[float, float, float] | None, unit: str) -> str:
    if values is None:
        return "未记录"
    return " × ".join(f"{value:g}" for value in values) + f" {unit}"


def _assembly_document(plan: PrintPlan, plate_numbers: dict[str, int]) -> str:
    lines = [
        "# 装配说明",
        "",
        "## 零件与分盘",
        "",
        "| 零件 | XYZ 尺寸 | XYZ 打印方向 | 打印盘 |",
        "| --- | --- | --- | --- |",
    ]
    for part in plan.parts:
        part_id = _markdown_text(part.part_id)
        bounds = _format_xyz(part.bounds_mm, "mm")
        orientation = _format_xyz(part.orientation_deg, "°")
        lines.append(f"| {part_id} | {bounds} | {orientation} | {plate_numbers[part.part_id]} |")

    for heading, entries in (
        ("连接方式", plan.connectors),
        ("五金", plan.hardware),
        ("装配顺序", plan.assembly_order),
        ("风险", plan.risks),
    ):
        lines.extend(("", f"## {heading}", ""))
        if heading == "装配顺序" and entries:
            lines.extend(f"{index}. {_markdown_text(entry)}" for index, entry in enumerate(entries, 1))
        else:
            lines.extend(f"- {_markdown_text(entry)}" for entry in entries or ("未记录",))
    return "\n".join(lines) + "\n"


def _json_text(record: InspectionReport | PrintPlan) -> str:
    data = record.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _paths(directory: Path) -> DeliveryPaths:
    return DeliveryPaths(
        directory / "inspection.json", directory / "print-plan.json", directory / "assembly.md"
    )


def _json_checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(project: ProjectDirectory, path: Path, evidence: ExitStack):
    try:
        stream = evidence.enter_context(open_project_file(path, project))
        return stream.read(), os.fstat(stream.fileno())
    except (OSError, ValueError) as error:
        raise ValueError(f"Delivery JSON is missing or damaged: {path.name}") from error


def _require_same_entry(project: ProjectDirectory, path: Path, expected: os.stat_result) -> None:
    try:
        actual = project.stat(path)
    except OSError as error:
        raise ValueError(f"Delivery file changed: {path.name}") from error
    if not os.path.samestat(actual, expected):
        raise ValueError(f"Delivery file changed: {path.name}")


def _verify_completed(
    project: ProjectDirectory, paths: DeliveryPaths, expected_plan: PrintPlan | None = None
) -> None:
    with ExitStack() as evidence:
        try:
            marker = evidence.enter_context(open_project_file(paths.assembly_md, project))
            assembly = marker.read()
            lines = assembly.decode("utf-8").splitlines()
        except (ValueError, OSError) as error:
            raise FileExistsError(errno.EEXIST, "No readable delivery completion proof", paths.assembly_md) from error
        entries = [match.groups() for line in lines if (match := _CHECKSUM.fullmatch(line))]
        digests = dict(entries)
        if len(entries) != 2 or digests.keys() != {"inspection.json", "print-plan.json"}:
            raise FileExistsError(errno.EEXIST, "No valid delivery completion proof", paths.assembly_md)

        identities = {paths.assembly_md: os.fstat(marker.fileno())}
        saved = {}
        for path in (paths.inspection_json, paths.print_plan_json):
            contents, identities[path] = _read_json(project, path, evidence)
            if _json_checksum(contents) != digests[path.name]:
                raise ValueError(f"Delivery JSON does not match its SHA-256: {path.name}")
            saved[path] = contents

        inspection = InspectionReport.model_validate_json(saved[paths.inspection_json])
        plan = PrintPlan.model_validate_json(saved[paths.print_plan_json])
        if expected_plan is not None and plan != expected_plan:
            raise ValueError("Existing delivery differs from the approved preview")
        documents, canonical_assembly = _delivery_contents(paths, inspection, plan)
        for path, canonical in documents:
            if saved[path] != canonical:
                raise ValueError(f"Delivery differs from canonical content: {path.name}")
        if assembly != canonical_assembly:
            raise ValueError(f"Delivery differs from canonical content: {paths.assembly_md.name}")

        project.verify()
        for path, identity in identities.items():
            _require_same_entry(project, path, identity)


def verify_delivery(
    project_dir: Path,
    *,
    project: ProjectDirectory | None = None,
    expected_plan: PrintPlan | None = None,
) -> DeliveryPaths:
    """Verify that all official documents match the canonical approved delivery."""
    with project_lock(project_dir) if project is None else nullcontext(project) as project:
        plan, geometry = _approved_delivery(project)
        if expected_plan is not None and plan != expected_plan:
            raise ValueError("Delivery plan differs from the approved preview")
        with project.subdirectory("printable") as printable:
            for filename, contents in geometry.items():
                with open_project_file(printable.path / filename, printable) as part:
                    if part.read() != contents:
                        raise ValueError(f"Printable part differs from the approved preview: {filename}")
        try:
            _verify_completed(project, _paths(project.path), plan)
        except FileExistsError as error:
            raise ValueError("Missing or invalid delivery proof in assembly.md") from error
    return _paths(project_dir)


@contextmanager
def _stage_delivery(project: ProjectDirectory):
    staging = project.path / f".delivery-{uuid4().hex}"
    entry = project.entry(staging)
    os.mkdir(entry, mode=0o700, dir_fd=project.fd)
    try:
        yield staging
    finally:
        shutil.rmtree(entry, dir_fd=project.fd)


def _publish(
    project: ProjectDirectory, staged: Path, destination: Path
) -> None:
    project.verify()
    publish_output(staged, destination, directory=project.path, directory_fd=project.fd)
    project.verify()


def _require_expected_json(
    project: ProjectDirectory,
    documents: tuple[tuple[Path, bytes], ...],
    identities: dict[Path, os.stat_result],
    evidence: ExitStack,
) -> None:
    for path, expected in documents:
        contents, actual = _read_json(project, path, evidence)
        if contents != expected or not os.path.samestat(actual, identities[path]):
            raise ValueError(f"Delivery JSON changed: {path.name}")
    for path, identity in identities.items():
        _require_same_entry(project, path, identity)


def _publish_delivery(
    project: ProjectDirectory,
    paths: DeliveryPaths,
    documents: tuple[tuple[Path, bytes], ...],
    assembly_contents: bytes,
) -> None:
    try:
        project.stat(paths.assembly_md)
    except FileNotFoundError:
        pass
    else:
        _verify_completed(project, paths)
        raise FileExistsError(errno.EEXIST, "Delivery already completed", paths.assembly_md)

    with ExitStack() as evidence:
        missing = []
        identities = {}
        for path, contents in documents:
            try:
                saved = evidence.enter_context(open_project_file(path, project))
            except FileNotFoundError:
                missing.append((path, contents))
                continue
            except ValueError as error:
                raise FileExistsError(errno.EEXIST, "Delivery output conflicts with this plan", path) from error
            if saved.read() != contents:
                raise FileExistsError(errno.EEXIST, "Delivery output conflicts with this plan", path)
            identities[path] = os.fstat(saved.fileno())

        staging = evidence.enter_context(_stage_delivery(project))
        for path, contents in (*missing, (paths.assembly_md, assembly_contents)):
            with project.open(staging / path.name, "xb") as stream:
                stream.write(contents)

        for path, _ in missing:
            staged = staging / path.name
            if project.fd is None:
                # Windows can deny renaming a file while a standard stream is open.
                identities[path] = project.stat(staged)
            else:
                source = evidence.enter_context(open_project_file(staged, project))
                identities[path] = os.fstat(source.fileno())
            _publish(project, staged, path)

        _require_expected_json(project, documents, identities, evidence)
        _publish(project, staging / paths.assembly_md.name, paths.assembly_md)
        _require_expected_json(project, documents, identities, evidence)


def write_delivery(
    project_dir: Path,
    inspection: InspectionReport,
    plan: PrintPlan,
    *,
    project: ProjectDirectory | None = None,
) -> DeliveryPaths:
    """Resume or publish official documents for a project with durable split approval."""
    with project_lock(project_dir) if project is None else nullcontext(project) as project:
        approved, geometry = _approved_delivery(project)
        paths = _paths(project.path)
        documents, assembly = _delivery_contents(paths, inspection, plan)
        if plan != approved:
            raise ValueError("Delivery plan differs from the approved preview")
        _finish_delivery(project, approved, geometry, prepared=(documents, assembly))
    return _paths(project_dir)


def _finish_delivery(project, plan, geometry, *, resume=False, prepared=None):
    paths = _paths(project.path)
    with project.subdirectory("printable") as directory:
        parts = [_ensure_file(directory, name, data) for name, data in geometry.items()]
        directory.verify()
    if resume:
        try:
            project.stat(paths.assembly_md)
        except FileNotFoundError:
            pass
        else:
            _verify_completed(project, paths, plan)
            return paths, parts
    documents, assembly = prepared or _delivery_contents(paths, plan.inspection, plan)
    _publish_delivery(project, paths, documents, assembly)
    return paths, parts


def _delivery_contents(
    paths: DeliveryPaths, inspection: InspectionReport, plan: PrintPlan
) -> tuple[tuple[tuple[Path, bytes], ...], bytes]:
    inspection = InspectionReport.model_validate(inspection.model_dump(warnings=False))
    plan = PrintPlan.model_validate(plan.model_dump(warnings=False))
    if plan.approval_state is not ApprovalState.split_approved:
        raise ValueError("Final delivery requires a plan with split_approved")
    if plan.inspection != inspection:
        raise ValueError("Delivery inspection must match the inspection in the print plan")
    plate_numbers = _plate_numbers(plan)

    documents = (
        (paths.inspection_json, _json_text(inspection).encode("utf-8")),
        (paths.print_plan_json, _json_text(plan).encode("utf-8")),
    )
    assembly = _assembly_document(plan, plate_numbers)
    checksums = "\n## 校验摘要\n\n" + "".join(
        f"- `{path.name}` SHA-256 `{_json_checksum(content)}`\n"
        for path, content in documents
    )
    return documents, (assembly + checksums).encode("utf-8")
