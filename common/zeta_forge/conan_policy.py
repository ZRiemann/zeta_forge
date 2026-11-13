"""Dependency versions and options for Forge's native Conan consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MANAGED_BOOST_VERSION = "1.91.0"
VERSION_OVERRIDES = {
    "boost": ("ZETA_BOOST_VERSION", MANAGED_BOOST_VERSION),
    "openssl": ("ZETA_OPENSSL_VERSION", "3.6.2"),
    "jemalloc": ("ZETA_JEMALLOC_VERSION", "5.3.0"),
    "fmt": ("ZETA_FMT_VERSION", "12.1.0"),
    "spdlog": ("ZETA_SPDLOG_VERSION", "1.17.0"),
    "sqlite3": ("ZETA_SQLITE_VERSION", "3.53.3"),
}
PINNED_VERSIONS = {
    "asio": "1.30.2",
    "bzip2": "1.0.8",
    "double-conversion": "3.3.0",
    "fast_float": "8.1.0",
    "gflags": "2.2.2",
    "glog": "0.7.1",
    "gtest": "1.17.0",
    "hwloc": "2.12.2",
    "libevent": "2.1.12",
    "libsodium": "1.0.20",
    "lz4": "1.10.0",
    "snappy": "1.2.1",
    "xz_utils": "5.8.1",
    "zstd": "1.5.7",
}
FOLLY_SUPPORT = (
    "libevent", "bzip2", "xz_utils", "lz4", "zstd", "snappy", "libsodium", "openssl",
)
TARGET_DEPENDENCIES = {
    # Downstream Folly config consumers also need its dependencies in the deps prefix.
    "deps": (
        "fmt", "spdlog", "gtest", "sqlite3", "boost", "gflags", "glog",
        "double-conversion", "fast_float", *FOLLY_SUPPORT,
    ),
    "hpx": ("boost", "asio", "hwloc", "jemalloc"),
    "folly": (
        "boost", "double-conversion", "fast_float", "fmt", "gflags", "glog", *FOLLY_SUPPORT,
    ),
}
STATIC_PACKAGES = frozenset((
    "asio", "boost", "bzip2", "double-conversion", "fmt", "gflags", "glog", "hwloc",
    "jemalloc", "libevent", "libsodium", "lz4", "openssl", "snappy", "sqlite3", "xz_utils", "zstd",
))
PACKAGE_OPTIONS = {
    "boost": {"header_only": False},
    "glog": {"with_unwind": False},
    "gtest": {"build_gmock": False, "no_main": False},
    "libevent": {"with_openssl": False},
    "spdlog": {"header_only": True},
}


@dataclass(frozen=True)
class NativeConanPolicy:
    requires: tuple[str, ...]
    default_options: dict[str, bool]


def package_version(package: str, env: Mapping[str, str]) -> str:
    override = VERSION_OVERRIDES.get(package)
    if override is None:
        return PINNED_VERSIONS[package]
    name, default = override
    return env.get(name, default)


def resolve_policy(
    target: str,
    env: Mapping[str, str],
    boost_reference: str,
) -> NativeConanPolicy:
    packages = TARGET_DEPENDENCIES[target]
    requires = tuple(
        boost_reference if name == "boost" else f"{name}/{package_version(name, env)}"
        for name in packages
    )
    # HPX can receive OpenSSL transitively through the managed Boost recipe.
    options = {
        f"{name}/*:shared": False for name in (*packages, "openssl") if name in STATIC_PACKAGES
    }
    for name in packages:
        options.update(
            {f"{name}/*:{option}": value for option, value in PACKAGE_OPTIONS.get(name, {}).items()}
        )
    if target == "folly":
        # Preserve Folly's independent Boost configuration.
        options["boost/*:zlib"] = False
    return NativeConanPolicy(requires, options)
