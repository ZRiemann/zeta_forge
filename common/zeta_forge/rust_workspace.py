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
from .rust_dependencies import DependencyReport, dependency_report, owned_manifest, package_identity

PROJECT_CONFIG_NAME = "zeta-rust.toml"
TOOLCHAIN_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXACT_VERSION_PATTERN = re.compile(r"^=[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
GIT_REV_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class CargoToolUnavailable(RuntimeError):
    """A Forge-managed Cargo tool is absent or has the wrong version."""


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

    @property
    def source_roots(self) -> tuple[Path, ...]:
        return (self.root, *self.local_roots)

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
        _string_list(record.get("required-features", []), f"{name}.required-features", path)
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
        if set(record) != {"name", "version", "executable", "crate", "crate-version"}:
            raise RuntimeError(f"Unknown Cargo tool field in {path}")
        if dependencies.get(crate, {}).get("version") != crate_version:
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
        dependencies=dependencies,
        capabilities={name: _mapping(value, name, path) for name, value in capabilities.items()},
        cargo_tools={name: tuple(records) for name, records in cargo_tools.items()},
    )


def _authorized_local_roots(root: Path, forge_root: Path) -> tuple[Path, ...]:
    registry = forge_root / "rust/projects.toml"
    if not registry.is_file():
        return ()
    document = _read_toml(registry)
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
    dependencies = _string_list(selected[0].get("local-dependencies", []), "local-dependencies", registry)
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
        local_roots=_authorized_local_roots(root, forge_root),
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
    target = (base / local_path).resolve()
    if not owned_manifest(target / "Cargo.toml", project.source_roots):
        raise RuntimeError(f"Local Rust dependency {name!r} escapes project root or authorized siblings")
    manifest = target / "Cargo.toml"
    package = _table(_read_toml(manifest), "package", manifest)
    if package.get("name") in project.catalog.dependencies:
        raise RuntimeError(f"Local Rust dependency {name!r} in {manifest} shadows a Forge crate")
    if value.get("package", name) != package.get("name"):
        raise RuntimeError(f"Local Rust dependency {name!r} has the wrong package name")
    owner = next(root for root in project.source_roots if target.is_relative_to(root))
    if owner != project.root:
        configuration = _read_toml(owner / PROJECT_CONFIG_NAME)
        workspace_manifest = _project_path(owner, configuration["manifest"], "manifest", owner / PROJECT_CONFIG_NAME)
        workspace = _table(_read_toml(workspace_manifest), "workspace", workspace_manifest)
        members = {workspace_manifest, *_member_manifests(project, workspace, workspace_manifest.parent)}
        if manifest not in members:
            raise RuntimeError(f"Local Rust dependency is not a registered workspace package: {manifest}")
        version = package.get("version")
        if isinstance(version, dict) and version.get("workspace") is True:
            version = workspace.get("package", {}).get("version")
        if not isinstance(version, str) or value.get("version") != "=" + version:
            raise RuntimeError(f"Local Rust dependency {name!r} requires exact package version ={version}")


