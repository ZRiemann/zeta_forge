"""Forge's library deliverables and build-tool tests."""

from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path
from typing import Mapping

from zeta_forge.build_contract import Product, Project, Request
from zeta_forge.config import load_repo_config
from zeta_forge.process import run_command
from zeta_forge.rust_audit import RustDependencyAudit
from zeta_forge.rust_updates import RustUpdates

from builder.deps.project import DepsBuilder
from builder.folly.project import FollyBuilder
from builder.grpc.project import GrpcBuilder
from builder.hpx.project import HpxBuilder
from builder.native_engine import (
    NativeEngine,
    NativeOptions,
    NativeTarget,
    stage_conan_packages,
)
from builder.native_plan import BUILD_ACTIONS, NATIVE_ACTIONS, TargetSpec
from builder.nng.project import NngBuilder

NATIVE_TARGETS = (
    NativeTarget(
        TargetSpec("deps", uses_conan=True), DepsBuilder, stage_conan_packages,
        description="Shared dependency environment",
    ),
    NativeTarget(TargetSpec("grpc"), GrpcBuilder),
    NativeTarget(TargetSpec("hpx", uses_conan=True), HpxBuilder),
    NativeTarget(
        TargetSpec("folly", prerequisites=("grpc",), uses_conan=True), FollyBuilder,
        staged_environment=(("ZETA_GRPC_STAGE", "grpc"),),
    ),
    NativeTarget(TargetSpec("nng"), NngBuilder),
)


class ForgeEngine(NativeEngine):
    def __init__(
        self, script_path: Path, targets: tuple[NativeTarget, ...] = NATIVE_TARGETS,
    ) -> None:
        super().__init__(script_path, targets)


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


def add_native_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh-conan-lock", action="store_true",
        help="resolve a fresh Conan lock for selected build, check or rebuild targets",
    )


def validate_options(
    request: Request, selected: tuple[Product, ...], *, conan_targets: frozenset[str],
) -> None:
    if not NativeOptions.from_request(request).refresh_conan_lock:
        return
    if request.action not in BUILD_ACTIONS or not any(
        product.name in conan_targets for product in selected
    ):
        raise ValueError("--refresh-conan-lock requires build, check or rebuild with a Conan target")


def project(
    script_path: Path, *, targets: tuple[NativeTarget, ...] = NATIVE_TARGETS,
) -> Project:
    engine = ForgeEngine(script_path, targets)
    products = tuple(
        Product(
            registration.spec.name,
            "native",
            registration.description,
            NATIVE_ACTIONS,
        )
        for registration in targets
    )
    products += (Product("build-tools", "tools", "Forge tooling regression tests", ("test",)),)
    products += (Product("rust-deps", "rust-deps", "Rust dependency governance audit", ("doctor", "check")),)
    products += (Product("rust-updates", "rust-updates", "Read-only upstream Rust upgrade candidates", ("check",)),)
    return Project(
        "zeta_forge",
        products,
        {"native": engine, "tools": ToolTests(script_path.parent),
         "rust-deps": RustDependencyAudit(load_repo_config(script_path)),
         "rust-updates": RustUpdates(load_repo_config(script_path))},
        tuple(registration.spec.name for registration in targets),
        action_defaults={"test": ("build-tools",), "install": ()},
        add_options=add_native_options,
        validate_request=partial(
            validate_options, conan_targets=frozenset(
                target.spec.name for target in targets if target.spec.uses_conan
            ),
        ),
    )
