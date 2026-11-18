"""Inspect the local optional fabrication toolchain."""

import sys

from personal_3d_fabricator.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["check-environment", *sys.argv[1:]]))
