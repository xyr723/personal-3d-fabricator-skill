"""Export the versioned JSON Schema contracts from the installed models."""

import json
from pathlib import Path

from .models import InspectionReport, PrinterProfile, PrintPlan, ProjectManifest

_MODELS = {
    "inspection": InspectionReport,
    "print-plan": PrintPlan,
    "printer-profile": PrinterProfile,
    "project": ProjectManifest,
}


def schema_documents() -> dict[str, str]:
    return {
        f"{name}.schema.json": json.dumps(
            model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        for name, model in _MODELS.items()
    }


def export_schemas(output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, contents in schema_documents().items():
        path = output / name
        # Existing matching schema exports are safe to reuse.
        try:
            with path.open("x", encoding="utf-8", newline="\n") as destination:
                destination.write(contents)
        except FileExistsError:
            if path.is_symlink() or path.read_text(encoding="utf-8") != contents:
                raise ValueError(f"Schema output already exists with different content: {path}")
        paths.append(path)
    return paths
