"""Render fixed orthographic mesh previews without a desktop session."""

import os
import re
from colorsys import hsv_to_rgb
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from struct import error as StructError
from uuid import uuid4

import matplotlib
import numpy as np
import trimesh
from fontTools.ttLib import TTCollection, TTLibError
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import fontManager
from matplotlib.ft2font import FT2Font
from matplotlib.patches import Patch
from matplotlib.text import Text
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from .adapters._output import stage_directory, stage_output
from .projects import open_project_file

_CAMERAS = ((24, 45), (24, 135), (24, 225), (24, 315))
# Step around the hue wheel by the golden ratio conjugate to separate adjacent parts.
_HUE_STEP = 0.618033988749895

_CJK_FONT_FAMILIES = (
    "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Hiragino Sans GB",
    "Heiti SC", "Microsoft YaHei", "Microsoft JhengHei", "SimHei", "WenQuanYi Zen Hei",
    "Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK KR",
)
_PREVIEW_FONT = "FABRICATOR_PREVIEW_FONT"
_FONT_CACHE_DIRECTORY = "personal-3d-fabricator-fonts"
_BUNDLED_FONT = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"


def _configured_font(value: str) -> tuple[Path, int | None]:
    filename, separator, face = value.rpartition("#")
    if not separator:
        return Path(value).expanduser(), None
    if not filename or not re.fullmatch(r"[0-9]{1,9}", face):
        raise ValueError(f"{_PREVIEW_FONT} requires a font path followed by an optional #index")
    return Path(filename).expanduser(), int(face)


def _font_candidates() -> list[tuple[Path, int | None]]:
    order = {family.casefold(): index for index, family in enumerate(_CJK_FONT_FAMILIES)}
    available = sorted(
        (order[entry.name.casefold()], entry.fname)
        for entry in fontManager.ttflist
        if entry.name.casefold() in order
    )
    fonts = dict.fromkeys(Path(filename).resolve() for _, filename in available)
    return [(_BUNDLED_FONT, None), *((font, None) for font in fonts)]


def _font_matches(cmap: dict, pending: dict[int, set[int]]) -> list[int]:
    return [index for index, codes in pending.items() if all(cmap.get(code) for code in codes)]


def _extract_matching_fonts(
    source,
    face_index: int | None,
    pending: dict[int, set[int]],
):
    signature = source.read(4)
    source.seek(0)
    if signature != b"ttcf":
        if face_index not in (None, 0):
            return
        matched = _font_matches(FT2Font(source).get_charmap(), pending)
        if matched:
            source.seek(0)
            yield source.read(), matched
        return

    with TTCollection(source, lazy=True, recalcTimestamp=False) as collection:
        if face_index is not None:
            indices = (face_index,) if face_index < len(collection.fonts) else ()
        else:
            families = {name.casefold(): index for index, name in enumerate(_CJK_FONT_FAMILIES)}
            indices = sorted(
                range(len(collection.fonts)),
                key=lambda index: families.get(
                    (collection.fonts[index]["name"].getBestFamilyName() or "").casefold(),
                    len(families),
                ),
            )
        for index in indices:
            face = collection.fonts[index]
            if not _font_matches(face.getBestCmap() or {}, pending):
                continue
            buffer = BytesIO()
            face.save(buffer)
            contents = buffer.getvalue()
            # Verify the same bytes that the Agg renderer will open.
            matched = _font_matches(FT2Font(BytesIO(contents)).get_charmap(), pending)
            if matched:
                yield contents, matched


def _cached_font(contents: bytes) -> Path:
    cache = Path(matplotlib.get_cachedir()) / _FONT_CACHE_DIRECTORY
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or not cache.is_dir():
        raise ValueError(f"Preview font cache must be a local directory: {cache}")
    path = cache / f"{sha256(contents).hexdigest()}.ttf"
    try:
        with open_project_file(path) as cached:
            if cached.read() != contents:
                raise ValueError(f"Preview font cache entry has unexpected contents: {path}")
        return path
    except FileNotFoundError:
        pass

    try:
        with stage_output(path) as staged:
            staged.write_bytes(contents)
    except FileExistsError:
        # A concurrent renderer may have published the same hash.
        with open_project_file(path) as cached:
            if cached.read() != contents:
                raise ValueError(f"Preview font cache entry has unexpected contents: {path}")
    return path


