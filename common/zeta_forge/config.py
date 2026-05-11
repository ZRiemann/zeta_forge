from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_VERSION_VALUES = {
    "ZETA_CXX_STANDARD": "20",
    "ZETA_BOOST_VERSION": "1.90.0",
    "ZETA_JEMALLOC_VERSION": "5.3.0",
    "ZETA_FMT_VERSION": "12.1.0",
}

THIRD_SOURCE_ENV_TO_SUBDIR = {
    "ZETA_GRPC_SRC_DIR": "grpc",
    "ZETA_HPX_SRC_DIR": "hpx",
    "ZETA_FOLLY_SRC_DIR": "folly",
    "ZETA_NNG_SRC_DIR": "nng",
    "ZETA_TASKFLOW_SRC_DIR": "taskflow",
    "ZETA_RAPIDJSON_SRC_DIR": "rapidjson",
}

def _expand_path(raw_path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()


def is_forge_root(candidate: Path) -> bool:
    return (
        (candidate / "common" / "zeta_forge" / "config.py").is_file()
        and (candidate / "builder").is_dir()
        and (candidate / "3rd").is_dir()
    )


def _candidate_roots(start: Path) -> list[Path]:
    candidates: list[Path] = []

    zetax_root_raw = os.environ.get("ZETAX_ROOT")
    if zetax_root_raw:
        candidates.append(_expand_path(zetax_root_raw) / "zeta_forge")

    search_roots = [start, *start.parents]
    for search_root in search_roots:
        candidates.append(search_root)
        candidates.append(search_root / "zeta_forge")

    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def discover_forge_root(start: Path) -> Path:
    for candidate in _candidate_roots(start):
        if is_forge_root(candidate):
            return candidate
    raise RuntimeError(
        "Unable to locate zeta_forge root. "
        "Set ZETAX_ROOT=/path/to/workspace and ensure "
        "$ZETAX_ROOT/zeta_forge exists, or run from a checkout near zeta_forge."
    )


@dataclass(frozen=True)
class RepoConfig:
    zetax_root: Path
    forge_root: Path
    common_dir: Path
    builder_dir: Path
    third_dir: Path
    install_prefix: Path
    cxx_standard: str
    env: dict[str, str]
    source_dirs: dict[str, Path]

    def source_dir(self, env_name: str) -> Path:
        return self.source_dirs[env_name]


def load_repo_config(script_path: Path, *, project_source_defaults: dict[str, Path] | None = None) -> RepoConfig:
    forge_root = discover_forge_root(script_path.resolve().parent)
    zetax_root = forge_root.parent
    common_dir = forge_root / "common"
    builder_dir = forge_root / "builder"

    env = dict(os.environ)
    env["ZETAX_ROOT"] = str(zetax_root)
    env["ZETA_BUILDER_DIR"] = str(builder_dir)
    env["ZETA_CMAKE_UTIL_DIR"] = str(forge_root / "cmake_util")
    third_dir_raw = env.setdefault("ZETA_3RD_DIR", str(forge_root / "3rd"))
    third_dir = _expand_path(third_dir_raw)
    env["ZETA_3RD_DIR"] = str(third_dir)

    for name, default in DEFAULT_VERSION_VALUES.items():
        env.setdefault(name, default)

    install_prefix_raw = env.setdefault("ZETA_INSTALL_PREFIX", str(Path.home() / ".local"))
    install_prefix = _expand_path(install_prefix_raw)
    env["ZETA_INSTALL_PREFIX"] = str(install_prefix)

    source_dirs: dict[str, Path] = {}
    for env_name, subdir in THIRD_SOURCE_ENV_TO_SUBDIR.items():
        raw_value = env.get(env_name)
        if raw_value:
            source_path = _expand_path(raw_value)
        else:
            source_path = (third_dir / subdir).resolve()
        env[env_name] = str(source_path)
        source_dirs[env_name] = source_path

    for env_name, default_path in (project_source_defaults or {}).items():
        raw_value = env.get(env_name)
        if raw_value:
            source_path = _expand_path(raw_value)
        else:
            source_path = default_path.resolve()
        env[env_name] = str(source_path)
        source_dirs[env_name] = source_path

    return RepoConfig(
        zetax_root=zetax_root,
        forge_root=forge_root,
        common_dir=common_dir,
        builder_dir=builder_dir,
        third_dir=third_dir,
        install_prefix=install_prefix,
        cxx_standard=env["ZETA_CXX_STANDARD"],
        env=env,
        source_dirs=source_dirs,
    )
