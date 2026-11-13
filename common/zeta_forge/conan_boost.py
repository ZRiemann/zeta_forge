from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .conan_policy import MANAGED_BOOST_VERSION, package_version
from .config import RepoConfig
from .process import run_command

UPSTREAM_RECIPE = f"boost/{MANAGED_BOOST_VERSION}#857a04e51881582791677311c881b00d"
RECIPE_PATCH = "conan-center-cobalt-openssl.patch"
SOURCE_PATCH = "cobalt-openssl-b2.patch"
OPENSSL_SNAPSHOT = "forge-openssl-version.txt"


def prepare_boost_recipe(config: RepoConfig) -> str:
    """Export the Forge recipe and select its exact revision for this Conan invocation."""
    env = config.env
    version = package_version("boost", env)
    if version != MANAGED_BOOST_VERSION:
        return f"boost/{version}"

    def cache_path(*flags: str) -> Path | None:
        result = run_command(
            ["conan", "cache", "path", UPSTREAM_RECIPE, *flags],
            env=env,
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            path = Path(result.stdout.strip())
            if path.is_absolute() and path.is_dir():
                return path
        return None

    recipe = cache_path()
    sources = cache_path("--folder=export_source")
    if recipe is None or sources is None:
        run_command(
            ["conan", "download", UPSTREAM_RECIPE, "--only-recipe", "-r", "conancenter"],
            env=env,
        )
        recipe = cache_path()
        sources = cache_path("--folder=export_source")
    if recipe is None or sources is None:
        raise RuntimeError(f"Incomplete Conan cache entry for {UPSTREAM_RECIPE}")

    patch_dir = config.builder_dir / "boost"
    with tempfile.TemporaryDirectory(prefix="zeta-forge-boost-") as directory:
        staging = Path(directory)
        for filename in ("conanfile.py", "conandata.yml"):
            shutil.copy2(recipe / filename, staging)
        shutil.copytree(recipe / "dependencies", staging / "dependencies")
        shutil.copytree(sources / "patches", staging / "patches")
        shutil.copy2(patch_dir / SOURCE_PATCH, staging / "patches")
        (staging / OPENSSL_SNAPSHOT).write_text(
            package_version("openssl", env) + "\n", encoding="utf-8"
        )
        run_command(["git", "apply", "--no-index", patch_dir / RECIPE_PATCH], cwd=staging, env=env)
        exported = run_command(
            [
                "conan", "export", ".", "--version", MANAGED_BOOST_VERSION,
                "--user", "zetax", "--channel", "forge", "--format=json",
            ],
            cwd=staging,
            env=env,
            check=False,
            capture_output=True,
        )
        if exported.returncode != 0:
            raise RuntimeError(f"Could not export the Forge Boost recipe: {exported.stderr.strip()}")
    return json.loads(exported.stdout)["reference"]
