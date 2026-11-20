#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.config import load_repo_config
from zeta_forge.debian_prep import DebianPreparer
from zeta_forge.host_toolchains import ensure_rust_toolchain, ensure_zig_toolchain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare zeta_forge-local dependencies after the host bootstrap layer is ready."
    )
    parser.add_argument(
        "--python-version",
        default=None,
        help="Optional Python version passed to uv for Conan tooling. By default, follow the active ZetaX/uv environment.",
    )
    parser.add_argument(
        "--toolchain", choices=("rust", "zig"),
        help="Install only the selected Forge-approved toolchain.",
    )
    parser.add_argument(
        "--rust-project", type=Path,
        help="Rust project providing the toolchain and capability declarations.",
    )
    args = parser.parse_args()
    if (args.toolchain == "rust") != (args.rust_project is not None):
        parser.error("--toolchain rust and --rust-project must be specified together")
    if args.toolchain and args.python_version is not None:
        parser.error("--python-version applies to Conan host preparation")
    return args


def main() -> int:
    args = parse_args()
    if args.toolchain == "rust":
        ensure_rust_toolchain(FORGE_ROOT, args.rust_project.resolve())
        return 0
    if args.toolchain == "zig":
        ensure_zig_toolchain()
        return 0
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
