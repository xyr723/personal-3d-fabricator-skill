"""Import and export STEP solids through the optional CadQuery backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ._output import stage_output
from .cadquery_adapter import CadQueryUnavailable, load_cadquery

if TYPE_CHECKING:
    from cadquery import Shape, Workplane


def _step_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError(f"Expected a .step or .stp path: {path}")
    return path


def import_step(path: Path) -> Workplane | CadQueryUnavailable:
    """Read a STEP model as CadQuery geometry."""
    path = _step_path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    cadquery = load_cadquery()
    if isinstance(cadquery, CadQueryUnavailable):
        return cadquery
    return cadquery.importers.importStep(str(path))


def export_step(model: Workplane | Shape, path: Path) -> Path | CadQueryUnavailable:
    """Export an editable STEP copy to a fresh output path."""
    path = _step_path(path)
    if os.path.lexists(path):
        raise FileExistsError(f"STEP output already exists: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    cadquery = load_cadquery()
    if isinstance(cadquery, CadQueryUnavailable):
        return cadquery
    with stage_output(path) as staged:
        cadquery.exporters.export(model, str(staged), exportType="STEP")
    return path
