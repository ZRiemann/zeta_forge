"""Construct commands for an approved Rust toolchain without installing tools."""

from __future__ import annotations

import shutil
from pathlib import Path

from .rust_project import RustProject


def rustup_command(project: RustProject, program: str, *arguments: str) -> list[object]:
    rustup = shutil.which("rustup")
    if rustup is None:
        raise RuntimeError("Required Rust toolchain manager not found on PATH: rustup")
    return [rustup, "run", project.toolchain_spec["channel"], program, *arguments]


def with_manifest(
    command: list[object], manifest: Path, arguments: tuple[str, ...],
) -> list[object]:
    manifest_arguments = ("--manifest-path", manifest)
    if "--" in arguments:
        separator = arguments.index("--")
        return [*command, *arguments[:separator], *manifest_arguments, *arguments[separator:]]
    return [*command, *arguments, *manifest_arguments]
