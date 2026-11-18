"""Deterministic geometry checks for STL, OBJ and 3MF files."""

import re
import stat
import struct
from io import BytesIO
from math import isfinite
from pathlib import Path
from string import ascii_lowercase, ascii_uppercase
from tempfile import TemporaryFile
from typing import BinaryIO
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import numpy as np
import trimesh
from lxml import etree

from .models import InspectionIssue, InspectionReport
from .projects import stable_input

SUPPORTED_FORMATS = {"stl", "obj", "3mf"}
_THIN_EXTENT_MM = 0.8
_OBJ_GEOMETRY_LINE = re.compile(rb"(?m)^[ \t]*([vf])[ \t]+([^\r\n]*)")
_OBJ_POSITION_INDEX = re.compile(rb"[+-]?[0-9]+")
_3MF_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_3MF_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_3MF_START_PART = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
_3MF_MAX_CONTAINER_BYTES = 132 * 1024 * 1024
_3MF_MAX_CENTRAL_BYTES = 1024 * 1024
_ZIP_END = struct.Struct("<4s4H2IH")
_ZIP_CENTRAL = struct.Struct("<4s6H3I5H2I")
_3MF_MAX_MEMBERS = 1024
_3MF_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_3MF_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_3MF_MAX_RATIO = 250
_TRIMESH_3MF_MODEL = "3D/3dmodel.model"
_ASCII_LOWERCASE_TABLE = str.maketrans(ascii_uppercase, ascii_lowercase)


class GeometryError(ValueError):
    """An inspected or prepared model cannot be used at this stage."""

    def __init__(self, message: str, report: InspectionReport | None = None):
        super().__init__(message)
        self.report = report


def require_usable_inspection(report: InspectionReport) -> None:
    errors = [issue for issue in report.issues if issue.severity == "error"]
    if any(issue.code in {"missing_scale", "unsupported_format"} for issue in errors):
        raise ValueError("; ".join(f"{issue.code}: {issue.message}" for issue in errors))
    if errors:
        raise GeometryError(
            "; ".join(f"{issue.code}: {issue.message}" for issue in errors), report
        )


def _error_report(
    mesh_format: str, code: str, message: str, *, face_count: int = 0
) -> InspectionReport:
    return InspectionReport(
        format=mesh_format,
        bounds_mm=None,
        watertight=False,
        winding_consistent=False,
        component_count=0,
        face_count=face_count,
        degenerate_face_count=0,
        issues=(InspectionIssue(code=code, severity="error", message=message),),
    )


def _scale_to_mm(unit: str | None) -> float | None:
    if unit is None:
        return None
    try:
        scale = trimesh.units.unit_conversion(unit, "mm")
    except ValueError:
        return None
    return scale if isfinite(scale) and scale > 0 else None


def _obj_position_geometry(data: bytes) -> BytesIO:
    vertex_count = 0

    def normalize_line(match: re.Match[bytes]) -> bytes:
        nonlocal vertex_count
        record, fields = match.groups()
        if record == b"v":
            vertex_count += 1
            return b"v " + fields

        # Geometry uses position indices only. Resolve relative references
        # against the vertices already declared when this face is read.
        positions = []
        for corner in fields.partition(b"#")[0].split():
            position = corner.split(b"/", 1)[0]
            if not _OBJ_POSITION_INDEX.fullmatch(position):
                raise ValueError("Invalid OBJ position index")
            index = int(position)
            if index < 0:
                index += vertex_count + 1
            if index <= 0:
                raise ValueError("Invalid OBJ position index")
            positions.append(str(index).encode("ascii"))
        if len(positions) < 3:
            raise ValueError("An OBJ face requires at least three positions")
        return b"f " + b" ".join(positions)

    data = data.replace(b"\\\r\n", b"").replace(b"\\\n", b"")
    return BytesIO(_OBJ_GEOMETRY_LINE.sub(normalize_line, data))


