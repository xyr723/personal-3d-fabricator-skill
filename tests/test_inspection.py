import io
from zipfile import ZipFile

import numpy as np
import pytest
import trimesh
from fixtures.generate_fixtures import box, open_box

from personal_3d_fabricator.inspection import inspect_mesh


@pytest.fixture
def box_stl(tmp_path):
    path = tmp_path / "box.STL"
    box().export(path, file_type="stl")
    return path


def issue_codes(report):
    return {issue.code: issue.severity for issue in report.issues}


def test_closed_box_reports_dimensions_and_watertight(box_stl):
    report = inspect_mesh(box_stl, assumed_unit="mm")

    assert report.format == "stl"
    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.watertight is True
    assert report.winding_consistent is True
    assert report.component_count == 1
    assert report.face_count == 12
    assert report.degenerate_face_count == 0
    assert not report.issues


@pytest.mark.parametrize("unit", [None, "unknown", "-1 * mm", "nan * mm"])
def test_unknown_units_block_scale_dependent_checks(tmp_path, unit):
    path = tmp_path / "thin-mm.stl"
    trimesh.creation.box(extents=(20.0, 30.0, 0.5)).export(path)

    report = inspect_mesh(path, assumed_unit=unit)

    assert report.bounds_mm is None
    assert issue_codes(report) == {"missing_scale": "error"}
    assert report.watertight is True
    assert report.face_count == 12


def test_open_box_stays_open_without_repair(tmp_path):
    path = tmp_path / "open.stl"
    open_box().export(path)
    original = path.read_bytes()

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.watertight is False
    assert report.winding_consistent is True
    assert report.component_count == 1
    assert report.face_count == 11
    assert issue_codes(report)["not_watertight"] == "error"
    assert path.read_bytes() == original


def test_reversed_face_is_not_reoriented(tmp_path):
    mesh = box()
    mesh.faces[0] = mesh.faces[0, ::-1]
    path = tmp_path / "inverted.stl"
    mesh.export(path)

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.watertight is True
    assert report.winding_consistent is False
    assert issue_codes(report)["inconsistent_winding"] == "error"


def test_components_inside_one_mesh_are_counted(tmp_path):
    first = box()
    second = box().apply_translation((50.0, 0.0, 0.0))
    path = tmp_path / "two.stl"
    trimesh.util.concatenate((first, second)).export(path)

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.bounds_mm == (70.0, 30.0, 40.0)
    assert report.component_count == 2
    assert report.face_count == 24
    assert report.watertight is True


def test_obj_converts_an_explicit_unit(tmp_path):
    path = tmp_path / "box.obj"
    box().export(path)

    report = inspect_mesh(path, assumed_unit="centimeters")

    assert report.format == "obj"
    assert report.bounds_mm == (200.0, 300.0, 400.0)
    assert report.watertight is True
    assert report.component_count == 1


