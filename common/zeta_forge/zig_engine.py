"""Thin native Zig adapter. Project build.zig owns the dependency graph."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .build_cli import Request
from .build_ops import remove_generated, run_program, validate_generated_path
from .process import require_command

ZIG_VERSION = "0.17.0"


@dataclass(frozen=True)
class ZigTarget:
    step: str
    executable: str | None = None


class ZigEngine:
    def __init__(self, root: Path, targets: Mapping[str, ZigTarget]) -> None:
        self.root = root.resolve()
        self.targets = dict(targets)

    def profile_dir(self, request: Request) -> Path:
        return self.root / "build" / "zig" / request.profile

    def artifact(self, request: Request, name: str) -> Path:
        executable = self.targets[name].executable
        if executable is None:
            raise ValueError(f"{name} has no runnable output")
        return self.profile_dir(request) / "bin" / executable

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        for name in names:
            if name not in self.targets:
                raise ValueError(f"unknown Zig target: {name}")
        if request.action in {"run", "dev"}:
            if len(names) != 1 or self.targets[names[0]].executable is None:
                raise ValueError("run/dev requires one executable target")

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        result: dict[str, object] = {
            "source": str(self.root / "build.zig"),
            "toolchain": ZIG_VERSION,
            "native_profile": "Debug" if request.profile == "debug" else "ReleaseFast",
            "target_dir": str(self.profile_dir(request)),
            "native_steps": [self.targets[name].step for name in names],
            "artifacts": {
                name: str(self.artifact(request, name))
                for name in names if self.targets[name].executable
            },
        }
        if request.action in {"clean", "rebuild"}:
            result.update(
                clean_scope="entire Zig project/profile", affected=list(self.targets)
            )
        if request.action in {"test", "check"}:
            result["verification_scope"] = "shared native project suite; all components and consumers"
        return result

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        validate_generated_path(self.profile_dir(request), self.root)
        if request.action == "clean":
            return
        executable = require_command("zig")
        try:
            version = subprocess.run(
                [executable, "version"], check=True, capture_output=True, text=True
            ).stdout.strip()
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"zig version failed with status {error.returncode}") from error
        if version != ZIG_VERSION:
            raise RuntimeError(
                f"Zig {ZIG_VERSION} required; found {version}. "
                "Prepare the toolchain separately."
            )
        if not (self.root / "build.zig").is_file() or not (self.root / "build.zig.zon").is_file():
            raise RuntimeError("missing native Zig build files")
        if request.action == "run" and not self.artifact(request, names[0]).is_file():
            raise RuntimeError(f"built artifact is missing: {self.artifact(request, names[0])}")

    def native(self, request: Request, steps: tuple[str, ...]) -> None:
        directory = self.profile_dir(request)
        command = [
            require_command("zig"), "build", *steps,
            "-Doptimize=" + ("Debug" if request.profile == "debug" else "ReleaseFast"),
            "--cache-dir", str(directory / "cache"),
            "--prefix", str(directory), f"-j{request.jobs}",
        ]
        # Inherit routing/environment. run_program manages the foreground process group.
        run_program(command, cwd=self.root, env=dict(os.environ))

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action == "doctor":
            return
        if request.action in {"clean", "rebuild"}:
            remove_generated(self.profile_dir(request), self.root)
            if request.action == "clean":
                return
        if request.action in {"build", "rebuild", "dev"}:
            steps = tuple(dict.fromkeys(self.targets[name].step for name in names))
            self.native(request, steps)
        elif request.action in {"check", "test"}:
            self.native(request, (request.action,))
        if request.action in {"run", "dev"}:
            run_program(
                [self.artifact(request, names[0]), *request.arguments],
                cwd=self.root, env=dict(os.environ),
            )
