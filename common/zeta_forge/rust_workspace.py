from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Mapping, Sequence

from .config import RepoConfig
from .conan_openssl import read_openssl_manifest
from .process import run_command


PROJECT_CONFIG_NAME = "zeta-rust.toml"
BASELINE_ID_PATTERN = re.compile(
    r"^(?P<rust_version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"-r(?P<revision>[1-9][0-9]*)$"
)


@dataclass(frozen=True)
class RustBaseline:
    identifier: str
    rust_version: str
    revision: int
    released: date
    toolchain: Mapping[str, object]
    dependencies: Mapping[str, object]
    groups: Mapping[str, tuple[str, ...]]
    group_requirements: Mapping[str, Mapping[str, tuple[str, ...]]]
    cargo_tools: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class RustProject:
    root: Path
    manifest: Path
    toolchain: Path
    default_profile: str
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


def _profile_name(value: object, name: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Expected {name} to be a non-empty profile name in {path}")
    if any(not (character.isalnum() or character in "-_") for character in value):
        raise RuntimeError(f"Unsupported characters in {name} profile {value!r}: {path}")
    return value


def _project_path(project_root: Path, value: object, name: str, config_path: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Expected {name} to be a non-empty path in {config_path}")
    path = (project_root / value).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as error:
        raise RuntimeError(f"{name} must stay inside the project root: {value}") from error
    return path


def _available_baseline_ids(forge_root: Path) -> tuple[str, ...]:
    baseline_dir = forge_root / "rust" / "baselines"
    if not baseline_dir.is_dir():
        return ()
    return tuple(
        sorted(
            path.stem
            for path in baseline_dir.glob("*.toml")
            if BASELINE_ID_PATTERN.fullmatch(path.stem)
        )
    )


def _unknown_baseline_error(forge_root: Path, identifier: object) -> RuntimeError:
    available = ", ".join(_available_baseline_ids(forge_root)) or "none"
    return RuntimeError(
        f"Unknown Rust baseline {identifier!r}; expected <rust-semver>-r<revision>; "
        f"available: {available}"
    )


def load_baseline(forge_root: Path, identifier: str) -> RustBaseline:
    if not isinstance(identifier, str):
        raise _unknown_baseline_error(forge_root, identifier)
    match = BASELINE_ID_PATTERN.fullmatch(identifier)
    if match is None:
        raise _unknown_baseline_error(forge_root, identifier)

    path = forge_root / "rust" / "baselines" / f"{identifier}.toml"
    if not path.is_file():
        raise _unknown_baseline_error(forge_root, identifier)
    document = _read_toml(path)
    baseline = _table(document, "baseline", path)
    if baseline.get("schema") != 1:
        raise RuntimeError(f"Unsupported Rust baseline schema in {path}")
    if baseline.get("id") != identifier:
        raise RuntimeError(f"Rust baseline id does not match its filename: {path}")

    rust_version = baseline.get("rust-version")
    expected_rust_version = match.group("rust_version")
    if rust_version != expected_rust_version:
        raise RuntimeError(f"Rust baseline version does not match its identifier: {path}")

    revision = baseline.get("revision")
    expected_revision = int(match.group("revision"))
    if type(revision) is not int or revision != expected_revision:
        raise RuntimeError(f"Rust baseline revision does not match its identifier: {path}")

    released_raw = baseline.get("released")
    if not isinstance(released_raw, str):
        raise RuntimeError(f"Rust baseline release date must use YYYY-MM-DD in {path}")
    try:
        released = date.fromisoformat(released_raw)
    except ValueError as error:
        raise RuntimeError(f"Invalid Rust baseline release date in {path}: {released_raw!r}") from error

    toolchain = _table(document, "toolchain", path)
    if toolchain.get("channel") != rust_version:
        raise RuntimeError(f"Rust baseline toolchain channel does not match its version: {path}")

    raw_groups = _table(document, "groups", path)
    groups = {
        name: _string_list(dependencies, f"groups.{name}", path)
        for name, dependencies in raw_groups.items()
    }
    raw_group_requirements = _table(document, "group-requirements", path)
    group_requirements = {
        name: {
            requirement: _string_list(
                values,
                f"group-requirements.{name}.{requirement}",
                path,
            )
            for requirement, values in _mapping(
                requirements, f"group-requirements.{name}", path
            ).items()
        }
        for name, requirements in raw_group_requirements.items()
    }
    unknown_requirement_groups = set(group_requirements) - set(groups)
    if unknown_requirement_groups:
        names = ", ".join(sorted(unknown_requirement_groups))
        raise RuntimeError(
            f"Rust requirements reference unknown dependency groups in {path}: {names}"
        )
    return RustBaseline(
        identifier=identifier,
        rust_version=rust_version,
        revision=revision,
        released=released,
        toolchain=toolchain,
        dependencies=_table(document, "dependencies", path),
        groups=groups,
        group_requirements=group_requirements,
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

    baseline_identifier = document.get("baseline")
    if not isinstance(baseline_identifier, str):
        raise RuntimeError(f"Expected baseline to be a string in {config_path}")
    baseline = load_baseline(forge_root, baseline_identifier)

    return RustProject(
        root=root,
        manifest=_project_path(root, document.get("manifest"), "manifest", config_path),
        toolchain=_project_path(root, document.get("toolchain"), "toolchain", config_path),
        default_profile=_profile_name(
            document.get("default-profile"), "default-profile", config_path
        ),
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
        if dependency == "openssl-conan":
            package_dir = read_openssl_manifest(repo_config.install_prefix)
            expected = {
                "OPENSSL_DIR": str(package_dir),
                "OPENSSL_LIB_DIR": str(package_dir / "lib"),
                "OPENSSL_INCLUDE_DIR": str(package_dir / "include"),
                "OPENSSL_STATIC": "1",
                "OPENSSL_NO_VENDOR": "1",
            }
            for name, value in expected.items():
                current = environment.get(name)
                if current is not None and current != value:
                    raise RuntimeError(
                        f"{name} conflicts with forge Conan OpenSSL: {current!r} != {value!r}"
                    )
            for name in environment:
                if name == "OPENSSL_LIBS" or (
                    name.endswith(
                        (
                            "_OPENSSL_DIR",
                            "_OPENSSL_LIB_DIR",
                            "_OPENSSL_INCLUDE_DIR",
                            "_OPENSSL_STATIC",
                            "_OPENSSL_NO_VENDOR",
                            "_OPENSSL_LIBS",
                        )
                    )
                    and name not in expected
                ):
                    raise RuntimeError(f"{name} overrides forge Conan OpenSSL; unset it")
            environment.update(expected)
            continue
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

    def resolve_profile(self, profile: str | None = None) -> str:
        selected = self.project.default_profile if profile is None else profile
        return _profile_name(selected, "selected", self.project.manifest)

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
        required_cargo_tools: set[str] = set()
        required_rust_targets: set[str] = set()
        for group in self.project.dependency_groups:
            requirements = self.project.baseline.group_requirements.get(group, {})
            required_cargo_tools.update(requirements.get("cargo-tools", ()))
            required_rust_targets.update(requirements.get("rust-targets", ()))
        for cargo_tool in sorted(required_cargo_tools):
            self.require_cargo_tool(cargo_tool)
        for rust_target in sorted(required_rust_targets):
            self.require_rust_target(rust_target)
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

    @property
    def cargo_tool_root(self) -> Path:
        return (
            self.repo_config.install_prefix
            / "share"
            / "zeta_forge"
            / "rust"
            / self.project.baseline.identifier
        )

    def require_cargo_tool(self, name: str) -> str:
        specification = self.project.baseline.cargo_tools.get(name)
        if specification is None:
            raise RuntimeError(f"Unknown forge-managed Cargo tool: {name}")
        executable_name = specification.get("executable")
        expected_version = specification.get("version")
        if not isinstance(executable_name, str) or not isinstance(expected_version, str):
            raise RuntimeError(f"Invalid forge Cargo tool definition: {name}")
        tool_root = self.cargo_tool_root
        executable = tool_root / "bin" / executable_name
        install = (
            f"cargo +{self.project.baseline.toolchain['channel']} install {name} "
            f"--version {expected_version} --locked --root {tool_root}"
        )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(
                f"Forge-managed Cargo tool {name} is missing or not executable at {executable}; "
                f"install it with: {install}"
            )
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
        return str(executable)

    def require_rust_target(self, target: str) -> None:
        rustup = shutil.which("rustup")
        if rustup is None:
            raise RuntimeError("Required Rust toolchain manager not found on PATH: rustup")
        channel = self.project.baseline.toolchain["channel"]
        completed = run_command(
            [rustup, "target", "list", "--toolchain", channel, "--installed"],
            cwd=self.project.workspace_dir,
            check=False,
            capture_output=True,
        )
        installed = (
            set((completed.stdout or "").splitlines())
            if completed.returncode == 0
            else set()
        )
        if target not in installed:
            raise RuntimeError(
                f"Rust target {target} is required for baseline {self.project.baseline.identifier}; "
                f"install it with: rustup target add {target} --toolchain {channel}"
            )

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

    def check(self, profile: str | None = None) -> int:
        self.validate()
        selected_profile = self.resolve_profile(profile)
        format_result = run_command(
            self.cargo_command("fmt", "--all", "--", "--check"),
            cwd=self.project.workspace_dir,
            check=False,
        )
        if format_result.returncode != 0:
            return format_result.returncode
        environment = _native_environment(self.project, self.repo_config)
        for arguments in (
            (
                "check",
                "--workspace",
                "--all-targets",
                "--locked",
                "--profile",
                selected_profile,
            ),
            (
                "clippy",
                "--workspace",
                "--all-targets",
                "--locked",
                "--profile",
                selected_profile,
                "--",
                "-D",
                "warnings",
            ),
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

    def build(self, profile: str | None = None) -> int:
        self.validate()
        selected_profile = self.resolve_profile(profile)
        completed = run_command(
            self.cargo_command(
                "build",
                "--workspace",
                "--locked",
                "--profile",
                selected_profile,
            ),
            cwd=self.project.workspace_dir,
            env=_native_environment(self.project, self.repo_config),
            check=False,
        )
        return completed.returncode

    def rebuild(self, profile: str | None = None) -> int:
        self.validate()
        selected_profile = self.resolve_profile(profile)
        clean_result = run_command(
            self.cargo_command(
                "clean",
                "--workspace",
                "--profile",
                selected_profile,
            ),
            cwd=self.project.workspace_dir,
            env=self.repo_config.env,
            check=False,
        )
        if clean_result.returncode != 0:
            return clean_result.returncode
        completed = run_command(
            self.cargo_command(
                "build",
                "--workspace",
                "--locked",
                "--profile",
                selected_profile,
            ),
            cwd=self.project.workspace_dir,
            env=_native_environment(self.project, self.repo_config),
            check=False,
        )
        return completed.returncode

    def test(self, profile: str | None = None) -> int:
        self.validate()
        selected_profile = self.resolve_profile(profile)
        completed = run_command(
            self.cargo_command(
                "test",
                "--workspace",
                "--all-targets",
                "--locked",
                "--profile",
                selected_profile,
            ),
            cwd=self.project.workspace_dir,
            env=_native_environment(self.project, self.repo_config),
            check=False,
        )
        return completed.returncode

    def run(
        self,
        package: str,
        *,
        profile: str | None = None,
        cargo_arguments: Sequence[str] = (),
        program_arguments: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> int:
        self.validate()
        selected_profile = self.resolve_profile(profile)
        command = self.cargo_command(
            "run",
            "--locked",
            "-p",
            package,
            "--profile",
            selected_profile,
            *cargo_arguments,
        )
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
    parser.add_argument(
        "command",
        choices=("doctor", "metadata", "check", "build", "rebuild", "test"),
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument(
        "--profile",
        help=(
            "Cargo build profile for check/build/rebuild/test; defaults to "
            "default-profile in the project's zeta-rust.toml."
        ),
    )
    return parser


def run_rust_cli(argv: Sequence[str], repo_config: RepoConfig) -> int:
    namespace = build_rust_parser().parse_args(argv)
    project = load_rust_project(namespace.project_root, repo_config.forge_root)
    manager = RustWorkspaceManager(project, repo_config)
    if namespace.profile is not None and namespace.command in {"doctor", "metadata"}:
        raise RuntimeError(f"--profile does not apply to rust {namespace.command}")
    if namespace.command == "doctor":
        return manager.doctor()
    if namespace.command == "metadata":
        status = manager.metadata()
    elif namespace.command == "check":
        status = manager.check(namespace.profile)
    elif namespace.command == "build":
        status = manager.build(namespace.profile)
    elif namespace.command == "rebuild":
        status = manager.rebuild(namespace.profile)
    else:
        status = manager.test(namespace.profile)
    profile_detail = ""
    if namespace.command in {"check", "build", "rebuild", "test"}:
        profile_detail = f" profile={manager.resolve_profile(namespace.profile)}"
    print(
        f"ZETA_RUST_DONE status={status}{profile_detail} baseline={project.baseline.identifier} "
        f"manifest={project.manifest}"
    )
    return status
