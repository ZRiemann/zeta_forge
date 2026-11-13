"""Cargo workspaces and Dioxus applications using Forge-governed tools."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from .build_contract import Request
from .build_invocation import invocation_snapshot
from .build_ops import remove_generated, run_program, validate_generated_path
from .config import RepoConfig
from .process import require_command, run_command
from .run_spec import RunSpec, launch_run
from .rust_environment import native_environment
from .rust_governance import (
    locked_dependency_report,
    validate_rust_project,
    workspace_dependencies,
)
from .rust_patches import validate_patch_inputs
from .rust_project import load_rust_project
from .rust_variants import RustTarget as RustTarget
from .rust_variants import cargo_build_flags, dioxus_command
from .rust_workspace import RustWorkspaceManager

RUST_ACTIONS = ("doctor", "check", "build", "rebuild", "test", "run", "dev", "clean", "fetch")
PREPARE_CARGO_TOOLS_ACTIONS = {"check", "build", "rebuild", "test", "dev", "fetch"}


@dataclass(frozen=True)
class RustOptions:
    platform: str | None

    @classmethod
    def from_request(cls, request: Request) -> RustOptions:
        platform = getattr(request.options, "platform", None)
        if platform is not None and not isinstance(platform, str):
            raise ValueError("Rust platform must be a string")
        return cls(platform)


class RustEngine:
    def __init__(
        self,
        root: Path,
        config: RepoConfig,
        targets: Mapping[str, RustTarget],
        target_dir: Path,
        *,
        support_packages: tuple[str, ...] = (),
        prepare_assets: Callable[[Request, tuple[str, ...]], None] | None = None,
        validate_assets: Callable[[Request, tuple[str, ...]], None] | None = None,
        application_environment: Callable[[Request, str], dict[str, str]] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.targets = targets
        self.target_dir = target_dir
        self.support_packages = support_packages
        self.prepare_assets = prepare_assets
        self.validate_assets = validate_assets
        self.application_environment = application_environment

    def manager(self) -> RustWorkspaceManager:
        return RustWorkspaceManager(
            load_rust_project(self.root, self.config.forge_root),
            replace(self.config, env=dict(self.config.env)),
        )

    def invocation_manager(self) -> RustWorkspaceManager:
        return invocation_snapshot(self, self.manager)

    def platform(self, request: Request, target: RustTarget) -> str | None:
        return invocation_snapshot((self, "options"), lambda: RustOptions.from_request(request)).platform or (
            target.platforms[0] if target.platforms else None
        )

    def artifact(self, request: Request, name: str) -> Path:
        target = self.targets[name]
        platform = self.platform(request, target)
        if target.example is not None:
            suffix = ".exe" if sys.platform == "win32" else ""
            return self.target_dir / request.profile / "examples" / f"{target.example}{suffix}"
        if target.kind == "fullstack":
            return self.target_dir / "dx" / target.package / request.profile / "web" / "server"
        if platform and platform != "desktop":
            return self.target_dir / "dx" / target.package / request.profile / platform / "public"
        return self.target_dir / request.profile / (target.artifact_name or target.package)

    def resolve_run(
        self, request: Request, name: str, *, manager: RustWorkspaceManager | None = None,
    ) -> RunSpec:
        artifact = self.artifact(request, name)
        target = self.targets[name]
        environment = self.environment(request, manager or self.manager())
        if self.application_environment:
            environment.update(self.application_environment(request, name))
        if target.kind == "fullstack":
            environment.setdefault("IP", "127.0.0.1")
            environment.setdefault("PORT", "8080")
            environment.setdefault("DIOXUS_PUBLIC_PATH", str(artifact.parent / "public"))
        if artifact.is_dir():
            return RunSpec(
                Path(sys.executable),
                ("-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory",
                 str(artifact), *request.arguments), artifact, environment,
            )
        directory = target.directory if target.example is not None else artifact.parent
        return RunSpec(artifact, request.arguments, directory, environment)

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        platform = invocation_snapshot((self, "options"), lambda: RustOptions.from_request(request)).platform
        for name in names:
            target = self.targets[name]
            if target.example is not None:
                if (
                    not target.example.strip()
                    or Path(target.example).name != target.example
                    or target.example in {".", ".."}
                    or "\\" in target.example
                    or ":" in target.example
                ):
                    raise ValueError("example must be a nonempty Cargo target name, not a path")
                if target.kind != "cargo" or target.platforms or target.artifact_name is not None:
                    raise ValueError("example requires a plain Cargo target without artifact_name")
            if target.artifact_name is not None and (
                not target.artifact_name
                or Path(target.artifact_name).name != target.artifact_name
                or target.artifact_name in {".", ".."}
                or "\\" in target.artifact_name
            ):
                raise ValueError("artifact_name must be a single output filename")
            if (target.kind != "cargo" or target.platforms) and (target.artifact_name or target.features):
                raise ValueError("artifact_name and features require a plain Cargo target")
            if any(not feature or feature.startswith("-") for feature in target.features):
                raise ValueError("Cargo target features must be nonempty feature names")
            if platform is not None and platform not in target.platforms:
                raise ValueError(f"platform {platform} is not supported by {name}")
            if (
                request.action == "test"
                and target.kind != "fullstack"
                and self.platform(request, target) in {"web", "android", "ios"}
            ):
                raise ValueError("selected platform has no host test harness")
            if request.action == "run" and self.platform(request, target) in {"android", "ios"}:
                raise ValueError("mobile delivery requires an explicit device workflow, not run")

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        result: dict[str, object] = {
            "native_engine": "cargo/dioxus",
            "target_dir": str(self.target_dir),
            "native_profile": request.cargo_profile,
            "artifacts": {name: str(self.artifact(request, name)) for name in names},
            "features": {name: list(self.targets[name].features) for name in names},
            "examples": {
                name: self.targets[name].example
                for name in names if self.targets[name].example is not None
            },
            "run_directories": {
                name: str(self.targets[name].directory)
                for name in names if self.targets[name].example is not None
            },
        }
        if request.action == "metadata":
            project = self.manager().project
            validate_rust_project(project)
            result.update(
                manifest=str(project.manifest),
                toolchain=project.toolchain_spec["channel"],
                native_dependencies=project.native_dependencies,
                source_patches=project.source_patches,
                capabilities=project.capabilities,
                dependencies={
                    name: value
                    for name, value in workspace_dependencies(project).items()
                    if not isinstance(value, dict) or "path" not in value
                },
                locked_dependencies=locked_dependency_report(project),
            )
        if request.action in {"clean", "rebuild"}:
            result.update(
                clean_scope="workspace/profile (including Dioxus output)",
                affected=list(self.targets),
                affected_artifacts={
                    name: str(self.artifact(request, name)) for name in self.targets
                },
                clean_paths=[str(path) for path in self.cleanup_paths(request)],
            )
        if request.action in {"check", "test"}:
            result["support_packages"] = self.support_packages
            result["format_scope"] = "entire Cargo workspace (check only)"
        return result

    def environment(self, request: Request, manager: RustWorkspaceManager) -> dict[str, str]:
        environment = native_environment(manager.project, manager.repo_config)
        environment.update(
            CARGO_TARGET_DIR=str(self.target_dir),
            CARGO_BUILD_JOBS=str(request.jobs),
            RUSTUP_TOOLCHAIN=str(manager.project.toolchain_spec["channel"]),
        )
        return environment

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        if not self.target_dir.resolve().is_relative_to(self.root.resolve()):
            raise RuntimeError("Cargo target directory must belong to the project")
        if request.action in {"clean", "rebuild"}:
            for path in self.cleanup_paths(request):
                validate_generated_path(path, self.root)
        if request.action == "clean":
            return
        manager = self.invocation_manager()
        manager.validate()
        for name in names:
            target = self.targets[name]
            if target.kind == "cargo":
                continue
            platform = self.platform(request, target)
            capability = (
                "dioxus-web-fullstack" if platform == "web" else "dioxus-desktop"
            )
            if capability not in manager.project.capabilities:
                raise RuntimeError(
                    f"Rust target {name!r} requires capability {capability!r}"
                )
        if request.action == "run":
            artifact = self.artifact(request, names[0])
            if not artifact.exists():
                raise RuntimeError(f"built artifact is missing: {artifact}")
            if self.application_environment:
                self.application_environment(request, names[0])
        else:
            doctor_options = {"pending_source": True} if request.action == "fetch" else {}
            manager.doctor(
                application_tools=any(self.targets[name].kind != "cargo" for name in names),
                check_cargo_tools=request.action not in PREPARE_CARGO_TOOLS_ACTIONS,
                **doctor_options,
            )
            if (
                request.action != "fetch"
                and sys.platform.startswith("linux")
                and any(
                    self.targets[name].kind != "cargo"
                    and self.platform(request, self.targets[name]) == "desktop"
                    for name in names
                )
            ):
                pkg_config = require_command("pkg-config")
                run_command(
                    [pkg_config, "--exists", "webkit2gtk-4.1", "libsoup-3.0"], env=self.config.env
                )
        if self.validate_assets:
            self.validate_assets(request, names)

    def prepare(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action not in PREPARE_CARGO_TOOLS_ACTIONS:
            return
        manager = self.invocation_manager()
        if request.action == "fetch":
            from .rust_patches import prepare_patches

            validate_patch_inputs(manager.project, manager.repo_config)
            prepare_patches(manager.project, manager.repo_config)
        for cargo_tool in self.cargo_tools(manager, request, names):
            manager.ensure_cargo_tool(cargo_tool)
        manager.validate_snapshot()
        self.environment(request, manager)

    def cargo_tools(
        self, manager: RustWorkspaceManager, request: Request, names: tuple[str, ...],
    ) -> tuple[str, ...]:
        cargo_tools: set[str] = set()
        for name in names:
            target = self.targets[name]
            if target.kind == "cargo":
                continue
            capability = (
                "dioxus-web-fullstack" if self.platform(request, target) == "web"
                else "dioxus-desktop"
            )
            cargo_tools.add(str(manager.project.catalog.capabilities[capability]["cargo-tool"]))
        return tuple(sorted(cargo_tools))

    def cargo(self, manager: RustWorkspaceManager, request: Request, *args: str) -> None:
        run_command(
            manager.cargo_command(*args),
            cwd=manager.project.workspace_dir,
            env=self.environment(request, manager),
        )

    def dx(
        self, manager: RustWorkspaceManager, request: Request, target: RustTarget, action: str
    ) -> None:
        command = dioxus_command(
            manager.require_cargo_tool("dioxus-cli"), request, target,
            self.platform(request, target), action,
        )
        if action == "serve":
            run_program(command, cwd=target.directory, env=self.environment(request, manager))
        else:
            run_command(command, cwd=target.directory, env=self.environment(request, manager))

    def cleanup_paths(self, request: Request) -> tuple[Path, ...]:
        native = "dev" if request.profile == "debug" else "release"
        profiles = (request.profile, f"server-{native}", f"wasm-{native}")
        paths = [self.target_dir / profile for profile in profiles]
        # Cargo places cross-compiled outputs under a target-triple directory.
        # Enumerate only project-owned native output parents, never source trees.
        if self.target_dir.is_dir():
            for directory in sorted(self.target_dir.iterdir()):
                if directory.is_dir() and directory.name not in {*profiles, "dx"}:
                    paths.extend(
                        directory / profile
                        for profile in profiles
                        if (directory / profile).exists()
                    )
        paths.extend(
            self.target_dir / "dx" / target.package / request.profile
            for target in self.targets.values()
        )
        return tuple(dict.fromkeys(paths))

    def clean(self, request: Request) -> None:
        paths = self.cleanup_paths(request)
        for path in paths:
            validate_generated_path(path, self.root)
        for path in paths:
            remove_generated(path, self.root)

    def build(
        self, manager: RustWorkspaceManager, request: Request, names: tuple[str, ...]
    ) -> None:
        for name in names:
            target = self.targets[name]
            platform = self.platform(request, target)
            if target.kind == "fullstack" or platform not in {None, "desktop"}:
                self.dx(manager, request, target, "build")
            else:
                self.cargo(
                    manager,
                    request,
                    "build",
                    "--locked",
                    "--profile",
                    request.cargo_profile,
                    "-p",
                    target.package,
                    *cargo_build_flags(target, platform),
                )

    def validate_code(
        self, manager: RustWorkspaceManager, request: Request, names: tuple[str, ...]
    ) -> None:
        if request.action == "check":
            self.cargo(manager, request, "fmt", "--all", "--", "--check")
        selected = [(self.targets[name].package, self.targets[name]) for name in names]
        selected.extend(
            (name, None) for name in self.support_packages if name not in {p for p, _ in selected}
        )
        for package, target in selected:
            selector = (
                ("--example", target.example)
                if target and target.example is not None else ("--all-targets",)
            )
            variants: tuple[tuple[str, ...], ...] = ((),)
            if target and target.features:
                variants = (("--features", ",".join(target.features)),)
            if target and target.kind == "fullstack":
                variants = (("--no-default-features", "--features", "server"),)
                if request.action == "check":
                    variants += (
                        (
                            "--no-default-features",
                            "--features",
                            "web",
                            "--target",
                            "wasm32-unknown-unknown",
                        ),
                    )
            elif target and self.platform(request, target):
                variants = (
                    ("--no-default-features", "--features", self.platform(request, target)),
                )
                if self.platform(request, target) == "web":
                    if request.action == "test":
                        raise RuntimeError(
                            "web-only tests require a browser harness; host tests use the desktop target"
                        )
                    variants = (variants[0] + ("--target", "wasm32-unknown-unknown"),)
            for variant in variants:
                for action in ("check", "clippy") if request.action == "check" else ("test",):
                    flags = ("--", "-D", "warnings") if action == "clippy" else ()
                    self.cargo(
                        manager,
                        request,
                        action,
                        "--locked",
                        "--profile",
                        request.cargo_profile,
                        *selector,
                        "-p",
                        package,
                        *variant,
                        *flags,
                    )

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action == "clean":
            self.clean(request)
            return
        manager = self.invocation_manager()
        if request.action == "rebuild":
            manager.validate_snapshot()
            self.environment(request, manager)
            for cargo_tool in self.cargo_tools(manager, request, names):
                manager.require_cargo_tool(cargo_tool)
            self.clean(request)
        if request.action == "doctor":
            return
        if request.action == "fetch":
            self.cargo(manager, request, "fetch", "--locked")
        if self.prepare_assets and request.action in {
            "build",
            "rebuild",
            "check",
            "test",
            "dev",
            "fetch",
        }:
            self.prepare_assets(request, names)
        if request.action in {"check", "test"}:
            self.validate_code(manager, request, names)
        elif request.action in {"build", "rebuild"}:
            self.build(manager, request, names)
        elif request.action == "dev":
            target = self.targets[names[0]]
            if target.kind != "cargo":
                self.dx(manager, request, target, "serve")
                return
            self.build(manager, request, names)
        if request.action in {"run", "dev"}:
            launch_run(self.resolve_run(request, names[0], manager=manager))
