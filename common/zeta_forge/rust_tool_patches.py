"""Pinned source repairs and verified installations for Forge-owned Cargo tools."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.request import urlopen

from .process import run_command
from .rust_patches import materialize, verify_source
from .rust_tool_recipe import ToolRepair as ToolRepair
from .rust_tool_recipe import tool_repair as tool_repair


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_tool_source(repair: ToolRepair, environment: Mapping[str, str]) -> None:
    if repair.source.exists():
        verify_source(repair.recipe, repair.source)
        return
    recipe = repair.recipe
    filename = f"{recipe['package']}-{recipe['version']}.crate"
    cargo_home = Path(environment.get("CARGO_HOME", str(Path.home() / ".cargo"))).expanduser()
    payload = None
    for archive in (cargo_home / "registry/cache").glob(f"*/{filename}"):
        if file_digest(archive) == recipe["archive_sha256"]:
            payload = archive.read_bytes()
            break
    if payload is None:
        url = f"https://static.crates.io/crates/{recipe['package']}/{filename}"
        with urlopen(url, timeout=30) as response:
            payload = response.read(32 * 1024 * 1024 + 1)
        if len(payload) > 32 * 1024 * 1024:
            raise RuntimeError("Cargo tool source archive exceeds 32 MiB")
    materialize(recipe, repair.source, payload)


def repair_receipt(repair: ToolRepair, channel: str, executable: Path) -> dict[str, object]:
    return {"schema": 1, "repair": repair.name, "recipe_sha256": repair.identity,
            "source_tree_sha256": repair.recipe["tree_sha256"], "toolchain": channel,
            "package": repair.recipe["package"], "version": repair.recipe["version"],
            "executable_sha256": file_digest(executable)}


def verify_tool_install(repair: ToolRepair, channel: str, executable: Path) -> None:
    receipt = executable.parent.parent / "forge-tool.json"
    try:
        recorded = json.loads(receipt.read_bytes())
        if recorded != repair_receipt(repair, channel, executable):
            raise ValueError("identity differs")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Missing or modified Cargo tool repair identity at {receipt}") from error


def install_repaired_tool(
    repair: ToolRepair, channel: str, executable_name: str, tool_root: Path,
    cargo: str, forge_root: Path, environment: dict[str, str],
) -> None:
    """Build privately, verify, then publish; preserve an earlier install on failure."""
    prepare_tool_source(repair, environment)
    tool_root.parent.mkdir(parents=True, exist_ok=True)
    lock = tool_root.with_name(tool_root.name + ".installing")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise RuntimeError(f"Cargo tool installation already in progress: {lock}") from error
    try:
        with tempfile.TemporaryDirectory(prefix=".install-", dir=tool_root.parent) as directory:
            candidate = Path(directory) / "product"
            target = forge_root / "build/rust/tool-targets" / channel / repair.identity
            # Cargo checks PATH after installing. This directory is private and temporary;
            # append it only for this command, preserving existing command precedence.
            install_environment = {
                **environment, "CARGO_TARGET_DIR": str(target),
                "PATH": os.pathsep.join((environment.get("PATH", os.defpath), str(candidate / "bin"))),
            }
            run_command(
                [cargo, f"+{channel}", "install", "--path", repair.source, "--locked",
                 "--force", "--root", candidate],
                cwd=repair.source, env=install_environment,
            )
            executable = candidate / "bin" / executable_name
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise RuntimeError(f"Cargo tool build did not produce {executable}")
            completed = run_command([executable, "--version"], env=environment,
                                    capture_output=True)
            tokens = {token.lstrip("v") for token in (completed.stdout or completed.stderr).split()}
            if repair.recipe["version"] not in tokens:
                raise RuntimeError("Built Cargo tool has an unexpected version")
            verify_source(repair.recipe, repair.source)
            (candidate / "forge-tool.json").write_text(
                json.dumps(repair_receipt(repair, channel, executable), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            backup = Path(directory) / "previous"
            try:
                if tool_root.exists():
                    tool_root.rename(backup)
                candidate.rename(tool_root)
            except BaseException:
                # Publication owns rollback even when the foreground command is interrupted.
                if backup.exists() and not tool_root.exists():
                    backup.rename(tool_root)
                raise
    finally:
        lock.rmdir()
