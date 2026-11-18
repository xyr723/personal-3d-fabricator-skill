import builtins
from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest

from personal_3d_fabricator.adapters import _output, step_adapter
from personal_3d_fabricator.adapters.blender_adapter import run_blender_script
from personal_3d_fabricator.adapters.cadquery_adapter import CadQueryUnavailable, load_cadquery
from personal_3d_fabricator.adapters.step_adapter import export_step, import_step
from personal_3d_fabricator.tooling import ToolInfo, discover_tool


@pytest.fixture(autouse=True)
def isolate_tool_discovery(monkeypatch, tmp_path):
    for tool in ("BLENDER", "FREECAD", "BAMBU_STUDIO", "ORCASLICER", "PRUSASLICER"):
        monkeypatch.delenv(f"PERSONAL_3D_FABRICATOR_{tool}", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Unsupported")


@pytest.fixture
def fake_blender(tmp_path):
    if os.name == "nt":
        pytest.skip("fake executable uses a POSIX shebang")

    def make(name="blender", version="Blender 4.x", behavior=""):
        path = tmp_path / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] == ['--version']:\n"
            f"    print({version!r})\n"
            "    sys.exit(0)\n"
            + behavior,
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    return make


def test_configured_executable_wins_over_path(fake_blender, monkeypatch):
    fake_blender()
    configured = fake_blender("configured", "Blender configured")
    monkeypatch.setenv("PERSONAL_3D_FABRICATOR_BLENDER", str(configured))

    assert discover_tool("blender") == ToolInfo("blender", configured, "Blender configured", True)


@pytest.mark.parametrize("invalid", ["non-executable", "missing", "directory", "dangling-link"])
def test_invalid_configuration_retains_path_without_fallback(
    fake_blender, monkeypatch, tmp_path, invalid
):
    fake_blender()
    configured = tmp_path / "invalid"
    if invalid == "non-executable":
        configured.touch()
    elif invalid == "directory":
        configured.mkdir()
    elif invalid == "dangling-link":
        configured.symlink_to(tmp_path / "missing")
    monkeypatch.setenv("PERSONAL_3D_FABRICATOR_BLENDER", str(configured))

    assert discover_tool("blender") == ToolInfo("blender", configured, None, False)


def test_discovers_executable_on_path(fake_blender):
    path_tool = fake_blender()

    assert discover_tool("blender") == ToolInfo("blender", path_tool, "Blender 4.x", True)


def test_common_windows_location_is_used_after_path(tmp_path, monkeypatch):
    executable = tmp_path / "Bambu Studio" / "bambu-studio.exe"
    executable.parent.mkdir()
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("shutil.which", lambda name: None)

    def version_probe(args, **kwargs):
        assert args == [str(executable), "--help"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] > 0
        return subprocess.CompletedProcess(
            args, 0, stdout="starting CLI\nUsage: bambu-studio [ OPTIONS ]\n",
            stderr="BambuStudio-02.08.04.57:\n",
        )

    monkeypatch.setattr(subprocess, "run", version_probe)

    assert discover_tool("bambu-studio") == ToolInfo("bambu-studio", executable, "BambuStudio-02.08.04.57", True)


@pytest.mark.parametrize("error", [OSError, subprocess.TimeoutExpired])
def test_version_probe_failure_is_structured(fake_blender, monkeypatch, error):
    executable = fake_blender()

    def fail(args, **kwargs):
        if error is subprocess.TimeoutExpired:
            raise error(args, kwargs["timeout"])
        raise error("cannot run executable")

    monkeypatch.setattr(subprocess, "run", fail)

    assert discover_tool("blender") == ToolInfo("blender", executable, None, False)


def test_nonzero_version_probe_is_structured(fake_blender):
    executable = fake_blender()
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(2)\n", encoding="utf-8")

    assert discover_tool("blender") == ToolInfo("blender", executable, None, False)


def test_empty_version_output_has_no_version(fake_blender):
    executable = fake_blender(version="")

    assert discover_tool("blender") == ToolInfo("blender", executable, None, True)


def test_uninstalled_tool_is_structured():
    assert discover_tool("freecad") == ToolInfo("freecad", None, None, False)


def test_rejects_unknown_tool_name():
    with pytest.raises(ValueError):
        discover_tool("unapproved-program")


