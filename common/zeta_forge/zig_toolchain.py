from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .zig_engine import ZIG_VERSION

DOWNLOAD_INDEX_URL = "https://ziglang.org/download/index.json"
ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "armv6l": "arm",
    "armv7l": "arm",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "ppc64le": "powerpc64le",
}


@dataclass(frozen=True)
class ZigRelease:
    version: str
    target: str
    url: str
    sha256: str


@dataclass
class ZigInstallContext:
    home: Path
    local_bin: Path
    fetch_text: Callable[[str], str]
    download_binary: Callable[[str, Path], None]
    run_subprocess: Callable[..., subprocess.CompletedProcess[str]]


def linux_target(machine: str) -> str:
    return f"{ARCHITECTURE_ALIASES.get(machine, machine)}-linux"


def resolve_release(index_text: str, machine: str) -> ZigRelease:
    try:
        index = json.loads(index_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("Invalid Zig download index JSON") from error
    if not isinstance(index, dict):
        raise TypeError("Invalid Zig download index: expected a release mapping")
    version = ZIG_VERSION
    target = linux_target(machine)
    release = index.get(version)
    if release is None:
        raise RuntimeError(
            f"Forge requires Zig {version}, absent from the official download index"
        )
    if not isinstance(release, dict):
        raise TypeError(f"Invalid release metadata for Zig {version}")
    asset = release.get(target)
    if asset is None:
        raise RuntimeError(f"Zig {version} has no official binary for {target}")
    if not isinstance(asset, dict):
        raise TypeError(f"Invalid binary metadata for Zig {version} ({target})")
    url = asset.get("tarball")
    sha256 = asset.get("shasum")
    if (
        not isinstance(url, str)
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)
    ):
        raise RuntimeError(
            f"Invalid download URL or SHA-256 for Zig {version} ({target})"
        )
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "ziglang.org"
        or not parsed_url.path.endswith(".tar.xz")
    ):
        raise RuntimeError(f"Unexpected official Zig download URL: {url}")
    return ZigRelease(version=version, target=target, url=url, sha256=sha256.lower())


def verify_archive(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(
            "Zig archive SHA-256 mismatch; the current installation was kept"
        )


def extract_archive(path: Path, destination: Path) -> Path:
    destination.mkdir()
    root = destination.resolve()
    with tarfile.open(path, "r:xz") as archive:
        for member in archive:
            target = destination / member.name
            if not target.resolve().is_relative_to(root) or not (
                member.isdir() or member.isfile()
            ):
                raise RuntimeError(f"Unsafe Zig archive member: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Missing Zig archive content: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
    packages = list(destination.iterdir())
    if (
        len(packages) != 1
        or not (packages[0] / "zig").is_file()
        or not (packages[0] / "lib").is_dir()
    ):
        raise RuntimeError("Zig archive does not contain a complete toolchain")
    return packages[0]


def matches_version(context: ZigInstallContext, executable: Path, version: str) -> bool:
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return False
    try:
        result = context.run_subprocess(
            [str(executable), "version"], check=False, capture_output=True, text=True
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == version


def install(context: ZigInstallContext) -> None:
    install_root = context.home / ".local/opt/zig"
    install_root.mkdir(parents=True, exist_ok=True)
    with (install_root / ".prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        install_locked(context, install_root)


def install_locked(context: ZigInstallContext, install_root: Path) -> None:
    machine = platform.machine()
    destination = install_root / f"{ZIG_VERSION}-{linux_target(machine)}"
    launcher = context.local_bin / "zig"
    available = shutil.which("zig")
    if available and matches_version(context, Path(available), ZIG_VERSION):
        print(f"==> Zig {ZIG_VERSION} ready: {available}")
        return
    if os.path.lexists(launcher) and (
        not launcher.is_symlink() or not launcher.resolve().is_relative_to(install_root)
    ):
        raise RuntimeError(f"Cannot replace an unmanaged Zig launcher at {launcher}")
    if destination.is_symlink():
        raise RuntimeError(
            f"Refusing to replace a symlink at the Zig installation directory: {destination}"
        )
    install_root.mkdir(parents=True, exist_ok=True)
    context.local_bin.mkdir(parents=True, exist_ok=True)

    # Stage each published path on its own filesystem so switching uses renames.
    with (
        tempfile.TemporaryDirectory(prefix=".install-", dir=install_root) as temporary,
        tempfile.TemporaryDirectory(
            prefix=".zig-link-", dir=context.local_bin
        ) as link_temporary,
    ):
        stage = Path(temporary)
        package = None
        if not (destination / "lib").is_dir() or not matches_version(
            context, destination / "zig", ZIG_VERSION
        ):
            release = resolve_release(context.fetch_text(DOWNLOAD_INDEX_URL), machine)
            archive = stage / "zig.tar.xz"
            print(f"==> Downloading Zig {release.version}")
            context.download_binary(release.url, archive)
            verify_archive(archive, release.sha256)
            package = extract_archive(archive, stage / "unpacked")
            if not matches_version(context, package / "zig", release.version):
                raise RuntimeError(
                    f"Downloaded Zig failed version validation: expected {release.version}"
                )

        pending_link = Path(link_temporary) / "zig"
        pending_link.symlink_to(destination / "zig")
        backup = stage / "previous"
        if package is not None:
            if destination.exists():
                destination.rename(backup)
            try:
                package.rename(destination)
            except OSError:
                if backup.exists():
                    backup.rename(destination)
                raise
        try:
            pending_link.replace(launcher)
        except OSError:
            if package is not None:
                shutil.rmtree(destination)
                if backup.exists():
                    backup.rename(destination)
            raise
    print(f"==> Zig {ZIG_VERSION} ready: {launcher}")
