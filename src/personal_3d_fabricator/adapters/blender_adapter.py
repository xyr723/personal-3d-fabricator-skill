"""Run a trusted local Blender Python script in a background process."""

from collections.abc import Iterator
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import subprocess
from tempfile import mkstemp

from ..tooling import discover_tool
from ._output import publish_output

_SCRIPT_TIMEOUT_SECONDS = 300
_MAX_BLENDER_SAVE_VERSIONS = 32
_LOGGER = logging.getLogger(__name__)


def _input_file(path: Path, suffix: str) -> Path:
    path = path.expanduser().resolve()
    if path.suffix.lower() != suffix or not path.is_file():
        raise ValueError(f"Expected an existing {suffix} file: {path}")
    return path


def _clean_blender_output(staged: Path) -> None:
    if staged.is_dir() and not staged.is_symlink():
        staged.rmdir()
    else:
        staged.unlink(missing_ok=True)

    # Blender writes to an @ sibling and allows up to 32 numbered save versions.
    staged.with_name(staged.name + "@").unlink(missing_ok=True)
    for version in range(1, _MAX_BLENDER_SAVE_VERSIONS + 1):
        staged.with_name(f"{staged.name}{version}").unlink(missing_ok=True)


@contextmanager
def _stage_blender_output(destination: Path) -> Iterator[Path]:
    # Blender remaps its relative resources against the save directory.
    descriptor, name = mkstemp(
        prefix=".fabricator-", suffix=destination.suffix, dir=destination.parent
    )
    staged = Path(name)
    try:
        os.close(descriptor)
        staged.unlink()
        yield staged
        publish_output(staged, destination)
    except BaseException as error:
        try:
            _clean_blender_output(staged)
        except OSError as cleanup_error:
            error.add_note(f"Could not finish cleaning Blender temporary output: {cleanup_error}")
        raise
    else:
        try:
            _clean_blender_output(staged)
        except OSError as cleanup_error:
            _LOGGER.warning("Could not finish cleaning Blender temporary output: %s", cleanup_error)


def run_blender_script(
    blend_file: Path | None, script: Path, output: Path
) -> subprocess.CompletedProcess[str]:
    """Pass a private output path as the first script argument after '--'.

    Publish the requested file after a successful exit and a nonempty output.
    Propagate subprocess.TimeoutExpired and subprocess.CalledProcessError
    with their captured diagnostics.
    """
    script = _input_file(script, ".py")
    if blend_file is not None:
        blend_file = _input_file(blend_file, ".blend")
    output = output.expanduser().absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"Blender output already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"Blender output directory does not exist: {output.parent}")
    output = output.parent.resolve() / output.name

    blender = discover_tool("blender")
    if not blender.available or blender.executable is None:
        detail = f": {blender.executable}" if blender.executable is not None else ""
        raise FileNotFoundError(f"Blender executable unavailable{detail}")

    command = [
        str(blender.executable),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
    ]
    if blend_file is not None:
        command.append(str(blend_file))
    with _stage_blender_output(output) as staged:
        command.extend(["--python-exit-code", "1", "--python", str(script), "--", str(staged)])
        result = subprocess.run(
            command,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SCRIPT_TIMEOUT_SECONDS,
        )
    return result
