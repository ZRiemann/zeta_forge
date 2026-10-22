"""Explicit upgrade discovery, separate from offline dependency governance."""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Mapping

from .build_cli import Request, log
from .config import RepoConfig
from .rust_audit import registered_projects, resolved_metadata
from .rust_dependencies import (
    approved_source, compatibility_series, dependency_chains, owned_manifest, resolved_dependency_report,
)
from .rust_releases import SemVer, TIMEOUT_SECONDS, git_releases, registry_releases, rust_version
from .rust_workspace import RustProject, load_catalog, validate_rust_project

MAX_CONCURRENT_QUERIES = 4


def _edge_declarations(
    owner: Mapping[str, object], target: Mapping[str, object], edge: Mapping[str, object],
) -> list[dict[str, object]]:
    """Correlate Cargo's resolved alias/kinds with its original requirements."""
    library_names = {t["name"].replace("-", "_") for t in target.get("targets", [])
                     if set(t.get("kind", [])) & {"lib", "rlib", "proc-macro", "dylib"}}
    library_names.add(target["name"].replace("-", "_"))
    matches = []
    for declaration in owner.get("dependencies", []):
        if declaration["name"] != target["name"]:
            continue
        rename = declaration.get("rename")
        aliases = {rename.replace("-", "_")} if rename else library_names
        if edge.get("name") not in aliases:
            continue
        if not any(k.get("kind") == declaration.get("kind") and k.get("target") == declaration.get("target")
                   for k in edge.get("dep_kinds", [])):
            continue
        source = declaration.get("source")
        target_source = target.get("source")
        if source != (target_source or "").split("#", 1)[0] and not (source is None and target_source is None):
            continue
        matches.append(declaration)
    return matches


def graph_context(project: RustProject, metadata: Mapping[str, object]) -> dict[str, dict[str, list]]:
    """Keep actual identities and incoming requirements from the current locked graph."""
    report = resolved_dependency_report(metadata, project.catalog.dependencies, project.source_roots)
    if report["errors"]:
        raise RuntimeError("Direct dependency drift: " + "; ".join(report["errors"]))
    packages = {p["id"]: p for p in metadata["packages"]}
    chains = dependency_chains(metadata)
    result = {name: {"consumers": [], "constraints": []} for name in project.catalog.dependencies}
    for edge in report["direct_edges"]:
        package = edge["dependency"]
        result[package["name"]]["consumers"].append({
            "workspace": str(project.manifest), "manifest": edge["manifest"],
            "package": edge["consumer"], "dependency": package,
            "toolchain": project.toolchain_spec["channel"],
            "alias": edge["alias"], "dep_kinds": edge["dep_kinds"],
        })
    for node in metadata["resolve"]["nodes"]:
        owner = packages[node["id"]]
        own = owner.get("source") is None and owned_manifest(owner["manifest_path"], project.source_roots)
        for edge in node["deps"]:
            target = packages[edge["pkg"]]
            approved = project.catalog.dependencies.get(target["name"])
            if approved is None or not approved_source(target.get("source"), approved):
                continue
            if "version" in approved and "=" + target["version"] != approved["version"]:
                continue
            for declaration in _edge_declarations(owner, target, edge):
                requirement = declaration["req"]
                exact = re.fullmatch(r"=\s*(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)", requirement)
                result[target["name"]]["constraints"].append({
                    "workspace": str(project.manifest), "consumer": owner["id"],
                    "consumer_is_zetax": own, "dependency": target["id"],
                    "requirement": requirement, "exact_version": exact[1] if exact else None,
                    "kind": declaration.get("kind"), "target": declaration.get("target"),
                    "chain": [*chains.get(owner["id"], []), f"{target['name']}@{target['version']}"],
                    "analysis": "exact" if exact else "needs_cargo_resolution",
                })
    return result


def msrv_status(release: Mapping[str, object], toolchains: list[str]) -> str:
    msrv = release.get("rust_version")
    if msrv is None:
        return "needs_verification"
    return "satisfied" if all(rust_version(t) >= rust_version(msrv) for t in toolchains) else "requires_newer_toolchain"


def upgrade_kind(current: str, version: str) -> str:
    before, after = SemVer.parse(current), SemVer.parse(version)
    if after < before:
        return "older"
    if after == before:
        return "current"
    if compatibility_series(current) != compatibility_series(version):
        return "breaking"
    return "patch" if before.core[:2] == after.core[:2] else "minor"


