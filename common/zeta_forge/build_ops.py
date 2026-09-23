"""Small, explicit filesystem and foreground-process operations."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from .build_cli import log


def validate_generated_path(path: Path, root: Path) -> None:
    """Reject escaping paths and links, including intermediate link components."""
    resolved = path.resolve()
    owner = root.resolve()
    if path.is_symlink() or resolved == owner or not resolved.is_relative_to(owner):
        raise RuntimeError(f"unsafe generated path: {path}")
    current = path.absolute()
    while current != owner and current != current.parent:
        if current.is_symlink():
            raise RuntimeError(f"unsafe generated path: {path}")
        current = current.parent


def remove_generated(path: Path, root: Path) -> None:
    """Remove only a caller-selected generated descendant, never a root or link."""
    validate_generated_path(path, root)
    resolved = path.resolve()
    if resolved.exists():
        log("I", f"remove generated state: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()


def relocate_installed_metadata(manifest: Path, stage: Path, prefix: Path) -> None:
    """Repair configured prefixes in metadata installed by this CMake invocation."""
    stage_prefix = str(stage).encode()
    target_prefix = str(prefix).encode()
    if stage_prefix == target_prefix:
        return
    if not manifest.is_file():
        raise RuntimeError(f"CMake install manifest is missing: {manifest}")
    prefix_root = prefix.resolve()
    for filename in manifest.read_text(encoding="utf-8").splitlines():
        installed = Path(filename)
        if installed.suffix not in {".cmake", ".pc"}:
            continue
        if (
            installed.is_symlink()
            or not installed.resolve().is_relative_to(prefix_root)
            or not installed.is_file()
        ):
            raise RuntimeError(f"installed metadata missing or unsafe: {installed}")
        content = installed.read_bytes()
        rewritten = content.replace(stage_prefix, target_prefix)
        if rewritten == content:
            continue
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{installed.name}.", dir=installed.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(rewritten)
            shutil.copymode(installed, temporary)
            os.replace(temporary, installed)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def run_program(command: Sequence[object], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run a foreground process group without logging application arguments."""
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
                # Reap the remaining group rather than leaving background work.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                continue
        raise
    if status != 0:
        if status == -signal.SIGINT or status == 130:
            raise KeyboardInterrupt
        raise RuntimeError(f"{Path(executable).name} exited with status {status}")
