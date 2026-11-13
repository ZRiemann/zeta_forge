"""Execute Forge native plans and own staging before publication."""

from __future__ import annotations

import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, cast

from zeta_forge import native_artifacts, native_publish
from zeta_forge.build_contract import Request
from zeta_forge.build_log import log
from zeta_forge.build_ops import destdir_path, remove_generated, validate_generated_path
from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs
from zeta_forge.config import RepoConfig, load_repo_config
from zeta_forge.process import require_command
from zeta_forge.state_io import writer_lock

from builder.native_plan import NativePlan, TargetSpec, plan_native, staging_directory


class NativeBuilder(Protocol):
    conan_root: Path
    repo_config: RepoConfig

    def validate(self) -> None: ...


class ConanPackageBuilder(NativeBuilder, Protocol):
    def run_conan(self) -> None: ...
    def install(self, *, prefix: Path | None = None) -> None: ...


def stage_source(
    builder: NativeBuilder, plan: NativePlan, stage: Path, work: Path,
) -> tuple[Path, Path, Path | None]:
    source_builder = cast(CMakeProjectBuilder, builder)
    destination = work / "destdir"
    candidate = destdir_path(destination, stage)
    source_builder.build(jobs=plan.jobs)
    source_builder.install(prefix=stage, destdir=destination)
    return candidate, source_builder.source_dir, None


def stage_conan_packages(
    builder: NativeBuilder, plan: NativePlan, stage: Path, work: Path,
) -> tuple[Path, Path, Path | None]:
    package_builder = cast(ConanPackageBuilder, builder)
    candidate = work / "candidate"
    package_builder.run_conan()
    package_builder.install(prefix=candidate)
    return candidate, plan.root, builder.repo_config.source_dir("ZETA_RAPIDJSON_SRC_DIR")


@dataclass(frozen=True)
class NativeTarget:
    spec: TargetSpec
    builder: Callable[..., NativeBuilder]
    stage: Callable[[NativeBuilder, NativePlan, Path, Path], tuple[Path, Path, Path | None]] = stage_source
    staged_environment: tuple[tuple[str, str], ...] = ()
    description: str = "Native library"


@dataclass(frozen=True)
class NativeOptions:
    refresh_conan_lock: bool

    @classmethod
    def from_request(cls, request: Request) -> NativeOptions:
        return cls(bool(getattr(request.options, "refresh_conan_lock", False)))


class NativeEngine:
    def __init__(self, script_path: Path, registrations: tuple[NativeTarget, ...]) -> None:
        self.root = script_path.resolve().parent
        self.config = load_repo_config(script_path)
        self.targets = tuple(registration.spec for registration in registrations)
        self.registrations = {registration.spec.name: registration for registration in registrations}

    def plan(self, request: Request, names: tuple[str, ...]) -> NativePlan:
        return plan_native(request, names, self.root, self.config.install_prefix, targets=self.targets)

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
        registration = self.registrations[name]
        builder = self.builder(request, name)
        stage.parent.mkdir(parents=True, exist_ok=True)
        dependencies = {
            prerequisite: self.require_built(request, prerequisite)["artifact_id"]
            for prerequisite in registration.spec.prerequisites
        }
        with tempfile.TemporaryDirectory(dir=stage.parent, prefix=f".{name}-") as directory:
            work = Path(directory)
            candidate, source, rapidjson = registration.stage(builder, plan, stage, work)
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

    def builder(self, request: Request, name: str) -> NativeBuilder:
        prefix = self.staging(request, name)
        environment = dict(self.config.env, ZETA_INSTALL_PREFIX=str(prefix))
        registration = self.registrations[name]
        for key, dependency in registration.staged_environment:
            environment[key] = str(self.staging(request, dependency))
        config = replace(self.config, install_prefix=prefix, env=environment)
        return registration.builder(
            script_path=self.root / "builder" / name / "project.py",
            repo_config=config,
            args=CommonBuildArgs(
                request.cmake_profile,
                jobs=request.jobs,
                refresh_conan_lock=NativeOptions.from_request(request).refresh_conan_lock,
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
        if NativeOptions.from_request(request).refresh_conan_lock:
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