def test_3mf_preserves_instances_and_uses_embedded_units(tmp_path):
    scene = trimesh.Scene()
    scene.add_geometry(box(), geom_name="box", node_name="first")
    translation = trimesh.transformations.translation_matrix((35.0, 0.0, 0.0))
    scene.graph.update(frame_to="second", geometry="box", matrix=translation)

    # The exporter always writes millimeters. Change the declared unit to test
    # reading embedded scale, while keeping the actual mesh and transforms.
    path = tmp_path / "assembly.3mf"
    with (
        ZipFile(io.BytesIO(scene.export(file_type="3mf"))) as original,
        ZipFile(path, "w") as archive,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "3D/3dmodel.model":
                data = data.replace(b'unit="millimeter"', b'unit="centimeter"', 1)
            archive.writestr(name, data)

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.format == "3mf"
    assert report.bounds_mm == (550.0, 300.0, 400.0)
    assert report.component_count == 2
    assert report.face_count == 24
    assert report.watertight is True
    assert issue_codes(report)["multiple_meshes"] == "error"
    assert inspect_mesh(path, assumed_unit="mm") == report


@pytest.mark.parametrize("face", [(0, 0, 1), (0, 1, 2)])
def test_degenerate_faces_are_counted_and_retained(tmp_path, face):
    mesh = trimesh.Trimesh(
        vertices=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        faces=(face, (0, 1, 3)),
        process=False,
    )
    path = tmp_path / "degenerate.stl"
    mesh.export(path)

    report = inspect_mesh(path)

    assert report.face_count == 2
    assert report.degenerate_face_count == 1
    assert issue_codes(report)["degenerate_faces"] == "error"


def test_thin_dimension_is_only_advisory(tmp_path):
    path = tmp_path / "thin.stl"
    trimesh.creation.box(extents=(20.0, 30.0, 0.5)).export(path)

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.bounds_mm == (20.0, 30.0, 0.5)
    assert issue_codes(report) == {"thin_wall_advisory": "warning"}
    assert "not exhaustive" in report.issues[0].message


def test_nonfinite_vertices_are_reported_before_geometry_checks(tmp_path, box_stl):
    data = bytearray(box_stl.read_bytes())
    # First X coordinate after the binary STL header and first facet normal.
    data[96:100] = np.array(np.nan, dtype="<f4").tobytes()
    path = tmp_path / "nonfinite.stl"
    path.write_bytes(data)

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.bounds_mm is None
    assert report.watertight is False
    assert issue_codes(report) == {"non_finite_coordinates": "error"}


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("model.ply", b"", "unsupported_format"),
        ("model", b"", "unsupported_format"),
        ("empty.stl", bytes(84), "empty_model"),
        ("vertices.obj", b"v 0 0 0\n", "empty_model"),
        ("bad.stl", b"this is not an STL", "parse_error"),
        ("bad.obj", b"v 0 0 0\nf 1 2 3\n", "parse_error"),
        ("bad.3mf", b"not a zip file", "parse_error"),
        ("missing.stl", None, "parse_error"),
    ],
)
def test_unusable_inputs_return_structured_errors(tmp_path, name, data, code):
    path = tmp_path / name
    if data is not None:
        path.write_bytes(data)

    report = inspect_mesh(path, assumed_unit="mm")

    assert issue_codes(report) == {code: "error"}
    assert report.bounds_mm is None
    assert report.face_count == 0
    assert report.component_count == 0
    assert str(tmp_path) not in report.model_dump_json()


def test_3mf_empty_build_is_an_empty_model(tmp_path):
    path = tmp_path / "empty.3mf"
    scene = trimesh.Scene(box())
    with (
        ZipFile(io.BytesIO(scene.export(file_type="3mf"))) as original,
        ZipFile(path, "w") as archive,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "3D/3dmodel.model":
                start = data.index(b"<build")
                end = data.index(b"</build>", start) + len(b"</build>")
                data = data[:start] + b"<build/>" + data[end:]
            archive.writestr(name, data)

    assert issue_codes(inspect_mesh(path)) == {"empty_model": "error"}


def test_3mf_translation_does_not_collapse_model_dimensions(tmp_path):
    path = tmp_path / "translated.3mf"
    transform = trimesh.transformations.translation_matrix((1e20, 1e20, 1e20))
    scene = trimesh.Scene()
    scene.add_geometry(box(), transform=transform)
    path.write_bytes(scene.export(file_type="3mf"))

    report = inspect_mesh(path)

    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.degenerate_face_count == 0


def test_3mf_small_instance_translation_contributes_to_bounds(tmp_path):
    path = tmp_path / "translated.3mf"
    scene = trimesh.Scene()
    scene.add_geometry(box(), geom_name="box", node_name="first")
    transform = trimesh.transformations.translation_matrix((5e-9, 0.0, 0.0))
    scene.graph.update(frame_to="second", geometry="box", matrix=transform)
    path.write_bytes(scene.export(file_type="3mf"))

    report = inspect_mesh(path)

    assert report.bounds_mm == (20.0 + 5e-9, 30.0, 40.0)


def test_obj_uses_position_topology_with_relative_texture_indices(tmp_path):
    mesh = box()
    path = tmp_path / "textured.obj"
    data = [f"v {x} {y} {z}" for x, y, z in mesh.vertices]
    data.extend(("vt 0 0", "vt 1 0"))
    for face in mesh.faces:
        # Different texture indices share vertex positions across the UV seam.
        corners = [
            f"{vertex - len(mesh.vertices)}/{texture}"
            for vertex, texture in zip(face, (-1, -2, -1))
        ]
        data.append("f " + " ".join(corners))
    path.write_text("\n".join(data), encoding="ascii")

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.watertight is True
    assert report.component_count == 1
    assert report.face_count == 12


def test_obj_relative_indices_use_vertices_before_each_face(tmp_path):
    path = tmp_path / "interleaved.obj"
    path.write_text(
        "v 0 0 0\nv 20 0 0\nv 0 10 0\nf -3 -2 -1\n"
        "v 100 0 0\nv 120 0 0\nv 100 10 0\nf -3 -2 -1\n",
        encoding="ascii",
    )

    report = inspect_mesh(path, assumed_unit="mm")

    assert report.bounds_mm == (120.0, 10.0, 0.0)
    assert report.component_count == 2
    assert report.face_count == 2
    assert report.winding_consistent is True


def test_obj_zero_position_index_is_invalid(tmp_path):
    path = tmp_path / "zero.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 2 3\n", encoding="ascii")

    assert issue_codes(inspect_mesh(path, assumed_unit="mm")) == {"parse_error": "error"}


