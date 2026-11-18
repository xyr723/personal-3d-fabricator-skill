"""Stage outputs on the destination filesystem and publish them exclusively."""

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory

_LINUX_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_MACOS_RENAME_EXCL = 0x00000004


def _rename_noreplace(
    staged: Path, destination: Path, *, directory_fd: int | None = None
) -> None:
    if sys.platform == "win32":
        os.rename(staged, destination)
        return

    if sys.platform not in {"linux", "darwin"}:
        # The staging context unlinks its name after the exclusive link publishes.
        if directory_fd is None:
            os.link(staged, destination)
        else:
            os.link(staged, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return

    source = os.fsencode(staged)
    target = os.fsencode(destination)
    if b"\0" in source or b"\0" in target:
        raise ValueError("Output paths must not contain a null byte")

    if sys.platform == "linux":
        symbol = "renameat2"
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fd = _LINUX_AT_FDCWD if directory_fd is None else directory_fd
        args = (fd, source, fd, target, _LINUX_RENAME_NOREPLACE)
    elif directory_fd is not None:
        symbol = "renameatx_np"
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (directory_fd, source, directory_fd, target, _MACOS_RENAME_EXCL)
    else:
        symbol = "renamex_np"
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (source, target, _MACOS_RENAME_EXCL)

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = getattr(libc, symbol)
    except AttributeError as error:
        raise OSError(errno.ENOSYS, f"The system C library does not provide {symbol}") from error
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def publish_output(
    staged: Path,
    destination: Path,
    *,
    directory: Path | None = None,
    directory_fd: int | None = None,
) -> None:
    source, target = staged, destination
    if directory_fd is not None:
        if directory is None:
            raise ValueError("The directory is required to publish relative to a descriptor")
        source, target = staged.relative_to(directory), destination.relative_to(directory)
    metadata = os.stat(source, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
        raise ValueError(f"Expected a nonempty regular output file: {destination}")

    try:
        if directory_fd is None:
            _rename_noreplace(source, target)
        else:
            _rename_noreplace(source, target, directory_fd=directory_fd)
    except FileExistsError as error:
        raise FileExistsError(
            error.errno, "Output already exists. Choose an unused filename", destination
        ) from error
    except OSError as error:
        message = (
            "Could not publish output atomically. Check space and directory permissions or "
            "choose a local volume or directory that supports atomic no-replace renames"
        )
        raise OSError(error.errno, message, destination) from error


@contextmanager
def stage_output(destination: Path) -> Iterator[Path]:
    """Publish only when the producer succeeds and writes a nonempty file."""
    with TemporaryDirectory(prefix=".fabricator-", dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        yield staged
        publish_output(staged, destination)


@contextmanager
def stage_directory(destination: Path) -> Iterator[Path]:
    """Publish a complete output directory by one exclusive rename."""
    with TemporaryDirectory(prefix=".fabricator-", dir=destination.parent) as directory:
        staged = Path(directory)
        yield staged
        _rename_noreplace(staged, destination)
