"""Identity-based inspection of Cargo dependency edges; no I/O or resolution."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable, Mapping, TypedDict
from urllib.parse import parse_qs, urlsplit, urlunsplit

CRATES_IO_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
THIRD_PARTY_DIRECTORIES = frozenset({"3rd", "third_party", "external", "vendor"})


class DependencyReport(TypedDict):
    schema_version: int
    direct_edges: list[dict[str, object]]
    transitive_versions: list[dict[str, object]]
    coexisting: list[dict[str, object]]
    errors: list[str]


def compatibility_series(version: str) -> tuple[int, ...]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    try:
        major, minor, patch = (int(part) for part in core.split("."))
    except ValueError as error:
        raise RuntimeError(f"Invalid Cargo package version: {version!r}") from error
    if min(major, minor, patch) < 0:
        raise RuntimeError(f"Invalid Cargo package version: {version!r}")
    return (major,) if major else ((0, minor) if minor else (0, 0, patch))


def approved_source(source: str | None, approved: Mapping[str, object]) -> bool:
    if "version" in approved:
        return source == CRATES_IO_SOURCE
    if not source or not source.startswith("git+"):
        return False
    parsed = urlsplit(source[4:])
    origin = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return (origin == approved["git"] and parsed.fragment == approved["rev"]
            and parse_qs(parsed.query) == {"rev": [approved["rev"]]})


def package_identity(raw: Mapping[str, object]) -> dict[str, object]:
    name, version, source = raw.get("name"), raw.get("version"), raw.get("source")
    if not isinstance(name, str) or not isinstance(version, str) or (
        source is not None and not isinstance(source, str)
    ):
        raise RuntimeError(f"Invalid Cargo package identity: {raw!r}")
    compatibility_series(version)
    return {"id": raw.get("id", f"{name} {version} ({source or 'path'})"),
            "name": name, "version": version, "source": source}


def owned_manifest(manifest: Path | str, roots: Iterable[Path]) -> bool:
    path = Path(manifest).resolve()
    return any(path.is_relative_to(root.resolve()) and not (
        set(path.relative_to(root.resolve()).parts) & THIRD_PARTY_DIRECTORIES
    ) for root in roots)


def dependency_chains(metadata: Mapping[str, object]) -> dict[str, list[str]]:
    packages, resolve, roots = metadata.get("packages"), metadata.get("resolve"), metadata.get("workspace_members")
    if not isinstance(packages, list) or not isinstance(resolve, dict) or not isinstance(roots, list):
        raise RuntimeError("Cargo metadata must contain packages, workspace_members and resolve")
    nodes = resolve.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("Cargo metadata resolve.nodes is missing")
    try:
        labels = {p["id"]: f"{p['name']}@{p['version']}" for p in packages}
        edges = {node["id"]: [dep["pkg"] for dep in node["deps"]] for node in nodes}
        paths = {root: [labels[root]] for root in roots}
        pending = deque(roots)
        while pending:
            current = pending.popleft()
            for child in edges.get(current, []):
                if child not in paths:
                    paths[child] = [*paths[current], labels[child]]
                    pending.append(child)
        return paths
    except (KeyError, TypeError) as error:
        raise RuntimeError(f"Invalid Cargo metadata dependency graph: {error}") from error


def dependency_report(
    packages: Iterable[Mapping[str, object]],
    catalog: Mapping[str, Mapping[str, object]],
    *, direct_edges: Iterable[dict[str, object]] = (), owned_ids: Iterable[str] = (),
    errors: Iterable[str] = (),
) -> DependencyReport:
    report: DependencyReport = {"schema_version": 2, "direct_edges": list(direct_edges),
                               "transitive_versions": [], "coexisting": [], "errors": list(errors)}
    identities = [package_identity(raw) for raw in packages]
    targeted = {edge["dependency"]["id"] for edge in report["direct_edges"]}
    owned = set(owned_ids)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in identities:
        grouped[record["name"]].append(record)
        if record["id"] not in targeted | owned:
            report["transitive_versions"].append(record)
    for edge in report["direct_edges"]:
        record = edge["dependency"]
        approved = catalog.get(record["name"])
        if (approved is None
            or ("version" in approved and "=" + record["version"] != approved["version"])
            or not approved_source(record["source"], approved)):
            report["errors"].append(
                f"{edge['manifest']} {edge['consumer']['name']} -> {record['name']}: "
                f"actual={record['version']} source={record['source']!r}; required={approved!r}"
            )
    for name in sorted(grouped):
        records = grouped[name]
        if len({(r["version"], r["source"]) for r in records}) > 1:
            report["coexisting"].append({"name": name, "packages": records})
    return report


def resolved_dependency_report(
    metadata: Mapping[str, object], catalog: Mapping[str, Mapping[str, object]], roots: Iterable[Path],
) -> DependencyReport:
    chains = dependency_chains(metadata)
    packages = {p["id"]: p for p in metadata["packages"]}
    owned = {key for key, p in packages.items() if p.get("source") is None
             and isinstance(p.get("manifest_path"), str) and owned_manifest(Path(p["manifest_path"]), roots)}
    if not set(metadata["workspace_members"]).issubset(owned):
        raise RuntimeError("Cargo workspace members must belong to registered own source roots")
    edges = []
    for node in metadata["resolve"]["nodes"]:
        if node["id"] not in owned:
            continue
        consumer = packages[node["id"]]
        for dep in node["deps"]:
            if dep["pkg"] in owned:
                continue
            target = packages[dep["pkg"]]
            edges.append({"consumer": package_identity(consumer), "manifest": consumer["manifest_path"],
                          "dependency": package_identity(target), "alias": dep.get("name"),
                          "dep_kinds": dep.get("dep_kinds", []),
                          "chain": [*chains.get(node["id"], []), f"{target['name']}@{target['version']}"]})
    return dependency_report(packages.values(), catalog, direct_edges=edges, owned_ids=owned)