def _member_manifests(project: RustProject, workspace: Mapping[str, object], base: Path | None = None) -> list[Path]:
    base = base or project.workspace_dir
    members = _string_list(workspace.get("members", []), "workspace.members", project.manifest)
    manifests: list[Path] = []
    for member in members:
        if any(character in member for character in "*?["):
            raise RuntimeError(f"Rust workspace members must be explicit paths: {member}")
        manifest = _project_path(
            base, f"{member}/Cargo.toml", "workspace member", project.manifest
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
    return _mapping(workspace.get("dependencies", {}), "workspace.dependencies", project.manifest)


def _project_cargo_tool(project: RustProject, name: str) -> Mapping[str, object]:
    declarations = workspace_dependencies(project)
    matching = []
    for specification in project.catalog.cargo_tools.get(name, ()):
        for alias, declaration in declarations.items():
            package = declaration.get("package", alias) if isinstance(declaration, dict) else alias
            version = declaration if isinstance(declaration, str) else (
                declaration.get("version") if isinstance(declaration, dict) else None
            )
            if package == specification["crate"] and version == specification["crate-version"]:
                matching.append(specification)
                break
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
    if set(declaration) - {"package", "version", "git", "rev", "features", "default-features"}:
        return False
    features = declaration.get("features", [])
    if not isinstance(features, list) or any(not isinstance(feature, str) for feature in features):
        return False
    if "default-features" in declaration and type(declaration["default-features"]) is not bool:
        return False
    package = declaration.get("package", name)
    approved = catalog.dependencies.get(package) if isinstance(package, str) else None
    if approved is None:
        return False
    if any(declaration.get(field) != approved.get(field) for field in ("version", "git", "rev")):
        return False
    if not set(approved.get("required-features", [])).issubset(features):
        return False
    return (
        "default-features" not in approved
        or declaration.get("default-features", True) == approved["default-features"]
    )


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
    workspace_dependencies = _mapping(
        workspace.get("dependencies", {}), "workspace.dependencies", project.manifest
    )
    for name, value in workspace_dependencies.items():
        if _is_local_dependency(value):
            _validate_local_dependency(project, name, value, project.workspace_dir)
            continue
        if not _approved_dependency(name, value, project.catalog):
            package = value.get("package", name) if isinstance(value, dict) else name
            expected = (
                project.catalog.dependencies.get(package, "no approved entry")
                if isinstance(package, str) else "valid package name required"
            )
            raise RuntimeError(
                f"Rust dependency {name!r} in {project.manifest} is not approved by Forge catalog: "
                f"actual={value!r}; required={expected!r}"
            )
    if len(set(project.capabilities)) != len(project.capabilities):
        raise RuntimeError("Duplicate Rust capability")
    declared_packages = {
        value.get("package", name) if isinstance(value, dict) else name
        for name, value in workspace_dependencies.items()
    }
    for name in project.capabilities:
        capability = project.catalog.capabilities.get(name)
        if capability is None:
            raise RuntimeError(f"Unknown Rust capability {name!r}")
        if capability["crate"] not in declared_packages:
            raise RuntimeError(f"Rust capability {name!r} requires {capability['crate']!r}")
        _project_cargo_tool(project, str(capability["cargo-tool"]))

    for member_manifest, workspace_manifest in own_manifests(project):
        member = _read_toml(member_manifest)
        declarations = _read_toml(workspace_manifest).get("workspace", {}).get("dependencies", {})
        for candidate in dict.fromkeys([member_manifest, workspace_manifest]):
            if set(_read_toml(candidate)) & {"patch", "replace"}:
                raise RuntimeError(f"Cargo patch/replace overrides are forbidden in {candidate}")
        for name, value in declarations.items():
            if _is_local_dependency(value):
                _validate_local_dependency(project, name, value, workspace_manifest.parent)
            elif not _approved_dependency(name, value, project.catalog):
                package = value.get("package", name) if isinstance(value, dict) else name
                raise RuntimeError(
                    f"Rust dependency {name!r} in {workspace_manifest} is not approved: "
                    f"actual={value!r}; required={project.catalog.dependencies.get(package)!r}"
                )
        for section, dependencies in _member_dependency_tables(member, member_manifest):
            for name, value in dependencies.items():
                if _is_local_dependency(value):
                    _validate_local_dependency(project, name, value, member_manifest.parent)
                    continue
                if (not isinstance(value, dict) or value.get("workspace") is not True
                    or set(value) - {"workspace", "features", "optional", "default-features"}
                    or name not in declarations):
                    raise RuntimeError(
                        f"Dependency {name!r} in {member_manifest} [{section}] must inherit "
                        f"from [workspace.dependencies]; actual={value!r}"
                    )

    lockfile = project.workspace_dir / "Cargo.lock"
    if not lockfile.is_file():
        raise RuntimeError(f"Committed Cargo lockfile is required: {lockfile}")
    report = locked_dependency_report(project)
    if report["errors"]:
        raise RuntimeError(f"Rust dependency drift in {lockfile}: " + "; ".join(report["errors"]))


def _own_workspace(project: RustProject, manifest: Path) -> Path:
    owner = next(root for root in project.source_roots if manifest.is_relative_to(root))
    document = _read_toml(manifest)
    explicit = document.get("package", {}).get("workspace")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise RuntimeError(f"Invalid package.workspace in {manifest}: {explicit!r}")
        return _project_path(owner, str(manifest.parent.relative_to(owner) / explicit / "Cargo.toml"),
                             "package.workspace", manifest)
    for parent in (manifest.parent, *manifest.parent.parents):
        if not parent.is_relative_to(owner):
            break
        candidate = parent / "Cargo.toml"
        if candidate.is_file() and "workspace" in _read_toml(candidate):
            return candidate
    raise RuntimeError(f"Own Rust crate has no workspace: {manifest}")


def own_manifests(project: RustProject) -> list[tuple[Path, Path]]:
    """Visit only registered members and their own in-repository path crates."""
    document = _read_toml(project.manifest)
    pending = [project.manifest, *_member_manifests(project, document["workspace"])]
    visited: dict[Path, Path] = {}
    while pending:
        manifest = pending.pop(0).resolve()
        if manifest in visited or not owned_manifest(manifest, project.source_roots):
            continue
        workspace = _own_workspace(project, manifest)
        visited[manifest] = workspace
        declarations = _read_toml(workspace)["workspace"].get("dependencies", {})
        for _, dependencies in _member_dependency_tables(_read_toml(manifest), manifest):
            for name, value in dependencies.items():
                base = manifest.parent
                if isinstance(value, dict) and value.get("workspace") is True:
                    value = declarations.get(name)
                    base = workspace.parent
                if _is_local_dependency(value):
                    _validate_local_dependency(project, name, value, base)
                    pending.append((base / value["path"] / "Cargo.toml").resolve())
    return list(visited.items())


def _lock_reference(reference: str, packages: list[Mapping[str, object]]) -> Mapping[str, object]:
    match = re.fullmatch(r"([^ ]+)(?: ([^ ]+))?(?: \((.*)\))?", reference)
    if match is None:
        raise RuntimeError(f"Invalid Cargo lock dependency reference: {reference!r}")
    name, version, source = match.groups()
    matches = [p for p in packages if p["name"] == name
               and (version is None or p["version"] == version)
               and (source is None or p.get("source") == source)]
    if len(matches) != 1:
        raise RuntimeError(f"Cargo lock dependency reference {reference!r} resolves to {len(matches)} identities")
    return matches[0]


def locked_dependency_report(project: RustProject) -> DependencyReport:
    lockfile = project.workspace_dir / "Cargo.lock"
    document = _read_toml(lockfile)
    packages = document.get("package", [])
    if document.get("version") not in (3, 4) or not isinstance(packages, list):
        raise RuntimeError(f"Invalid Cargo lockfile: {lockfile}")
    for package in packages:
        package_identity(package)
    edges, errors, owned = [], [], []
    workspace = _read_toml(project.manifest)["workspace"]
    required = {project.manifest, *_member_manifests(project, workspace)}
    for manifest, workspace_manifest in own_manifests(project):
        member = _read_toml(manifest)
        if "package" not in member:
            continue
        package = member["package"]
        workspace = _read_toml(workspace_manifest)["workspace"]
        version = package.get("version")
        if isinstance(version, dict) and version.get("workspace") is True:
            version = workspace.get("package", {}).get("version")
        owners = [p for p in packages if p["name"] == package["name"]
                  and p["version"] == version and p.get("source") is None]
        if len(owners) != 1:
            if manifest in required or not manifest.is_relative_to(project.root):
                errors.append(f"{manifest}: lockfile has {len(owners)} consumer identities for {package['name']}@{version}")
            continue
        owner = owners[0]
        owned.append(package_identity(owner)["id"])
        references = owner.get("dependencies", [])
        for section, dependencies in _member_dependency_tables(member, manifest):
            for alias, value in dependencies.items():
                optional = isinstance(value, dict) and value.get("optional") is True
                if isinstance(value, dict) and value.get("workspace") is True:
                    value = workspace.get("dependencies", {}).get(alias)
                if _is_local_dependency(value):
                    continue
                name = value.get("package", alias) if isinstance(value, dict) else alias
                found = [r for r in references if isinstance(r, str) and r.split(" ", 1)[0] == name]
                if not found and (optional or (manifest not in required and section.endswith("dev-dependencies"))):
                    continue
                if len(found) != 1:
                    errors.append(f"{manifest} [{section}] {name}: expected one lock dependency reference, actual={found!r}")
                    continue
                try:
                    dependency = _lock_reference(found[0], packages)
                except RuntimeError as error:
                    errors.append(f"{manifest} [{section}] {name}: {error}")
                    continue
                edges.append({"consumer": package_identity(owner), "manifest": str(manifest),
                              "dependency": package_identity(dependency), "alias": alias, "section": section})
    return dependency_report(packages, project.catalog.dependencies, direct_edges=edges,
                             owned_ids=owned, errors=errors)


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