@pytest.mark.parametrize("with_blend", [True, False])
def test_blender_passes_literal_paths_to_background_script(fake_blender, tmp_path, with_blend):
    executable = fake_blender(
        behavior="import json\n"
        "with Path(sys.argv[-1]).open('x') as output:\n"
        "    json.dump(sys.argv[1:], output)\n"
        "print('script ran')\n"
    )
    script = tmp_path / "script $(touch marker).py"
    script.touch()
    blend = tmp_path / "source document.blend" if with_blend else None
    if blend:
        blend.touch()
    output = tmp_path / "result ; touch marker.blend"

    result = run_blender_script(blend, script, output)

    assert result.args[0] == str(executable)
    assert result.returncode == 0
    assert result.stdout.strip() == "script ran"
    assert not (tmp_path / "marker").exists()
    args = json.loads(output.read_text())
    assert "--background" in args
    assert "--disable-autoexec" in args
    assert args[args.index("--python") + 1] == str(script)
    stage, = map(Path, args[args.index("--") + 1 :])
    assert stage != output
    assert stage.parent == output.parent
    assert stage.name.startswith('.')
    assert stage.suffix == output.suffix
    assert not stage.exists()
    if blend:
        assert args.index("--disable-autoexec") < args.index(str(blend)) < args.index("--python")


@pytest.mark.parametrize("invalid", ["script", "blend", "output-exists", "output-parent"])
def test_blender_validates_paths_before_discovery(tmp_path, invalid):
    script = tmp_path / "script.py"
    if invalid != "script":
        script.touch()
    blend = tmp_path / "missing.blend" if invalid == "blend" else None
    output = tmp_path / "result.blend"
    if invalid == "output-exists":
        output.touch()
    elif invalid == "output-parent":
        output = tmp_path / "missing directory" / output.name

    expected = FileExistsError if invalid == "output-exists" else ValueError
    with pytest.raises(expected):
        run_blender_script(blend, script, output)


def test_blender_surfaces_script_failure_with_diagnostics(fake_blender, tmp_path):
    fake_blender(
        behavior="Path(sys.argv[-1]).write_text('partial')\n"
        "print('script failed', file=sys.stderr)\n"
        "raise SystemExit(9)\n"
    )
    script = tmp_path / "script.py"
    script.touch()

    before = set(tmp_path.iterdir())
    with pytest.raises(subprocess.CalledProcessError) as failed:
        run_blender_script(None, script, tmp_path / "result.blend")

    assert failed.value.returncode == 9
    assert failed.value.stderr.strip() == "script failed"
    assert set(tmp_path.iterdir()) == before


def test_blender_surfaces_execution_timeout(fake_blender, tmp_path, monkeypatch):
    fake_blender()
    script = tmp_path / "script.py"
    script.touch()
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[-1] == "--version":
            return real_run(args, **kwargs)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] > 0
        Path(args[-1]).write_text("partial")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    before = set(tmp_path.iterdir())
    with pytest.raises(subprocess.TimeoutExpired):
        run_blender_script(None, script, tmp_path / "result.blend")
    assert set(tmp_path.iterdir()) == before


def test_blender_requires_output_even_when_process_succeeds(fake_blender, tmp_path):
    fake_blender()
    script = tmp_path / "script.py"
    script.touch()

    with pytest.raises(FileNotFoundError):
        run_blender_script(None, script, tmp_path / "result.blend")


@pytest.mark.parametrize("missing", ["cadquery", "OCP"])
def test_cadquery_and_step_report_missing_dependency(tmp_path, monkeypatch, missing):
    real_import = builtins.__import__

    def import_without_cadquery(name, *args, **kwargs):
        if name == "cadquery":
            raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_cadquery)
    source = tmp_path / "model.step"
    source.touch()
    output = tmp_path / "export.stp"

    status = load_cadquery()
    assert isinstance(status, CadQueryUnavailable)
    assert status.available is False
    assert status.dependency == missing
    assert import_step(source) == status
    assert export_step(object(), output) == status
    assert not output.exists()


def test_step_round_trip_with_optional_cadquery(tmp_path):
    cadquery = load_cadquery()
    if isinstance(cadquery, CadQueryUnavailable):
        pytest.skip(cadquery.reason)
    source = cadquery.Workplane("XY").box(2, 3, 5)
    path = tmp_path / "box.stp"

    assert export_step(source, path) == path

    bounds = import_step(path).val().BoundingBox()
    assert (bounds.xlen, bounds.ylen, bounds.zlen) == pytest.approx((2, 3, 5))


def test_step_preserves_existing_output(tmp_path):
    path = tmp_path / "source.step"
    path.write_text("original")

    with pytest.raises(FileExistsError):
        export_step(object(), path)

    assert path.read_text() == "original"


