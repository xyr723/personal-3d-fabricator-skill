# Personal 3D Fabricator v0.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cross-platform Codex Skill that creates, inspects, modifies, splits, and documents FDM-printable household parts and personal props with two mandatory human approval gates.

**Architecture:** One installable Skill routes three workflows to a typed Python core. The core owns project state, printer capabilities, deterministic mesh inspection, split planning, connector geometry, previews, and delivery reports; CadQuery and Blender remain optional adapters for authoring and complex mesh work.

**Tech Stack:** Python 3.11+, Pydantic 2, trimesh, NumPy, Pillow, Matplotlib, pytest, optional CadQuery and Blender Python, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-18-personal-3d-fabricator-design.md`

## Global Constraints

- Support Windows, macOS, and Linux; do not depend on Bash-only commands or hard-coded personal paths.
- Preserve every source model and create versioned outputs instead of overwriting files.
- Never infer an absolute scale when no trusted dimension or scale reference exists.
- Route by printer capabilities; model-specific profiles must not introduce model-name conditionals into core logic.
- Require `overall_approved` before splitting and `split_approved` before final printable export.
- Do not control slicer UI, heat a printer, start a print, or retry a failed print.
- Keep CadQuery and Blender optional; missing optional tools disable only their workflows.
- Do not store printer credentials or commit personal models, images, machine addresses, or project records.

---

## Planned File Map

- `SKILL.md`: concise routing, confirmation gates, and reference links.
- `agents/openai.yaml`: discoverable Skill metadata.
- `pyproject.toml`: package metadata, core/optional dependencies, CLI, and pytest configuration.
- `src/personal_3d_fabricator/models.py`: Pydantic domain models and enums.
- `src/personal_3d_fabricator/profiles.py`: printer-profile loading and override merge.
- `src/personal_3d_fabricator/projects.py`: project creation, artifact versioning, and approval transitions.
- `src/personal_3d_fabricator/inspection.py`: deterministic mesh and solid inspection.
- `src/personal_3d_fabricator/planning.py`: printer fit, cut candidates, and plate grouping.
- `src/personal_3d_fabricator/splitting.py`: mesh cutting and post-cut validation.
- `src/personal_3d_fabricator/connectors.py`: pin/socket, magnet, and screw-interface geometry.
- `src/personal_3d_fabricator/tooling.py`: cross-platform executable discovery.
- `src/personal_3d_fabricator/adapters/`: optional CadQuery, Blender, STEP, and slicer adapters.
- `src/personal_3d_fabricator/preview.py`: headless preview generation.
- `src/personal_3d_fabricator/delivery.py`: print plan, inspection report, and assembly Markdown.
- `src/personal_3d_fabricator/cli.py`: stable command-line interface used by Skill scripts.
- `scripts/`: thin executable wrappers and Skill validation helper.
- `profiles/`: generic and sourced printer capability profiles.
- `schemas/`: generated JSON Schemas checked into the repository.
- `references/`: workflow and safety instructions loaded by `SKILL.md`.
- `tests/fixtures/`: generated, redistributable geometry fixtures only.
- `.github/workflows/test.yml`: three-OS test matrix.

### Task 1: Installable Skill and Python Package Skeleton

**Files:**
- Create: `SKILL.md`
- Create: `agents/openai.yaml`
- Create: `pyproject.toml`
- Create: `src/personal_3d_fabricator/__init__.py`
- Create: `tests/test_skill_layout.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: importable package `personal_3d_fabricator`; console command `personal-3d-fabricator` reserved for Task 9.

- [ ] **Step 1: Write the failing layout test**

```python
from pathlib import Path

ROOT = Path(__file__).parents[1]

def test_skill_entrypoint_and_metadata_exist():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
    assert "name: personal-3d-fabricator" in skill
    assert "overall_approved" in skill
    assert "split_approved" in skill
    assert "display_name:" in metadata
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m pytest tests/test_skill_layout.py -v`  
Expected: FAIL because `SKILL.md` and package metadata do not exist.

- [ ] **Step 3: Create the minimal package and Skill metadata**

Use `pyproject.toml` with `requires-python = ">=3.11"`, core dependencies `pydantic>=2,<3`, `trimesh>=4,<5`, `numpy>=2,<3`, and `Pillow>=11,<13`; define optional groups `cadquery` and `dev`. Add a short `SKILL.md` that routes create/modify/split requests, names both approval flags, and links the workflow references that Task 7 will add.

