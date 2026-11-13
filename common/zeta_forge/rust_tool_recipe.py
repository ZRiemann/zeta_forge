"""Read-only declarations for approved Cargo tool source repairs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .rust_patches import SourceRecipe, recipe_files


@dataclass(frozen=True)
class ToolRepair:
    name: str
    identity: str
    recipe: SourceRecipe
    source: Path


def tool_repair(forge_root: Path, specification: Mapping[str, object]) -> ToolRepair | None:
    name = specification.get("source-repair")
    if name is None:
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
        raise RuntimeError(f"Invalid Cargo tool source-repair: {name!r}")
    path = forge_root / "rust/tool-patches" / f"{name}.json"
    try:
        payload = path.read_bytes()
        recipe = json.loads(payload)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Missing or invalid Cargo tool source repair: {path}") from error
    required = {"schema", "package", "version", "archive_sha256", "tree_sha256", "upstream"}
    if isinstance(recipe, dict) and recipe.get("schema") == 1:
        required |= {"file", "replacements"}
    else:
        required |= {"files"}
    if (not isinstance(recipe, dict) or set(recipe) != required
        or type(recipe["schema"]) is not int or recipe["schema"] not in (1, 2)
        or recipe["package"] != specification["name"]
        or recipe["version"] != specification["version"]
        or any(not isinstance(recipe[key], str) or not re.fullmatch(r"[0-9a-f]{64}", recipe[key])
               for key in ("archive_sha256", "tree_sha256"))
        or not isinstance(recipe["upstream"], str) or not recipe["upstream"].startswith("https://")):
        raise RuntimeError(f"Invalid Cargo tool source repair: {path}")
    try:
        recipe_files(recipe)
    except RuntimeError as error:
        raise RuntimeError(f"Invalid Cargo tool source repair: {path}: {error}") from error
    identity = hashlib.sha256(payload).hexdigest()
    return ToolRepair(name, identity, recipe,
                      forge_root / "build/rust/tool-sources" / identity / str(recipe["package"]))
