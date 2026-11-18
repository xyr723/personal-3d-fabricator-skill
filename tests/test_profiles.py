import json
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl, ValidationError

from personal_3d_fabricator.models import (
    ApprovalState,
    InspectionReport,
    PrinterProfile,
    PrintPlan,
    ProjectManifest,
)
from personal_3d_fabricator.profiles import load_printer_profile

ROOT = Path(__file__).resolve().parents[1]
SINGLE_NOZZLE = ROOT / "profiles" / "generic-single-nozzle.json"
X2D = ROOT / "profiles" / "bambu-x2d.json"


def test_profile_override_changes_volume_without_mutating_source():
    base = load_printer_profile(SINGLE_NOZZLE)
    custom = load_printer_profile(SINGLE_NOZZLE, {"build_volume_mm": [300, 300, 400]})

    assert base.build_volume_mm == (220.0, 220.0, 250.0)
    assert custom.build_volume_mm == (300.0, 300.0, 400.0)
    assert custom.supported_materials == base.supported_materials
    assert load_printer_profile(SINGLE_NOZZLE) == base


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("build_volume_mm", [0, 220, 250]),
        ("build_volume_mm", [220, 250]),
        ("build_volume_mm", [220, 250, float("inf")]),
        ("toolhead_count", 0),
        ("nozzle_diameters_mm", []),
        ("nozzle_diameters_mm", [0.4, -0.2]),
    ],
)
def test_profile_rejects_invalid_capabilities(field, value):
    with pytest.raises(ValueError, match=field):
        load_printer_profile(SINGLE_NOZZLE, {field: value})


def test_profile_rejects_mutation():
    profile = load_printer_profile(SINGLE_NOZZLE)

    with pytest.raises(ValidationError):
        profile.enclosed = True


@pytest.mark.parametrize(
    ("filename", "toolhead_count"),
    [("generic-single-nozzle.json", 1), ("generic-dual-nozzle.json", 2)],
)
def test_generic_profiles_are_templates(filename, toolhead_count):
    profile = load_printer_profile(ROOT / "profiles" / filename)

    assert profile.is_template is True
    assert profile.toolhead_count == toolhead_count
    assert profile.source_url is None
    assert profile.verified_on is None


def test_x2d_profile_uses_the_verified_dual_nozzle_envelope():
    profile = load_printer_profile(X2D)

    assert profile.is_template is False
    assert profile.build_volume_mm == (235.5, 256.0, 256.0)
    assert profile.toolhead_count == 2
    assert profile.nozzle_diameters_mm == (0.2, 0.4, 0.6, 0.8)
    assert profile.enclosed is True
    assert profile.heated_chamber is True
    assert {"PLA", "PETG", "ABS"} <= set(profile.supported_materials)
    assert profile.slicer == "bambu-studio"
    assert profile.source_url is not None
    assert profile.source_url.host == "store.bblcdn.com"
    assert profile.verified_on == date(2026, 11, 18)


@pytest.mark.parametrize(
    "value",
    [0, 0.0, "0", "2026-11-18T00:00:00", datetime(2026, 11, 18), "2026-02-29"],
)
def test_profile_rejects_dates_outside_its_schema(value):
    with pytest.raises(ValidationError, match="verified_on"):
        load_printer_profile(X2D, {"verified_on": value})


@pytest.mark.parametrize("value", [0, "2026-11-18T00:00:00"])
def test_profile_rejects_non_date_verification_in_json(value):
    payload = json.loads(X2D.read_text(encoding="utf-8"))
    payload["verified_on"] = value

    with pytest.raises(ValidationError, match="verified_on"):
        PrinterProfile.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("value", ["2024-02-29", date(2024, 2, 29)])
def test_profile_accepts_calendar_date_inputs(value):
    profile = load_printer_profile(X2D, {"verified_on": value})

    assert profile.verified_on == date(2024, 2, 29)
    assert profile.model_dump(mode="json")["verified_on"] == "2024-02-29"