- [ ] **Step 4: Run the focused test**

Run: `python -m pytest tests/test_skill_layout.py -v`  
Expected: PASS.

- [ ] **Step 5: Validate package installation**

Run: `python -m pip install -e '.[dev]'` followed by `python -c "import personal_3d_fabricator"`  
Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add SKILL.md agents/openai.yaml pyproject.toml src tests/test_skill_layout.py .gitignore
git commit -m "feat: scaffold personal 3d fabricator skill"
```

### Task 2: Typed Project and Printer Capability Models

**Files:**
- Create: `src/personal_3d_fabricator/models.py`
- Create: `src/personal_3d_fabricator/profiles.py`
- Create: `profiles/generic-single-nozzle.json`
- Create: `profiles/generic-dual-nozzle.json`
- Create: `profiles/bambu-x2d.json`
- Create: `tests/test_profiles.py`

**Interfaces:**
- Produces: `PrinterProfile`, `ProjectManifest`, `InspectionReport`, `PrintPlan`, `ApprovalState`.
- Produces: `load_printer_profile(path: Path, override: dict | None = None) -> PrinterProfile`.

- [ ] **Step 1: Write failing profile tests**

```python
from pathlib import Path
import pytest
from personal_3d_fabricator.profiles import load_printer_profile

def test_profile_override_changes_volume_without_mutating_source():
    path = Path("profiles/generic-single-nozzle.json")
    base = load_printer_profile(path)
    custom = load_printer_profile(path, {"build_volume_mm": [300, 300, 400]})
    assert base.build_volume_mm == (220.0, 220.0, 250.0)
    assert custom.build_volume_mm == (300.0, 300.0, 400.0)

def test_profile_rejects_non_positive_volume():
    with pytest.raises(ValueError):
        load_printer_profile(Path("profiles/generic-single-nozzle.json"), {"build_volume_mm": [0, 220, 250]})
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_profiles.py -v`  
Expected: FAIL because profile models and files do not exist.

- [ ] **Step 3: Implement exact model contracts**

Define immutable `PrinterProfile` with positive three-axis `build_volume_mm`, `toolhead_count >= 1`, non-empty positive `nozzle_diameters_mm`, booleans for enclosure/chamber, material strings, slicer identifier, `source_url`, and `verified_on`. Define `ApprovalState` as `draft`, `overall_approved`, and `split_approved`. Use tuples internally for stable comparisons.

- [ ] **Step 4: Add generic and X2D profiles**

Keep generic profiles explicitly labeled as templates. Add X2D values only after checking official product documentation; include the official URL and verification date in the JSON. Do not copy unsupported capabilities from marketing text.

- [ ] **Step 5: Run tests and export schemas**

Run: `python -m pytest tests/test_profiles.py -v`  
Expected: PASS. Then generate `schemas/printer-profile.schema.json`, `schemas/project.schema.json`, `schemas/inspection.schema.json`, and `schemas/print-plan.schema.json` with `model_json_schema()` and stable sorted JSON.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/models.py src/personal_3d_fabricator/profiles.py profiles schemas tests/test_profiles.py
git commit -m "feat: add typed printer capability profiles"
```

### Task 3: Project Creation, Versioning, and Approval Gates

**Files:**
- Create: `src/personal_3d_fabricator/projects.py`
- Create: `tests/test_projects.py`

**Interfaces:**
- Consumes: `ProjectManifest`, `ApprovalState` from Task 2.
- Produces: `create_project(root: Path, name: str, scale_reference_mm: float | None) -> ProjectManifest`.
- Produces: `record_artifact(project_dir: Path, source: Path, role: str) -> Path`.
- Produces: `advance_approval(project_dir: Path, target: ApprovalState) -> ProjectManifest`.

- [ ] **Step 1: Write failing state-transition tests**

