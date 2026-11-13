"""Durable per-file application and recovery for native publication."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

from .build_cli import log
from .conan_inputs import file_digest
from .conan_state import read_json, write_json

STATE_PATH = PurePosixPath("share/zeta_forge/native")
SCHEMA = 1


def public_path(prefix: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not path.parts or path.is_absolute() or path.as_posix() != relative
        or ".." in path.parts or "\\" in relative or path.is_relative_to(STATE_PATH)
    ):
        raise RuntimeError(f"Unsafe native publication path: {relative}")
    current = prefix
    for part in path.parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RuntimeError(f"Unsafe native publication parent: {current}")
    return prefix.joinpath(*path.parts)


def file_identity(path: Path) -> dict | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return {"kind": "symlink", "mode": stat.S_IMODE(mode), "target": os.readlink(path)}
    if stat.S_ISREG(mode):
        return {"kind": "file", "mode": stat.S_IMODE(mode), "sha256": file_digest(path)}
    raise RuntimeError(f"Native publication expected a file or symlink: {path}")


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def sync_tree(path: Path) -> None:
    for root, _, files in os.walk(path, topdown=False):
        directory = Path(root)
        for name in files:
            file = directory / name
            if not file.is_symlink():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
        sync_directory(directory)


def copy_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)


def replace_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_symlink():
        with source.open("rb") as stream:
            os.fsync(stream.fileno())
    source.replace(destination)
    sync_directory(destination.parent)
    sync_directory(source.parent)


def ownership_index(state: Path) -> dict:
    index = state / "owners.json"
    if index.is_symlink():
        raise RuntimeError(f"Unsafe native ownership index: {index}")
    if not index.exists():
        return {"schema": SCHEMA, "transaction": None, "owners": {}}
    data = read_json(index)
    if data.get("schema") != SCHEMA or not isinstance(data.get("owners"), dict):
        raise RuntimeError(f"Unsupported native ownership index: {index}")
    for key, owner in data["owners"].items():
        if (
            not isinstance(owner, dict) or key != f"{owner.get('profile')}:{owner.get('target')}"
            or not isinstance(owner.get("artifact_id"), str)
            or any(not isinstance(owner.get(field), dict) for field in (
                "dependencies", "link_packages", "files",
            ))
            or any(not isinstance(identity, dict) for identity in owner["files"].values())
        ):
            raise RuntimeError(f"Malformed native ownership entry {key} at {index}")
    return data


def prepare_transaction(
    prefix: Path,
    work: Path,
    changes: dict[str, tuple[dict | None, dict | None, Path | None]],
    index: dict,
) -> None:
    operations = []
    missing_dirs = set()
    for relative, (old, new, source) in sorted(changes.items()):
        destination = public_path(prefix, relative)
        if old is not None:
            copy_entry(destination, work / "old" / relative)
        if new is not None:
            if source is None:
                raise RuntimeError(f"Missing native publication candidate: {relative}")
            copy_entry(source, work / "new" / relative)
            for parent in destination.parents:
                if parent == prefix:
                    break
                if not parent.exists():
                    missing_dirs.add(parent.relative_to(prefix).as_posix())
        operations.append({"path": relative, "old": old, "new": new})
    write_json(work / "owners.json", index)
    write_json(work / "journal.json", {
        "schema": SCHEMA,
        "transaction": index["transaction"],
        "operations": operations,
        "missing_dirs": sorted(missing_dirs, key=lambda path: (path.count("/"), path)),
    })
    sync_tree(work)


def apply_transaction(prefix: Path, state: Path, pending: Path) -> None:
    journal = read_json(pending / "journal.json")
    for operation in journal["operations"]:
        relative = operation["path"]
        destination = public_path(prefix, relative)
        if file_identity(destination) != operation["old"]:
            raise RuntimeError(f"Native publication destination changed: {destination}")
        if operation["new"] is None:
            destination.unlink()
            sync_directory(destination.parent)
        else:
            candidate = pending / "new" / relative
            if file_identity(candidate) != operation["new"]:
                raise RuntimeError(f"Native publication candidate changed: {candidate}")
            replace_entry(candidate, destination)
    for relative in reversed(journal["missing_dirs"]):
        sync_directory(public_path(prefix, relative).parent)
    replace_entry(pending / "owners.json", state / "owners.json")


def recover_transaction(prefix: Path, state: Path) -> None:
    pending = state / "pending"
    if pending.is_symlink():
        raise RuntimeError(f"Unsafe native transaction directory: {pending}")
    if not pending.exists():
        return
    try:
        journal = read_json(pending / "journal.json")
        if (
            journal.get("schema") != SCHEMA
            or not isinstance(journal.get("transaction"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", journal["transaction"])
            or not isinstance(journal.get("operations"), list)
            or not isinstance(journal.get("missing_dirs"), list)
        ):
            raise RuntimeError("invalid native publication journal")
        committed = ownership_index(state).get("transaction") == journal["transaction"]
        if not committed:
            for operation in reversed(journal["operations"]):
                relative = operation["path"]
                destination = public_path(prefix, relative)
                actual = file_identity(destination)
                if actual == operation["old"]:
                    continue
                if actual != operation["new"]:
                    raise RuntimeError(f"Native publication destination changed during recovery: {destination}")
                if operation["old"] is None:
                    destination.unlink()
                    sync_directory(destination.parent)
                else:
                    backup = pending / "old" / relative
                    if file_identity(backup) != operation["old"]:
                        raise RuntimeError(f"Native publication backup changed: {backup}")
                    restored = pending / "restore" / relative
                    restored.unlink(missing_ok=True)
                    copy_entry(backup, restored)
                    replace_entry(restored, destination)
            for relative in reversed(journal["missing_dirs"]):
                directory = public_path(prefix, relative)
                if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                    directory.rmdir()
                    sync_directory(directory.parent)
        retired = state / f".retired-{journal['transaction']}"
        pending.rename(retired)
        sync_directory(state)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        raise RuntimeError(f"Native publication recovery failed. Journal retained at {pending}: {error}") from error
    try:
        shutil.rmtree(retired)
    except OSError:
        log("W", f"Native publication completed; retired transaction remains at {retired}")