@pytest.mark.parametrize(
    ("name", "flag", "header"),
    [
        ("freecad", "--version", "FreeCAD 1.1.0"),
        ("orcaslicer", "--help", "OrcaSlicer-2.4.0"),
        ("prusaslicer", "--help", "PrusaSlicer-2.9.6+linux"),
    ],
)
def test_tool_probes_support_their_real_cli(tmp_path, monkeypatch, name, flag, header):
    executable = tmp_path / (name + ".exe")
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setenv(f"PERSONAL_3D_FABRICATOR_{name.upper()}", str(executable))

    def run(args, **kwargs):
        assert args == [str(executable), flag]
        output = header if flag == "--version" else f"{header}:\nUsage: {name}\n"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert discover_tool(name) == ToolInfo(name, executable, header, True)


def test_blender_version_can_be_on_stderr(tmp_path, monkeypatch):
    executable = tmp_path / "blender.exe"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setenv("PERSONAL_3D_FABRICATOR_BLENDER", str(executable))

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=" \n", stderr="Blender 4.4.0\n")

    monkeypatch.setattr(subprocess, "run", run)

    assert discover_tool("blender") == ToolInfo("blender", executable, "Blender 4.4.0", True)


@pytest.mark.parametrize("suffix", [".bat", ".cmd"])
def test_windows_path_batch_does_not_shadow_later_executable(tmp_path, monkeypatch, suffix):
    earlier, later = tmp_path / "first", tmp_path / "second"
    earlier.mkdir()
    later.mkdir()
    batch = earlier / ("blender" + suffix)
    executable = later / "blender.exe"
    for path in (batch, executable):
        path.touch()
        path.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, (earlier, later))))
    monkeypatch.setenv("PATHEXT", ";.BAT;.CMD;.EXE")
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("shutil.which", lambda name: str(executable if name.endswith(".exe") else batch))

    def run(args, **kwargs):
        assert args[0] == str(executable)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args, 0, stdout="Blender 4.4.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert discover_tool("blender") == ToolInfo("blender", executable, "Blender 4.4.0", True)


@pytest.mark.parametrize("bad_output", ["empty", "directory"])
def test_blender_rejects_invalid_artifact_and_cleans_stage(fake_blender, tmp_path, bad_output):
    operation = "touch" if bad_output == "empty" else "mkdir"
    fake_blender(behavior="path = Path(sys.argv[-1])\n"
                 "path.unlink(missing_ok=True)\n"
                 f"path.{operation}()\n")
    script = tmp_path / "script.py"
    script.touch()
    output = tmp_path / "result.blend"
    before = set(tmp_path.iterdir())

    with pytest.raises(ValueError):
        run_blender_script(None, script, output)

    assert set(tmp_path.iterdir()) == before


def test_blender_preserves_concurrent_output(fake_blender, tmp_path):
    output = tmp_path / "result.blend"
    fake_blender(
        behavior=f"Path({str(output)!r}).write_text('concurrent')\n"
        "Path(sys.argv[-1]).write_text('from Blender')\n"
    )
    script = tmp_path / "script.py"
    script.touch()
    before = set(tmp_path.iterdir())

    with pytest.raises(FileExistsError):
        run_blender_script(None, script, output)

    assert output.read_text() == "concurrent"
    assert set(tmp_path.iterdir()) == before | {output}


@pytest.mark.parametrize("outcome", ["success", "empty", "partial", "directory", "conflict"])
def test_step_exports_privately_and_only_publishes_complete_output(tmp_path, monkeypatch, outcome):
    target = tmp_path / "model.stp"
    stages = []

    def export(model, filename, *, exportType):
        stage = Path(filename)
        stages.append(stage)
        assert exportType == "STEP"
        assert not target.exists()
        if outcome == "empty":
            stage.touch()
        elif outcome == "directory":
            stage.mkdir()
        else:
            if outcome == "conflict":
                target.write_text("concurrent")
            stage.write_text("STEP data")
            if outcome == "partial":
                raise RuntimeError("export interrupted")

    backend = SimpleNamespace(exporters=SimpleNamespace(export=export))
    monkeypatch.setattr(step_adapter, "load_cadquery", lambda: backend)

    if outcome == "success":
        assert export_step(object(), target) == target
        assert target.read_text() == "STEP data"
    else:
        error = {
            "conflict": FileExistsError,
            "empty": ValueError,
            "directory": ValueError,
            "partial": RuntimeError,
        }[outcome]
        with pytest.raises(error):
            export_step(object(), target)

    stage, = stages
    assert stage != target
    assert stage.is_relative_to(target.parent)
    assert stage.suffix == target.suffix
    assert not stage.exists()
    if outcome == "conflict":
        assert target.read_text() == "concurrent"
    assert set(tmp_path.iterdir()) == ({target} if outcome in {"success", "conflict"} else set())


