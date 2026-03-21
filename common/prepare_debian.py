#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "common"))

from zeta_forge.config import load_repo_config
from zeta_forge.debian_prep import DebianPreparer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a Debian-family system for building zeta_forge dependencies."
    )
    parser.add_argument(
        "--python-version",
        default="3.12",
        help="Python version installed through uv for Conan tooling (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_config = load_repo_config(Path(__file__))
    DebianPreparer(
        script_path=Path(__file__),
        repo_config=repo_config,
        python_version=args.python_version,
    ).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
