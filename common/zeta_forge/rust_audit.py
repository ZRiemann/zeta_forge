"""Explicit, read-only audits of the registered ZetaX Rust consumers."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping, TypedDict

from .build_cli import Request, log
from .config import RepoConfig
from .process import run_command
from .rust_dependencies import DependencyReport, resolved_dependency_report
from .rust_patches import patch_environment, verify_resolved_patches
from .rust_workspace import (
    PROJECT_CONFIG_NAME,
    RustProject,
    RustWorkspaceManager,
    _project_path,
    _read_toml,
    _string_list,
    load_rust_project,
    locked_dependency_report,
    validate_rust_project,
    workspace_dependencies,
)


def registered_projects(config: RepoConfig) -> list[RustProject]:
    path = config.forge_root / "rust" / "projects.toml"
    document = _read_toml(path)
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
        root = _project_path(config.zetax_root, relative, "consumer", path)
        if root in roots:
            raise RuntimeError(f"Duplicate Rust consumer: {root}")
        roots.add(root)
        project = load_rust_project(root, config.forge_root)
        candidates = [project]
        for extra in _string_list(record.get("additional-manifests", []), "additional-manifests", path):
            if Path(extra).is_absolute():
                raise RuntimeError(f"Additional manifest must be relative: {extra}")
            manifest = _project_path(root, extra, "additional manifest", path)
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


class ProjectInspection(TypedDict):
    manifest: str
    toolchain: str
    errors: list[str]
    declarations: dict[str, object]
    locked: DependencyReport | None


def inspect_project(project: RustProject) -> ProjectInspection:
    errors: list[str] = []
    try:
        validate_rust_project(project)
    except RuntimeError as error:
        errors.append(str(error))
    report: ProjectInspection = {
        "manifest": str(project.manifest),
        "toolchain": project.toolchain_spec["channel"],
        "errors": errors,
        "declarations": {},
        "locked": None,
    }
    try:
        report["declarations"] = dict(workspace_dependencies(project))
        report["locked"] = locked_dependency_report(project)
    except RuntimeError as error:
        if str(error) not in errors:
            errors.append(str(error))
    return report


def resolved_metadata(project: RustProject, config: RepoConfig, *, timeout: float | None = None) -> dict[str, object]:
    manager = RustWorkspaceManager(project, config)
    completed = run_command(
        manager.cargo_command("metadata", "--format-version", "1", "--locked", "--offline"),
        cwd=project.workspace_dir, env={**config.env, **patch_environment(project, config)},
        check=False, capture_output=True, timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"Locked offline Cargo resolution failed for {project.manifest}: {detail}\n"
            "Prepare missing dependencies with the owning project's ./zbuild.py fetch; "
            "dependency changes require an explicit coordinated manifest/lockfile migration."
        )
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid Cargo metadata for {project.manifest}: {error}") from error
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Expected Cargo metadata object for {project.manifest}")
    verify_resolved_patches(metadata, project, config)
    return metadata


def resolved_project(project: RustProject, config: RepoConfig) -> DependencyReport:
    return resolved_dependency_report(resolved_metadata(project, config),
                                      project.catalog.dependencies, project.source_roots)


class RustDependencyAudit:
    def __init__(self, config: RepoConfig) -> None:
        self.config = config
        self.projects: list[RustProject] = []

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        result: dict[str, object] = {
            "scope": "registered Rust workspaces, including standalone consumers",
            "registry": str(self.config.forge_root / "rust" / "projects.toml"),
            "resolution": "locked/offline, no compilation" if request.action == "check" else "static",
        }
        if request.action == "metadata":
            result["consumers"] = [inspect_project(project) for project in registered_projects(self.config)]
        return result

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        self.projects = registered_projects(self.config)
        failures: list[str] = []
        for project in self.projects:
            report = inspect_project(project)
            failures.extend(report["errors"])
        if failures:
            raise RuntimeError("Rust dependency audit failed:\n" + "\n".join(failures))

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        failures: list[str] = []
        for project in self.projects:
            if request.action == "check":
                try:
                    report = resolved_project(project, self.config)
                except RuntimeError as error:
                    failures.append(str(error))
                    continue
            else:
                report = locked_dependency_report(project)
            # Audit is an explicit report surface; the structured dependency
            # details are intentional, unlike routine build progress logging.
            print(json.dumps({"manifest": str(project.manifest), **report}, sort_keys=True))
            failures.extend(report["errors"])
            log("I", f"Rust dependency audit manifest={project.manifest} status={'failed' if report['errors'] else 'passed'}")
        if failures:
            raise RuntimeError("Rust dependency audit failed:\n" + "\n".join(failures))