@pytest.mark.parametrize("fails", [False, True])
def test_blender_only_cleans_its_own_temporary_backups(fake_blender, tmp_path, fails):
    unrelated = tmp_path / "other.blend1"
    unrelated.write_text("keep")
    output = tmp_path / "result.blend"
    script = tmp_path / "script.py"
    script.touch()
    fake_blender(
        behavior="output = Path(sys.argv[-1])\n"
        "output.write_text('blend data')\n"
        "for suffix in ('1', '2', '32', '@'):\n"
        "    output.with_name(output.name + suffix).touch()\n"
        "for suffix in ('0', '01', '٢', '.metadata'):\n"
        "    output.with_name(output.name + suffix).write_text('keep too')\n"
        f"raise SystemExit({int(fails)})\n"
    )
    before = set(tmp_path.iterdir())

    if fails:
        with pytest.raises(subprocess.CalledProcessError):
            run_blender_script(None, script, output)
    else:
        run_blender_script(None, script, output)
        assert output.read_text() == "blend data"

    assert unrelated.read_text() == "keep"
    remaining = set(tmp_path.iterdir()) - before - {output}
    assert {entry.suffix for entry in remaining} == {".blend0", ".blend01", ".blend٢", ".metadata"}
    assert all(entry.read_text() == "keep too" for entry in remaining)
    assert output.exists() != fails


def test_blender_publishes_without_listing_destination(fake_blender, tmp_path, monkeypatch):
    fake_blender(behavior="Path(sys.argv[-1]).write_text('blend data')\n")
    script = tmp_path / "script.py"
    script.touch()
    output = tmp_path / "result.blend"
    original_iterdir = Path.iterdir

    def no_listing(path):
        if path == output.parent:
            raise PermissionError(errno.EACCES, "directory listing denied", path)
        return original_iterdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", no_listing)
        run_blender_script(None, script, output)

    assert output.read_text() == "blend data"
    assert set(tmp_path.iterdir()) == {script, output, tmp_path / "blender"}


@pytest.fixture
def fake_step_export(monkeypatch):
    def configure(write):
        def export(model, filename, **kwargs):
            write(model, Path(filename))

        backend = SimpleNamespace(exporters=SimpleNamespace(export=export))
        monkeypatch.setattr(step_adapter, "load_cadquery", lambda: backend)

    return configure


def test_concurrent_step_exports_publish_one_complete_model(tmp_path, fake_step_export):
    output = tmp_path / "model.stp"
    ready = Barrier(2)

    def write(model, path):
        path.write_text(model)
        ready.wait(timeout=5)

    fake_step_export(write)

    def export(model):
        try:
            return export_step(model, output)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(export, ("first", "second")))

    assert outcomes.count(output) == 1
    assert output.read_text() in {"first", "second"}
    assert set(tmp_path.iterdir()) == {output}


def _normalized_link_target(path):
    if isinstance(path, PureWindowsPath):
        name = path.as_posix()
        if name.startswith("//?/"):
            name = name[4:]
            if name[:4].lower() == "unc/":
                name = "//" + name[4:]
            return PureWindowsPath(name)
    return path


@pytest.mark.parametrize(("recorded", "expected", "same"), [
    (PureWindowsPath(r"\\?\C:\Temp\Missing"), PureWindowsPath("c:/temp/missing"), True),
    (PureWindowsPath("//?/UNC/Host/Share/Missing"), PureWindowsPath("//host/share/missing"), True),
    (PureWindowsPath("//?/C:/temp/other"), PureWindowsPath("C:/temp/missing"), False),
    (PureWindowsPath("//?/D:/temp/missing"), PureWindowsPath("C:/temp/missing"), False),
    (PurePosixPath("/tmp/Missing"), PurePosixPath("/tmp/missing"), False),
])
def test_link_targets_follow_platform_path_semantics(recorded, expected, same):
    assert (_normalized_link_target(recorded) == _normalized_link_target(expected)) is same


