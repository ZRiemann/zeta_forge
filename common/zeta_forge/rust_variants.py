"""Ordered Cargo and Dioxus flags for selected Rust delivery variants."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .build_contract import Request


@dataclass(frozen=True)
class RustTarget:
    package: str
    directory: Path
    kind: str = "cargo"
    platforms: tuple[str, ...] = ()
    web_release_debug_symbols: bool | None = None
    artifact_name: str | None = None
    features: tuple[str, ...] = ()
    example: str | None = None


def dioxus_command(
    tool: str, request: Request, target: RustTarget, platform: str | None, action: str,
) -> list[str]:
    command = [
        tool,
        action,
        "--locked",
        "--package",
        target.package,
    ]
    command.append(f"--{platform or 'desktop'}")
    if request.profile == "release":
        command.append("--release")
        if (
            action == "build"
            and platform == "web"
            and target.web_release_debug_symbols is not None
        ):
            command.extend(
                ("--debug-symbols", str(target.web_release_debug_symbols).lower())
            )
    if target.kind == "fullstack":
        command.extend(("--fullstack", "true", "--force-sequential", "true"))
    return command


def cargo_build_flags(target: RustTarget, platform: str | None) -> tuple[str, ...]:
    flags = ("--example", target.example) if target.example is not None else ()
    if platform == "desktop":
        flags += ("--no-default-features", "--features", "desktop")
    if target.features:
        flags += ("--features", ",".join(target.features))
    return flags
