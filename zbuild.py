#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.build_cli import cli
from builder.project import project


if __name__ == "__main__":
    raise SystemExit(cli(project(Path(__file__))))
