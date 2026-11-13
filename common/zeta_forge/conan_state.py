"""Persistent Conan lock generations and receipts for generated files."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .conan_inputs import safe_value
from .state_io import canonical_json as canonical_json
from .state_io import digest_json as digest_json
from .state_io import file_digest as file_digest
from .state_io import read_json as read_state_json
from .state_io import write_json as write_json
from .state_io import writer_lock as writer_lock

SCHEMA = 1
LOCK_SCHEMA = "0.5"


def read_json(path: Path) -> dict[str, object]:
    return read_state_json(path, artifact="Conan state")


def validate_lock(path: Path) -> None:
    value = read_json(path)
    if value.get("version") != LOCK_SCHEMA or any(
        not isinstance(value.get(key), list)
        for key in ("requires", "build_requires", "python_requires", "config_requires")
    ):
        raise RuntimeError(f"Unsupported Conan lock schema at {path}; refresh with supported Conan 2")


def graph_summary(path: Path) -> list[dict]:
    graph = read_json(path).get("graph")
    if not isinstance(graph, dict) or graph.get("error") or not isinstance(graph.get("nodes"), dict):
        raise RuntimeError(f"Unsupported or unsuccessful Conan graph JSON at {path}")
    nodes = graph["nodes"]
    packages = []
    for node in nodes.values():
        reference = node.get("ref")
        if not isinstance(reference, str) or "/" not in reference:
            continue
        edges = [
            (nodes[key].get("context"), nodes[key].get("ref"))
            for key in node.get("dependencies", {})
            if key in nodes and isinstance(nodes[key].get("ref"), str)
        ]
        packages.append({
            "context": node["context"],
            "ref": reference,
            "package_id": node.get("package_id"),
            "package_revision": node.get("prev"),
            "package_folder": node.get("package_folder"),
            "settings": safe_value(node.get("settings", {})),
            "options": safe_value(node.get("options", {})),
            "requires": sorted(edges),
        })
    if not packages:
        raise RuntimeError(f"Conan graph at {path} has no package nodes")
    return sorted(packages, key=lambda node: (node["context"], node["ref"]))


def generator_inventory(directory: Path) -> dict[str, str] | None:
    if directory.is_symlink() or not (directory / "conan_toolchain.cmake").is_file():
        return None
    inventory = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            return None
        if path.is_file():
            inventory[path.relative_to(directory).as_posix()] = file_digest(path)
    return inventory


def active_generation(directory: Path) -> tuple[str, Path, dict] | None:
    pointer = directory / "active.json"
    if not pointer.exists():
        return None
    try:
        data = read_json(pointer)
        identity = data.get("generation")
        if data.get("schema") != SCHEMA or not isinstance(identity, str) or not re.fullmatch(
            r"[a-f0-9]{64}", identity
        ):
            raise RuntimeError("invalid active generation")
        generation = directory / "generations" / identity
        lock = generation / "conan.lock"
        receipt = read_json(generation / "resolution.json")
        validate_lock(lock)
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("input_fingerprint") != directory.name
            or not isinstance(receipt.get("generators"), dict)
            or not isinstance(receipt.get("graph"), list)
            or digest_json(receipt) != identity
            or receipt.get("lock_sha256") != file_digest(lock)
        ):
            raise RuntimeError("invalid resolution receipt or lock digest")
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"Invalid Conan generation under {directory}; use --refresh-conan-lock") from error
    return identity, lock, receipt


def store_generation(directory: Path, lock: Path, receipt: dict) -> str:
    identity = digest_json(receipt)
    generations = directory / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generation = generations / identity
    if not generation.exists():
        with tempfile.TemporaryDirectory(dir=generations) as temporary:
            candidate = Path(temporary)
            (candidate / "conan.lock").write_bytes(lock.read_bytes())
            write_json(candidate / "resolution.json", receipt)
            candidate.rename(generation)
    return identity