```python
from pathlib import Path
import pytest
from personal_3d_fabricator.models import ApprovalState
from personal_3d_fabricator.projects import create_project, advance_approval

def test_cannot_approve_split_before_overall(tmp_path: Path):
    create_project(tmp_path, "lamp-bracket", 100.0)
    with pytest.raises(ValueError, match="overall_approved"):
        advance_approval(tmp_path / "lamp-bracket", ApprovalState.split_approved)

def test_missing_scale_reference_remains_draft(tmp_path: Path):
    manifest = create_project(tmp_path, "photo-prop", None)
    assert manifest.approval_state is ApprovalState.draft
    assert manifest.scale_reference_mm is None
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_projects.py -v`  
Expected: FAIL because project APIs do not exist.

- [ ] **Step 3: Implement project directories and atomic manifests**

Create `source`, `editable`, `printable`, `previews`, and `versions`. Write JSON through a sibling temporary file and `Path.replace()` so interrupted writes do not corrupt `project.json`. Copy source artifacts with a timestamped version name and SHA-256 digest; reject attempts to use the project output as its own source.

- [ ] **Step 4: Implement approval transitions**

Allow `draft -> overall_approved -> split_approved` only. Reject skipping, reversing, or approving overall geometry when `scale_reference_mm` is absent.

- [ ] **Step 5: Run focused and accumulated tests**

Run: `python -m pytest tests/test_projects.py tests/test_profiles.py -v`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/projects.py tests/test_projects.py
git commit -m "feat: add versioned project approval workflow"
```

### Task 4: Deterministic Mesh Inspection

**Files:**
- Create: `src/personal_3d_fabricator/inspection.py`
- Create: `tests/fixtures/generate_fixtures.py`
- Create: `tests/test_inspection.py`

**Interfaces:**
- Consumes: `InspectionReport` from Task 2.
- Produces: `inspect_mesh(path: Path, assumed_unit: str | None = None) -> InspectionReport`.

- [ ] **Step 1: Generate redistributable fixtures in tests**

Create boxes and a deliberately open box at test runtime with `trimesh.creation.box`; do not commit third-party models.

- [ ] **Step 2: Write failing inspection tests**

```python
def test_closed_box_reports_dimensions_and_watertight(box_stl):
    report = inspect_mesh(box_stl, assumed_unit="mm")
    assert report.watertight is True
    assert report.bounds_mm == (20.0, 30.0, 40.0)
    assert report.component_count == 1

def test_unknown_units_block_scale_dependent_checks(box_stl):
    report = inspect_mesh(box_stl)
    assert "missing_scale" in {issue.code for issue in report.issues}
```

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/test_inspection.py -v`  
Expected: FAIL because `inspect_mesh` does not exist.

- [ ] **Step 4: Implement inspection**

Load STL, OBJ, and 3MF through trimesh without automatic repair. Report format, bounds, watertightness, winding consistency, body count, face count, degenerate faces, and scale-dependent issues. Treat thin-wall results as advisory unless a reliable local-thickness sample can be computed; never label an advisory result as exhaustive.

- [ ] **Step 5: Run inspection tests**

Run: `python -m pytest tests/test_inspection.py -v`  
Expected: PASS for closed/open, single/multiple body, known/unknown scale cases.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/inspection.py tests/fixtures tests/test_inspection.py
git commit -m "feat: inspect printable mesh geometry"
```

### Task 5: Printer Fit, Cut Candidates, and Plate Grouping

**Files:**
- Create: `src/personal_3d_fabricator/planning.py`
- Create: `tests/test_planning.py`

**Interfaces:**
- Consumes: `PrinterProfile`, `PrintPlan`, inspection bounds.
- Produces: `fits_build_volume(bounds_mm: tuple[float, float, float], profile: PrinterProfile, clearance_mm: float = 2.0) -> bool`.
- Produces: `propose_axis_cuts(bounds_mm: tuple[float, float, float], profile: PrinterProfile) -> list[CutCandidate]`.
- Produces: `group_parts_on_plates(parts: list[PartEnvelope], profile: PrinterProfile) -> list[PlatePlan]`.

- [ ] **Step 1: Write failing planning tests**

```python
def test_oversized_axis_gets_minimum_equal_segments(single_nozzle):
    cuts = propose_axis_cuts((500.0, 100.0, 100.0), single_nozzle)
    best = cuts[0]
    assert best.axis == "x"
    assert best.segment_count == 3

