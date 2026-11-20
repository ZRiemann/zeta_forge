"""Install project-approved toolchains without running Forge builds."""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from .rust_project import load_rust_project, read_project_toml


def download(url: str, destination: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=90) as source,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(source, output)


def ensure_rustup() -> Path:
    installed = shutil.which("rustup")
    if installed:
        return Path(installed)
    cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
    rustup = cargo_home / "bin/rustup"
    if not os.access(rustup, os.X_OK):
        with TemporaryDirectory(prefix="forge-rustup-") as temporary:
            installer = Path(temporary) / "rustup-init.sh"
            download("https://sh.rustup.rs", installer)
            subprocess.run(
                [
                    "sh",
                    installer,
                    "-y",
                    "--profile",
                    "minimal",
                    "--default-toolchain",
                    "none",
                    "--no-modify-path",
                ],
                check=True,
            )
    if not os.access(rustup, os.X_OK):
        raise RuntimeError(f"rustup is unavailable after installation at {rustup}")
    return rustup


def rust_requirements_ready(
    rustup: Path, channel: str, components: tuple[str, ...], targets: set[str]
) -> bool:
    def installed(group: str) -> tuple[str, ...]:
        result = subprocess.run(
            [rustup, group, "list", "--toolchain", channel, "--installed"],
            check=False,
            capture_output=True,
            text=True,
        )
        return tuple(result.stdout.splitlines()) if result.returncode == 0 else ()

    actual = installed("component")
    required = ("rustc", "cargo", *components)
    return all(
        any(item == name or item.startswith(name + "-") for item in actual)
        for name in required
    ) and targets <= set(installed("target"))


def ensure_rust_toolchain(forge_root: Path, project_root: Path) -> None:
    project = load_rust_project(project_root, forge_root)
    spec = project.toolchain_spec
    if read_project_toml(project.toolchain).get("toolchain") != spec:
        raise RuntimeError(
            f"Rust toolchain does not match Forge definition: {project.toolchain}"
        )
    targets = set()
    for capability in project.capabilities:
        if capability not in project.catalog.capabilities:
            raise RuntimeError(
                f"Unknown Rust capability {capability!r} in {project_root}"
            )
        targets.update(project.catalog.capabilities[capability].get("rust-targets", ()))
    channel = str(spec["channel"])
    rustup = ensure_rustup()
    components = tuple(spec["components"])
    command = [
        str(rustup),
        "toolchain",
        "install",
        channel,
        "--profile",
        str(spec["profile"]),
        "--no-self-update",
        "--no-update",
    ]
    for component in components:
        command.extend(("--component", component))
    for target in sorted(targets):
        command.extend(("--target", target))
    if not rust_requirements_ready(rustup, channel, components, targets):
        subprocess.run(command, check=True)
        if not rust_requirements_ready(rustup, channel, components, targets):
            raise RuntimeError(f"Incomplete Rust {channel} installation.")
    for program in ("rustc", "cargo"):
        subprocess.run([rustup, "run", channel, program, "--version"], check=True)
    print(f"==> Rust {channel} ready for {project_root}")


def ensure_zig_toolchain() -> None:
    from .zig_toolchain import ZigInstallContext, install

    def fetch_text(url: str) -> str:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode()

    home = Path.home()
    install(
        ZigInstallContext(
            home, home / ".local/bin", fetch_text, download, subprocess.run
        )
    )