@pytest.mark.parametrize("include_unrelated_model", [False, True])
def test_3mf_start_part_selects_the_primary_model(tmp_path, include_unrelated_model):
    path = tmp_path / "primary.3mf"
    with (
        ZipFile(io.BytesIO(trimesh.Scene(box()).export(file_type="3mf"))) as original,
        ZipFile(path, "w") as archive,
    ):
        if include_unrelated_model:
            decoy = trimesh.creation.box(extents=(1.0, 2.0, 3.0))
            with ZipFile(io.BytesIO(trimesh.Scene(decoy).export(file_type="3mf"))) as other:
                archive.writestr("3D/3dmodel.model", other.read("3D/3dmodel.model"))
        for name in original.namelist():
            data = original.read(name)
            if name == "3D/3dmodel.model":
                name = "3D/primary.model"
            elif name == "_rels/.rels":
                data = data.replace(b"/3D/3dmodel.model", b"/3D/primary.model")
            archive.writestr(name, data)

    report = inspect_mesh(path)

    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.component_count == 1
    assert report.watertight is True
    assert not report.issues


@pytest.mark.parametrize("invalid_build", ["missing", "undefined", "partially_undefined"])
def test_3mf_invalid_build_reports_parse_error_without_partial_geometry(tmp_path, invalid_build):
    path = tmp_path / "invalid.3mf"
    with (
        ZipFile(io.BytesIO(trimesh.Scene(box()).export(file_type="3mf"))) as original,
        ZipFile(path, "w") as archive,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "3D/3dmodel.model":
                if invalid_build == "missing":
                    start = data.index(b"<build")
                    end = data.index(b"</build>") + len(b"</build>")
                    data = data[:start] + data[end:]
                elif invalid_build == "undefined":
                    assert b'objectid="1"' in data
                    data = data.replace(b'objectid="1"', b'objectid="999"')
                else:
                    data = data.replace(b"</build>", b'<item objectid="999"/></build>')
            archive.writestr(name, data)

    report = inspect_mesh(path)

    assert issue_codes(report) == {"parse_error": "error"}
    assert report.bounds_mm is None
    assert report.component_count == 0
    assert report.face_count == 0
    assert str(tmp_path) not in report.model_dump_json()


@pytest.mark.parametrize("model_part", ["3D/3dmodel.model", "3D/Primary.Model"])
def test_3mf_start_part_matches_ascii_case_insensitively(tmp_path, model_part):
    path = tmp_path / "case.3mf"
    with (
        ZipFile(io.BytesIO(trimesh.Scene(box()).export(file_type="3mf"))) as original,
        ZipFile(path, "w") as archive,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "3D/3dmodel.model":
                name = model_part
            elif name == "_rels/.rels":
                target = f"/{model_part.swapcase()}".encode("ascii")
                data = data.replace(b"/3D/3dmodel.model", target)
            archive.writestr(name, data)
    original_bytes = path.read_bytes()

    report = inspect_mesh(path)

    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.watertight is True
    assert not report.issues
    assert path.read_bytes() == original_bytes


def test_3mf_rejects_conflicting_ascii_case_members(tmp_path):
    path = tmp_path / "conflicting-parts.3mf"
    path.write_bytes(trimesh.Scene(box()).export(file_type="3mf"))
    with ZipFile(path, "a") as archive:
        archive.writestr("3d/3DMODEL.MODEL", archive.read("3D/3dmodel.model"))

    report = inspect_mesh(path)

    assert issue_codes(report) == {"parse_error": "error"}
    assert report.face_count == 0
    assert report.bounds_mm is None
    assert str(tmp_path) not in report.model_dump_json()
