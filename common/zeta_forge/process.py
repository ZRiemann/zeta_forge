from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


class CommandError(RuntimeError):
    pass


def _stringify_args(args: Sequence[object]) -> list[str]:
    return [str(arg) for arg in args]


def shell_join(args: Sequence[object]) -> str:
    return shlex.join(_stringify_args(args))


def run_command(
    args: Sequence[object],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    stringified = _stringify_args(args)
    print(f"==> {shell_join(stringified)}")
    completed = subprocess.run(
        stringified,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if check and completed.returncode != 0:
        raise CommandError(f"Command failed with exit code {completed.returncode}: {shell_join(stringified)}")
    return completed


def require_command(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"Required command not found on PATH: {name}")
    return resolved


def cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def run_optional(args: Sequence[object], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> bool:
    completed = run_command(args, cwd=cwd, env=env, check=False)
    return completed.returncode == 0