def select_candidates(
    name: str, entry: Mapping[str, object], discovery: Mapping[str, object],
    context: Mapping[str, list], fallback_toolchains: list[str],
) -> dict[str, object]:
    current = entry.get("version", "").removeprefix("=") or discovery["current_manifest"]["version"]
    toolchains = sorted({c["toolchain"] for c in context["consumers"]}) or fallback_toolchains
    releases = sorted(discovery["releases"], key=lambda r: SemVer.parse(r["version"]))
    annotated = [{**release, "msrv_status": msrv_status(release, toolchains),
                  "upgrade_type": upgrade_kind(current, release["version"])} for release in releases]
    newer = [r for r in annotated if SemVer.parse(r["version"]) > SemVer.parse(current)]
    same_series = [r for r in newer if compatibility_series(r["version"]) == compatibility_series(current)]
    breaking = [r for r in newer if compatibility_series(r["version"]) != compatibility_series(current)]
    eligible = [r for r in newer if r["msrv_status"] == "satisfied"]
    candidate = eligible[-1] if eligible else None
    stable = annotated[-1] if annotated else None
    blockers = []
    alternatives = {r["version"]: r for r in (same_series[-1:] + breaking[-1:])}
    for alternative in alternatives.values():
        for constraint in context["constraints"]:
            if not constraint["consumer_is_zetax"] and constraint["exact_version"] is not None and (
                SemVer.parse(constraint["exact_version"]) != SemVer.parse(alternative["version"])
                and compatibility_series(constraint["exact_version"]) == compatibility_series(alternative["version"])
            ):
                blockers.append({**constraint, "candidate": alternative["version"],
                                 "reason": "exact_version_constraint"})
    status = "update_available" if newer else "current"
    if stable and SemVer.parse(stable["version"]) < SemVer.parse(current):
        status = "current_ahead_of_stable"
    if not stable:
        status = "no_verified_stable_release"
    return {
        "name": name, "current": {"version": current, "commit": entry.get("rev"),
                                    "source": entry.get("git", "crates.io")},
        "status": status, "consumers": context["consumers"], "toolchains": toolchains,
        "toolchain_basis": "direct_consumers" if context["consumers"] else "registered_toolchains_no_direct_consumers",
        "latest_stable": stable, "compatible_series_update": same_series[-1] if same_series else None,
        "breaking_update": breaking[-1] if breaking else None,
        "toolchain_candidate": candidate,
        "compatible_series_toolchain_candidate": next(
            (r for r in reversed(same_series) if r["msrv_status"] == "satisfied"), None),
        "unverified_msrv_update": next((r for r in reversed(newer) if r["msrv_status"] == "needs_verification"), None),
        "constraints": context["constraints"], "known_blockers": blockers,
        "adoption": "needs_cargo_resolution_and_delivery_validation" if newer else "no_upgrade_recommended",
        "prereleases": [{**r, "newer_than_current": SemVer.parse(r["version"]) > SemVer.parse(current)}
                        for r in discovery["prereleases"]],
        "unverified_tags": discovery["unverified_tags"], "sources": discovery["sources"],
        "errors": discovery["errors"],
    }


def query_dependency(
    name: str, entry: Mapping[str, object], context: Mapping[str, list],
    toolchains: list[str], environment: Mapping[str, str],
) -> dict[str, object]:
    try:
        discovery = git_releases(name, entry, environment) if "git" in entry else registry_releases(name)
        return select_candidates(name, entry, discovery, context, toolchains)
    except (RuntimeError, OSError, ValueError, TypeError, KeyError) as error:
        versions = {c["dependency"]["version"] for c in context["consumers"] if "dependency" in c}
        current = entry.get("version", "").removeprefix("=") or (
            next(iter(versions)) if len(versions) == 1 else None
        )
        return {"name": name,
                "current": {"version": current, "commit": entry.get("rev"),
                            "source": entry.get("git", "crates.io")},
                "consumers": context["consumers"], "errors": [str(error)], "status": "query_failed"}


class RustUpdates:
    def __init__(self, config: RepoConfig) -> None:
        self.config = config
        self.projects: list[RustProject] = []

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        return {"scope": "catalog direct dependencies and registered consumers' fixed toolchains",
                "registry": str(self.config.forge_root / "rust/projects.toml"),
                "catalog": str(self.config.forge_root / "rust/catalog.toml"),
                "network": "only check execution queries crates.io and GitHub; no source downloads",
                "resolution": "current locked/offline graph, candidates require future Cargo resolution",
                "read_only": True, "timeout_seconds": TIMEOUT_SECONDS,
                "max_concurrent_queries": MAX_CONCURRENT_QUERIES}

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        self.projects = registered_projects(self.config)
        for project in self.projects:
            validate_rust_project(project)

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        catalog = load_catalog(self.config.forge_root)
        contexts = {name: {"consumers": [], "constraints": []} for name in catalog.dependencies}
        errors = []
        for project in self.projects:
            try:
                metadata = resolved_metadata(project, self.config, timeout=TIMEOUT_SECONDS)
                context = graph_context(project, metadata)
                for name, info in context.items():
                    for key in ("consumers", "constraints"):
                        contexts[name][key].extend(info[key])
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                errors.append(f"{project.manifest}: {error}")
        results = []
        toolchains = sorted({p.toolchain_spec["channel"] for p in self.projects})
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_QUERIES) as executor:
            tasks = {executor.submit(query_dependency, name, entry, contexts[name], toolchains, self.config.env): name
                     for name, entry in catalog.dependencies.items()}
            for task in as_completed(tasks):
                result = task.result()
                if errors:
                    # An incomplete graph must never imply that all consumers
                    # were checked, even when upstream discovery succeeded.
                    result["toolchain_candidate"] = None
                    result["compatible_series_toolchain_candidate"] = None
                    result["consumer_graph_status"] = "incomplete"
                    result["adoption"] = "blocked_by_incomplete_consumer_graph"
                results.append(result)
                log("I", f"Rust update query crate={result['name']} status={result['status']}")
        failures = [*errors, *(error for result in results for error in result["errors"])]
        updates = sum(r["status"] == "update_available" for r in results)
        report = {"schema_version": 1, "scope": "rust-updates", "results": sorted(results, key=lambda r: r["name"]),
                  "errors": failures, "summary": {"queried": len(results), "updates": updates, "errors": len(failures)}}
        print(json.dumps(report, sort_keys=True))
        log("I", f"Rust update candidates queried={len(results)} updates={updates} "
            f"errors={len(failures)}; no inputs changed")
        if failures:
            raise RuntimeError("Rust update discovery incomplete:\n" + "\n".join(failures))
