#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.dispatcher import main as dispatcher_main



if __name__ == "__main__":
    try:
        raise SystemExit(dispatcher_main(Path(__file__)))
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
