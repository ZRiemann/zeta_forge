from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Sequence

from .build_log import log


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
    timeout: float | None = None,
    log_command: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not args:
        raise ValueError("command must include an executable")
    stringified = _stringify_args(args)
    display = shell_join(stringified) if log_command else Path(stringified[0]).name
    if log_command:
        print(f"==> {display}")
    try:
        completed = subprocess.run(
            stringified,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=capture_output,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        if log_command:
            raise
        raise subprocess.TimeoutExpired(
            display, error.timeout, output=error.output, stderr=error.stderr,
        ) from None
    if check and completed.returncode != 0:
        raise CommandError(f"Command failed with exit code {completed.returncode}: {display}")
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


def run_program(command: Sequence[object], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run a foreground process group without logging application arguments."""
    if not command:
        raise ValueError("foreground command must include an executable")
    executable = str(command[0])
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"executable is missing or not executable: {executable}")
    log("I", f"launch {Path(executable).name}; application arguments omitted")
    process = subprocess.Popen(
        [str(arg) for arg in command], cwd=cwd, env=env, start_new_session=True
    )
    try:
        status = process.wait()
    except KeyboardInterrupt:
        for sig, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=timeout)
                # The launcher may exit before its watcher/server descendants.
                # Terminate remaining group members before returning to the caller.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                continue
        # Always wait for the direct child, even if the group already exited
        # or the previous wait was itself interrupted.
        while process.poll() is None:
            try:
                process.wait(timeout=3)
            except KeyboardInterrupt:
                continue
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    if status != 0:
        if status == -signal.SIGINT or status == 130:
            raise KeyboardInterrupt
        raise RuntimeError(f"{Path(executable).name} exited with status {status}")