def _primary_3mf_model(members: dict[str, bytes], member_names: dict[str, str]) -> str | None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    relationships = etree.parse(BytesIO(members["_rels/.rels"]), parser).getroot()
    start_parts = [
        relationship
        for relationship in relationships.findall(_3MF_RELATIONSHIPS + "Relationship")
        if relationship.get("Type") == _3MF_START_PART
    ]
    if relationships.tag != _3MF_RELATIONSHIPS + "Relationships" or len(start_parts) != 1:
        raise ValueError("Expected a single 3MF StartPart relationship")

    start_part = start_parts[0]
    target = urlsplit(start_part.get("Target", ""))
    if (
        start_part.get("TargetMode", "Internal") != "Internal"
        or target.scheme
        or target.netloc
        or target.query
        or target.fragment
    ):
        raise ValueError("The 3MF StartPart must name an internal model")
    part_name = target.path.removeprefix("/")
    decoded_name = unquote(part_name)
    if set(decoded_name.split("/")) & {"", ".", ".."}:
        raise ValueError("Invalid 3MF model part name")
    model_name = member_names.get(part_name.translate(_ASCII_LOWERCASE_TABLE))
    if model_name is None:
        model_name = member_names.get(decoded_name.translate(_ASCII_LOWERCASE_TABLE))
    if model_name is None:
        raise ValueError("The 3MF StartPart target is missing")

    model = etree.parse(BytesIO(members[model_name]), parser).getroot()
    build = model.findall(_3MF_CORE + "build")
    if model.tag != _3MF_CORE + "model" or len(build) != 1:
        raise ValueError("Expected a single build element in the primary 3MF model")
    object_ids = {
        resource.get("id")
        for resource in model.findall(f"{_3MF_CORE}resources/{_3MF_CORE}object")
        if resource.get("id") is not None
    }
    items = build[0].findall(_3MF_CORE + "item")
    if any(item.get("objectid") not in object_ids for item in items):
        raise ValueError("A 3MF build item references an undefined object")
    return model_name if items else None


def _bounded_3mf_members(package: ZipFile) -> tuple[dict[str, bytes], dict[str, str]]:
    entries = package.infolist()
    if len(entries) > _3MF_MAX_MEMBERS:
        raise ValueError("Too many 3MF package members")
    names = {}
    declared_total = 0
    for entry in entries:
        name = entry.orig_filename
        decoded = unquote(name.removesuffix("/"))
        key = name.translate(_ASCII_LOWERCASE_TABLE)
        if (
            key in names or entry.filename != name or "\\" in decoded or ":" in decoded
            or any(ord(char) < 32 for char in decoded)
            or set(decoded.split("/")) & {"", ".", ".."}
            or entry.flag_bits & 1 or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
            or (entry.create_system == 3 and stat.S_IFMT(entry.external_attr >> 16)
                not in {0, stat.S_IFREG, stat.S_IFDIR})
        ):
            raise ValueError("Invalid or conflicting 3MF member")
        names[key] = name
        declared_total += entry.file_size
        if (
            entry.file_size > _3MF_MAX_MEMBER_BYTES or declared_total > _3MF_MAX_TOTAL_BYTES
            or entry.file_size > _3MF_MAX_RATIO * entry.compress_size
        ):
            raise ValueError("3MF package exceeds its decompression budget")

    members = {}
    total = 0
    for entry in entries:
        contents = BytesIO()
        with package.open(entry) as stream:
            while True:
                remaining = min(entry.file_size - contents.tell(), _3MF_MAX_TOTAL_BYTES - total)
                chunk = stream.read(min(65536, remaining + 1))
                if len(chunk) > remaining:
                    raise ValueError("3MF stream exceeds its declared budget")
                if not chunk:
                    break
                contents.write(chunk)
                total += len(chunk)
        if contents.tell() != entry.file_size:
            raise ValueError("Incomplete 3MF member")
        members[entry.filename] = contents.getvalue()
    return members, names


def check_3mf_size(size: int) -> None:
    if size > _3MF_MAX_CONTAINER_BYTES:
        raise ValueError("3MF container exceeds its size limit")


def require_3mf_container_size(source: BinaryIO) -> int:
    size = source.seek(0, 2)
    source.seek(0)
    check_3mf_size(size)
    return size