def test_step_publish_preserves_dangling_symlink_created_during_export(tmp_path, fake_step_export):
    output = tmp_path / "model.stp"
    missing = tmp_path / "missing"

    def write(model, path):
        path.write_text("STEP data")
        try:
            output.symlink_to(missing)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows session cannot create symlinks")
            raise

    fake_step_export(write)

    with pytest.raises(FileExistsError):
        export_step(object(), output)

    assert _normalized_link_target(output.readlink()) == _normalized_link_target(missing)
    assert not missing.exists()
    assert set(tmp_path.iterdir()) == {output}


@pytest.fixture
def simulate_linux_rename(monkeypatch):
    def configure(rename):
        monkeypatch.setattr(_output, "sys", SimpleNamespace(platform="linux"), raising=False)

        def load_libc(library, *, use_errno):
            assert library is None
            assert use_errno is True
            return SimpleNamespace(renameat2=rename)

        monkeypatch.setattr(ctypes, "CDLL", load_libc)

    return configure


def test_linux_publish_uses_renameat2_noreplace(
    tmp_path, monkeypatch, fake_step_export, simulate_linux_rename
):
    output = tmp_path / "模型.stp"
    fake_step_export(lambda model, path: path.write_text("STEP data"))
    called = []

    def renameat2(source_dirfd, source, target_dirfd, target, flags):
        assert (source_dirfd, target_dirfd, flags) == (-100, -100, 1)
        assert (renameat2.argtypes, renameat2.restype) == (
            [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint],
            ctypes.c_int,
        )
        assert target == os.fsencode(output)
        assert not output.exists()
        called.append(target)
        os.rename(source, target)
        return 0

    simulate_linux_rename(renameat2)

    assert export_step(object(), output) == output
    assert called == [os.fsencode(output)]
    assert output.read_text() == "STEP data"


@pytest.mark.parametrize("failure_code", [errno.ENOSYS, errno.ENOTSUP, errno.ENOSPC])
def test_native_publish_failure_is_actionable_without_reserving_or_removing_final_name(
    tmp_path, monkeypatch, fake_step_export, simulate_linux_rename, failure_code
):
    output = tmp_path / "model.stp"
    fake_step_export(lambda model, path: path.write_text("STEP data"))

    def renameat2(source_dirfd, source, target_dirfd, target, flags):
        assert target == os.fsencode(output)
        assert not output.exists()
        if failure_code == errno.ENOSPC:
            output.write_text("concurrent writer")
        ctypes.set_errno(failure_code)
        return -1

    simulate_linux_rename(renameat2)
    real_open = os.open

    def guard_output_open(path, flags, *args, **kwargs):
        assert path != output
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guard_output_open)

    with pytest.raises(OSError) as error:
        export_step(object(), output)

    assert error.value.errno == failure_code
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == failure_code
    assert "choose a local volume or directory" in str(error.value)
    if failure_code == errno.ENOSPC:
        assert output.read_text() == "concurrent writer"
        assert set(tmp_path.iterdir()) == {output}
    else:
        assert not list(tmp_path.iterdir())


def test_linux_publish_handles_missing_libc_symbol(tmp_path, monkeypatch, fake_step_export):
    output = tmp_path / "model.stp"
    fake_step_export(lambda model, path: path.write_text("STEP data"))
    monkeypatch.setattr(_output, "sys", SimpleNamespace(platform="linux"), raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())

    with pytest.raises(OSError) as error:
        export_step(object(), output)

    assert error.value.errno == errno.ENOSYS
    assert isinstance(error.value.__cause__.__cause__, AttributeError)
    assert "choose a local volume or directory" in str(error.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("conflict", [False, True])
def test_windows_publish_uses_noreplace_rename(tmp_path, monkeypatch, fake_step_export, conflict):
    output = tmp_path / "model.stp"
    fake_step_export(lambda model, path: path.write_text("STEP data"))
    monkeypatch.setattr(_output, "sys", SimpleNamespace(platform="win32"), raising=False)
    real_rename = os.rename
    called = []

    def windows_rename(source, target):
        assert target == output
        assert not target.exists()
        called.append(target)
        if conflict:
            target.write_text("keep")
            raise FileExistsError(errno.EEXIST, "already exists", target)
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", windows_rename)
    if conflict:
        with pytest.raises(FileExistsError):
            export_step(object(), output)
    else:
        assert export_step(object(), output) == output

    assert called == [output]
    assert output.read_text() == ("keep" if conflict else "STEP data")
    assert set(tmp_path.iterdir()) == {output}
