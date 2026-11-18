import errno
import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import matplotlib
import pytest
import trimesh
from jsonschema import Draft202012Validator
from markdown_it import MarkdownIt
from matplotlib import pyplot
from matplotlib.backends.backend_agg import FigureCanvasAgg
from PIL import Image

from personal_3d_fabricator import delivery, projects, workflow
from personal_3d_fabricator.delivery import write_delivery
from personal_3d_fabricator.models import ApprovalState, InspectionReport, PrintPart, PrintPlan
from personal_3d_fabricator.preview import render_turntable

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {"inspection.json", "print-plan.json", "assembly.md"}


@pytest.fixture(scope="module")
def reviewed_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("delivery")
    project = root / "project"
    projects.create_project(root, project.name, 300)
    source = root / "box.stl"
    trimesh.creation.box(extents=(300, 20, 10)).export(source)
    workflow.prepare_overall_preview(project, source, "mm")
    workflow.approve_overall(project, None, None)
    split = workflow.plan_project_split(project, ROOT / "profiles" / "generic-single-nozzle.json")
    workflow.approve_split(project)
    return project, PrintPlan.model_validate(split["plan"] | {"approval_state": "split_approved"})


@pytest.fixture
def project_dir(tmp_path, reviewed_project):
    return shutil.copytree(reviewed_project[0], tmp_path / "project")


@pytest.fixture
def approved_plan(reviewed_project):
    return reviewed_project[1].model_copy(deep=True)


def _output_names(project_dir: Path) -> set[str]:
    return {path.name for path in project_dir.iterdir() if path.name in DOCUMENTS
            or path.name.startswith(".delivery-")}



def detailed_plan() -> PrintPlan:
    inspection = InspectionReport(
        format="stl",
        bounds_mm=(30, 20, 10),
        watertight=True,
        winding_consistent=True,
        component_count=1,
        face_count=12,
        degenerate_face_count=0,
        issues=({"code": "scale", "severity": "info", "message": "比例已核验"},),
    )
    return PrintPlan(
        inspection=inspection,
        approval_state=ApprovalState.split_approved,
        parts=(
            PrintPart(part_id="P01", bounds_mm=(15, 20, 10), orientation_deg=(0, 90, 0)),
            PrintPart(part_id="P02", bounds_mm=(15, 20, 10), orientation_deg=(0, 0, 0)),
        ),
        plates=(("P02",), ("P01",)),
        connectors=("定位销",),
        hardware=("磁铁 × 2",),
        assembly_order=("试装 P01", "装入磁铁并合上 P02"),
        risks=("先核对磁极",),
    )


def test_delivery_contains_valid_sorted_utf8_json(project_dir, approved_plan):
    paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)

    for path, schema, expected in (
        (paths.inspection_json, "inspection", approved_plan.inspection),
        (paths.print_plan_json, "print-plan", approved_plan),
    ):
        assert path.parent == project_dir
        contents = path.read_text(encoding="utf-8")
        data = json.loads(contents)
        schema_path = ROOT / "schemas" / f"{schema}.schema.json"
        Draft202012Validator(json.loads(schema_path.read_text())).validate(data)
        assert data == expected.model_dump(mode="json")
        assert contents == json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert "按切割轴顺序摆放" in paths.print_plan_json.read_text(encoding="utf-8")


def test_assembly_includes_plate_assignments_and_assembly_details():
    approved_plan = detailed_plan()
    markdown = delivery._assembly_document(approved_plan, delivery._plate_numbers(approved_plan))
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    rows = []
    row = None
    for token in tokens:
        if token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            rows.append(row)
            row = None
        elif token.type == "inline" and row is not None:
            row.append(token.content)

    parts = {row[0]: row[1:] for row in rows[1:]}
    assert set(parts) == {"P01", "P02"}
    assert any("15" in cell and "20" in cell and "10" in cell for cell in parts["P01"])
    assert any("90" in cell for cell in parts["P01"])
    assert "2" in parts["P01"] and "1" in parts["P02"]
    assert sum(token.type == "heading_open" for token in tokens) >= 5
    assert any(token.type == "ordered_list_open" for token in tokens)
    content = [token.content for token in tokens if token.type == "inline"]
    for value in ("定位销", "磁铁 × 2", "先核对磁极"):
        assert any(value in entry for entry in content)
    assert content.index(approved_plan.assembly_order[0]) < content.index(
        approved_plan.assembly_order[1]
    )