def _check_3mf_directory(source: BinaryIO, size: int) -> None:
    # Classic EOCD occupies 22 bytes plus at most 65535 bytes of ZIP comment.
    length = min(size, _ZIP_END.size + 65535)
    source.seek(size - length)
    tail = source.read(length)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < _ZIP_END.size:
        raise ValueError("Missing 3MF central directory")
    _, disk, start_disk, on_disk, count, span, offset, comment = _ZIP_END.unpack_from(tail, index)
    end = size - length + index
    if end >= 20:
        source.seek(end - 20)
        if source.read(4) == b"PK\x06\x07":
            raise ValueError("ZIP64 3MF is unsupported")
    if 0xffff in (count, on_disk) or 0xffffffff in (span, offset):
        raise ValueError("ZIP64 3MF is unsupported")
    if (
        disk or start_disk or count != on_disk or count > _3MF_MAX_MEMBERS
        or span > _3MF_MAX_CENTRAL_BYTES or offset + span != end
        or index + _ZIP_END.size + comment != len(tail)
    ):
        raise ValueError("Invalid or oversized 3MF central directory")

    # ZipFile builds its list until the byte span is exhausted, independently
    # of the advertised count. Validate each record boundary before that allocation.
    source.seek(offset)
    for _ in range(count):
        if source.tell() + _ZIP_CENTRAL.size > end:
            raise ValueError("Incomplete 3MF central record")
        fields = _ZIP_CENTRAL.unpack(source.read(_ZIP_CENTRAL.size))
        if fields[0] != b"PK\x01\x02":
            raise ValueError("Invalid 3MF central record")
        if fields[13] != 0:
            raise ValueError("Multi-disk 3MF member is unsupported")
        if fields[2] >= 45 or 0xffffffff in (fields[8], fields[9], fields[-1]):
            raise ValueError("ZIP64 or newer 3MF entry is unsupported")
        source.seek(sum(fields[10:13]), 1)
    if source.tell() != end:
        raise ValueError("3MF directory size and member count disagree")
    source.seek(0)


