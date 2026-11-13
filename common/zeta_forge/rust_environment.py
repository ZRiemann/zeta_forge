"""Validate and resolve Forge's native and patched Rust environment."""

from __future__ import annotations

from .conan_openssl import read_openssl_manifest
from .config import RepoConfig
from .rust_patches import patch_environment, validate_patch_inputs
from .rust_project import RustProject


def native_environment(project: RustProject, repo_config: RepoConfig, *, pending_source: bool = False) -> dict[str, str]:
    environment = repo_config.env.copy()
    for dependency in project.native_dependencies:
        if dependency == "openssl-conan":
            package_dir = read_openssl_manifest(repo_config.install_prefix)
            expected = {
                "OPENSSL_DIR": str(package_dir),
                "OPENSSL_LIB_DIR": str(package_dir / "lib"),
                "OPENSSL_INCLUDE_DIR": str(package_dir / "include"),
                "OPENSSL_STATIC": "1",
                "OPENSSL_NO_VENDOR": "1",
            }
            for name, value in expected.items():
                current = environment.get(name)
                if current is not None and current != value:
                    raise RuntimeError(
                        f"{name} conflicts with forge Conan OpenSSL: {current!r} != {value!r}"
                    )
            for name in environment:
                if name == "OPENSSL_LIBS" or (
                    name.endswith(
                        (
                            "_OPENSSL_DIR",
                            "_OPENSSL_LIB_DIR",
                            "_OPENSSL_INCLUDE_DIR",
                            "_OPENSSL_STATIC",
                            "_OPENSSL_NO_VENDOR",
                            "_OPENSSL_LIBS",
                        )
                    )
                    and name not in expected
                ):
                    raise RuntimeError(f"{name} overrides forge Conan OpenSSL; unset it")
            environment.update(expected)
            continue
        if dependency != "nng":
            raise RuntimeError(f"Unsupported native Rust dependency: {dependency}")
        prefix = repo_config.install_prefix
        header = prefix / "include" / "nng" / "nng.h"
        library_candidates = (prefix / "lib" / "libnng.a", prefix / "lib64" / "libnng.a")
        if not header.is_file() or not any(path.is_file() for path in library_candidates):
            raise RuntimeError(
                f"Forge NNG installation is missing at {prefix}; install it with "
                f"{repo_config.forge_root}/zbuild.py install nng"
            )
        environment.update(NNG_NO_VENDOR="1", NNG_DIR=str(prefix), NNG_STATIC="1")
    if pending_source:
        validate_patch_inputs(project, repo_config)
    else:
        environment.update(patch_environment(project, repo_config))
    return environment