def test_exact_fit_reserves_clearance(single_nozzle):
    assert not fits_build_volume((220.0, 100.0, 100.0), single_nozzle)
    assert fits_build_volume((218.0, 100.0, 100.0), single_nozzle)
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_planning.py -v`  
Expected: FAIL because planning functions do not exist.

- [ ] **Step 3: Implement conservative planning**

Calculate the minimum segment count per axis with reserved clearance. Rank candidates by segment count, estimated visible seam penalty supplied by the caller, and connector accessibility. Implement deterministic first-fit-decreasing plate grouping using part envelopes and configurable spacing; label it an estimate rather than slicer-quality nesting.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_planning.py -v`  
Expected: PASS, including deterministic ordering and no envelope exceeding the build volume.

- [ ] **Step 5: Commit**

```bash
git add src/personal_3d_fabricator/planning.py tests/test_planning.py
git commit -m "feat: plan printer fit cuts and plates"
```

### Task 6: Mesh Splitting and Connector Geometry

**Files:**
- Create: `src/personal_3d_fabricator/splitting.py`
- Create: `src/personal_3d_fabricator/connectors.py`
- Create: `tests/test_splitting.py`
- Create: `tests/test_connectors.py`

**Interfaces:**
- Consumes: approved project state, `CutCandidate`, source mesh.
- Produces: `split_mesh(mesh: trimesh.Trimesh, cut: CutCandidate) -> SplitResult`.
- Produces: `PinSocketSpec(pin_diameter_mm, pin_length_mm, clearance_mm, count)`.
- Produces: `build_pin_socket_pair(spec: PinSocketSpec) -> ConnectorPair`.

- [ ] **Step 1: Write failing split invariants**

```python
def test_split_preserves_volume_within_tolerance(box_mesh, center_x_cut):
    result = split_mesh(box_mesh, center_x_cut)
    assert len(result.parts) == 2
    combined = sum(part.volume for part in result.parts)
    assert combined == pytest.approx(box_mesh.volume, rel=1e-4)
    assert all(part.is_watertight for part in result.parts)

def test_socket_clearance_is_radial():
    pair = build_pin_socket_pair(PinSocketSpec(6.0, 12.0, 0.25, 2))
    assert pair.socket_diameter_mm == pytest.approx(6.5)
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_splitting.py tests/test_connectors.py -v`  
Expected: FAIL because splitting and connector functions do not exist.

- [ ] **Step 3: Implement planar splitting first**

Use trimesh plane slicing with capping for watertight meshes. Reject non-watertight input, cuts that do not intersect the mesh, fragments below a configurable volume threshold, or results whose combined volume deviates beyond tolerance. Do not attempt arbitrary curved seams in v0.1.0.

- [ ] **Step 4: Implement connector primitives**

Use CadQuery when installed to create pin/socket, magnet pocket, and screw insert solids. Keep parameters in typed specs. Compute socket diameter as pin diameter plus twice radial clearance. Return explicit placement transforms; do not silently boolean them into the model until the user approves the split preview.

- [ ] **Step 5: Run focused tests with and without optional CAD dependency**

