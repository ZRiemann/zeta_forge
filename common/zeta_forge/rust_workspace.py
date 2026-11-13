"""Compatibility facade and execution manager for governed Rust workspaces."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import RepoConfig
from .process import run_command
from .rust_commands import rustup_command, with_manifest
from .rust_environment import native_environment as native_environment
from .rust_governance import locked_dependency_report as locked_dependency_report
from .rust_governance import own_manifests as own_manifests
from .rust_governance import project_cargo_tool
from .rust_governance import validate_rust_project as validate_rust_project
from .rust_governance import workspace_dependencies as workspace_dependencies
from .rust_patches import patch_environment
from .rust_project import PROJECT_CONFIG_NAME as PROJECT_CONFIG_NAME
from .rust_project import RustCatalog as RustCatalog
from .rust_project import RustProject as RustProject
from .rust_project import load_catalog as load_catalog
from .rust_project import load_rust_project as load_rust_project
from .rust_project import load_toolchain as load_toolchain
from .rust_tool_patches import install_repaired_tool, verify_tool_install
from .rust_tool_recipe import tool_repair


class CargoToolUnavailable(RuntimeError):
    """A Forge-managed Cargo tool is absent or has the wrong version."""


class RustWorkspaceManager:
    def __init__(self, project: RustProject, repo_config: RepoConfig):
        self.project = project
        self.repo_config = repo_config

    def validate(self) -> None:
        validate_rust_project(self.project)

    def validate_snapshot(self) -> None:
        current = load_rust_project(self.project.root, self.repo_config.forge_root)
        if current != self.project:
            raise RuntimeError("Rust project or catalog changed during preparation; retry the command")
        self.validate()

    def doctor(
        self, *, application_tools: bool = True, check_cargo_tools: bool = True,
        pending_source: bool = False,
    ) -> int:
        self.validate()
        for program in ("rustc", "cargo", "rustfmt", "clippy-driver"):
            completed = run_command(
                self.tool_command(program, "--version"),
                cwd=self.project.workspace_dir,
                check=False,
                capture_output=True,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                channel = self.project.toolchain_spec["channel"]
                raise RuntimeError(
                    f"Rust toolchain program {program} is unavailable for {channel}: {detail}"
                )
        required_cargo_tools: set[str] = set()
        required_rust_targets: set[str] = set()
        for name in self.project.capabilities if application_tools else ():
            requirements = self.project.catalog.capabilities[name]
            required_cargo_tools.add(str(requirements["cargo-tool"]))
            required_rust_targets.update(requirements.get("rust-targets", ()))
        if check_cargo_tools:
            for cargo_tool in sorted(required_cargo_tools):
                self.require_cargo_tool(cargo_tool)
        for rust_target in sorted(required_rust_targets):
            self.require_rust_target(rust_target)
        native_environment(self.project, self.repo_config, pending_source=pending_source)
        print(
            f"ZETA_RUST_DONE status=0 toolchain={self.project.toolchain_spec['channel']} "
            f"manifest={self.project.manifest}"
        )
        return 0

    def tool_command(self, program: str, *arguments: str) -> list[object]:
        return rustup_command(self.project, program, *arguments)

    def tool_version(self, program: str) -> str:
        completed = run_command(
            self.tool_command(program, "--version"),
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Unable to query Rust toolchain program {program}: {detail}")
        return (completed.stdout or completed.stderr).strip()

    def cargo_tool_root(self, name: str, version: str) -> Path:
        root = (
            self.repo_config.install_prefix
            / "share"
            / "zeta_forge"
            / "rust-tools"
            / str(self.project.toolchain_spec["channel"])
            / name
            / version
        )
        specification = next(
            (item for item in self.project.catalog.cargo_tools.get(name, ())
             if item["version"] == version), None,
        )
        repair = tool_repair(self.repo_config.forge_root, specification) if specification else None
        return root / f"source-{repair.identity}" if repair else root

    def require_cargo_tool(self, name: str) -> str:
        if name not in self.project.catalog.cargo_tools:
            raise RuntimeError(f"Unknown forge-managed Cargo tool: {name}")
        specification = project_cargo_tool(self.project, name)
        executable_name = specification.get("executable")
        expected_version = specification.get("version")
        if not isinstance(executable_name, str) or not isinstance(expected_version, str):
            raise RuntimeError(f"Invalid forge Cargo tool definition: {name}")
        tool_root = self.cargo_tool_root(name, expected_version)
        executable = tool_root / "bin" / executable_name
        install = (
            f"cargo +{self.project.toolchain_spec['channel']} install {name} "
            f"--version {expected_version} --locked --force --root {tool_root}"
        )
        repair = tool_repair(self.repo_config.forge_root, specification)
        if repair:
            install = f"./zbuild.py fetch <target> (builds the Forge-approved repaired {name})"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise CargoToolUnavailable(
                f"Forge-managed Cargo tool {name} is missing or not executable at {executable}; "
                f"install it with: {install}"
            )
        if repair:
            try:
                verify_tool_install(repair, str(self.project.toolchain_spec["channel"]), executable)
            except RuntimeError as error:
                raise CargoToolUnavailable(f"{error}; install it with: {install}") from error
        completed = run_command(
            [executable, "--version"],
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        version_output = (completed.stdout or completed.stderr).strip()
        version_tokens = {token.lstrip("v") for token in version_output.split()}
        if completed.returncode != 0 or expected_version not in version_tokens:
            raise CargoToolUnavailable(
                f"Forge-managed Cargo tool {name} must be version {expected_version}; "
                f"got {version_output or 'unavailable'}. Install it with: {install}"
            )
        return str(executable)

    def ensure_cargo_tool(self, name: str) -> str:
        try:
            return self.require_cargo_tool(name)
        except CargoToolUnavailable:
            pass
        specification = project_cargo_tool(self.project, name)
        expected_version = str(specification["version"])
        tool_root = self.cargo_tool_root(name, expected_version)
        cargo = shutil.which("cargo")
        if cargo is None:
            raise RuntimeError("Required command not found on PATH: cargo")
        repair = tool_repair(self.repo_config.forge_root, specification)
        if repair:
            install_repaired_tool(
                repair, str(self.project.toolchain_spec["channel"]),
                str(specification["executable"]), tool_root, cargo,
                self.repo_config.forge_root, self.repo_config.env,
            )
            return self.require_cargo_tool(name)
        run_command(
            [
                cargo,
                f"+{self.project.toolchain_spec['channel']}",
                "install",
                name,
                "--version",
                expected_version,
                "--locked",
                "--force",
                "--root",
                tool_root,
            ],
            cwd=self.project.workspace_dir,
            env=self.repo_config.env,
        )
        return self.require_cargo_tool(name)

    def require_rust_target(self, target: str) -> None:
        rustup = shutil.which("rustup")
        if rustup is None:
            raise RuntimeError("Required Rust toolchain manager not found on PATH: rustup")
        channel = self.project.toolchain_spec["channel"]
        completed = run_command(
            [rustup, "target", "list", "--toolchain", channel, "--installed"],
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        installed = (
            set((completed.stdout or "").splitlines()) if completed.returncode == 0 else set()
        )
        if target not in installed:
            raise RuntimeError(
                f"Rust target {target} is required for toolchain {channel}; "
                f"install it with: rustup target add {target} --toolchain {channel}"
            )

    def cargo_command(self, *arguments: str) -> list[object]:
        return with_manifest(self.tool_command("cargo"), self.project.manifest, arguments)

    def metadata(self) -> int:
        self.validate()
        completed = run_command(
            self.cargo_command("metadata", "--format-version", "1", "--locked", "--offline"),
            cwd=self.project.workspace_dir,
            env={**self.repo_config.env, **patch_environment(self.project, self.repo_config)},
            check=False,
            capture_output=True,
        )
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.returncode != 0 and completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return completed.returncode


# Older callers used this spelling before the native environment had a public name.
_native_environment = native_environment
