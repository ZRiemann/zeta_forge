"""Publish verified staged artifacts and maintain prefix ownership."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import native_transaction as transaction
from .build_cli import log
from .build_ops import validate_generated_path
from .conan_state import writer_lock
from .native_artifacts import relative_link_target, require_receipt

LINK_NAMES = re.compile(
    rb'find_(?:dependency|package)\s*\(\s*"?([\w.+-]+)|\b([\w.+-]+)::',
    re.IGNORECASE,
)
PACKAGE_ALIASES = {"xz_utils": {"liblzma"}, "libsodium": {"sodium"}}


@dataclass(frozen=True)
class SelectedStage:
    target: str
    profile: str
    path: Path
    receipt: dict

    @property
    def key(self) -> str:
        return f"{self.profile}:{self.target}"


def content_equal(left: dict | None, right: dict | None) -> bool:
    if left is None or right is None:
        return left is right
    return all(left.get(key) == right.get(key) for key in ("kind", "sha256", "target"))


def relocate_metadata(
    data: bytes,
    selected: SelectedStage,
    prefix: Path,
    forge_root: Path,
) -> bytes:
    mappings = [selected.path]
    mappings.extend(selected.path.parent / name for name in selected.receipt["dependencies"])
    boundary = rb'(?=$|[/\\;"\s])'
    for source in mappings:
        pattern = re.escape(os.fsencode(source)) + boundary
        data = re.sub(pattern, lambda _: os.fsencode(prefix), data)
    evidence = data
    allowed = [prefix]
    allowed.extend(Path(ref["path"]) for ref in selected.receipt["external"])
    for path in sorted(set(allowed), key=lambda path: len(str(path)), reverse=True):
        evidence = re.sub(re.escape(os.fsencode(path)) + boundary, b"<external>", evidence)
    root = re.escape(os.fsencode(forge_root))
    if re.search(root + rb'/(?:build(?:/|[";\s]|$)|builder/[^/]+/build(?:/|[";\s]|$))', evidence):
        raise RuntimeError(f"Unresolved Forge build/staging reference in {selected.target} metadata")
    return data


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def link_packages(receipt: dict, metadata: bytes) -> dict:
    conan = receipt.get("conan")
    if not conan:
        return {}
    names = {
        normalized_name((package or namespace).decode())
        for package, namespace in LINK_NAMES.findall(metadata)
    }
    packages = {}
    for node in conan["graph"]:
        if node["context"] != "host":
            continue
        name = node["ref"].split("/", 1)[0]
        aliases = {normalized_name(name), *PACKAGE_ALIASES.get(name, ())}
        folder = node.get("package_folder")
        if aliases & names or (folder and os.fsencode(folder) in metadata):
            packages[name] = {
                "ref": node["ref"],
                "package_id": node["package_id"],
                "package_revision": node["package_revision"],
                "settings": node.get("settings", {}),
                "options": node.get("options", {}),
                "links_iostreams": b"boost::iostreams" in metadata.lower(),
            }
    return packages


def compatible_package(name: str, left: dict, right: dict, targets: set[str]) -> bool:
    if left["ref"] != right["ref"] or left["settings"] != right["settings"]:
        return False
    if left["package_id"] == right["package_id"]:
        return left["package_revision"] == right["package_revision"]
    # Folly's exported Boost components do not use the iostreams zlib option.
    if name != "boost" or "folly" not in targets or len(targets) != 2:
        return False
    old_options = dict(left["options"])
    new_options = dict(right["options"])
    return (
        old_options.pop("zlib", None) != new_options.pop("zlib", None)
        and old_options == new_options
    )


def check_dependencies(owners: dict) -> None:
    for key, record in owners.items():
        for prerequisite, identity in record["dependencies"].items():
            name = f"{record['profile']}:{prerequisite}"
            installed = owners.get(name)
            if installed is None or installed["artifact_id"] != identity:
                raise RuntimeError(
                    f"{key} requires matching published {name}; explicitly install {prerequisite} "
                    f"with {record['target']} for {record['profile']}"
                )
    keys = sorted(owners)
    for index, key in enumerate(keys):
        left = owners[key]
        for other in keys[index + 1:]:
            right = owners[other]
            if left["profile"] != right["profile"]:
                continue
            common = left["link_packages"].keys() & right["link_packages"].keys()
            for package in sorted(common):
                a, b = left["link_packages"][package], right["link_packages"][package]
                targets = {left["target"], right["target"]}
                folly = a if left["target"] == "folly" else b
                iostreams_conflict = (
                    package == "boost" and "folly" in targets and folly["links_iostreams"]
                    and a["package_id"] != b["package_id"]
                )
                if iostreams_conflict or not compatible_package(package, a, b, targets):
                    raise RuntimeError(f"Incompatible Conan link dependency {package} for {key} and {other}")


def collect_owner(
    selected: SelectedStage, work: Path, prefix: Path, forge_root: Path
) -> tuple[dict, dict[str, Path]]:
    files = {}
    candidates = {}
    metadata = []
    for entry in selected.receipt["entries"]:
        if entry["kind"] == "directory":
            continue
        relative = entry["path"]
        transaction.public_path(prefix, relative)
        source = selected.path / relative
        candidate = work / relative
        if entry["kind"] == "symlink":
            relative_link_target(relative, entry["target"])
        transaction.copy_entry(source, candidate)
        if transaction.file_identity(candidate) != {key: value for key, value in entry.items() if key != "path"}:
            raise RuntimeError(f"Native stage artifact changed during snapshot: {source}")
        if entry["kind"] == "file" and candidate.suffix in {".cmake", ".pc"}:
            data = candidate.read_bytes()
            metadata.append(data)
            updated = relocate_metadata(data, selected, prefix, forge_root)
            if updated != data:
                candidate.write_bytes(updated)
        files[relative] = transaction.file_identity(candidate)
        candidates[relative] = candidate
    return {
        "target": selected.target,
        "profile": selected.profile,
        "source_receipt_digest": selected.receipt["build_id"],
        "artifact_id": selected.receipt["artifact_id"],
        "dependencies": selected.receipt["dependencies"],
        "link_packages": link_packages(selected.receipt, b"\n".join(metadata)),
        "files": files,
    }, candidates


def collect_paths(owners: dict, prefix: Path) -> dict[str, tuple[dict, set[str]]]:
    paths: dict[str, tuple[dict, set[str]]] = {}
    for key, record in owners.items():
        for relative, identity in record["files"].items():
            transaction.public_path(prefix, relative)
            if relative not in paths:
                paths[relative] = identity, {key}
                continue
            expected, keys = paths[relative]
            if not content_equal(identity, expected):
                raise RuntimeError(f"Conflicting native owners for {relative}: {', '.join(sorted(keys | {key}))}")
            keys.add(key)
    return paths


def publication_changes(
    prefix: Path, previous: dict, owners: dict, selected: set[str], candidates: dict[str, Path]
) -> dict:
    old_paths = collect_paths(previous, prefix)
    new_paths = collect_paths(owners, prefix)
    changes = {}
    affected = set(candidates)
    affected.update(path for path, (_, keys) in old_paths.items() if keys & selected)
    for relative in sorted(affected):
        destination = transaction.public_path(prefix, relative)
        old, _ = old_paths.get(relative, (None, set()))
        new, keys = new_paths.get(relative, (None, set()))
        actual = transaction.file_identity(destination)
        if new is None:
            if actual is not None and actual == old:
                changes[relative] = (actual, None, None)
            continue
        if relative not in candidates:
            continue
        if content_equal(new, actual):
            for key in keys:
                owners[key]["files"][relative] = actual
            if old is None:
                log("I", f"adopt identical native file {destination}")
            continue
        if actual is not None and actual != old:
            raise RuntimeError(f"Native publication conflict at {destination}: found {actual}, expected {new}")
        candidate = candidates[relative]
        replacement = transaction.file_identity(candidate)
        if not content_equal(replacement, new):
            raise RuntimeError(f"Conflicting native publication candidates for {destination}")
        for key in keys:
            owners[key]["files"][relative] = replacement
        changes[relative] = (actual, replacement, candidate)
    return changes


def publish(prefix: Path, forge_root: Path, stages: tuple[tuple[str, str, Path], ...]) -> None:
    if not prefix.is_absolute() or prefix.is_symlink():
        raise RuntimeError(f"Invalid native publication prefix: {prefix}")
    prefix = prefix.resolve()
    state = prefix / transaction.STATE_PATH
    validate_generated_path(state / "writer.lock", prefix)
    with writer_lock(state / "writer.lock"):
        transaction.recover_transaction(prefix, state)
        previous = transaction.ownership_index(state)["owners"]
        owners = {key: {**record, "files": dict(record["files"])} for key, record in previous.items()}
        selected_keys = set()
        with tempfile.TemporaryDirectory(dir=state, prefix=".draft-") as directory:
            work = Path(directory)
            candidates = {}
            for target, profile, path in stages:
                try:
                    current = require_receipt(path, target, profile)
                except (OSError, RuntimeError) as error:
                    raise RuntimeError(
                        f"{target} has no valid {profile} build; "
                        f"run ./zbuild.py build {target} --profile {profile} first. {error}"
                    ) from error
                stage = SelectedStage(target, profile, path, current)
                selected_keys.add(stage.key)
                log("I", f"publish target={target} profile={profile} build={current['build_id'][:12]}")
                record, files = collect_owner(stage, work / "candidates" / stage.key, prefix, forge_root)
                owners[stage.key] = record
                candidates.update(files)
            check_dependencies(owners)
            changes = publication_changes(prefix, previous, owners, selected_keys, candidates)
            index = {"schema": transaction.SCHEMA, "transaction": uuid.uuid4().hex, "owners": owners}
            transaction.prepare_transaction(prefix, work, changes, index)
            pending = state / "pending"
            work.rename(pending)
            transaction.sync_directory(state)
            try:
                transaction.apply_transaction(prefix, state, pending)
            except BaseException:
                transaction.recover_transaction(prefix, state)
                raise
            transaction.recover_transaction(prefix, state)
            log("I", f"published {', '.join(sorted(selected_keys))} to {prefix}")
