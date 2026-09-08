from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
import tomllib
from typing import Mapping, Sequence

from .config import RepoConfig
from .process import run_command


PROJECT_CONFIG_NAME = "zeta-rust.toml"


@dataclass(frozen=True)
class RustBaseline:
    identifier: str
    toolchain: Mapping[str, object]
    dependencies: Mapping[str, object]
    groups: Mapping[str, tuple[str, ...]]
    cargo_tools: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class RustProject:
    root: Path
    manifest: Path
    toolchain: Path
    dependency_groups: tuple[str, ...]
    native_dependencies: tuple[str, ...]
    baseline: RustBaseline

    @property
    def workspace_dir(self) -> Path:
        return self.manifest.parent


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as error:
        raise RuntimeError(f"Required Rust project file is missing: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"Invalid TOML in {path}: {error}") from error


def _mapping(value: object, name: str, path: Path) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected [{name}] table in {path}")
    return value


def _table(document: Mapping[str, object], name: str, path: Path) -> Mapping[str, object]:
    return _mapping(document.get(name), name, path)


def _string_list(value: object, name: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"Expected {name} to be a string array in {path}")
    return tuple(value)


def _project_path(project_root: Path, value: object, name: str, config_path: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Expected {name} to be a non-empty path in {config_path}")
    path = (project_root / value).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as error:
        raise RuntimeError(f"{name} must stay inside the project root: {value}") from error
    return path


def load_baseline(forge_root: Path) -> RustBaseline:
    path = forge_root / "rust" / "baseline.toml"
    document = _read_toml(path)
    baseline = _table(document, "baseline", path)
    identifier = baseline.get("id")
    if baseline.get("schema") != 1 or not isinstance(identifier, str):
        raise RuntimeError(f"Unsupported Rust baseline schema in {path}")

    raw_groups = _table(document, "groups", path)
    groups = {
        name: _string_list(dependencies, f"groups.{name}", path)
        for name, dependencies in raw_groups.items()
    }
    return RustBaseline(
        identifier=identifier,
        toolchain=_table(document, "toolchain", path),
        dependencies=_table(document, "dependencies", path),
        groups=groups,
        cargo_tools={
            name: _mapping(tool, f"cargo-tools.{name}", path)
            for name, tool in _table(document, "cargo-tools", path).items()
        },
    )


def load_rust_project(project_root: Path, forge_root: Path) -> RustProject:
    root = project_root.resolve()
    config_path = root / PROJECT_CONFIG_NAME
    document = _read_toml(config_path)
    if document.get("schema") != 1:
        raise RuntimeError(f"Unsupported Rust project schema in {config_path}")

    baseline = load_baseline(forge_root)
    if document.get("baseline") != baseline.identifier:
        raise RuntimeError(
            f"Rust baseline mismatch in {config_path}: expected {baseline.identifier}, "
            f"got {document.get('baseline')!r}"
        )

    return RustProject(
        root=root,
        manifest=_project_path(root, document.get("manifest"), "manifest", config_path),
        toolchain=_project_path(root, document.get("toolchain"), "toolchain", config_path),
        dependency_groups=_string_list(document.get("dependency-groups"), "dependency-groups", config_path),
        native_dependencies=_string_list(document.get("native-dependencies", []), "native-dependencies", config_path),
        baseline=baseline,
    )


def _selected_dependencies(project: RustProject) -> set[str]:
    selected: set[str] = set()
    for group in project.dependency_groups:
        dependencies = project.baseline.groups.get(group)
        if dependencies is None:
            raise RuntimeError(f"Unknown Rust dependency group {group!r}")
        selected.update(dependencies)
    return selected


def _is_local_dependency(value: object) -> bool:
    return isinstance(value, dict) and "path" in value


def _member_manifests(project: RustProject, workspace: Mapping[str, object]) -> list[Path]:
    members = _string_list(workspace.get("members"), "workspace.members", project.manifest)
    manifests: list[Path] = []
    for member in members:
        if any(character in member for character in "*?["):
            raise RuntimeError(f"Rust workspace members must be explicit paths: {member}")
        manifest = _project_path(project.workspace_dir, f"{member}/Cargo.toml", "workspace member", project.manifest)
        if not manifest.is_file():
            raise RuntimeError(f"Rust workspace member manifest is missing: {manifest}")
        manifests.append(manifest)
    return manifests


def _member_dependency_tables(member: Mapping[str, object], manifest: Path) -> list[tuple[str, Mapping[str, object]]]:
    tables: list[tuple[str, Mapping[str, object]]] = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        dependencies = member.get(section, {})
        if not isinstance(dependencies, dict):
            raise RuntimeError(f"Expected [{section}] table in {manifest}")
        tables.append((section, dependencies))

    targets = member.get("target", {})
    if not isinstance(targets, dict):
        raise RuntimeError(f"Expected [target] table in {manifest}")
    for target_name, target in targets.items():
        if not isinstance(target, dict):
            raise RuntimeError(f"Expected target.{target_name} table in {manifest}")
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            dependencies = target.get(section, {})
            if not isinstance(dependencies, dict):
                raise RuntimeError(f"Expected [target.{target_name}.{section}] table in {manifest}")
            tables.append((f"target.{target_name}.{section}", dependencies))
    return tables


def validate_rust_project(project: RustProject) -> None:
    toolchain = _table(_read_toml(project.toolchain), "toolchain", project.toolchain)
    if dict(toolchain) != dict(project.baseline.toolchain):
        raise RuntimeError(
            f"Rust toolchain does not match forge baseline {project.baseline.identifier}: {project.toolchain}"
        )

    manifest_document = _read_toml(project.manifest)
    workspace = _table(manifest_document, "workspace", project.manifest)
    metadata = _table(workspace, "metadata", project.manifest)
    forge_metadata = _table(metadata, "zeta-forge", project.manifest)
    if forge_metadata.get("baseline") != project.baseline.identifier:
        raise RuntimeError(f"Workspace baseline metadata is missing or stale: {project.manifest}")

    selected = _selected_dependencies(project)
    workspace_dependencies = _table(workspace, "dependencies", project.manifest)
    managed = {name for name, value in workspace_dependencies.items() if not _is_local_dependency(value)}
    if managed != selected:
        missing = ", ".join(sorted(selected - managed)) or "none"
        unexpected = ", ".join(sorted(managed - selected)) or "none"
        raise RuntimeError(
            "Rust workspace dependency set differs from forge baseline; "
            f"missing={missing}; unexpected={unexpected}"
        )
    for name in sorted(selected):
        if workspace_dependencies[name] != project.baseline.dependencies.get(name):
            raise RuntimeError(f"Rust dependency {name!r} differs from forge baseline {project.baseline.identifier}")

    for member_manifest in _member_manifests(project, workspace):
        member = _read_toml(member_manifest)
        for section, dependencies in _member_dependency_tables(member, member_manifest):
            for name, value in dependencies.items():
                if _is_local_dependency(value):
                    continue
                if not isinstance(value, dict) or value.get("workspace") is not True:
                    raise RuntimeError(
                        f"Dependency {name!r} in {member_manifest} must inherit from [workspace.dependencies]"
                    )

    lockfile = project.workspace_dir / "Cargo.lock"
    if not lockfile.is_file():
        raise RuntimeError(f"Committed Cargo lockfile is required: {lockfile}")


def _native_environment(project: RustProject, repo_config: RepoConfig) -> dict[str, str]:
    environment = repo_config.env.copy()
    for dependency in project.native_dependencies:
        if dependency != "nng":
            raise RuntimeError(f"Unsupported native Rust dependency: {dependency}")
        prefix = repo_config.install_prefix
        header = prefix / "include" / "nng" / "nng.h"
        library_candidates = (prefix / "lib" / "libnng.a", prefix / "lib64" / "libnng.a")
        if not header.is_file() or not any(path.is_file() for path in library_candidates):
            raise RuntimeError(
                f"Forge NNG installation is missing at {prefix}; install it with "
                f"{repo_config.forge_root}/zbuild.py nng --install"
            )
        environment.update(NNG_NO_VENDOR="1", NNG_DIR=str(prefix), NNG_STATIC="1")
    return environment


class RustWorkspaceManager:
    def __init__(self, project: RustProject, repo_config: RepoConfig):
        self.project = project
        self.repo_config = repo_config

    def validate(self) -> None:
        validate_rust_project(self.project)

    def doctor(self) -> int:
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
                channel = self.project.baseline.toolchain["channel"]
                raise RuntimeError(f"Rust baseline tool {program} is unavailable for {channel}: {detail}")
        _native_environment(self.project, self.repo_config)
        print(
            f"ZETA_RUST_DONE status=0 baseline={self.project.baseline.identifier} "
            f"manifest={self.project.manifest}"
        )
        return 0

    def tool_command(self, program: str, *arguments: str) -> list[object]:
        rustup = shutil.which("rustup")
        if rustup is None:
            raise RuntimeError("Required Rust toolchain manager not found on PATH: rustup")
        channel = self.project.baseline.toolchain["channel"]
        return [rustup, "run", channel, program, *arguments]

    def tool_version(self, program: str) -> str:
        completed = run_command(
            self.tool_command(program, "--version"),
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Unable to query Rust baseline tool {program}: {detail}")
        return (completed.stdout or completed.stderr).strip()

    def require_cargo_tool(self, name: str) -> str:
        specification = self.project.baseline.cargo_tools.get(name)
        if specification is None:
            raise RuntimeError(f"Unknown forge-managed Cargo tool: {name}")
        executable_name = specification.get("executable")
        expected_version = specification.get("version")
        if not isinstance(executable_name, str) or not isinstance(expected_version, str):
            raise RuntimeError(f"Invalid forge Cargo tool definition: {name}")
        executable = shutil.which(executable_name)
        install = (
            f"cargo +{self.project.baseline.toolchain['channel']} install {name} "
            f"--version {expected_version} --locked"
        )
        if executable is None:
            raise RuntimeError(f"Forge-managed Cargo tool {name} is missing; install it with: {install}")
        completed = run_command(
            [executable, "--version"],
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        version_output = (completed.stdout or completed.stderr).strip()
        version_tokens = {token.lstrip("v") for token in version_output.split()}
        if completed.returncode != 0 or expected_version not in version_tokens:
            raise RuntimeError(
                f"Forge-managed Cargo tool {name} must be version {expected_version}; "
                f"got {version_output or 'unavailable'}. Install it with: {install}"
            )
        return executable

    def cargo_command(self, *arguments: str) -> list[object]:
        cargo_arguments = list(arguments)
        manifest_arguments: list[object] = ["--manifest-path", self.project.manifest]
        command = self.tool_command("cargo")
        if "--" in cargo_arguments:
            separator = cargo_arguments.index("--")
            return [
                *command,
                *cargo_arguments[:separator],
                *manifest_arguments,
                *cargo_arguments[separator:],
            ]
        return [*command, *cargo_arguments, *manifest_arguments]

    def metadata(self) -> int:
        self.validate()
        completed = run_command(
            self.cargo_command("metadata", "--format-version", "1", "--locked", "--offline"),
            cwd=self.project.workspace_dir,
            env=self.repo_config.env,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0 and completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return completed.returncode

    def check(self) -> int:
        self.validate()
        format_result = run_command(
            self.cargo_command("fmt", "--all", "--", "--check"),
            cwd=self.project.workspace_dir,
            check=False,
        )
        if format_result.returncode != 0:
            return format_result.returncode
        environment = _native_environment(self.project, self.repo_config)
        for arguments in (
            ("check", "--workspace", "--all-targets", "--locked"),
            ("clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"),
        ):
            completed = run_command(
                self.cargo_command(*arguments),
                cwd=self.project.workspace_dir,
                env=environment,
                check=False,
            )
            if completed.returncode != 0:
                return completed.returncode
        return 0

    def test(self) -> int:
        self.validate()
        completed = run_command(
            self.cargo_command("test", "--workspace", "--all-targets", "--locked"),
            cwd=self.project.workspace_dir,
            env=_native_environment(self.project, self.repo_config),
            check=False,
        )
        return completed.returncode

    def run(
        self,
        package: str,
        *,
        cargo_arguments: Sequence[str] = (),
        program_arguments: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> int:
        self.validate()
        command = self.cargo_command("run", "--locked", "-p", package, *cargo_arguments)
        if program_arguments:
            command.extend(("--", *program_arguments))
        run_environment = _native_environment(self.project, self.repo_config)
        if environment is not None:
            run_environment.update(environment)
        completed = run_command(command, cwd=self.project.workspace_dir, env=run_environment, check=False)
        return completed.returncode


def build_rust_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./zbuild.py rust",
        description="Validate or build a forge-managed Rust project.",
    )
    parser.add_argument("command", choices=("doctor", "metadata", "check", "test"))
    parser.add_argument("--project-root", required=True, type=Path)
    return parser


def run_rust_cli(argv: Sequence[str], repo_config: RepoConfig) -> int:
    namespace = build_rust_parser().parse_args(argv)
    project = load_rust_project(namespace.project_root, repo_config.forge_root)
    manager = RustWorkspaceManager(project, repo_config)
    if namespace.command == "doctor":
        return manager.doctor()
    if namespace.command == "metadata":
        status = manager.metadata()
    elif namespace.command == "check":
        status = manager.check()
    else:
        status = manager.test()
    print(
        f"ZETA_RUST_DONE status={status} baseline={project.baseline.identifier} "
        f"manifest={project.manifest}"
    )
    return status
