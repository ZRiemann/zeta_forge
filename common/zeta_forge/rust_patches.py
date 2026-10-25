"""Explicit Forge-owned, checksum-bound source repairs for published Rust crates.

Only fetch may acquire/materialize a source. Other operations verify the complete
tree and fail closed. Cargo caches are reused as download artifacts, never edited.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import tomllib
from typing import Mapping, TYPE_CHECKING, TypedDict
from urllib.request import urlopen

if TYPE_CHECKING:
    from .config import RepoConfig
    from .rust_workspace import RustProject

PATCH_NAME = "wry-0.53.5-gtk-ipc-weak"


class Replacement(TypedDict):
    before: str
    after: str


class SourceRecipe(TypedDict):
    schema: int
    package: str
    version: str
    archive_sha256: str
    tree_sha256: str
    upstream: str
    file: str
    replacements: list[Replacement]


def patch_recipe(forge_root: Path, names: tuple[str, ...]) -> tuple[SourceRecipe, Path]:
    if names != (PATCH_NAME,):
        raise RuntimeError(f"Unsupported Forge Rust source-patches: {names!r}")
    recipe_path = forge_root / "rust/patches" / f"{PATCH_NAME}.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if (recipe.get("schema") != 1 or recipe.get("package") != "wry"
        or recipe.get("version") != "0.53.5"
        or any(not re.fullmatch(r"[0-9a-f]{64}", recipe.get(key, ""))
               for key in ("archive_sha256", "tree_sha256"))):
        raise RuntimeError(f"Invalid Forge Rust source repair: {recipe_path}")
    identity = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
    return recipe, forge_root / "build/rust/source-patches" / identity / "wry"


def tree_digest(root: Path) -> str:
    fingerprint = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        if path.is_symlink():
            raise RuntimeError(f"Symlink in managed Rust source: {path}")
        if path.is_file():
            fingerprint.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return fingerprint.hexdigest()


def verify_source(recipe: SourceRecipe, root: Path) -> None:
    if not root.is_dir() or tree_digest(root) != recipe["tree_sha256"]:
        raise RuntimeError(f"Missing or modified Forge Rust source at {root}; use the project's zbuild.py fetch")


def materialize(recipe: SourceRecipe, root: Path, payload: bytes) -> None:
    if hashlib.sha256(payload).hexdigest() != recipe["archive_sha256"]:
        raise RuntimeError("Forge Rust source archive checksum mismatch")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root.parent) as directory:
        prepared = Path(directory) / "source"
        prepared.mkdir()
        prefix = f"{recipe['package']}-{recipe['version']}/"
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive.getmembers():
                relative = member.name.removeprefix(prefix)
                if (not member.name.startswith(prefix) or Path(relative).is_absolute()
                    or ".." in Path(relative).parts or not (member.isdir() or member.isfile())):
                    raise RuntimeError("Unsafe member in Forge Rust source archive")
                destination = prepared / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        destination.write_bytes(source.read())
        target = prepared / recipe["file"]
        text = target.read_text(encoding="utf-8")
        for replacement in recipe["replacements"]:
            if text.count(replacement["before"]) != 1:
                raise RuntimeError("Forge Rust source repair does not match the pinned input")
            text = text.replace(replacement["before"], replacement["after"])
        target.write_text(text, encoding="utf-8")
        verify_source(recipe, prepared)
        if root.exists():
            verify_source(recipe, root)
        else:
            prepared.rename(root)


def prepare_patches(project: RustProject, config: RepoConfig) -> None:
    if not project.source_patches:
        return
    recipe, root = patch_recipe(config.forge_root, project.source_patches)
    if root.exists():
        verify_source(recipe, root)
        patch_environment(project, config, prepare=True)
        return
    cargo_home = Path(config.env.get("CARGO_HOME", str(Path.home() / ".cargo"))).expanduser().resolve()
    archives = list((cargo_home / "registry/cache").glob(f"*/wry-{recipe['version']}.crate"))
    payload = next((p.read_bytes() for p in archives
                    if hashlib.sha256(p.read_bytes()).hexdigest() == recipe["archive_sha256"]), None)
    if payload is None:
        url = f"https://static.crates.io/crates/wry/wry-{recipe['version']}.crate"
        with urlopen(url, timeout=30) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise RuntimeError("Forge Rust source archive exceeds 8 MiB")
    materialize(recipe, root, payload)
    patch_environment(project, config, prepare=True)


def patch_environment(project: RustProject, config: RepoConfig, *, prepare: bool = False) -> dict[str, str]:
    if not project.source_patches:
        return {}
    recipe, source = patch_recipe(config.forge_root, project.source_patches)
    verify_source(recipe, source)
    base = Path(config.env.get("CARGO_HOME", str(Path.home() / ".cargo"))).expanduser().resolve()
    # Keep user settings at their original location so include-relative paths retain meaning.
    user_config = next((base / name for name in ("config", "config.toml") if (base / name).is_file()), None)
    if user_config is not None:
        document = tomllib.loads(user_config.read_text(encoding="utf-8"))
        if user_config.suffix != ".toml" or set(document) & {"patch", "paths", "source", "include"}:
            raise RuntimeError("Managed Rust source repairs require config.toml without source/patch/paths/include overrides")
    identity = hashlib.sha256((str(base) + str(source)).encode()
                              + (user_config.read_bytes() if user_config else b"")).hexdigest()
    home = config.forge_root / "build/rust/cargo-homes" / identity
    if prepare:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not home.is_dir():
        raise RuntimeError("Managed Cargo home is missing; use the project's zbuild.py fetch")
    if (home / "config").exists():
        raise RuntimeError("Unexpected legacy config in managed Cargo home")
    for name in ("registry", "git", "bin", "credentials.toml", "credentials", ".package-cache",
                 ".package-cache-mutate", ".global-cache"):
        link = home / name
        if not link.is_symlink():
            if not prepare:
                raise RuntimeError(f"Missing managed Cargo cache link: {link}; use zbuild.py fetch")
            if link.exists():
                raise RuntimeError(f"Unexpected file in managed Cargo home: {link}")
            link.symlink_to(base / name, target_is_directory=name in {"registry", "git", "bin"})
        elif link.resolve() != (base / name).resolve():
            raise RuntimeError(f"Wrong cache link in managed Cargo home: {link}")
    includes = f"include = [{json.dumps(str(user_config))}]\n" if user_config else ""
    content = includes + f"[patch.crates-io]\nwry = {{ path = {json.dumps(str(source))} }}\n"
    path = home / "config.toml"
    if prepare and not path.exists():
        path.write_text(content, encoding="utf-8")
    elif not path.exists() or path.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"Managed Cargo configuration changed at {path}")
    identity = {"name": PATCH_NAME, "package": "wry", "version": recipe["version"],
                "archive_sha256": recipe["archive_sha256"], "tree_sha256": recipe["tree_sha256"],
                "manifest": str(source / "Cargo.toml")}
    return {"CARGO_HOME": str(home), "ZETA_RUST_SOURCE_PATCHES": json.dumps([identity], sort_keys=True)}


def verify_resolved_patches(metadata: Mapping, project: RustProject, config: RepoConfig) -> None:
    if not project.source_patches:
        return
    recipe, source = patch_recipe(config.forge_root, project.source_patches)
    verify_source(recipe, source)
    packages = [p for p in metadata["packages"] if p["name"] == recipe["package"]]
    if (len(packages) != 1 or packages[0]["version"] != recipe["version"]
        or Path(packages[0]["manifest_path"]).resolve() != source / "Cargo.toml"):
        raise RuntimeError("Resolved Cargo graph bypasses the Forge Rust source repair")