@pytest.mark.parametrize("state", [ApprovalState.draft, ApprovalState.overall_approved])
def test_delivery_requires_split_approval(project_dir, approved_plan, state):
    plan = approved_plan.model_copy(update={"approval_state": state})
    with pytest.raises(ValueError, match="split_approved"):
        write_delivery(project_dir, plan.inspection, plan)
    assert _output_names(project_dir) == set()


@pytest.mark.parametrize(
    "plates",
    [(), (("part-001",),), (("part-001", "part-002"), ("part-001",)),
     (("part-001", "part-099"),), (("part-001", "part-002"), ())],
)
def test_delivery_rejects_inconsistent_plates(project_dir, approved_plan, plates):
    plan = approved_plan.model_copy(update={"plates": plates})
    with pytest.raises(ValueError, match="plate"):
        write_delivery(project_dir, plan.inspection, plan)
    assert _output_names(project_dir) == set()


@pytest.mark.parametrize("field", ["inspection", "embedded_inspection", "part"])
def test_delivery_revalidates_nested_records(project_dir, approved_plan, field):
    inspection = approved_plan.inspection.model_copy(deep=True)
    if field == "inspection":
        inspection.face_count = -1
    elif field == "embedded_inspection":
        approved_plan.inspection.face_count = -1
    else:
        approved_plan.parts[0].orientation_deg = (0, 0, float("nan"))

    with pytest.raises(ValueError):
        write_delivery(project_dir, inspection, approved_plan)
    assert _output_names(project_dir) == set()


def test_delivery_rejects_inspection_different_from_the_plan(project_dir, approved_plan):
    inspection = approved_plan.inspection.model_copy(update={"face_count": 24})
    with pytest.raises(ValueError, match="inspection"):
        write_delivery(project_dir, inspection, approved_plan)
    assert _output_names(project_dir) == set()


def test_delivery_rejects_duplicate_parts(project_dir, approved_plan):
    plan = approved_plan.model_copy(update={"parts": (approved_plan.parts[0],) * 2})
    with pytest.raises(ValueError, match="part"):
        write_delivery(project_dir, plan.inspection, plan)
    assert _output_names(project_dir) == set()


def test_delivery_preserves_existing_document(project_dir, approved_plan):
    existing = project_dir / "assembly.md"
    existing.write_text("previous approval", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == {existing.name}
    assert existing.read_text(encoding="utf-8") == "previous approval"


def test_delivery_preserves_all_complete_files_if_a_file_appears_during_publication(
    project_dir, approved_plan, monkeypatch
):
    publish_output = delivery.publish_output

    def publish_with_collision(staged, destination, **kwargs):
        if destination.name == "print-plan.json":
            destination.write_text("another writer", encoding="utf-8")
        publish_output(staged, destination, **kwargs)

    monkeypatch.setattr(delivery, "publish_output", publish_with_collision)
    with pytest.raises(FileExistsError):
        delivery.write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == {"inspection.json", "print-plan.json"}
    assert (project_dir / "print-plan.json").read_text(encoding="utf-8") == "another writer"


def test_delivery_resumes_when_interrupted_after_a_rename(project_dir, approved_plan, monkeypatch):
    publish_output = delivery.publish_output

    def interrupt_after_rename(staged, destination, **kwargs):
        publish_output(staged, destination, **kwargs)
        if destination.name == "print-plan.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(delivery, "publish_output", interrupt_after_rename)
    with pytest.raises(KeyboardInterrupt):
        delivery.write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == {"inspection.json", "print-plan.json"}
    first = project_dir / "inspection.json"
    second = project_dir / "print-plan.json"
    identities = first.stat().st_ino, second.stat().st_ino
    monkeypatch.setattr(delivery, "publish_output", publish_output)
    paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)
    assert paths.assembly_md.is_file()
    assert identities == (first.stat().st_ino, second.stat().st_ino)
    with pytest.raises(FileExistsError):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)


