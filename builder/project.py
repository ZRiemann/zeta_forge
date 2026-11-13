"""Forge's library deliverables and build-tool tests."""

from __future__ import annotations

import argparse
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Mapping

from zeta_forge import native_artifacts, native_publish
from zeta_forge.build_cli import Product, Project, Request, log
from zeta_forge.build_ops import (
    destdir_path,
    remove_generated,
    validate_generated_path,
)
from zeta_forge.cmake_builder import CommonBuildArgs
from zeta_forge.conan_state import writer_lock
from zeta_forge.config import load_repo_config
from zeta_forge.process import require_command, run_command
from zeta_forge.rust_audit import RustDependencyAudit
from zeta_forge.rust_updates import RustUpdates

from builder.deps.project import DepsBuilder
from builder.folly.project import FollyBuilder
from builder.grpc.project import GrpcBuilder
from builder.hpx.project import HpxBuilder
from builder.native_plan import (
    BUILD_ACTIONS,
    NATIVE_ACTIONS,
    TARGETS,
    NativePlan,
    plan_native,
    staging_directory,
)
from builder.nng.project import NngBuilder

ORDER = tuple(target.name for target in TARGETS)
PREREQUISITES = {target.name: target.prerequisites for target in TARGETS}
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

    def plan(self, request: Request, names: tuple[str, ...]) -> NativePlan:
        return plan_native(request, names, self.root, self.config.install_prefix)

    def staging(self, request: Request, name: str) -> Path:
        return staging_directory(self.root, request.profile, name)

    def build_stamp(self, request: Request, name: str) -> Path:
        return self.staging(request, name) / native_artifacts.RECEIPT_NAME

    def require_built(self, request: Request, name: str) -> dict:
        try:
            return native_artifacts.require_receipt(
                self.staging(request, name), name, request.profile
            )
        except (OSError, RuntimeError) as error:
            raise RuntimeError(
                f"{name} has no valid {request.profile} build; "
                f"run ./zbuild.py build {name} --profile {request.profile} first. {error}"
            ) from error

    @contextmanager
    def stage_locks(self, profile: str, names: tuple[str, ...]) -> Iterator[None]:
        with ExitStack() as locks:
            for name in sorted(set(names)):
                path = self.root / "build" / "native-locks" / profile / f"{name}.lock"
                validate_generated_path(path, self.root)
                locks.enter_context(writer_lock(path))
            yield

    def stage_build(self, request: Request, plan: NativePlan, name: str) -> None:
        stage = plan.staging(name)
        validate_generated_path(stage, plan.root)
        self.build_stamp(request, name).unlink(missing_ok=True)
        builder = self.builder(request, name)
        stage.parent.mkdir(parents=True, exist_ok=True)
        dependencies = {
            prerequisite: self.require_built(request, prerequisite)["artifact_id"]
            for prerequisite in PREREQUISITES[name]
        }
        with tempfile.TemporaryDirectory(dir=stage.parent, prefix=f".{name}-") as directory:
            work = Path(directory)
            source = self.root
            rapidjson = None
            if name == "deps":
                candidate = work / "candidate"
                builder.run_conan()
                builder.install(prefix=candidate)
                rapidjson = self.config.source_dir("ZETA_RAPIDJSON_SRC_DIR")
            else:
                destination = work / "destdir"
                candidate = destdir_path(destination, stage)
                builder.build(jobs=plan.jobs)
                builder.install(prefix=stage, destdir=destination)
                source = builder.source_dir
            conan = native_artifacts.conan_identity(
                self.config.forge_root, name, plan.profile, builder.conan_root
            ) if name in plan.conan_scope else None
            references = native_artifacts.external_references(
                candidate, name, plan.native_profile, conan, rapidjson
            )
            sources = [native_artifacts.source_identity(self.root, self.config.env)]
            if source != self.root:
                sources.append(native_artifacts.source_identity(source, self.config.env))
            receipt = native_artifacts.make_receipt(
                candidate, name, plan.profile,
                sources=sources, conan=conan, references=references, dependencies=dependencies,
            )
            native_artifacts.promote(candidate, stage, work / "old-stage", receipt)
            log("I", f"staged target={name} profile={plan.profile} build={receipt['build_id'][:12]}")

    def builder(self, request: Request, name: str):
        prefix = self.staging(request, name)
        environment = dict(self.config.env, ZETA_INSTALL_PREFIX=str(prefix))
        if name == "folly":
            environment["ZETA_GRPC_STAGE"] = str(self.staging(request, "grpc"))
        config = replace(self.config, install_prefix=prefix, env=environment)
        return BUILDERS[name](
            script_path=self.root / "builder" / name / "project.py",
            repo_config=config,
            args=CommonBuildArgs(
                request.cmake_profile,
                jobs=request.jobs,
                refresh_conan_lock=getattr(request.options, "refresh_conan_lock", False),
            ),
        )

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        self.plan(request, names)

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        plan = self.plan(request, names)
        description = dict(
            order=plan.order,
            native_profile=plan.native_profile,
            build_dirs=[str(plan.build_directory(name)) for name in plan.order],
            staging={name: str(plan.staging(name)) for name in plan.order},
            install_prefix=str(plan.install_prefix) if plan.install_prefix is not None else None,
            clean_scope=list(plan.clean_scope),
        )
        if getattr(request.options, "refresh_conan_lock", False):
            description["refresh_conan_lock"] = plan.conan_scope
        return description

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        plan = self.plan(request, names)
        for name in plan.clean_scope:
            validate_generated_path(plan.build_directory(name), plan.root)
            validate_generated_path(plan.staging(name), plan.root)
        if plan.action in {"clean", "install"}:
            return
        require_command("cmake")
        require_command("ninja")
        for name in plan.order:
            self.builder(request, name).validate()
            if name in plan.conan_scope:
                require_command("conan")

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        plan = self.plan(request, names)
        if plan.install_prefix is not None:
            with self.stage_locks(plan.profile, plan.publish_scope):
                native_publish.publish(
                    plan.install_prefix, plan.root,
                    tuple((name, plan.profile, plan.staging(name)) for name in plan.publish_scope),
                )
            return
        with self.stage_locks(plan.profile, (*plan.clean_scope, *plan.build_scope)):
            for name in plan.clean_scope:
                remove_generated(plan.build_directory(name), plan.root)
                remove_generated(plan.staging(name), plan.root)
            for name in plan.build_scope:
                self.stage_build(request, plan, name)


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


