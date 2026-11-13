"""Load governed Rust projects, approved catalog entries and consumer registrations."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .config import RepoConfig
from .rust_patches import patch_recipe
from .rust_tool_recipe import tool_repair

PROJECT_CONFIG_NAME = "zeta-rust.toml"
TOOLCHAIN_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXACT_VERSION_PATTERN = re.compile(r"^=[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
GIT_REV_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class RustCatalog:
    dependencies: Mapping[str, Mapping[str, object]]
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
    local_roots: tuple[Path, ...] = ()
    source_patches: tuple[str, ...] = ()

    @property
    def source_roots(self) -> tuple[Path, ...]:
        return (self.root, *self.local_roots)

    @property
    def workspace_dir(self) -> Path:
        return self.manifest.parent


def read_project_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as error:
        raise RuntimeError(f"Required Rust project file is missing: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"Invalid TOML in {path}: {error}") from error


def require_mapping(value: object, name: str, path: Path) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected [{name}] table in {path}")
    return value


def require_table(document: Mapping[str, object], name: str, path: Path) -> Mapping[str, object]:
    return require_mapping(document.get(name), name, path)


def require_string_list(value: object, name: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"Expected {name} to be a string array in {path}")
    return tuple(value)


def project_relative_path(project_root: Path, value: object, name: str, config_path: Path) -> Path:
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
    toolchain = require_table(read_project_toml(path), "toolchain", path)
    if toolchain.get("channel") != channel:
        raise RuntimeError(f"Rust toolchain channel does not match filename: {path}")
    if (
        set(toolchain) != {"channel", "profile", "components"}
        or not isinstance(toolchain["profile"], str)
        or not toolchain["profile"]
    ):
        raise RuntimeError(f"Invalid Rust toolchain definition in {path}")
    require_string_list(toolchain["components"], "toolchain.components", path)
    return toolchain


def _catalog_records(
    document: Mapping[str, object], name: str, path: Path
) -> tuple[Mapping[str, object], ...]:
    records = document.get(name)
    if not isinstance(records, list):
        raise RuntimeError(f"Expected [[{name}]] records in {path}")
    return tuple(require_mapping(record, name, path) for record in records)


def load_catalog(forge_root: Path) -> RustCatalog:
    path = forge_root / "rust" / "catalog.toml"
    document = read_project_toml(path)
    if document.get("schema") != 2:
        raise RuntimeError(f"Unsupported Rust catalog schema in {path}")
    if set(document) != {"schema", "dependencies", "cargo-tools", "capabilities"}:
        raise RuntimeError(f"Invalid Rust catalog sections in {path}")
    dependencies: dict[str, Mapping[str, object]] = {}
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
        else:
            if (
                not isinstance(git, str)
                or not git.startswith("https://")
                or not isinstance(rev, str)
                or not GIT_REV_PATTERN.fullmatch(rev)
            ):
                raise RuntimeError(f"Invalid Git source or full revision for {name!r} in {path}")
        allowed_fields = {
            "name", "version", "git", "rev", "required-features", "default-features",
            "release-manifest", "release-tag-prefix"
        }
        if set(record) - allowed_fields:
            raise RuntimeError(f"Unknown Rust catalog dependency field for {name!r} in {path}")
        for field in ("release-manifest", "release-tag-prefix"):
            value = record.get(field)
            if value is not None and (git is None or not isinstance(value, str) or not value):
                raise RuntimeError(f"Invalid {field} for {name!r} in {path}")
        release_manifest = record.get("release-manifest")
        if release_manifest is not None and (
            Path(release_manifest).is_absolute() or ".." in Path(release_manifest).parts
            or Path(release_manifest).name != "Cargo.toml"
        ):
            raise RuntimeError(f"Invalid release-manifest for {name!r} in {path}")
        require_string_list(record.get("required-features", []), f"{name}.required-features", path)
        if "default-features" in record and type(record["default-features"]) is not bool:
            raise RuntimeError(f"Invalid default-features constraint for {name!r} in {path}")
        if name in dependencies:
            raise RuntimeError(f"Duplicate Rust catalog dependency {name!r} in {path}")
        dependencies[name] = record

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
        required_tool_fields = {"name", "version", "executable", "crate", "crate-version"}
        if not required_tool_fields <= set(record) or set(record) - required_tool_fields - {"source-repair"}:
            raise RuntimeError(f"Unknown Cargo tool field in {path}")
        if "source-repair" in record:
            if record["source-repair"] is None:
                raise RuntimeError(f"Invalid Cargo tool source-repair in {path}")
            tool_repair(forge_root, record)
        if dependencies.get(crate, {}).get("version") != crate_version:
            raise RuntimeError(f"Cargo tool {name!r} references an unapproved crate version")
        if any(item["crate-version"] == crate_version for item in cargo_tools.get(name, ())):
            raise RuntimeError(f"Duplicate Cargo tool {name!r} for {crate_version}")
        cargo_tools.setdefault(name, []).append(record)

    capabilities = require_table(document, "capabilities", path)
    for name, raw in capabilities.items():
        capability = require_mapping(raw, f"capabilities.{name}", path)
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
        require_string_list(capability.get("rust-targets", []), f"capabilities.{name}.rust-targets", path)
    return RustCatalog(
        dependencies=dependencies,
        capabilities={name: require_mapping(value, name, path) for name, value in capabilities.items()},
        cargo_tools={name: tuple(records) for name, records in cargo_tools.items()},
    )


def _authorized_local_roots(root: Path, forge_root: Path) -> tuple[Path, ...]:
    registry = forge_root / "rust/projects.toml"
    if not registry.is_file():
        return ()
    document = read_project_toml(registry)
    records = document.get("projects")
    if document.get("schema") != 1 or set(document) != {"schema", "projects"} or not isinstance(records, list):
        raise RuntimeError(f"Invalid Rust consumer registry: {registry}")
    for record in records:
        if not isinstance(record, dict) or set(record) - {"path", "additional-manifests", "local-dependencies"}:
            raise RuntimeError(f"Invalid Rust consumer entry in {registry}")
        name = record.get("path")
        if not isinstance(name, str) or Path(name).is_absolute() or len(Path(name).parts) != 1:
            raise RuntimeError(f"Rust consumer must name a workspace sibling: {name!r}")
    names = {record["path"] for record in records}
    selected = [record for record in records if record.get("path") == root.name]
    if not selected:
        return ()
    if len(selected) != 1:
        raise RuntimeError(f"Duplicate Rust consumer: {root.name}")
    dependencies = require_string_list(selected[0].get("local-dependencies", []), "local-dependencies", registry)
    if len(set(dependencies)) != len(dependencies):
        raise RuntimeError("Duplicate local Rust dependency authorization")
    workspace_root = forge_root.resolve().parent
    if dependencies and root != (workspace_root / root.name).resolve():
        raise RuntimeError("Local Rust dependencies require the registered workspace checkout")
    result = []
    for name in dependencies:
        if name not in names or name == root.name or Path(name).name != name:
            raise RuntimeError(f"Unregistered local Rust dependency: {name!r}")
        sibling = (workspace_root / name).resolve()
        if sibling.parent != workspace_root or not (sibling / PROJECT_CONFIG_NAME).is_file():
            raise RuntimeError(f"Local Rust dependency must be a registered workspace sibling: {name}")
        result.append(sibling)
    return tuple(result)


def load_rust_project(project_root: Path, forge_root: Path) -> RustProject:
    root = project_root.resolve()
    config_path = root / PROJECT_CONFIG_NAME
    document = read_project_toml(config_path)
    if document.get("schema") != 2:
        raise RuntimeError(f"Unsupported Rust project schema in {config_path}")
    if set(document) - {"schema", "manifest", "toolchain", "capabilities", "native-dependencies", "source-patches"}:
        raise RuntimeError(f"Unknown Rust project field in {config_path}")
    toolchain_path = project_relative_path(root, document.get("toolchain"), "toolchain", config_path)
    toolchain = require_table(read_project_toml(toolchain_path), "toolchain", toolchain_path)

    patches = require_string_list(document.get("source-patches", []), "source-patches", config_path)
    if patches:
        patch_recipe(forge_root, patches)
    return RustProject(
        root=root,
        manifest=project_relative_path(root, document.get("manifest"), "manifest", config_path),
        toolchain=toolchain_path,
        capabilities=require_string_list(document.get("capabilities", []), "capabilities", config_path),
        native_dependencies=require_string_list(
            document.get("native-dependencies", []), "native-dependencies", config_path
        ),
        toolchain_spec=load_toolchain(forge_root, toolchain.get("channel")),
        catalog=load_catalog(forge_root),
        local_roots=_authorized_local_roots(root, forge_root),
        source_patches=patches,
    )


def registered_projects(config: RepoConfig) -> list[RustProject]:
    path = config.forge_root / "rust" / "projects.toml"
    document = read_project_toml(path)
    records = document.get("projects")
    if document.get("schema") != 1 or set(document) != {"schema", "projects"} or not isinstance(records, list):
        raise RuntimeError(f"Invalid Rust consumer registry: {path}")
    projects: list[RustProject] = []
    roots: set[Path] = set()
    manifests: set[Path] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) - {"path", "additional-manifests", "local-dependencies"}:
            raise RuntimeError(f"Invalid Rust consumer entry in {path}: {record!r}")
        relative = record.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or len(Path(relative).parts) != 1:
            raise RuntimeError(f"Rust consumer must name a workspace sibling in {path}: {relative!r}")
        root = project_relative_path(config.zetax_root, relative, "consumer", path)
        if root in roots:
            raise RuntimeError(f"Duplicate Rust consumer: {root}")
        roots.add(root)
        project = load_rust_project(root, config.forge_root)
        candidates = [project]
        for extra in require_string_list(record.get("additional-manifests", []), "additional-manifests", path):
            if Path(extra).is_absolute():
                raise RuntimeError(f"Additional manifest must be relative: {extra}")
            manifest = project_relative_path(root, extra, "additional manifest", path)
            if manifest.name != "Cargo.toml":
                raise RuntimeError(f"Expected Cargo.toml for additional manifest: {manifest}")
            candidates.append(replace(project, manifest=manifest, capabilities=(), native_dependencies=()))
        for candidate in candidates:
            if candidate.manifest in manifests:
                raise RuntimeError(f"Duplicate Rust consumer manifest: {candidate.manifest}")
            manifests.add(candidate.manifest)
            projects.append(candidate)
    discovered = {
        child.resolve() for child in config.zetax_root.iterdir()
        if child.is_dir() and (child / PROJECT_CONFIG_NAME).is_file()
    }
    missing = discovered - roots
    if missing:
        raise RuntimeError("Unregistered Rust projects: " + ", ".join(str(root) for root in sorted(missing)))
    if not projects:
        raise RuntimeError(f"Rust consumer registry is empty: {path}")
    return projects
