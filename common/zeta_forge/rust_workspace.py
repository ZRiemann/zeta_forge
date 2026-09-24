from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import tomllib

from .conan_openssl import read_openssl_manifest
from .config import RepoConfig
from .process import run_command

PROJECT_CONFIG_NAME = "zeta-rust.toml"
TOOLCHAIN_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXACT_VERSION_PATTERN = re.compile(r"^=[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
GIT_REV_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class CargoToolUnavailable(RuntimeError):
    """A Forge-managed Cargo tool is absent or has the wrong version."""


@dataclass(frozen=True)
class RustCatalog:
    dependencies: Mapping[str, tuple[Mapping[str, object], ...]]
    capabilities: Mapping[str, Mapping[str, object]]
    cargo_tools: Mapping[str, tuple[Mapping[str, object], ...]]


@dataclass(frozen=True)
class RustProject:
    root: Path
    manifest: Path
    toolchain: Path
    capabilities: tuple[str, ...]
    native_dependencies: tuple[str, ...]
    toolchain_spec: Mapping[str, object]
    catalog: RustCatalog

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


def load_toolchain(forge_root: Path, channel: object) -> Mapping[str, object]:
    if not isinstance(channel, str) or not TOOLCHAIN_PATTERN.fullmatch(channel):
        raise RuntimeError(f"Invalid Rust toolchain channel: {channel!r}")
    path = forge_root / "rust" / "toolchains" / f"{channel}.toml"
    if not path.is_file():
        raise RuntimeError(f"Forge has not approved Rust toolchain {channel!r}")
    toolchain = _table(_read_toml(path), "toolchain", path)
    if toolchain.get("channel") != channel:
        raise RuntimeError(f"Rust toolchain channel does not match filename: {path}")
    if (
        set(toolchain) != {"channel", "profile", "components"}
        or not isinstance(toolchain["profile"], str)
        or not toolchain["profile"]
    ):
        raise RuntimeError(f"Invalid Rust toolchain definition in {path}")
    _string_list(toolchain["components"], "toolchain.components", path)
    return toolchain


def _catalog_records(
    document: Mapping[str, object], name: str, path: Path
) -> tuple[Mapping[str, object], ...]:
    records = document.get(name)
    if not isinstance(records, list):
        raise RuntimeError(f"Expected [[{name}]] records in {path}")
    return tuple(_mapping(record, name, path) for record in records)


def load_catalog(forge_root: Path) -> RustCatalog:
    path = forge_root / "rust" / "catalog.toml"
    document = _read_toml(path)
    if document.get("schema") != 1:
        raise RuntimeError(f"Unsupported Rust catalog schema in {path}")
    if set(document) != {"schema", "dependencies", "cargo-tools", "capabilities"}:
        raise RuntimeError(f"Invalid Rust catalog sections in {path}")
    dependencies: dict[str, list[Mapping[str, object]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for record in _catalog_records(document, "dependencies", path):
        name = record.get("name")
        version = record.get("version")
        git = record.get("git")
        rev = record.get("rev")
        if not isinstance(name, str) or not PACKAGE_NAME_PATTERN.fullmatch(name):
            raise RuntimeError(f"Invalid Rust catalog dependency name in {path}")
        if version is not None:
            if (
                not isinstance(version, str)
                or not EXACT_VERSION_PATTERN.fullmatch(version)
                or git is not None
                or rev is not None
            ):
                raise RuntimeError(f"Invalid exact version or source for {name!r} in {path}")
            identity = (name, "registry", version)
        else:
            if (
                not isinstance(git, str)
                or not git.startswith("https://")
                or not isinstance(rev, str)
                or not GIT_REV_PATTERN.fullmatch(rev)
            ):
                raise RuntimeError(f"Invalid Git source or full revision for {name!r} in {path}")
            identity = (name, git, rev)
        allowed_fields = {
            "name", "version", "git", "rev", "required-features", "default-features"
        }
        if set(record) - allowed_fields:
            raise RuntimeError(f"Unknown Rust catalog dependency field for {name!r} in {path}")
        _string_list(record.get("required-features", []), f"{name}.required-features", path)
        if "default-features" in record and type(record["default-features"]) is not bool:
            raise RuntimeError(f"Invalid default-features constraint for {name!r} in {path}")
        if identity in seen:
            raise RuntimeError(f"Duplicate Rust catalog dependency {identity!r}")
        seen.add(identity)
        dependencies.setdefault(name, []).append(record)

    cargo_tools: dict[str, list[Mapping[str, object]]] = {}
    for record in _catalog_records(document, "cargo-tools", path):
        name = record.get("name")
        version = record.get("version")
        crate = record.get("crate")
        crate_version = record.get("crate-version")
        if (
            not isinstance(name, str)
            or not PACKAGE_NAME_PATTERN.fullmatch(name)
            or not isinstance(crate, str)
            or not PACKAGE_NAME_PATTERN.fullmatch(crate)
            or not isinstance(version, str)
            or not TOOLCHAIN_PATTERN.fullmatch(version)
            or not isinstance(crate_version, str)
            or not EXACT_VERSION_PATTERN.fullmatch(crate_version)
            or not isinstance(record.get("executable"), str)
            or not PACKAGE_NAME_PATTERN.fullmatch(record["executable"])
        ):
            raise RuntimeError(f"Invalid Cargo tool record in {path}")
        if set(record) != {"name", "version", "executable", "crate", "crate-version"}:
            raise RuntimeError(f"Unknown Cargo tool field in {path}")
        if not any(item.get("version") == crate_version for item in dependencies.get(crate, ())):
            raise RuntimeError(f"Cargo tool {name!r} references an unapproved crate version")
        if any(item["crate-version"] == crate_version for item in cargo_tools.get(name, ())):
            raise RuntimeError(f"Duplicate Cargo tool {name!r} for {crate_version}")
        cargo_tools.setdefault(name, []).append(record)

    capabilities = _table(document, "capabilities", path)
    for name, raw in capabilities.items():
        capability = _mapping(raw, f"capabilities.{name}", path)
        if set(capability) - {"crate", "cargo-tool", "rust-targets"}:
            raise RuntimeError(f"Unknown Rust capability field for {name!r}")
        crate = capability.get("crate")
        tool = capability.get("cargo-tool")
        if (
            not isinstance(crate, str)
            or crate not in dependencies
            or not isinstance(tool, str)
            or tool not in cargo_tools
        ):
            raise RuntimeError(f"Invalid Rust capability {name!r} in {path}")
        _string_list(capability.get("rust-targets", []), f"capabilities.{name}.rust-targets", path)
    return RustCatalog(
        dependencies={name: tuple(records) for name, records in dependencies.items()},
        capabilities={name: _mapping(value, name, path) for name, value in capabilities.items()},
        cargo_tools={name: tuple(records) for name, records in cargo_tools.items()},
    )


def load_rust_project(project_root: Path, forge_root: Path) -> RustProject:
    root = project_root.resolve()
    config_path = root / PROJECT_CONFIG_NAME
    document = _read_toml(config_path)
    if document.get("schema") != 2:
        raise RuntimeError(f"Unsupported Rust project schema in {config_path}")
    if set(document) - {"schema", "manifest", "toolchain", "capabilities", "native-dependencies"}:
        raise RuntimeError(f"Unknown Rust project field in {config_path}")
    toolchain_path = _project_path(root, document.get("toolchain"), "toolchain", config_path)
    toolchain = _table(_read_toml(toolchain_path), "toolchain", toolchain_path)

    return RustProject(
        root=root,
        manifest=_project_path(root, document.get("manifest"), "manifest", config_path),
        toolchain=toolchain_path,
        capabilities=_string_list(document.get("capabilities", []), "capabilities", config_path),
        native_dependencies=_string_list(
            document.get("native-dependencies", []), "native-dependencies", config_path
        ),
        toolchain_spec=load_toolchain(forge_root, toolchain.get("channel")),
        catalog=load_catalog(forge_root),
    )


def _is_local_dependency(value: object) -> bool:
    return isinstance(value, dict) and "path" in value


def _validate_local_dependency(
    project: RustProject, name: str, value: Mapping[str, object], base: Path
) -> None:
    local_path = value.get("path")
    if not isinstance(local_path, str) or not local_path:
        raise RuntimeError(f"Invalid local Rust dependency path for {name!r}")
    if set(value) & {"git", "rev", "branch", "tag", "registry"}:
        raise RuntimeError(f"Local Rust dependency {name!r} mixes path and external source")
    if not (base / local_path).resolve().is_relative_to(project.root):
        raise RuntimeError(f"Local Rust dependency {name!r} escapes project root")


def _member_manifests(project: RustProject, workspace: Mapping[str, object]) -> list[Path]:
    members = _string_list(workspace.get("members"), "workspace.members", project.manifest)
    manifests: list[Path] = []
    for member in members:
        if any(character in member for character in "*?["):
            raise RuntimeError(f"Rust workspace members must be explicit paths: {member}")
        manifest = _project_path(
            project.workspace_dir, f"{member}/Cargo.toml", "workspace member", project.manifest
        )
        if not manifest.is_file():
            raise RuntimeError(f"Rust workspace member manifest is missing: {manifest}")
        manifests.append(manifest)
    return manifests


def _member_dependency_tables(
    member: Mapping[str, object], manifest: Path
) -> list[tuple[str, Mapping[str, object]]]:
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


def workspace_dependencies(project: RustProject) -> Mapping[str, object]:
    workspace = _table(_read_toml(project.manifest), "workspace", project.manifest)
    return _table(workspace, "dependencies", project.manifest)


def _project_cargo_tool(project: RustProject, name: str) -> Mapping[str, object]:
    declarations = workspace_dependencies(project)
    matching = []
    for specification in project.catalog.cargo_tools.get(name, ()):
        declaration = declarations.get(str(specification["crate"]))
        version = declaration if isinstance(declaration, str) else (
            declaration.get("version") if isinstance(declaration, dict) else None
        )
        if version == specification["crate-version"]:
            matching.append(specification)
    if len(matching) != 1:
        raise RuntimeError(
            f"Cargo tool {name!r} has no unique approved version for this workspace"
        )
    return matching[0]


def _approved_dependency(name: str, value: object, catalog: RustCatalog) -> bool:
    if isinstance(value, str):
        declaration: Mapping[str, object] = {"version": value}
    elif isinstance(value, dict):
        declaration = value
    else:
        return False
    if set(declaration) - {"version", "git", "rev", "features", "default-features"}:
        return False
    features = declaration.get("features", [])
    if not isinstance(features, list) or any(not isinstance(feature, str) for feature in features):
        return False
    if "default-features" in declaration and type(declaration["default-features"]) is not bool:
        return False
    for approved in catalog.dependencies.get(name, ()):
        source_fields = ("version", "git", "rev")
        if any(declaration.get(field) != approved.get(field) for field in source_fields):
            continue
        required = approved.get("required-features", [])
        if not set(required).issubset(features):
            continue
        if (
            "default-features" in approved
            and declaration.get("default-features", True) != approved["default-features"]
        ):
            continue
        return True
    return False


def validate_rust_project(project: RustProject) -> None:
    toolchain = _table(_read_toml(project.toolchain), "toolchain", project.toolchain)
    if dict(toolchain) != dict(project.toolchain_spec):
        raise RuntimeError(
            f"Rust toolchain does not match Forge definition: {project.toolchain}"
        )

    manifest_document = _read_toml(project.manifest)
    workspace = _table(manifest_document, "workspace", project.manifest)
    metadata = workspace.get("metadata", {})
    if isinstance(metadata, dict) and "zeta-forge" in metadata:
        raise RuntimeError(f"Obsolete Zeta Forge workspace metadata in {project.manifest}")
    workspace_dependencies = _table(workspace, "dependencies", project.manifest)
    for name, value in workspace_dependencies.items():
        if _is_local_dependency(value):
            _validate_local_dependency(project, name, value, project.workspace_dir)
            continue
        if not _approved_dependency(name, value, project.catalog):
            raise RuntimeError(f"Rust dependency {name!r} is not approved by Forge catalog")
    if len(set(project.capabilities)) != len(project.capabilities):
        raise RuntimeError("Duplicate Rust capability")
    for name in project.capabilities:
        capability = project.catalog.capabilities.get(name)
        if capability is None:
            raise RuntimeError(f"Unknown Rust capability {name!r}")
        if capability["crate"] not in workspace_dependencies:
            raise RuntimeError(f"Rust capability {name!r} requires {capability['crate']!r}")
        _project_cargo_tool(project, str(capability["cargo-tool"]))

    for member_manifest in _member_manifests(project, workspace):
        member = _read_toml(member_manifest)
        for section, dependencies in _member_dependency_tables(member, member_manifest):
            for name, value in dependencies.items():
                if _is_local_dependency(value):
                    _validate_local_dependency(project, name, value, member_manifest.parent)
                    continue
                if (
                    not isinstance(value, dict)
                    or value.get("workspace") is not True
                    or set(value) - {"workspace", "features", "optional", "default-features"}
                    or name not in workspace_dependencies
                ):
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
                f"{repo_config.forge_root}/zbuild.py install nng"
            )
        environment.update(NNG_NO_VENDOR="1", NNG_DIR=str(prefix), NNG_STATIC="1")
    return environment


class RustWorkspaceManager:
    def __init__(self, project: RustProject, repo_config: RepoConfig):
        self.project = project
        self.repo_config = repo_config

    def validate(self) -> None:
        validate_rust_project(self.project)

    def doctor(self, *, application_tools: bool = True, check_cargo_tools: bool = True) -> int:
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
        _native_environment(self.project, self.repo_config)
        print(
            f"ZETA_RUST_DONE status=0 toolchain={self.project.toolchain_spec['channel']} "
            f"manifest={self.project.manifest}"
        )
        return 0

    def tool_command(self, program: str, *arguments: str) -> list[object]:
        rustup = shutil.which("rustup")
        if rustup is None:
            raise RuntimeError("Required Rust toolchain manager not found on PATH: rustup")
        channel = self.project.toolchain_spec["channel"]
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
            raise RuntimeError(f"Unable to query Rust toolchain program {program}: {detail}")
        return (completed.stdout or completed.stderr).strip()

    def cargo_tool_root(self, name: str, version: str) -> Path:
        return (
            self.repo_config.install_prefix
            / "share"
            / "zeta_forge"
            / "rust-tools"
            / str(self.project.toolchain_spec["channel"])
            / name
            / version
        )

    def require_cargo_tool(self, name: str) -> str:
        if name not in self.project.catalog.cargo_tools:
            raise RuntimeError(f"Unknown forge-managed Cargo tool: {name}")
        specification = _project_cargo_tool(self.project, name)
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
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise CargoToolUnavailable(
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
        specification = _project_cargo_tool(self.project, name)
        expected_version = str(specification["version"])
        tool_root = self.cargo_tool_root(name, expected_version)
        cargo = shutil.which("cargo")
        if cargo is None:
            raise RuntimeError("Required command not found on PATH: cargo")
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
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.returncode != 0 and completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return completed.returncode