Run core: `python -m pytest tests/test_splitting.py -v`  
Run CAD extra: `python -m pytest tests/test_connectors.py -v`  
Expected: core tests always PASS; CAD tests PASS when installed and otherwise skip with the exact reason `cadquery extra not installed`.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/splitting.py src/personal_3d_fabricator/connectors.py tests/test_splitting.py tests/test_connectors.py
git commit -m "feat: split meshes and generate connector pairs"
```

### Task 7: Optional Authoring Tools and Workflow References

**Files:**
- Create: `src/personal_3d_fabricator/tooling.py`
- Create: `src/personal_3d_fabricator/adapters/cadquery_adapter.py`
- Create: `src/personal_3d_fabricator/adapters/blender_adapter.py`
- Create: `src/personal_3d_fabricator/adapters/step_adapter.py`
- Create: `references/create-model.md`
- Create: `references/modify-model.md`
- Create: `references/split-large-model.md`
- Create: `references/safety-boundaries.md`
- Create: `tests/test_tooling.py`

**Interfaces:**
- Produces: `ToolInfo(name: str, executable: Path | None, version: str | None, available: bool)`.
- Produces: `discover_tool(name: Literal["blender", "freecad", "bambu-studio", "orcaslicer", "prusaslicer"]) -> ToolInfo`.
- Produces: `run_blender_script(blend_file: Path | None, script: Path, output: Path) -> CompletedProcess[str]`.

- [ ] **Step 1: Write failing discovery tests using fake executables**

```python
def test_configured_executable_wins_over_path(tmp_path, monkeypatch):
    fake = make_fake_tool(tmp_path, "blender", "Blender 4.x")
    monkeypatch.setenv("PERSONAL_3D_FABRICATOR_BLENDER", str(fake))
    info = discover_tool("blender")
    assert info.available
    assert info.executable == fake
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_tooling.py -v`  
Expected: FAIL because tool discovery does not exist.

- [ ] **Step 3: Implement cross-platform discovery and adapters**

Resolve an explicit environment variable first, then `shutil.which`, then documented OS-specific application locations. Execute version checks and Blender scripts with argument lists and `shell=False`. Return structured unavailability rather than raising during ordinary discovery.

- [ ] **Step 4: Write workflow references**

`create-model.md` must require a trusted scale and distinguish functional CadQuery work from artistic Blender work. `modify-model.md` must preserve originals and explain mesh-versus-solid routing. `split-large-model.md` must enforce both approvals. `safety-boundaries.md` must cover load-bearing, protective, child-use, food-contact, electrical, and high-temperature risks without claiming certification.

- [ ] **Step 5: Run tests and Skill validation**

Run: `python -m pytest tests/test_tooling.py tests/test_skill_layout.py -v`  
Expected: PASS and every reference linked by `SKILL.md` exists.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/tooling.py src/personal_3d_fabricator/adapters references tests/test_tooling.py SKILL.md
git commit -m "feat: add optional authoring tool adapters"
```

### Task 8: Headless Previews and Delivery Documents

**Files:**
- Create: `src/personal_3d_fabricator/preview.py`
- Create: `src/personal_3d_fabricator/delivery.py`
- Create: `tests/test_delivery.py`

**Interfaces:**
- Produces: `render_turntable(meshes: list[trimesh.Trimesh], output_dir: Path, labels: list[str]) -> list[Path]`.
- Produces: `write_delivery(project_dir: Path, inspection: InspectionReport, plan: PrintPlan) -> DeliveryPaths`.

- [ ] **Step 1: Write failing delivery tests**

```python
def test_delivery_contains_numbered_parts_and_hardware(tmp_path, approved_plan):
    paths = write_delivery(tmp_path, approved_plan.inspection, approved_plan)
    text = paths.assembly_md.read_text(encoding="utf-8")
    assert "P01" in text and "P02" in text
    assert "磁铁" in text
    assert paths.print_plan_json.exists()

def test_final_delivery_requires_split_approval(tmp_path, overall_only_plan):
    with pytest.raises(ValueError, match="split_approved"):
        write_delivery(tmp_path, overall_only_plan.inspection, overall_only_plan)
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_delivery.py -v`  
Expected: FAIL because delivery functions do not exist.

- [ ] **Step 3: Implement previews and reports**

Render deterministic orthographic views with a headless Matplotlib `Agg` backend, fixed camera vectors, equal axis scale, part colors, and visible labels. Write sorted JSON with UTF-8 and an `assembly.md` containing part table, connector/hardware list, orientation notes, assembly order, and risk notices.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_delivery.py -v`  
Expected: PASS; generated PNG files have non-zero size and JSON validates against Task 2 schemas.

- [ ] **Step 5: Commit**

```bash
git add src/personal_3d_fabricator/preview.py src/personal_3d_fabricator/delivery.py tests/test_delivery.py pyproject.toml
git commit -m "feat: render previews and delivery documents"
```

### Task 9: Stable CLI and End-to-End Workflow

**Files:**
- Create: `src/personal_3d_fabricator/cli.py`
- Create: `scripts/check_environment.py`
- Create: `scripts/quick_validate.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_end_to_end.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces CLI commands: `init`, `inspect`, `plan-split`, `approve-overall`, `approve-split`, `deliver`, `check-environment`, `export-schemas`.
- Produces: `main(argv: list[str] | None = None) -> int` for both the console script and direct tests.
- Exit codes: `0` success, `2` invalid input/configuration, `3` missing optional tool, `4` approval gate not satisfied, `5` geometry operation failed.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_split_before_overall_approval_exits_four(project_dir, capsys):
    exit_code = main(["plan-split", str(project_dir)])
    assert exit_code == 4
    assert "overall_approved" in capsys.readouterr().err