def validate_options(request: Request, selected: tuple[Product, ...]) -> None:
    if not request.options.refresh_conan_lock:
        return
    conan_targets = {target.name for target in TARGETS if target.uses_conan}
    if request.action not in BUILD_ACTIONS or not any(
        product.name in conan_targets for product in selected
    ):
        raise ValueError("--refresh-conan-lock requires build, check or rebuild with a Conan target")


def project(script_path: Path) -> Project:
    products = tuple(
        Product(
            name,
            "native",
            "Shared dependency environment" if name == "deps" else "Native library",
            NATIVE_ACTIONS,
        )
        for name in ORDER
    )
    products += (Product("build-tools", "tools", "Forge tooling regression tests", ("test",)),)
    products += (Product("rust-deps", "rust-deps", "Rust dependency governance audit", ("doctor", "check")),)
    products += (Product("rust-updates", "rust-updates", "Read-only upstream Rust upgrade candidates", ("check",)),)
    return Project(
        "zeta_forge",
        products,
        {"native": ForgeEngine(script_path), "tools": ToolTests(script_path.parent),
         "rust-deps": RustDependencyAudit(load_repo_config(script_path)),
         "rust-updates": RustUpdates(load_repo_config(script_path))},
        ORDER,
        action_defaults={"test": ("build-tools",), "install": ()},
        add_options=add_native_options,
        validate_request=validate_options,
    )
