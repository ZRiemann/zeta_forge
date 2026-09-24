"""Cargo workspaces and Dioxus applications using Forge-governed tools."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .build_cli import Request
from .build_ops import remove_generated, run_program, validate_generated_path
from .config import RepoConfig
from .process import require_command, run_command
from .rust_workspace import (
    RustWorkspaceManager,
    _native_environment,
    load_rust_project,
    validate_rust_project,
    workspace_dependencies,
)

RUST_ACTIONS = ("doctor", "check", "build", "rebuild", "test", "run", "dev", "clean", "fetch")
PREPARE_CARGO_TOOLS_ACTIONS = {"check", "build", "rebuild", "test", "dev", "fetch"}


@dataclass(frozen=True)
class RustTarget:
    package: str
    directory: Path
    kind: str = "cargo"
    platforms: tuple[str, ...] = ()


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
            load_rust_project(self.root, self.config.forge_root), self.config
        )

    def platform(self, request: Request, target: RustTarget) -> str | None:
        return getattr(request.options, "platform", None) or (
            target.platforms[0] if target.platforms else None
        )

    def artifact(self, request: Request, name: str) -> Path:
        target = self.targets[name]
        platform = self.platform(request, target)
        if target.kind == "fullstack":
            return self.target_dir / "dx" / target.package / request.profile / "web" / "server"
        if platform and platform != "desktop":
            return self.target_dir / "dx" / target.package / request.profile / platform / "public"
        return self.target_dir / request.profile / target.package

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        platform = getattr(request.options, "platform", None)
        for name in names:
            target = self.targets[name]
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
        }
        if request.action == "metadata":
            project = self.manager().project
            validate_rust_project(project)
            result.update(
                manifest=str(project.manifest),
                toolchain=project.toolchain_spec["channel"],
                native_dependencies=project.native_dependencies,
                capabilities=project.capabilities,
                dependencies={
                    name: value
                    for name, value in workspace_dependencies(project).items()
                    if not isinstance(value, dict) or "path" not in value
                },
            )
        if request.action in {"clean", "rebuild"}:
            result.update(
                clean_scope="workspace/profile (including Dioxus output)",
                affected=list(self.targets),
                clean_paths=[str(path) for path in self.cleanup_paths(request)],
            )
        if request.action in {"check", "test"}:
            result["support_packages"] = self.support_packages
            result["format_scope"] = "entire Cargo workspace (check only)"
        return result

    def environment(self, request: Request, manager: RustWorkspaceManager) -> dict[str, str]:
        environment = _native_environment(manager.project, self.config)
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
        manager = self.manager()
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
            manager.doctor(
                application_tools=any(self.targets[name].kind != "cargo" for name in names),
                check_cargo_tools=request.action not in PREPARE_CARGO_TOOLS_ACTIONS,
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
        manager = self.manager()
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
        for cargo_tool in sorted(cargo_tools):
            manager.ensure_cargo_tool(cargo_tool)

    def cargo(self, manager: RustWorkspaceManager, request: Request, *args: str) -> None:
        run_command(
            manager.cargo_command(*args),
            cwd=manager.project.workspace_dir,
            env=self.environment(request, manager),
        )

    def dx(
        self, manager: RustWorkspaceManager, request: Request, target: RustTarget, action: str
    ) -> None:
        command = [
            manager.require_cargo_tool("dioxus-cli"),
            action,
            "--locked",
            "--package",
            target.package,
        ]
        command.append(f"--{self.platform(request, target) or 'desktop'}")
        if request.profile == "release":
            command.append("--release")
        if target.kind == "fullstack":
            command.extend(("--fullstack", "true", "--force-sequential", "true"))
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
                features = (
                    ("--no-default-features", "--features", "desktop")
                    if platform == "desktop"
                    else ()
                )
                self.cargo(
                    manager,
                    request,
                    "build",
                    "--locked",
                    "--profile",
                    request.cargo_profile,
                    "-p",
                    target.package,
                    *features,
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
            variants: tuple[tuple[str, ...], ...] = ((),)
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
                        "--all-targets",
                        "-p",
                        package,
                        *variant,
                        *flags,
                    )

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action in {"clean", "rebuild"}:
            self.clean(request)
            if request.action == "clean":
                return
        manager = self.manager()
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
            name = names[0]
            artifact = self.artifact(request, name)
            environment = self.environment(request, manager)
            if self.application_environment:
                environment.update(self.application_environment(request, name))
            if self.targets[name].kind == "fullstack":
                environment.setdefault("IP", "127.0.0.1")
                environment.setdefault("PORT", "8080")
                environment.setdefault("DIOXUS_PUBLIC_PATH", str(artifact.parent / "public"))
            if artifact.is_dir():
                run_program(
                    [
                        sys.executable,
                        "-m",
                        "http.server",
                        "8080",
                        "--bind",
                        "127.0.0.1",
                        "--directory",
                        artifact,
                        *request.arguments,
                    ],
                    cwd=artifact,
                    env=environment,
                )
            else:
                run_program([artifact, *request.arguments], cwd=artifact.parent, env=environment)