def test_environment_report_is_json(capsys):
    exit_code = main(["check-environment", "--json"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["python"]["available"] is True
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_cli.py -v`  
Expected: FAIL because the CLI does not exist.

- [ ] **Step 3: Implement CLI as a thin boundary**

Use `argparse` from the standard library. CLI handlers validate paths, call existing service functions, print structured results, and map typed exceptions to the documented exit codes. They must not contain geometry or business logic.

- [ ] **Step 4: Add two end-to-end fixtures**

Test A creates a dimensioned bracket project, records a generated CadQuery source when the extra is installed, approves it, and writes delivery files. Test B creates a 500 mm box prop, confirms that three segments are planned for a 220 mm printer axis, approves the split, and produces numbered parts and assembly documentation.

- [ ] **Step 5: Run full local suite**

Run: `python -m pytest -q`  
Expected: all core tests PASS; unavailable optional-tool tests are explicitly skipped.

- [ ] **Step 6: Commit**

```bash
git add src/personal_3d_fabricator/cli.py scripts tests/test_cli.py tests/test_end_to_end.py pyproject.toml
git commit -m "feat: add end-to-end fabrication CLI"
```

### Task 10: Cross-Platform CI, Documentation, Validation, and Release

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `README.md`
- Create: `LICENSE`
- Modify: `.gitignore`
- Modify: `SKILL.md`

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: installable public repository and `v0.1.0` release after CI succeeds.

- [ ] **Step 1: Add a failing repository validation test**

Extend `tests/test_skill_layout.py` to require README installation commands for Windows, macOS, and Linux; MIT license text; all referenced paths; no absolute developer path; and no tracked files under sample project-output directories.

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_skill_layout.py -v`  
Expected: FAIL until documentation, license, and ignore rules exist.

- [ ] **Step 3: Add CI and documentation**

Configure GitHub Actions for `ubuntu-latest`, `macos-latest`, and `windows-latest` with Python 3.11 and 3.12. Install core plus dev dependencies, run `scripts/quick_validate.py`, run `pytest -q`, and upload failure diagnostics only. README must document installation, optional CadQuery/Blender setup, environment variables, the two approval gates, supported inputs, limitations, and uninstallation.

- [ ] **Step 4: Run pre-release verification locally**

Run:

```text
python scripts/quick_validate.py .
python -m pytest -q
python -m build
python -m pip install --force-reinstall dist/personal_3d_fabricator-0.1.0-py3-none-any.whl
personal-3d-fabricator check-environment --json
```

Expected: every command exits 0; optional unavailable tools are reported as unavailable without failing core validation.

- [ ] **Step 5: Commit verified release content**

```bash
git add .github README.md LICENSE .gitignore SKILL.md
git commit -m "docs: prepare cross-platform v0.1.0 release"
```

- [ ] **Step 6: Authenticate and create the public GitHub repository**

Run `gh auth login --hostname github.com --web`, let the user complete browser/device verification, then verify with `gh auth status`. Create the public repository with `gh repo create xyr723/personal-3d-fabricator-skill --public --source=. --remote=origin --push`.

- [ ] **Step 7: Verify remote CI before release**

Run `gh run list --workflow test.yml --limit 1`, then run `gh run watch --exit-status` and select that latest run when prompted.  
Expected: Windows, macOS, and Linux jobs all conclude `success`. If any job fails, fix and re-run the full relevant local command before pushing a corrective commit.

- [ ] **Step 8: Create v0.1.0 only after CI succeeds**

Run `gh release create v0.1.0 --target main --title "Personal 3D Fabricator v0.1.0" --generate-notes`.  
Expected: release exists and points to the verified main-branch commit.

---

## Final Verification Checklist

- [ ] `python scripts/quick_validate.py .` passes.
- [ ] `python -m pytest -q` passes with only justified optional-tool skips.
- [ ] Both end-to-end fixtures produce previews, reports, and assembly instructions.
- [ ] No source model is overwritten during any test.
- [ ] Split delivery is impossible before both approvals.
- [ ] All generated parts fit the selected printer capability profile.
- [ ] Repository contains no personal paths, credentials, user models, or generated user project data.
- [ ] GitHub Actions passes on Windows, macOS, and Linux.
- [ ] Public MIT repository and v0.1.0 release are available.