@pytest.mark.parametrize(
    "value",
    [
        "https://reader@example.com/x2d.pdf",
        HttpUrl("https://reader:dummy@example.com/x2d.pdf"),
        "https://:dummy@example.com/x2d.pdf",
        "https://@example.com/x2d.pdf",
        "ftp://example.com/x2d.pdf",
    ],
)
def test_profile_rejects_urls_with_userinfo_or_non_http_schemes(value):
    with pytest.raises(ValidationError, match="source_url"):
        load_printer_profile(X2D, {"source_url": value})


def test_profile_accepts_at_sign_outside_the_url_authority():
    profile = load_printer_profile(X2D, {"source_url": "https://example.com/@docs/x2d.pdf"})

    assert str(profile.source_url) == "https://example.com/@docs/x2d.pdf"


@pytest.mark.parametrize(
    "override",
    [
        {"build_volume_mm": [256, 256, 260]},
        {"slicer": "generic", "source_url": "https://example.com/local-spec.pdf"},
        {"enclosed": False, "verified_on": "2026-11-19"},
    ],
)
def test_changed_capability_clears_source_metadata_without_an_explicit_pair(override):
    custom = load_printer_profile(X2D, override)

    assert custom.source_url is None
    assert custom.verified_on is None
    assert load_printer_profile(X2D).verified_on == date(2026, 11, 18)


def test_equivalent_capability_override_preserves_source_metadata():
    base = load_printer_profile(X2D)
    custom = load_printer_profile(X2D, {"build_volume_mm": (235.5, 256.0, 256.0)})

    assert custom == base


def test_template_classification_override_preserves_source_metadata():
    base = load_printer_profile(X2D)
    custom = load_printer_profile(X2D, {"is_template": True})

    assert custom.is_template is True
    assert custom.source_url == base.source_url
    assert custom.verified_on == base.verified_on


def test_changed_capability_uses_an_explicit_new_source_metadata_pair():
    custom = load_printer_profile(
        X2D,
        {
            "build_volume_mm": [256, 256, 260],
            "source_url": "https://example.com/local-spec.pdf",
            "verified_on": "2026-11-19",
        },
    )

    assert custom.build_volume_mm == (256.0, 256.0, 260.0)
    assert str(custom.source_url) == "https://example.com/local-spec.pdf"
    assert custom.verified_on == date(2026, 11, 19)


def test_project_manifest_preserves_missing_scale_and_approval_on_round_trip():
    manifest = ProjectManifest(name="photo-prop")
    restored = ProjectManifest.model_validate_json(manifest.model_dump_json())

    assert restored.scale_reference_mm is None
    assert restored.approval_state is ApprovalState.draft
    with pytest.raises(ValidationError, match="approval_state"):
        ProjectManifest(name="photo-prop", approval_state="approved")


def test_delivery_models_preserve_geometry_and_issues_on_round_trip():
    inspection = InspectionReport(
        format="stl",
        bounds_mm=[20, 30, 40],
        watertight=False,
        winding_consistent=True,
        component_count=1,
        face_count=11,
        degenerate_face_count=0,
        issues=[{"code": "open_mesh", "severity": "error", "message": "Open surface"}],
    )
    plan = PrintPlan(
        inspection=inspection,
        approval_state=ApprovalState.split_approved,
        parts=[{"part_id": "P01", "orientation_deg": [0, 90, 0]}],
        plates=[["P01"]],
        hardware=["磁铁"],
        assembly_order=["P01"],
    )
    restored = PrintPlan.model_validate_json(plan.model_dump_json())

    assert restored.inspection.bounds_mm == (20.0, 30.0, 40.0)
    assert restored.inspection.issues[0].code == "open_mesh"
    assert restored.parts[0].orientation_deg == (0.0, 90.0, 0.0)
    assert restored.plates == (("P01",),)
    assert restored.hardware == ("磁铁",)
    assert restored.approval_state is ApprovalState.split_approved


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("printer-profile.schema.json", PrinterProfile),
        ("project.schema.json", ProjectManifest),
        ("inspection.schema.json", InspectionReport),
        ("print-plan.schema.json", PrintPlan),
    ],
)
def test_committed_schemas_match_model_exports(filename, model):
    expected = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)
    assert (ROOT / "schemas" / filename).read_text(encoding="utf-8") == expected + "\n"
