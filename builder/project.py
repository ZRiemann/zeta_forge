"""Forge's library deliverables and build-tool tests."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from zeta_forge.build_cli import Product, Project, Request, log
from zeta_forge.build_ops import (
    relocate_installed_metadata,
    remove_generated,
    validate_generated_path,
)
from zeta_forge.cmake_builder import CommonBuildArgs
from zeta_forge.config import load_repo_config
from zeta_forge.process import require_command, run_command

from builder.deps.project import DepsBuilder
from builder.folly.project import FollyBuilder
from builder.grpc.project import GrpcBuilder
from builder.hpx.project import HpxBuilder
from builder.nng.project import NngBuilder

ORDER = ("deps", "grpc", "hpx", "folly", "nng")
BUILDERS = {
    "deps": DepsBuilder,
    "grpc": GrpcBuilder,
    "hpx": HpxBuilder,
    "folly": FollyBuilder,
    "nng": NngBuilder,
}


class ForgeEngine:
    def __init__(self, script_path: Path) -> None:
        self.root = script_path.resolve().parent
        self.config = load_repo_config(script_path)

    def selected(self, names: tuple[str, ...], action: str) -> tuple[str, ...]:
        selected = set(names)
        if "folly" in names and action in {"build", "rebuild", "check", "doctor"}:
            selected.add("grpc")
        return tuple(name for name in ORDER if name in selected)

    def staging(self, request: Request, name: str) -> Path:
        return self.root / "build" / "staging" / request.profile / name

    def build_stamp(self, request: Request, name: str) -> Path:
        return self.staging(request, name) / ".zeta-build-complete.json"

    def require_built(self, request: Request, name: str) -> None:
        stamp = self.build_stamp(request, name)
        expected = {"target": name, "profile": request.profile}
        try:
            actual = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            actual = None
        if actual != expected or not self.builder(request, name).build_dir.is_dir():
            raise RuntimeError(
                f"{name} has no successful {request.profile} build; "
                f"run ./zbuild.py build {name} --profile {request.profile} first"
            )

    def mark_built(self, request: Request, name: str) -> None:
        stamp = self.build_stamp(request, name)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"target": name, "profile": request.profile}) + "\n", encoding="utf-8"
        )
        temporary.replace(stamp)

    def builder(self, request: Request, name: str):
        prefix = self.staging(request, name)
        environment = dict(self.config.env, ZETA_INSTALL_PREFIX=str(prefix))
        if name == "folly":
            environment["ZETA_GRPC_STAGE"] = str(self.staging(request, "grpc"))
        config = replace(self.config, install_prefix=prefix, env=environment)
        return BUILDERS[name](
            script_path=self.root / "builder" / name / "project.py",
            repo_config=config,
            args=CommonBuildArgs(request.cmake_profile),
        )

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        order = self.selected(names, request.action)
        return dict(
            order=order,
            native_profile=request.cmake_profile,
            build_dirs=[str(self.builder(request, name).build_dir) for name in order],
            staging={name: str(self.staging(request, name)) for name in order},
            install_prefix=str(self.config.install_prefix) if request.action == "install" else None,
            clean_scope=list(names) if request.action in {"clean", "rebuild"} else [],
        )

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action in {"clean", "rebuild"}:
            for name in names:
                validate_generated_path(self.builder(request, name).build_dir, self.root)
                validate_generated_path(self.staging(request, name), self.root)
        if request.action == "clean":
            return
        if request.action == "install":
            if any(name != "deps" for name in names):
                require_command("cmake")
            for name in names:
                self.require_built(request, name)
            return
        require_command("cmake")
        require_command("ninja")
        for name in self.selected(names, request.action):
            self.builder(request, name).validate()
            if name in {"deps", "hpx", "folly"}:
                require_command("conan")

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action == "doctor":
            return
        if request.action == "install":
            for name in self.selected(names, request.action):
                builder = self.builder(request, name)
                log(
                    "I",
                    f"publish target={name} profile={request.profile} "
                    f"prefix={self.config.install_prefix}",
                )
                if name == "deps":
                    builder.publish(self.config.install_prefix)
                else:
                    builder.install(prefix=self.config.install_prefix)
                    relocate_installed_metadata(
                        builder.build_dir / "install_manifest.txt",
                        self.staging(request, name),
                        self.config.install_prefix,
                    )
                log("I", f"published target={name}")
            return
        if request.action in {"clean", "rebuild"}:
            for name in names:
                remove_generated(self.builder(request, name).build_dir, self.root)
                remove_generated(self.staging(request, name), self.root)
            if request.action == "clean":
                return
        for name in self.selected(names, request.action):
            self.build_stamp(request, name).unlink(missing_ok=True)
            builder = self.builder(request, name)
            if name == "deps":
                builder.run_conan()
                builder.install()
            else:
                builder.build(jobs=request.jobs)
                builder.install(prefix=self.staging(request, name))
            self.mark_built(request, name)


class ToolTests:
    def __init__(self, root: Path) -> None:
        self.root = root

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        return {"test_scope": "Forge Python build-tool tests, not upstream library suites"}

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=self.root)


def project(script_path: Path) -> Project:
    actions = ("doctor", "check", "build", "rebuild", "clean", "install")
    products = tuple(
        Product(
            name,
            "native",
            "Shared dependency environment" if name == "deps" else "Native library",
            actions,
        )
        for name in ORDER
    )
    products += (Product("build-tools", "tools", "Forge tooling regression tests", ("test",)),)
    return Project(
        "zeta_forge",
        products,
        {"native": ForgeEngine(script_path), "tools": ToolTests(script_path.parent)},
        ORDER,
        action_defaults={"test": ("build-tools",), "install": ()},
    )