def _prepare_3mf(source: BinaryIO) -> BinaryIO | None:
    size = require_3mf_container_size(source)
    # Preflight and load the same private bytes, even if the input is edited.
    with TemporaryFile() as snapshot:
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 256 * 1024))
            if not chunk or len(chunk) > remaining:
                raise ValueError("3MF container changed during reading")
            snapshot.write(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise ValueError("3MF container changed during reading")
        _check_3mf_directory(snapshot, size)
        with ZipFile(snapshot) as package:
            members, names = _bounded_3mf_members(package)
    model_name = _primary_3mf_model(members, names)
    if model_name is None:
        return None
    loader_default = next(
        (name for name in members if _TRIMESH_3MF_MODEL.lower() in name.lower()), None
    )
    # Give the mesh loader only bytes that passed streaming validation. For a
    # nonstandard StartPart, place the selected model first under its expected alias.
    adapted = BytesIO()
    with ZipFile(adapted, "w") as normalized:
        if model_name != loader_default:
            alias = _TRIMESH_3MF_MODEL
            while alias.translate(_ASCII_LOWERCASE_TABLE) in names:
                alias = "_/" + alias
            if (len(members) + 1 > _3MF_MAX_MEMBERS
                    or sum(map(len, members.values())) + len(members[model_name]) > _3MF_MAX_TOTAL_BYTES):
                raise ValueError("Normalized 3MF package exceeds its budget")
            normalized.writestr(alias, members[model_name])
        for name, contents in members.items():
            normalized.writestr(name, contents)
    adapted.seek(0)
    return adapted


def _load_scene(source: BinaryIO, mesh_format: str) -> trimesh.Scene:
    options = {"process": False, "validate": False}
    if mesh_format == "obj":
        options.update(
            skip_materials=True,
            maintain_order=True,
            split_objects=True,
            group_material=False,
        )
    elif mesh_format == "3mf":
        options["postprocess"] = False

    if mesh_format == "3mf":
        mesh_source = _prepare_3mf(source)
        if mesh_source is None:
            return trimesh.Scene()
    else:
        if not source.read(1):
            return trimesh.Scene()
        source.seek(0)
        mesh_source = source if mesh_format == "stl" else _obj_position_geometry(source.read())

    scene = trimesh.load_scene(mesh_source, file_type=mesh_format, **options)
    if mesh_format == "stl" and not scene.geometry:
        # The STL loader also returns an empty scene for unrecognized text.
        source.seek(0)
        data = source.read()
        empty_binary = len(data) == 84 and data[80:] == b"\0" * 4
        lines = data.strip().lower().splitlines()
        empty_ascii = (
            len(lines) == 2
            and lines[0].split()[:1] == [b"solid"]
            and lines[1].split()[:1] == [b"endsolid"]
        )
        if not (empty_binary or empty_ascii):
            raise ValueError("Invalid STL data")
    return scene


def _degenerate_count(triangles: np.ndarray) -> int:
    edges = triangles[:, 1:] - triangles[:, :1]
    # Normalize the two edge directions for a scale-independent zero-area check.
    lengths = np.abs(edges).max(axis=2, keepdims=True)
    np.divide(edges, lengths, out=edges, where=lengths != 0)
    normals = np.cross(edges[:, 0], edges[:, 1])
    return int(np.count_nonzero(np.all(normals == 0, axis=1)))


def _scene_units(scene: trimesh.Scene, mesh_format: str, assumed_unit: str | None) -> str | None:
    return scene.metadata.get("units", assumed_unit) if mesh_format == "3mf" else assumed_unit


def _stl_topology(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    vertices, indices = np.unique(mesh.vertices, axis=0, return_inverse=True)
    return trimesh.Trimesh(
        vertices=vertices, faces=indices[mesh.faces], process=False, validate=False
    )


def _inspect_scene(
    scene: trimesh.Scene, mesh_format: str, assumed_unit: str | None
) -> InspectionReport:
    instances = []
    for node in scene.graph.nodes_geometry:
        transform, key = scene.graph[node]
        geometry = scene.geometry[key]
        if isinstance(geometry, trimesh.Trimesh) and len(geometry.faces):
            instances.append((geometry, transform))

    if not instances:
        return _error_report(mesh_format, "empty_model", "The model has no triangle faces.")

    face_count = sum(len(mesh.faces) for mesh, _ in instances)
    scale = _scale_to_mm(_scene_units(scene, mesh_format, assumed_unit))
    limits = []
    origin = instances[0][1][:3, 3]
    thin_extent = False
    watertight = winding_consistent = True
    component_count = degenerate_face_count = 0

    for mesh, transform in instances:
        vertices, faces = mesh.vertices, mesh.faces
        if not (np.isfinite(vertices).all() and np.isfinite(transform).all()):
            return _error_report(
                mesh_format,
                "non_finite_coordinates",
                "The model contains non-finite coordinates or transforms.",
                face_count=face_count,
            )

        if faces.min() < 0 or faces.max() >= len(vertices):
            raise ValueError("Vertex index is out of range")

        if mesh_format == "stl":
            # STL has no shared vertex indices. Identify exactly coincident
            # corners for the topology checks, preserving every triangle.
            mesh = _stl_topology(mesh)
            vertices, faces = mesh.vertices, mesh.faces

        # Keep translation separate so a large scene offset does not erase
        # a small object's dimensions or collapse its triangles.
        triangles = (vertices @ transform[:3, :3].T)[faces]
        degenerate_face_count += _degenerate_count(triangles)
        watertight &= mesh.is_watertight
        winding_consistent &= mesh.is_winding_consistent
        component_count += len(
            trimesh.graph.connected_components(mesh.edges_unique, engine="networkx")
        )

        if scale is not None:
            minimum = triangles.min(axis=(0, 1))
            maximum = triangles.max(axis=(0, 1))
            offset = transform[:3, 3] - origin
            limits.append((minimum + offset, maximum + offset))
            smallest_extent = float((maximum - minimum).min()) * scale
            thin_extent |= 0 < smallest_extent < _THIN_EXTENT_MM

    issues: list[InspectionIssue] = []
    if len(instances) > 1:
        issues.append(
            InspectionIssue(
                code="multiple_meshes",
                severity="error",
                message="The scene contains multiple mesh instances. The report includes all instances.",
            )
        )
    if not watertight:
        issues.append(
            InspectionIssue(
                code="not_watertight",
                severity="error",
                message="Some edges are not shared by exactly two faces.",
            )
        )
    if not winding_consistent:
        issues.append(
            InspectionIssue(
                code="inconsistent_winding",
                severity="error",
                message="Some neighboring faces have inconsistent winding.",
            )
        )
    if degenerate_face_count:
        issues.append(
            InspectionIssue(
                code="degenerate_faces",
                severity="error",
                message="The model contains zero-area faces.",
            )
        )

    bounds_mm = None
    if scale is None:
        issues.append(
            InspectionIssue(
                code="missing_scale",
                severity="error",
                message="A supported unit is required to report dimensions or assess thin features.",
            )
        )
    else:
        minima, maxima = np.asarray(limits).transpose(1, 0, 2)
        extents_mm = (maxima.max(axis=0) - minima.min(axis=0)) * scale
        bounds_mm = tuple(float(extent) for extent in extents_mm)
        if thin_extent:
            issues.append(
                InspectionIssue(
                    code="thin_wall_advisory",
                    severity="warning",
                    message=(
                        "A mesh has a bounding-box dimension below 0.8 mm. "
                        "This advisory is not exhaustive and does not measure local wall thickness."
                    ),
                )
            )

    return InspectionReport(
        format=mesh_format,
        bounds_mm=bounds_mm,
        watertight=watertight,
        winding_consistent=winding_consistent,
        component_count=component_count,
        face_count=face_count,
        degenerate_face_count=degenerate_face_count,
        issues=tuple(issues),
    )


def inspect_mesh(path: Path, assumed_unit: str | None = None) -> InspectionReport:
    """Inspect a mesh without repair or guessing an undocumented scale."""
    mesh_format = path.suffix.removeprefix(".").lower() or "unknown"
    if mesh_format not in SUPPORTED_FORMATS:
        return _error_report(
            mesh_format, "unsupported_format", "Only STL, OBJ and 3MF are supported."
        )

    try:
        with path.open("rb") as source, stable_input(source) as size:
            if mesh_format == "3mf":
                check_3mf_size(size)
            report, _ = inspect_mesh_stream(source, mesh_format, assumed_unit)
    except (OSError, ValueError):
        return _error_report(mesh_format, "parse_error", "The mesh file could not be read or parsed.")
    return report


def inspect_mesh_stream(
    source: BinaryIO, mesh_format: str, assumed_unit: str | None = None
) -> tuple[InspectionReport, trimesh.Scene | None]:
    """Inspect an already-opened mesh and return the same parsed scene for reuse."""
    if mesh_format not in SUPPORTED_FORMATS:
        return _error_report(
            mesh_format, "unsupported_format", "Only STL, OBJ and 3MF are supported."
        ), None
    try:
        scene = _load_scene(source, mesh_format)
    except Exception:
        # Loaders raise format-specific errors that may include the input path.
        return _error_report(
            mesh_format, "parse_error", "The mesh file could not be read or parsed."
        ), None

    try:
        with np.errstate(over="raise", invalid="raise"):
            return _inspect_scene(scene, mesh_format, assumed_unit), scene
    except FloatingPointError:
        return _error_report(
            mesh_format, "non_finite_coordinates",
            "The model's coordinates exceed the supported numeric range.",
        ), None
    except (IndexError, KeyError, ValueError):
        return _error_report(
            mesh_format, "parse_error", "The mesh contains invalid geometry data."
        ), None


def scene_mesh_mm(
    scene: trimesh.Scene, mesh_format: str, assumed_unit: str | None
) -> trimesh.Trimesh:
    """Prepare the successfully inspected single instance for splitting in millimeters."""
    mesh = scene.to_geometry()
    if mesh_format == "stl":
        mesh = _stl_topology(mesh)
    mesh.apply_scale(_scale_to_mm(_scene_units(scene, mesh_format, assumed_unit)))
    return mesh
