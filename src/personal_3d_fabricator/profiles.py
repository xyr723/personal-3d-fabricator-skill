"""Read and validate printer capability configurations."""

import json
from pathlib import Path

from pydantic import ValidationError

from .models import PrinterProfile

_SOURCE_FIELDS = {"source_url", "verified_on"}
_PROFILE_METADATA_FIELDS = _SOURCE_FIELDS | {"is_template"}


def load_printer_profile(path: Path, override: dict | None = None) -> PrinterProfile:
    """Validate a JSON profile with optional per-field replacements."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Printer profile must be a JSON object: {path}")

    profile = PrinterProfile.model_validate(payload | (override or {}))
    if (
        not override
        or _SOURCE_FIELDS <= override.keys()
        or override.keys() <= _PROFILE_METADATA_FIELDS
    ):
        return profile

    base_payload = {
        field: value for field, value in payload.items() if field not in _PROFILE_METADATA_FIELDS
    }
    try:
        base = PrinterProfile.model_validate(base_payload)
    except ValidationError:
        # The override repaired an invalid capability, so its provenance cannot carry over.
        base = None

    if base is not None and base.model_dump(exclude=_PROFILE_METADATA_FIELDS) == profile.model_dump(
        exclude=_PROFILE_METADATA_FIELDS
    ):
        return profile
    return profile.model_copy(update=dict.fromkeys(_SOURCE_FIELDS))


def supported_material(material: str | None, profile: PrinterProfile) -> str | None:
    """Match the project selection using the spelling from the device profile."""
    if material is None:
        return None
    selected = material.strip().casefold()
    for supported in profile.supported_materials:
        if supported.casefold() == selected:
            return supported
    choices = ", ".join(profile.supported_materials) or "(none)"
    raise ValueError(f"Unsupported material {material!r}. Supported materials: {choices}")
