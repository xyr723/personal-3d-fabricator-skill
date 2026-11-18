"""Discover optional authoring and slicing tools without installing them."""

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Iterator, Literal

ToolName = Literal["blender", "freecad", "bambu-studio", "orcaslicer", "prusaslicer"]

_PATH_NAMES = {
    "blender": ("blender",),
    "freecad": ("freecad", "FreeCAD", "freecadcmd"),
    "bambu-studio": ("bambu-studio", "bambustudio"),
    "orcaslicer": ("orcaslicer", "orca-slicer"),
    "prusaslicer": ("prusa-slicer", "prusaslicer"),
}
_MAC_APPS = {
    "blender": "Blender.app/Contents/MacOS/Blender",
    "freecad": "FreeCAD.app/Contents/MacOS/FreeCAD",
    "bambu-studio": "BambuStudio.app/Contents/MacOS/BambuStudio",
    "orcaslicer": "OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "prusaslicer": "PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
}
_WINDOWS_APPS = {
    "blender": ("Blender Foundation/Blender/blender.exe", "Blender Foundation/Blender */blender.exe"),
    "freecad": ("FreeCAD/bin/FreeCAD.exe", "FreeCAD */bin/FreeCAD.exe"),
    "bambu-studio": ("Bambu Studio/bambu-studio.exe",),
    "orcaslicer": ("OrcaSlicer/orca-slicer.exe",),
    "prusaslicer": ("Prusa3D/PrusaSlicer/prusa-slicer-console.exe",),
}
_HELP_NAMES = {
    "bambu-studio": r"Bambu[ -]?Studio",
    "orcaslicer": r"Orca[ -]?Slicer",
    "prusaslicer": r"Prusa[ -]?Slicer",
}
_PROBE_FLAGS = {
    "blender": "--version",
    "freecad": "--version",
    "bambu-studio": "--help",
    "orcaslicer": "--help",
    "prusaslicer": "--help",
}
_VERSION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ToolInfo:
    name: str
    executable: Path | None
    version: str | None
    available: bool


def _common_locations(name: ToolName, system: str) -> Iterator[Path]:
    if system == "Darwin":
        yield Path("/Applications") / _MAC_APPS[name]
        try:
            yield Path.home() / "Applications" / _MAC_APPS[name]
        except RuntimeError:
            return
    elif system == "Windows":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if root := os.environ.get(variable):
                for pattern in _WINDOWS_APPS[name]:
                    yield from sorted(Path(root).glob(pattern), reverse=True)
    elif system == "Linux":
        for directory in ("/usr/bin", "/usr/local/bin"):
            for basename in _PATH_NAMES[name]:
                yield Path(directory) / basename


def _is_executable(path: Path, system: str) -> bool:
    if system == "Windows" and path.suffix.lower() != ".exe":
        return False
    return path.is_file() and os.access(path, os.X_OK)


def _path_locations(name: ToolName, system: str) -> Iterator[Path]:
    names = _PATH_NAMES[name]
    if system == "Windows":
        if name == "prusaslicer":
            names = ("prusa-slicer-console", *names)
        for directory in os.get_exec_path():
            for basename in names:
                yield Path(directory) / f"{basename}.exe"
    else:
        for basename in names:
            if located := shutil.which(basename):
                yield Path(located)


def _reported_version(name: ToolName, output: list[str]) -> str | None:
    if name not in _HELP_NAMES:
        return next((line.strip() for line in output if line.strip()), None)
    banner = re.compile(rf"^{_HELP_NAMES[name]}[-\s]+v?[0-9][^\s:]*", re.IGNORECASE)
    for line in output:
        if match := banner.match(line.strip()):
            return match.group(0)
    return None


def _version(name: ToolName, executable: Path) -> ToolInfo:
    try:
        process = subprocess.run(
            [str(executable), _PROBE_FLAGS[name]],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ToolInfo(name, executable, None, False)
    if process.returncode:
        return ToolInfo(name, executable, None, False)

    output = (process.stdout or "").splitlines() + (process.stderr or "").splitlines()
    return ToolInfo(name, executable, _reported_version(name, output), True)


def discover_tool(name: ToolName) -> ToolInfo:
    """Use an explicit path, then PATH, then common installation locations.

    An invalid explicit path is returned with available=False and stops discovery.
    """
    if name not in _PATH_NAMES:
        raise ValueError(f"Unsupported tool: {name!r}")

    system = platform.system()
    variable = "PERSONAL_3D_FABRICATOR_" + name.upper().replace("-", "_")
    executable = None
    try:
        if variable in os.environ:
            executable = Path(os.environ[variable])
            executable = executable.expanduser().absolute()
            if _is_executable(executable, system):
                return _version(name, executable)
            return ToolInfo(name, executable, None, False)

        for candidate in _path_locations(name, system):
            if _is_executable(candidate, system):
                return _version(name, candidate.absolute())

        for candidate in _common_locations(name, system):
            if _is_executable(candidate, system):
                return _version(name, candidate.absolute())
    except (OSError, RuntimeError, ValueError):
        return ToolInfo(name, executable, None, False)

    return ToolInfo(name, None, None, False)