def _label_fonts(labels: list[str]) -> list[Path | None]:
    if any(not label.strip() for label in labels):
        raise ValueError("Preview labels must not be blank")
    pending = {index: set(map(ord, label)) for index, label in enumerate(labels) if not label.isascii()}
    selected: list[Path | None] = [None] * len(labels)
    if not pending:
        return selected

    configured = os.environ.get(_PREVIEW_FONT)
    candidates = [_configured_font(configured)] if configured else _font_candidates()
    for candidate, face_index in candidates:
        try:
            with open_project_file(candidate) as source:
                for contents, matched in _extract_matching_fonts(source, face_index, pending):
                    path = _cached_font(contents)
                    for label_index in matched:
                        selected[label_index] = path
                        del pending[label_index]
                    if not pending:
                        return selected
        except (OSError, ValueError, RuntimeError, TTLibError, StructError) as error:
            if configured:
                raise ValueError(
                    f"{_PREVIEW_FONT} must name a readable regular font file "
                    "with an optional #index. Install Noto Sans CJK for CJK coverage."
                ) from error
    raise ValueError(
        f"Preview font cannot render label {labels[next(iter(pending))]!r}. "
        f"Set {_PREVIEW_FONT} to a font file covering the label or install Noto Sans CJK."
    )


def _require_triangle(mesh: trimesh.Trimesh) -> None:
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise ValueError("Cannot render an empty mesh")
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("Mesh coordinates must be finite")
    if mesh.faces.min() < 0 or mesh.faces.max() >= len(mesh.vertices):
        raise ValueError("Mesh triangle references a missing vertex")
    scale = float(np.abs(mesh.vertices).max()) or 1.0
    triangles = (mesh.vertices / scale)[mesh.faces]
    left = triangles[:, 1] - triangles[:, 0]
    right = triangles[:, 2] - triangles[:, 0]
    if not np.any(np.cross(left, right)):
        raise ValueError("Mesh requires a triangle with positive area")


def render_turntable(meshes: list[trimesh.Trimesh], output_dir: Path, labels: list[str]) -> list[Path]:
    """Render fixed cameras into an exclusive, versioned preview directory.

    FABRICATOR_PREVIEW_FONT accepts a regular font path, optionally followed by
    #index for a zero-based face in a TrueType or OpenType collection.
    The personal-3d-fabricator-fonts directory under the Matplotlib cache can
    be removed after all processes using these cached fonts have exited.
    """
    if not meshes:
        raise ValueError("Cannot render an empty mesh list")
    if len(meshes) != len(labels):
        raise ValueError("Provide one label per mesh; labels must match the number of meshes")
    for mesh in meshes:
        _require_triangle(mesh)
    fonts = _label_fonts(labels)
    # Normalize the whole scene by the same factor to preserve relative dimensions.
    scale = max(float(np.abs(mesh.vertices).max()) for mesh in meshes) or 1.0
    vertices = [mesh.vertices / scale for mesh in meshes]
    minimum = np.min([points.min(axis=0) for points in vertices], axis=0)
    maximum = np.max([points.max(axis=0) for points in vertices], axis=0)
    center = (maximum + minimum) / 2
    radius = float(np.max(maximum - minimum)) * 0.56 or 1.0

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    version = output_dir / f"turntable-{timestamp}-{uuid4().hex}"
    filenames = [f"view-{index:02d}.png" for index in range(1, len(_CAMERAS) + 1)]

    figure = Figure(figsize=(7, 6), dpi=120, facecolor="white")
    canvas = FigureCanvasAgg(figure)
    try:
        axes = figure.add_axes((0.02, 0.02, 0.96, 0.96), projection="3d", proj_type="ortho")
        axes.set_axis_off()
        axes.set_box_aspect((1, 1, 1))
        axes.set_xlim3d(-radius, radius)
        axes.set_ylim3d(-radius, radius)
        axes.set_zlim3d(-radius, radius)

        legend = []
        for index, (mesh, points) in enumerate(zip(meshes, vertices)):
            color = hsv_to_rgb((0.58 + index * _HUE_STEP) % 1.0, 0.68, 0.76)
            triangles = (points - center)[mesh.faces]
            collection = Poly3DCollection(
                triangles, facecolors=color, edgecolors="#293846", linewidths=0.35
            )
            axes.add_collection3d(collection)
            legend.append(Patch(facecolor=color))
        key = axes.legend(
            legend, labels, loc="upper right", framealpha=1.0, facecolor="white"
        )
        label_fonts = dict(zip(key.get_texts(), fonts))
        for text in figure.findobj(Text):
            text.set_usetex(False)
            text.set_parse_math(False)
            properties = text.get_fontproperties().copy()
            properties.set_file(label_fonts.get(text) or _BUNDLED_FONT)
            text.set_fontproperties(properties)

        with stage_directory(version) as staging:
            for filename, (elevation, azimuth) in zip(filenames, _CAMERAS):
                axes.view_init(elev=elevation, azim=azimuth)
                canvas.print_png(staging / filename)
    finally:
        figure.clear()
    return [version / name for name in filenames]
