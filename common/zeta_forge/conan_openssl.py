from __future__ import annotations

import json
from pathlib import Path
import re


MANIFEST_NAME = "openssl-native.json"


def _cmake_value(path: Path, name: str) -> str:
    if not path.is_file():
        raise RuntimeError(f"Conan OpenSSL metadata is missing: {path}")
    match = re.search(
        rf'^set\({re.escape(name)} "([^"]+)"\)$',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError(f"Conan OpenSSL metadata has no {name}: {path}")
    return match.group(1)


def validate_openssl_package(package_dir: Path) -> None:
    required = (
        package_dir / "include" / "openssl" / "ssl.h",
        package_dir / "lib" / "libssl.a",
        package_dir / "lib" / "libcrypto.a",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Conan OpenSSL package is incomplete: {', '.join(missing)}")


def package_from_generators(generators_dir: Path, build_type: str) -> tuple[str, Path]:
    candidates = sorted(generators_dir.glob(f"OpenSSL-{build_type.lower()}-*-data.cmake"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one Conan OpenSSL {build_type} data file under {generators_dir}; "
            f"found {len(candidates)}"
        )
    package_dir = Path(
        _cmake_value(candidates[0], f"openssl_PACKAGE_FOLDER_{build_type.upper()}")
    )
    version = _cmake_value(generators_dir / "OpenSSLConfigVersion.cmake", "PACKAGE_VERSION")
    if not package_dir.is_absolute():
        raise RuntimeError(f"Conan OpenSSL package path must be absolute: {package_dir}")
    validate_openssl_package(package_dir)
    return version, package_dir


def write_openssl_manifest(install_dir: Path, version: str, package_dir: Path) -> None:
    manifest = install_dir / MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {"schema": 1, "version": version, "package_dir": str(package_dir)}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def read_openssl_manifest(install_prefix: Path) -> Path:
    manifest = install_prefix / "lib" / "cmake" / "zeta_deps" / "Release" / MANIFEST_NAME
    hint = "Run zeta_forge/zbuild.py deps --BUILD_TYPE=Release --install"
    if not manifest.is_file():
        raise RuntimeError(f"Forge Conan OpenSSL manifest is missing: {manifest}. {hint}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid Forge Conan OpenSSL manifest: {manifest}: {error}") from error
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or not isinstance(data.get("version"), str)
        or not data["version"]
        or not isinstance(data.get("package_dir"), str)
    ):
        raise RuntimeError(f"Invalid Forge Conan OpenSSL manifest: {manifest}")
    package_dir = Path(data["package_dir"])
    if not package_dir.is_absolute():
        raise RuntimeError(f"Conan OpenSSL package path must be absolute: {package_dir}")
    try:
        validate_openssl_package(package_dir)
    except RuntimeError as error:
        raise RuntimeError(f"{error}. {hint}") from error
    return package_dir
