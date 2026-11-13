"""Read-only Rust manifest policy and locked dependency reports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from .rust_dependencies import (
    DependencyReport,
    dependency_report,
    owned_manifest,
    package_identity,
)
from .rust_project import (
    PROJECT_CONFIG_NAME,
    RustCatalog,
    RustProject,
    project_relative_path,
    read_project_toml,
    require_mapping,
    require_string_list,
    require_table,
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
    package = require_table(read_project_toml(manifest), "package", manifest)
    if package.get("name") in project.catalog.dependencies:
        raise RuntimeError(f"Local Rust dependency {name!r} in {manifest} shadows a Forge crate")
    if value.get("package", name) != package.get("name"):
        raise RuntimeError(f"Local Rust dependency {name!r} has the wrong package name")
    owner = next(root for root in project.source_roots if target.is_relative_to(root))
    if owner != project.root:
        configuration = read_project_toml(owner / PROJECT_CONFIG_NAME)
        workspace_manifest = project_relative_path(owner, configuration["manifest"], "manifest", owner / PROJECT_CONFIG_NAME)
        workspace = require_table(read_project_toml(workspace_manifest), "workspace", workspace_manifest)
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
    members = require_string_list(workspace.get("members", []), "workspace.members", project.manifest)
    manifests: list[Path] = []
    for member in members:
        if any(character in member for character in "*?["):
            raise RuntimeError(f"Rust workspace members must be explicit paths: {member}")
        manifest = project_relative_path(
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
    workspace = require_table(read_project_toml(project.manifest), "workspace", project.manifest)
    return require_mapping(workspace.get("dependencies", {}), "workspace.dependencies", project.manifest)


def project_cargo_tool(project: RustProject, name: str) -> Mapping[str, object]:
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
    toolchain = require_table(read_project_toml(project.toolchain), "toolchain", project.toolchain)
    if dict(toolchain) != dict(project.toolchain_spec):
        raise RuntimeError(
            f"Rust toolchain does not match Forge definition: {project.toolchain}"
        )

    manifest_document = read_project_toml(project.manifest)
    workspace = require_table(manifest_document, "workspace", project.manifest)
    metadata = workspace.get("metadata", {})
    if isinstance(metadata, dict) and "zeta-forge" in metadata:
        raise RuntimeError(f"Obsolete Zeta Forge workspace metadata in {project.manifest}")
    workspace_dependencies = require_mapping(
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
        project_cargo_tool(project, str(capability["cargo-tool"]))

    for member_manifest, workspace_manifest in own_manifests(project):
        member = read_project_toml(member_manifest)
        declarations = read_project_toml(workspace_manifest).get("workspace", {}).get("dependencies", {})
        for candidate in dict.fromkeys([member_manifest, workspace_manifest]):
            if set(read_project_toml(candidate)) & {"patch", "replace"}:
                raise RuntimeError(f"Cargo patch/replace overrides are forbidden in {candidate}")
            if project.source_patches:
                for directory in candidate.parents:
                    for filename in ("config", "config.toml"):
                        config_file = directory / ".cargo" / filename
                        if config_file.is_file() and set(read_project_toml(config_file)) & {"patch", "paths", "source", "include"}:
                            raise RuntimeError(f"Cargo source overrides conflict with Forge source-patches: {config_file}")
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
    if project.source_patches:
        repaired = [record for record in read_project_toml(lockfile).get("package", []) if record.get("name") == "wry"]
        if (len(repaired) != 1 or repaired[0].get("version") != "0.53.5"
            or repaired[0].get("source") not in (None, "registry+https://github.com/rust-lang/crates.io-index")):
            raise RuntimeError("Forge GTK IPC source repair requires exactly one locked Wry 0.53.5 identity")


def _own_workspace(project: RustProject, manifest: Path) -> Path:
    owner = next(root for root in project.source_roots if manifest.is_relative_to(root))
    document = read_project_toml(manifest)
    explicit = document.get("package", {}).get("workspace")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise RuntimeError(f"Invalid package.workspace in {manifest}: {explicit!r}")
        return project_relative_path(owner, str(manifest.parent.relative_to(owner) / explicit / "Cargo.toml"),
                             "package.workspace", manifest)
    for parent in (manifest.parent, *manifest.parent.parents):
        if not parent.is_relative_to(owner):
            break
        candidate = parent / "Cargo.toml"
        if candidate.is_file() and "workspace" in read_project_toml(candidate):
            return candidate
    raise RuntimeError(f"Own Rust crate has no workspace: {manifest}")


def own_manifests(project: RustProject) -> list[tuple[Path, Path]]:
    """Visit only registered members and their own in-repository path crates."""
    document = read_project_toml(project.manifest)
    pending = [project.manifest, *_member_manifests(project, document["workspace"])]
    visited: dict[Path, Path] = {}
    while pending:
        manifest = pending.pop(0).resolve()
        if manifest in visited or not owned_manifest(manifest, project.source_roots):
            continue
        workspace = _own_workspace(project, manifest)
        visited[manifest] = workspace
        declarations = read_project_toml(workspace)["workspace"].get("dependencies", {})
        for _, dependencies in _member_dependency_tables(read_project_toml(manifest), manifest):
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
    document = read_project_toml(lockfile)
    packages = document.get("package", [])
    if document.get("version") not in (3, 4) or not isinstance(packages, list):
        raise RuntimeError(f"Invalid Cargo lockfile: {lockfile}")
    for package in packages:
        package_identity(package)
    edges, errors, owned = [], [], []
    workspace = read_project_toml(project.manifest)["workspace"]
    required = {project.manifest, *_member_manifests(project, workspace)}
    for manifest, workspace_manifest in own_manifests(project):
        member = read_project_toml(manifest)
        if "package" not in member:
            continue
        package = member["package"]
        workspace = read_project_toml(workspace_manifest)["workspace"]
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
