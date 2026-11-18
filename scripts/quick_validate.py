"""Validate repository models, exported schemas, and shipped printer profiles."""

import argparse
import sys
from pathlib import Path

from personal_3d_fabricator.profiles import load_printer_profile
from personal_3d_fabricator.schemas import schema_documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root
    try:
        for name, expected in schema_documents().items():
            path = root / "schemas" / name
            if path.read_text(encoding="utf-8") != expected:
                raise ValueError(f"Schema differs from its installed model: {path}")
        profiles = sorted((root / "profiles").glob("*.json"))
        if not profiles:
            raise ValueError(f"No printer profiles in {root / 'profiles'}")
        for path in profiles:
            load_printer_profile(path)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    print(f"Validated JSON schemas and {len(profiles)} printer profiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