def test_turntable_uses_agg_and_stable_orthographic_views(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    box = trimesh.creation.box(extents=(4, 2, 1))
    sphere = trimesh.creation.icosphere(subdivisions=1)
    sphere.apply_translation([2, 1, 3])
    meshes = [box, sphere]
    for index in range(5):
        marker = trimesh.creation.box(extents=(0.5, 0.5, 0.5))
        marker.apply_translation([index - 2, -2, 0])
        meshes.append(marker)
    labels = [f"P{index:02d}" for index in range(1, 8)]
    rendered_views = []
    print_png = FigureCanvasAgg.print_png

    def inspect_axes(canvas, *args, **kwargs):
        axes = canvas.figure.axes[0]
        bounds = axes.get_xlim3d(), axes.get_ylim3d(), axes.get_zlim3d()
        legend = tuple(text.get_text() for text in axes.get_legend().get_texts())
        colors = tuple(tuple(part.get_facecolor()[0]) for part in axes.collections)
        rendered_views.append(
            (axes.elev, axes.azim, axes.get_proj()[3, :3], bounds, axes.get_box_aspect(), legend, colors)
        )
        return print_png(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", inspect_axes)
    first = render_turntable(meshes, tmp_path, labels)
    second = render_turntable(meshes, tmp_path, labels)

    assert len(first) == len(second) >= 3
    assert set(first).isdisjoint(second)
    assert len({path.parent for path in first}) == 1
    assert len({path.parent for path in second}) == 1
    assert len({view[1] for view in rendered_views[: len(first)]}) == len(first)
    for elevation, azimuth, projection, bounds, aspect, legend_labels, colors in rendered_views:
        assert projection == pytest.approx((0, 0, 0))
        assert len({limit[1] - limit[0] for limit in bounds}) == 1
        assert aspect == pytest.approx((aspect[0],) * 3)
        assert legend_labels == tuple(labels)
        assert len(set(colors)) == len(meshes)
    for index, (left, right) in enumerate(zip(first, second)):
        assert left.read_bytes() == right.read_bytes()
        assert rendered_views[index][:2] == rendered_views[index + len(first)][:2]
        assert rendered_views[index][-1] == rendered_views[index + len(first)][-1]
        with Image.open(left) as image:
            assert image.format == "PNG"
            assert all(size > 0 for size in image.size)
            image.verify()
    assert len({path.read_bytes() for path in first}) == len(first)


@pytest.mark.parametrize("failure", ["empty_list", "empty_mesh", "nonfinite", "blank", "zero_area", "label_count"])
def test_turntable_rejects_invalid_inputs_without_writing(tmp_path, failure):
    mesh = trimesh.creation.box()
    meshes = [mesh]
    labels = ["P01"]
    if failure == "empty_list":
        meshes, labels = [], []
    elif failure == "empty_mesh":
        meshes = [trimesh.Trimesh()]
    elif failure == "nonfinite":
        mesh.vertices[0, 0] = float("inf")
    elif failure == "zero_area":
        meshes = [trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [2, 0, 0]], faces=[[0, 1, 2]])]
    elif failure == "blank":
        labels = ["  　"]
    else:
        labels.append("P02")

    with pytest.raises(ValueError, match="empty|finite|labels|triangle|area"):
        render_turntable(meshes, tmp_path, labels)
    assert list(tmp_path.iterdir()) == []


