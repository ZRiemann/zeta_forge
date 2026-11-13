"""Native staged inventories and build receipts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .conan_openssl import MANIFEST_NAME, validate_openssl_package
from .conan_state import active_generation
from .process import run_command
from .state_io import digest_json, file_digest, read_json, write_json

RECEIPT_NAME = ".zeta-build-complete.json"
RECEIPT_SCHEMA = 1


def relative_link_target(relative: str, target: str) -> PurePosixPath:
    link = PurePosixPath(target)
    parts = list(PurePosixPath(relative).parent.parts)
    if link.is_absolute() or "\\" in target:
        raise RuntimeError(f"Unsafe native stage symlink: {relative} -> {target}")
    for part in link.parts:
        if part == "..":
            if not parts:
                raise RuntimeError(f"Unsafe native stage symlink: {relative} -> {target}")
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts)


def inventory(stage: Path) -> list[dict]:
    if stage.is_symlink() or not stage.is_dir():
        raise RuntimeError(f"Native stage is missing or unsafe: {stage}")
    root = stage.resolve()
    entries = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage).as_posix()
        if relative == RECEIPT_NAME:
            continue
        mode = path.lstat().st_mode
        entry = {"path": relative, "mode": stat.S_IMODE(mode)}
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            relative_link_target(relative, target)
            try:
                safe = path.resolve(strict=True).is_relative_to(root)
            except (OSError, RuntimeError):
                safe = False
            if not safe:
                raise RuntimeError(f"Unsafe native stage symlink: {path} -> {target}")
            entry.update(kind="symlink", target=target)
        elif stat.S_ISREG(mode):
            entry.update(kind="file", sha256=file_digest(path))
        elif stat.S_ISDIR(mode):
            entry.update(kind="directory")
        else:
            raise RuntimeError(f"Unsupported native stage entry: {path}")
        entries.append(entry)
    if not any(entry["kind"] == "file" for entry in entries):
        raise RuntimeError(f"Native stage contains no regular artifacts: {stage}")
    return entries


def source_identity(root: Path, env: dict[str, str]) -> dict:
    identity = {"path": str(root), "revision": None, "dirty": None}
    git = shutil.which("git", path=env.get("PATH"))
    if git is None:
        return identity

    def query(*args: str) -> subprocess.CompletedProcess[str]:
        return run_command(
            [git, "-C", str(root), *args],
            env=dict(env, GIT_OPTIONAL_LOCKS="0"),
            capture_output=True,
            log_command=False,
            check=False,
        )

    worktree = query("rev-parse", "--show-toplevel")
    if worktree.returncode != 0 or Path(worktree.stdout.strip()).resolve() != root.resolve():
        return identity
    revision = query("rev-parse", "HEAD")
    dirty = query("status", "--porcelain", "--untracked-files=normal")
    if revision.returncode == 0:
        identity["revision"] = revision.stdout.strip()
    if dirty.returncode == 0:
        identity["dirty"] = bool(dirty.stdout)
    return identity


def conan_identity(forge_root: Path, target: str, profile: str, output_folder: Path) -> dict:
    receipt = read_json(output_folder / ".forge-conan-receipt.json", artifact="Conan output receipt")
    fingerprint = receipt.get("input_fingerprint")
    if (
        not isinstance(fingerprint, str) or len(fingerprint) != 64
        or any(letter not in "0123456789abcdef" for letter in fingerprint)
    ):
        raise RuntimeError(f"Invalid Conan fingerprint for native target {target}")
    state = forge_root / "build" / "conan-state" / target / profile / fingerprint
    generation = active_generation(state)
    if generation is None or generation[0] != receipt.get("generation"):
        raise RuntimeError(f"Conan output and resolution receipt disagree for native target {target}")
    graph = generation[2]["graph"]
    return {
        "input_fingerprint": fingerprint,
        "generation": generation[0],
        "lock_sha256": generation[2]["lock_sha256"],
        "graph": graph,
    }


def external_references(
    candidate: Path, target: str, native_profile: str, conan: dict | None, rapidjson: Path | None
) -> list[dict]:
    refs = []
    graph = conan["graph"] if conan else ()
    for node in graph:
        package = node.get("package_folder")
        if node["context"] == "host" and package:
            refs.append({"kind": "conan", "path": package, "ref": node["ref"]})
    if target == "deps":
        manifest = candidate / "lib" / "cmake" / "zeta_deps" / native_profile / MANIFEST_NAME
        data = read_json(manifest, artifact="staged OpenSSL manifest")
        package = data.get("package_dir")
        expected = {
            node.get("package_folder") for node in graph
            if node["context"] == "host"
            and node["ref"].split("#", 1)[0] == f"openssl/{data.get('version')}"
        }
        if data.get("schema") != 1 or not isinstance(package, str) or package not in expected:
            raise RuntimeError(f"Staged OpenSSL manifest disagrees with the Conan resolution: {manifest}")
        refs.append({"kind": "openssl", "path": package})
        if rapidjson is not None:
            header = rapidjson / "include" / "rapidjson" / "rapidjson.h"
            refs.append({"kind": "source_header", "path": str(header)})
    validate_external(refs)
    return sorted(refs, key=lambda ref: (ref["kind"], ref["path"]))


def validate_external(refs: list[dict]) -> None:
    for reference in refs:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise RuntimeError("Malformed native external reference")
        path = Path(reference["path"])
        if not path.is_absolute():
            raise RuntimeError(f"Native external reference is not absolute: {path}")
        kind = reference.get("kind")
        if kind == "openssl":
            validate_openssl_package(path)
        elif kind == "conan" and path.is_dir():
            continue
        elif kind == "source_header" and path.is_file():
            continue
        else:
            raise RuntimeError(f"Native external reference is missing or unsupported: {path}")


def make_receipt(
    candidate: Path,
    target: str,
    profile: str,
    *,
    sources: list[dict],
    conan: dict | None,
    references: list[dict],
    dependencies: dict[str, str],
) -> dict:
    entries = inventory(candidate)
    identity = {"entries": entries, "external": references, "dependencies": dependencies}
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "target": target,
        "profile": profile,
        **identity,
        "artifact_id": digest_json(identity),
        "sources": sources,
        "conan": conan,
    }
    return {**receipt, "build_id": digest_json(receipt)}


def promote(candidate: Path, stage: Path, backup: Path, receipt: dict) -> None:
    if stage.is_symlink():
        raise RuntimeError(f"Unsafe native stage: {stage}")
    if stage.exists():
        stage.rename(backup)
    try:
        candidate.rename(stage)
    except BaseException:
        if backup.exists():
            backup.rename(stage)
        raise
    write_json(stage / RECEIPT_NAME, receipt)


def require_receipt(stage: Path, target: str, profile: str) -> dict:
    path = stage / RECEIPT_NAME
    if stage.is_symlink() or path.is_symlink():
        raise RuntimeError(f"Unsafe native build receipt: {path}")
    receipt = read_json(path, artifact="native build receipt")
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("target") != target
        or receipt.get("profile") != profile
        or receipt.get("build_id") != digest_json({key: val for key, val in receipt.items() if key != "build_id"})
    ):
        raise RuntimeError(f"Unsupported or mismatched native build receipt: {path}")
    if receipt.get("entries") != inventory(stage):
        raise RuntimeError(f"Native stage contents differ from the build receipt: {stage}")
    external = receipt.get("external")
    if not isinstance(external, list):
        raise RuntimeError(f"Malformed native external reference inventory: {path}")
    validate_external(external)
    return receipt
