"""Validated records shared by the project and print preparation workflows."""

import hashlib
import json
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic.dataclasses import dataclass as validated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    HttpUrl,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
    model_serializer,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveMillimeters = Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)]
NonNegativeMillimeters = Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]
NonNegativeCount = Annotated[int, Field(ge=0, strict=True)]
BuildVolume = tuple[PositiveMillimeters, PositiveMillimeters, PositiveMillimeters]
Bounds = tuple[NonNegativeMillimeters, NonNegativeMillimeters, NonNegativeMillimeters]


class PrinterProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    build_volume_mm: BuildVolume = Field(description="Usable X, Y, Z envelope in millimeters.")
    toolhead_count: Annotated[int, Field(ge=1, strict=True)]
    nozzle_diameters_mm: Annotated[
        tuple[PositiveMillimeters, ...],
        Field(min_length=1, description="Supported nozzle diameter options in millimeters."),
    ]
    enclosed: StrictBool
    heated_chamber: StrictBool
    supported_materials: tuple[NonEmptyText, ...]
    slicer: NonEmptyText
    is_template: bool = False
    source_url: HttpUrl | None = None
    verified_on: date | None = None

    @field_validator("source_url", mode="before")
    @classmethod
    def validate_source_authority(cls, value: object) -> object:
        if isinstance(value, str):
            authority = urlsplit(value).netloc
            if not authority or "\\" in authority:
                raise ValueError("source URL must have a valid authority")
            if "@" in authority:
                raise ValueError("source URL must not contain user information")
        return value

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username is not None or value.password is not None):
            raise ValueError("source URL must not contain user information")
        return value

    @field_validator("verified_on", mode="before")
    @classmethod
    def validate_verification_date(cls, value: object) -> object:
        if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return value
        raise ValueError("verification date must use YYYY-MM-DD")


class ApprovalState(StrEnum):
    draft = "draft"
    overall_approved = "overall_approved"
    split_approved = "split_approved"


class InspectionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: NonEmptyText
    severity: Literal["info", "warning", "error"]
    message: NonEmptyText


class InspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: NonEmptyText
    bounds_mm: Bounds | None = Field(description="X, Y, Z extents when the scale is known.")
    watertight: bool
    winding_consistent: bool
    component_count: NonNegativeCount
    face_count: NonNegativeCount
    degenerate_face_count: NonNegativeCount
    issues: tuple[InspectionIssue, ...] = ()


SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ApprovedMesh(BaseModel):
    """Identity and dimensions of the archived input reviewed for overall approval."""

    model_config = ConfigDict(extra="forbid")

    archive_version: NonEmptyText
    filename: NonEmptyText
    sha256: SHA256
    assumed_unit: NonEmptyText | None = None
    inspection: InspectionReport


class OverallPreview(BaseModel):
    """Draft source and hashes of the views prepared for overall approval."""

    model_config = ConfigDict(extra="forbid")

    mesh: ApprovedMesh
    scale_reference_mm: PositiveMillimeters | None = Field(
        default=None, description="Project scale reference in millimeters at review time."
    )
    views_sha256: tuple[SHA256, ...] = Field(min_length=1)


class ArchivedArtifact(BaseModel):
    """A version committed after verifying the published file and checksum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_version: NonEmptyText
    filename: NonEmptyText
    role: NonEmptyText
    sha256: SHA256
    size_bytes: NonNegativeCount
    committed_at: AwareDatetime


def _archive_index_digest(records: tuple[ArchivedArtifact, ...]) -> str:
    rows = [record.model_dump(mode="json") for record in records]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@validated(frozen=True)
class PinSocketSpec:
    pin_diameter_mm: PositiveMillimeters
    pin_length_mm: PositiveMillimeters
    clearance_mm: NonNegativeMillimeters
    count: Annotated[int, Field(strict=True, gt=0)]


class SplitConnector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["flat", "pin"]
    pin: PinSocketSpec | None = None

    @model_validator(mode="after")
    def check_pin_parameters(self):
        if (self.mode == "pin") != (self.pin is not None):
            raise ValueError("Pin parameters must match the selected connector")
        if self.pin is not None and self.pin.count != 1:
            raise ValueError("Each mating part pair supports one pin")
        return self


class ProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyText
    units: Literal["mm"] = "mm"
    scale_reference_mm: PositiveMillimeters | None = None
    requirements: tuple[NonEmptyText, ...] = ()
    material: NonEmptyText | None = None
    printer_profile: PrinterProfile | None = None
    approval_state: ApprovalState = ApprovalState.draft
    overall_preview: OverallPreview | None = None
    approved_mesh: ApprovedMesh | None = None
    split_preview_sha256: SHA256 | None = None
    split_connector: SplitConnector | None = None
    approved_split_sha256: SHA256 | None = None
    artifacts: tuple[ArchivedArtifact, ...] | None = None
    artifacts_sha256: SHA256 | None = None

    def with_artifacts(self, records: tuple[ArchivedArtifact, ...]) -> "ProjectManifest":
        return ProjectManifest.model_validate(self.model_dump() | {
            "artifacts": records, "artifacts_sha256": _archive_index_digest(records),
        })

    @model_validator(mode="after")
    def check_archive_index(self):
        expected = None if self.artifacts is None else _archive_index_digest(self.artifacts)
        if self.artifacts_sha256 != expected:
            raise ValueError("Committed archive index differs from its recorded SHA-256")
        return self

    @field_validator("artifacts")
    @classmethod
    def unique_versions(cls, records):
        if records is not None and len({row.archive_version.casefold() for row in records}) != len(records):
            raise ValueError("Duplicate committed archive version")
        return records


class PrintPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: NonEmptyText
    bounds_mm: Bounds | None = None
    orientation_deg: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None


class PrintPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inspection: InspectionReport
    material: NonEmptyText | None = None
    approval_state: ApprovalState = ApprovalState.draft
    parts: tuple[PrintPart, ...] = ()
    plates: tuple[tuple[NonEmptyText, ...], ...] = Field(
        default=(), description="Estimated plate groups, each containing part IDs."
    )
    connectors: tuple[NonEmptyText, ...] = ()
    hardware: tuple[NonEmptyText, ...] = ()
    assembly_order: tuple[NonEmptyText, ...] = ()
    risks: tuple[NonEmptyText, ...] = ()

    @model_serializer(mode="wrap", when_used="json")
    def serialize_print_plan(self, handler):
        data = handler(self)
        if self.material is None:
            # Keep existing preview ZIPs and completed document checksums reproducible.
            data.pop("material", None)
        return data
