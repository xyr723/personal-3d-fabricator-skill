"""Command-line entry point for the fabrication workflow."""

import argparse
import json
import platform
import sys
from dataclasses import asdict
from pathlib import Path
from typing import get_args

from .adapters.cadquery_adapter import CadQueryUnavailable, load_cadquery
from .connectors import ConnectorBackendUnavailable
from .inspection import inspect_mesh
from .projects import ApprovalError, create_project
from .schemas import export_schemas
from .tooling import ToolName, discover_tool
from .workflow import (
    GeometryError, approve_overall, approve_split, deliver_project, plan_project_split,
    prepare_overall_preview, require_usable_inspection,
)

_TOOLS = get_args(ToolName)


class ToolUnavailable(ValueError):
    """One or more requested optional tools could not be loaded."""

    def __init__(self, missing, report):
        super().__init__(f"Required optional tools unavailable: {', '.join(missing)}")
        self.report = report


def _inspect(args):
    if args.project is not None:
        return prepare_overall_preview(args.project, args.mesh, args.assumed_unit)
    if args.mesh is None:
        raise ValueError("Provide a mesh path or --project to inspect the unique archived source")
    if not args.mesh.is_file():
        raise ValueError(f"Mesh must be an existing file: {args.mesh}")
    report = inspect_mesh(args.mesh, args.assumed_unit)
    require_usable_inspection(report)
    return report.model_dump(mode="json")


def _environment(args):
    cadquery = load_cadquery()
    report = {
        "python": {
            "available": True,
            "executable": sys.executable,
            "version": platform.python_version(),
        },
        "cadquery": asdict(cadquery) if isinstance(cadquery, CadQueryUnavailable) else {
            "available": True, "version": getattr(cadquery, "__version__", None)
        },
    }
    report.update({name: asdict(discover_tool(name)) for name in _TOOLS})
    missing = [name for name in args.require if not report[name]["available"]]
    if missing:
        raise ToolUnavailable(missing, report)
    return report


def _manifest_json(manifest):
    return manifest.model_dump(mode="json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-3d-fabricator")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, summary, handler):
        subparser = commands.add_parser(name, help=summary, description=summary)
        subparser.add_argument("--json", action="store_true", help="emit compact JSON")
        subparser.set_defaults(handler=handler)
        return subparser

    init = command("init", "Create a draft project", lambda a: _manifest_json(
        create_project(a.root, a.name, a.scale_reference_mm)
    ))
    init.add_argument("root", type=Path)
    init.add_argument("name")
    init.add_argument("--scale-reference-mm", type=float)

    inspect = command("inspect", "Inspect an STL, OBJ or 3MF model", _inspect)
    inspect.add_argument("mesh", type=Path, nargs="?")
    inspect.add_argument("--project", type=Path, help="prepare an overall preview in a draft project")
    inspect.add_argument("--assumed-unit")

    overall = command("approve-overall", "Approve the archived overall model", lambda a: _manifest_json(
        approve_overall(a.project, a.mesh, a.assumed_unit)
    ))
    overall.add_argument("project", type=Path)
    overall.add_argument("--mesh", type=Path, help="verify the reviewed external mesh still matches the archived source")
    overall.add_argument("--assumed-unit", help="known units for meshes without embedded units")

    plan = command("plan-split", "Prepare a split preview using the approved source", lambda a:
                   plan_project_split(a.project, a.profile, connector=a.connector,
                                      pin_diameter_mm=a.pin_diameter_mm, pin_length_mm=a.pin_length_mm,
                                      pin_clearance_mm=a.pin_clearance_mm))
    plan.add_argument("project", type=Path)
    plan.add_argument("--profile", type=Path, help="printer profile JSON (or use the project profile)")

    plan.add_argument("--connector", choices=("flat", "pin"), default="flat")
    plan.add_argument("--pin-diameter-mm", type=float, help="pin diameter (default 4 mm)")
    plan.add_argument("--pin-length-mm", type=float, help="pin length and pocket depth (default 6 mm)")
    plan.add_argument("--pin-clearance-mm", type=float, help="radial fit allowance (default 0.2 mm)")

    split = command("approve-split", "Approve the saved split preview", lambda a:
                    _manifest_json(approve_split(a.project)))
    split.add_argument("project", type=Path)

    deliver = command("deliver", "Publish approved numbered parts and assembly documents", lambda a:
                      deliver_project(a.project))
    deliver.add_argument("project", type=Path)

    environment = command("check-environment", "Report installed optional tools", _environment)
    environment.add_argument("--require", action="append", default=[], choices=("cadquery", *_TOOLS))

    schemas = command("export-schemas", "Export JSON Schema files", lambda a:
                      {"schemas": export_schemas(a.output)})
    schemas.add_argument("output", type=Path)
    return parser


def _json_value(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _print_json(value, compact: bool):
    # ASCII JSON can be written to Windows consoles with a legacy code page.
    print(json.dumps(value, indent=None if compact else 2, default=_json_value, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return error.code

    try:
        _print_json(args.handler(args), args.json)
    except ConnectorBackendUnavailable as error:
        if args.json:
            _print_json({"cadquery": {"available": False, "reason": str(error)}}, True)
        print(error, file=sys.stderr)
        return 3
    except ToolUnavailable as error:
        if args.json:
            _print_json(error.report, True)
        print(error, file=sys.stderr)
        return 3
    except ApprovalError as error:
        print(error, file=sys.stderr)
        return 4
    except GeometryError as error:
        if args.json and error.report is not None:
            _print_json(error.report.model_dump(mode="json"), True)
        print(error, file=sys.stderr)
        return 5
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