def test_turntable_failed_render_leaves_no_images_or_open_figure(tmp_path, monkeypatch):
    print_png = FigureCanvasAgg.print_png
    before = pyplot.get_fignums()
    writes = 0

    def fail_on_second_image(canvas, *args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        return print_png(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", fail_on_second_image)
    with pytest.raises(OSError, match="disk full"):
        render_turntable([trimesh.creation.box()], tmp_path, ["P01"])
    assert writes == 2
    assert list(tmp_path.iterdir()) == []
    assert pyplot.get_fignums() == before


def test_approved_plan_cannot_bypass_draft_project(tmp_path, approved_plan):
    projects.create_project(tmp_path, "draft", 30)
    project_dir = tmp_path / "draft"

    with pytest.raises(ValueError, match="split_approved"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == set()


@pytest.mark.parametrize("problem", ["wrong_name", "no_scale", "symlink", "missing"])
def test_delivery_requires_local_verified_manifest(project_dir, tmp_path, approved_plan, problem):
    manifest_path = project_dir / "project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if problem == "symlink":
        external = tmp_path / "external.json"
        manifest_path.rename(external)
        manifest_path.symlink_to(external)
    elif problem == "missing":
        manifest_path.unlink()
    else:
        field, value = ("name", "elsewhere") if problem == "wrong_name" else ("scale_reference_mm", None)
        manifest[field] = value
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises((ValueError, FileNotFoundError), match="manifest|scale"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == set()


def test_delivery_rejects_different_partial_json_before_publication(project_dir, approved_plan):
    conflicting = project_dir / "print-plan.json"
    conflicting.write_bytes(b'{"unrelated":true}\n')

    with pytest.raises(FileExistsError, match="print-plan.json"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert conflicting.read_bytes() == b'{"unrelated":true}\n'
    assert _output_names(project_dir) == {"print-plan.json"}


def test_delivery_can_resume_with_only_one_matching_json(project_dir, approved_plan, monkeypatch):
    publish_output = delivery.publish_output

    def interrupt_before_second_file(staged, destination, **kwargs):
        if destination.name == "print-plan.json":
            raise OSError("interrupted")
        publish_output(staged, destination, **kwargs)

    monkeypatch.setattr(delivery, "publish_output", interrupt_before_second_file)
    with pytest.raises(OSError, match="interrupted"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert _output_names(project_dir) == {"inspection.json"}
    original = (project_dir / "inspection.json").stat().st_ino
    monkeypatch.setattr(delivery, "publish_output", publish_output)
    paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)
    assert paths.inspection_json.stat().st_ino == original
    assert paths.print_plan_json.is_file() and paths.assembly_md.is_file()


def test_delivery_waits_for_shared_project_lock(project_dir, approved_plan):
    entered = Event()
    published = Event()

    def deliver():
        entered.set()
        result = write_delivery(project_dir, approved_plan.inspection, approved_plan)
        published.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as workers:
        with projects.project_lock(project_dir):
            result = workers.submit(deliver)
            assert entered.wait(3)
            assert not published.wait(0.2)
            assert _output_names(project_dir) == set()
        assert result.result(timeout=5).assembly_md.is_file()


def test_turntable_keeps_backend_and_ignores_usetex(tmp_path, monkeypatch):
    def forbid_backend_change(*args, **kwargs):
        pytest.fail("Preview changed the process backend")

    monkeypatch.setattr(matplotlib, "use", forbid_backend_change)
    with matplotlib.rc_context({"text.usetex": True}):
        paths = render_turntable([trimesh.creation.box()], tmp_path, ["P01"])
        assert paths[0].is_file()
        assert matplotlib.rcParams["text.usetex"]


def test_turntable_uses_cjk_font_when_available(tmp_path, tmp_path_factory, monkeypatch):
    from matplotlib.ft2font import FT2Font

    monkeypatch.delenv("FABRICATOR_PREVIEW_FONT", raising=False)
    cache = tmp_path_factory.mktemp("font-cache")
    monkeypatch.setattr(matplotlib, "get_cachedir", lambda: str(cache))
    label = "零件甲 ABC"
    chosen = []
    print_png = FigureCanvasAgg.print_png

    def inspect_font(canvas, *args, **kwargs):
        text = canvas.figure.axes[0].get_legend().get_texts()[0]
        font_path = text.get_fontproperties().get_file()
        assert font_path
        assert set(map(ord, label)) <= FT2Font(font_path).get_charmap().keys()
        chosen.append(font_path)
        return print_png(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", inspect_font)
    try:
        outputs = render_turntable([trimesh.creation.box()], tmp_path, [label])
    except ValueError as error:
        assert "FABRICATOR_PREVIEW_FONT" in str(error) and "Noto Sans CJK" in str(error)
        assert not list(tmp_path.iterdir())
    else:
        assert chosen and len(set(chosen)) == 1
        assert all(path.is_file() for path in outputs)


def test_turntable_rejects_configured_font_without_cjk_coverage(tmp_path, monkeypatch):
    from matplotlib.font_manager import findfont

    monkeypatch.setenv("FABRICATOR_PREVIEW_FONT", findfont("DejaVu Sans"))
    with pytest.raises(ValueError, match="FABRICATOR_PREVIEW_FONT.*Noto Sans CJK"):
        render_turntable([trimesh.creation.box()], tmp_path, ["零件甲"])
    assert not list(tmp_path.iterdir())


def test_turntable_uses_bundled_font_for_accented_label(tmp_path, tmp_path_factory, monkeypatch):
    from matplotlib.font_manager import fontManager

    monkeypatch.delenv("FABRICATOR_PREVIEW_FONT", raising=False)
    monkeypatch.setattr(fontManager, "ttflist", [])
    cache = tmp_path_factory.mktemp("font-cache")
    monkeypatch.setattr(matplotlib, "get_cachedir", lambda: str(cache))
    outputs = render_turntable([trimesh.creation.box()], tmp_path, ["Café"])
    assert outputs[0].is_file()


@pytest.mark.skipif(os.name == "nt", reason="Windows directory pin prevents the injected rename")
def test_delivery_never_publishes_to_replacement_project(project_dir, approved_plan, monkeypatch):
    publish_output = delivery.publish_output
    moved = project_dir.with_name("displaced")
    invoked = False

    def replace_project(staged, destination, **kwargs):
        nonlocal invoked
        if not invoked:
            invoked = True
            project_dir.rename(moved)
            project_dir.mkdir()
        publish_output(staged, destination, **kwargs)

    monkeypatch.setattr(delivery, "publish_output", replace_project)
    with pytest.raises(ValueError, match="directory.*changed"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)

    assert invoked and not list(project_dir.iterdir())
    assert "assembly.md" not in _output_names(moved)
    assert not any(name.startswith(".delivery-") for name in _output_names(moved))


def _try_json_change(change, sharing_denied):
    try:
        if sharing_denied:
            raise PermissionError(errno.EACCES, "simulated Windows sharing violation")
        change()
    except PermissionError:
        if os.name != "nt" and not sharing_denied:
            raise
        return False
    return True


@pytest.mark.parametrize("sharing_denied", [False, True], ids=["native", "sharing-denied"])
@pytest.mark.parametrize("attack", ["remove", "replace", "same_bytes"])
def test_resumed_delivery_rechecks_previously_published_json(
    project_dir, approved_plan, monkeypatch, attack, sharing_denied
):
    publish_output = delivery.publish_output
    target = project_dir / "inspection.json"
    changed = None

    def stop_after_inspection(staged, destination, **kwargs):
        if destination.name == "print-plan.json":
            raise OSError("interrupted")
        publish_output(staged, destination, **kwargs)

    monkeypatch.setattr(delivery, "publish_output", stop_after_inspection)
    with pytest.raises(OSError, match="interrupted"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)
    old_content = target.read_bytes()
    substituted = old_content if attack == "same_bytes" else b"external writer"
    replacement = project_dir / "replacement"
    if attack != "remove":
        replacement.write_bytes(substituted)

    def change_inspection(staged, destination, **kwargs):
        nonlocal changed
        publish_output(staged, destination, **kwargs)
        if destination.name == "print-plan.json":
            change = target.unlink if attack == "remove" else lambda: replacement.replace(target)
            changed = _try_json_change(change, sharing_denied)

    monkeypatch.setattr(delivery, "publish_output", change_inspection)
    try:
        paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)
    except ValueError as error:
        assert changed is True and "inspection.json" in str(error)
        assert not (project_dir / "assembly.md").exists()
        if attack != "remove":
            assert target.read_bytes() == substituted
    else:
        assert changed is False
        assert target.read_bytes() == old_content
        assert delivery.verify_delivery(project_dir) == paths


@pytest.mark.parametrize("sharing_denied", [False, True], ids=["native", "sharing-denied"])
def test_completed_delivery_detects_json_replaced_during_marker_publication(
    project_dir, approved_plan, monkeypatch, sharing_denied
):
    publish_output = delivery.publish_output
    target = project_dir / "print-plan.json"
    replacement = project_dir / "replacement"
    external = b"modified by external writer"
    replacement.write_bytes(external)
    changed = None

    def alter_before_marker(staged, destination, **kwargs):
        nonlocal changed
        if destination.name == "assembly.md":
            changed = _try_json_change(lambda: replacement.replace(target), sharing_denied)
        publish_output(staged, destination, **kwargs)

    monkeypatch.setattr(delivery, "publish_output", alter_before_marker)
    try:
        paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)
    except ValueError as error:
        assert changed is True and "print-plan.json" in str(error)
        assert target.read_bytes() == external
        with pytest.raises(ValueError, match="print-plan.json"):
            delivery.verify_delivery(project_dir)
    else:
        assert changed is False
        assert replacement.read_bytes() == external
        assert delivery.verify_delivery(project_dir) == paths


def test_complete_delivery_requires_both_recorded_json_hashes(project_dir, approved_plan):
    paths = write_delivery(project_dir, approved_plan.inspection, approved_plan)
    markdown = paths.assembly_md.read_text(encoding="utf-8")
    for path in (paths.inspection_json, paths.print_plan_json):
        assert hashlib.sha256(path.read_bytes()).hexdigest() in markdown
    assert delivery.verify_delivery(project_dir) == paths

    paths.inspection_json.write_bytes(b"changed after completion")
    with pytest.raises(ValueError, match="inspection.json"):
        delivery.verify_delivery(project_dir)
    with pytest.raises(ValueError, match="inspection.json"):
        write_delivery(project_dir, approved_plan.inspection, approved_plan)


def test_turntable_does_not_change_host_tex_setting_while_drawing(tmp_path, monkeypatch):
    from matplotlib.text import Text

    drawing = Event()
    inspect_done = Event()
    print_png = FigureCanvasAgg.print_png

    def inspect(canvas, *args, **kwargs):
        drawing.set()
        assert inspect_done.wait(4)
        assert all(not text.get_usetex() for text in canvas.figure.findobj(Text))
        return print_png(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", inspect)
    with matplotlib.rc_context({"text.usetex": True}):
        with ThreadPoolExecutor(max_workers=1) as workers:
            result = workers.submit(render_turntable, [trimesh.creation.box()], tmp_path, ["P01"])
            try:
                assert drawing.wait(4)
                assert matplotlib.rcParams["text.usetex"]
            finally:
                inspect_done.set()
            assert result.result(timeout=5)


@pytest.fixture
def collection_font(tmp_path):
    from fontTools.ttLib import TTCollection, TTFont
    from matplotlib.font_manager import findfont

    first = TTFont(findfont("DejaVu Sans"))
    second = TTFont(findfont("DejaVu Sans"))
    for subtable in second["cmap"].tables:
        if subtable.isUnicode():
            subtable.cmap[0x3404] = "A"
    collection = TTCollection()
    collection.fonts = [first, second]
    path = tmp_path / "test.ttc"
    try:
        collection.save(path)
    finally:
        collection.close()
    return path


def test_turntable_reuses_cached_ttc_face_across_renders(tmp_path, collection_font, monkeypatch):
    from matplotlib.ft2font import FT2Font

    monkeypatch.setenv("FABRICATOR_PREVIEW_FONT", f"{collection_font}#1")
    monkeypatch.setattr(matplotlib, "get_cachedir", lambda: str(tmp_path))
    chosen_paths = set()
    print_png = FigureCanvasAgg.print_png

    def inspect(canvas, *args, **kwargs):
        text = canvas.figure.axes[0].get_legend().get_texts()[0]
        chosen = Path(text.get_fontproperties().get_file())
        assert chosen != collection_font
        assert 0x3404 in FT2Font(chosen).get_charmap()
        chosen_paths.add(chosen)
        return print_png(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", inspect)
    first = render_turntable([trimesh.creation.box()], tmp_path / "previews", ["㐄"])
    second = render_turntable([trimesh.creation.box()], tmp_path / "previews", ["㐄"])
    assert first != second
    assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]
    assert len(chosen_paths) == 1
    (cached,) = chosen_paths
    assert cached.is_file()
    assert cached.stem == hashlib.sha256(cached.read_bytes()).hexdigest()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_turntable_rejects_nonregular_configured_font_without_opening(tmp_path, monkeypatch):
    path = tmp_path / "font.ttf"
    os.mkfifo(path)
    monkeypatch.setenv("FABRICATOR_PREVIEW_FONT", str(path))
    source_open = os.open

    def reject_fifo(name, flags, *args, **kwargs):
        if Path(name) == path:
            pytest.fail("The renderer tried to open the configured FIFO")
        return source_open(name, flags, *args, **kwargs)

    from personal_3d_fabricator import preview

    real_font = preview.FT2Font

    def reject_direct_font(name, *args, **kwargs):
        if isinstance(name, (str, Path)) and Path(name) == path:
            pytest.fail("FreeType tried to open the configured FIFO")
        return real_font(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", reject_fifo)
    monkeypatch.setattr(preview, "FT2Font", reject_direct_font)
    with pytest.raises(ValueError, match="FABRICATOR_PREVIEW_FONT.*regular"):
        render_turntable([trimesh.creation.box()], tmp_path / "previews", ["㐄"])
    assert not (tmp_path / "previews").exists()
