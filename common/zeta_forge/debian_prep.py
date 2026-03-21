from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import RepoConfig
from .process import run_command


class DebianPreparer:
    base_packages = [
        "ca-certificates",
        "curl",
        "git",
        "unzip",
        "zip",
        "tar",
        "xz-utils",
        "file",
        "patch",
        "patchelf",
        "software-properties-common",
    ]
    build_packages = [
        "build-essential",
        "cmake",
        "ninja-build",
        "pkg-config",
        "ccache",
        "gdb",
    ]
    source_helper_packages = [
        "autoconf",
        "automake",
        "libtool",
        "m4",
        "perl",
        "nasm",
        "bison",
        "flex",
    ]
    python_packages = [
        "python3",
        "python3-dev",
    ]

    def __init__(self, *, script_path: Path, repo_config: RepoConfig, python_version: str = "3.12") -> None:
        self.script_path = script_path.resolve()
        self.repo_config = repo_config
        self.common_dir = self.script_path.parent
        self.repo_root = repo_config.repo_root
        self.sudo_prefix = ["sudo"] if shutil.which("sudo") else []
        self.python_version = python_version

    def _read_os_release(self) -> dict[str, str]:
        os_release = Path("/etc/os-release")
        if not os_release.is_file():
            raise RuntimeError("Unsupported system: /etc/os-release not found")

        values: dict[str, str] = {}
        for raw_line in os_release.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            values[key] = raw_value.strip().strip('"')
        return values

    def validate_platform(self) -> None:
        values = self._read_os_release()
        distribution_id = values.get("ID", "")
        distribution_like = values.get("ID_LIKE", "")
        like_parts = set(distribution_like.split())
        if distribution_id not in {"debian", "ubuntu"} and "debian" not in like_parts:
            raise RuntimeError("This script currently supports Debian-family distributions with apt")
        if not shutil.which("apt-get"):
            raise RuntimeError("Unsupported system: apt-get not found")

    def apt_install(self, packages: list[str]) -> None:
        env = dict(self.repo_config.env)
        env["DEBIAN_FRONTEND"] = "noninteractive"
        run_command([*self.sudo_prefix, "apt-get", "install", "-y", *packages], env=env)

    def ensure_user_local_bin_on_path(self) -> None:
        export_line = 'export PATH="$HOME/.local/bin:$PATH"'
        for shell_rc in [Path.home() / ".profile", Path.home() / ".bashrc"]:
            if not shell_rc.exists():
                shell_rc.touch()
            content = shell_rc.read_text(encoding="utf-8") if shell_rc.stat().st_size else ""
            if export_line not in content:
                with shell_rc.open("a", encoding="utf-8") as handle:
                    if content and not content.endswith("\n"):
                        handle.write("\n")
                    handle.write("\n")
                    handle.write(f"{export_line}\n")
        os.environ["PATH"] = f"{Path.home() / '.local' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        self.repo_config.env["PATH"] = os.environ["PATH"]

    def install_or_upgrade_uv(self) -> None:
        uv_cmd = Path.home() / ".local" / "bin" / "uv"
        if uv_cmd.is_file() and os.access(uv_cmd, os.X_OK):
            run_command([uv_cmd, "self", "update"], env=self.repo_config.env)
        else:
            run_command(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], env=self.repo_config.env)
        if not uv_cmd.is_file():
            raise RuntimeError("uv installation failed")

    def install_or_upgrade_conan(self) -> None:
        uv_cmd = Path.home() / ".local" / "bin" / "uv"
        conan_cmd = Path.home() / ".local" / "bin" / "conan"

        run_command([uv_cmd, "python", "install", self.python_version], env=self.repo_config.env)
        run_command([uv_cmd, "tool", "uninstall", "conan"], env=self.repo_config.env, check=False)
        run_command([uv_cmd, "tool", "install", "--python", self.python_version, "conan>=2,<3"], env=self.repo_config.env)

        if not conan_cmd.is_file():
            raise RuntimeError("Conan installation failed")

        version_result = run_command([conan_cmd, "--version"], env=self.repo_config.env, capture_output=True)
        print(f"Using {version_result.stdout.strip()} with Python {self.python_version}")

        profile_check = run_command([conan_cmd, "profile", "path", "default"], env=self.repo_config.env, check=False)
        if profile_check.returncode != 0:
            run_command([conan_cmd, "profile", "detect", "--force"], env=self.repo_config.env, check=False)

    def update_submodules(self) -> None:
        git_dir = self.repo_root / ".git"
        if git_dir.exists() or run_command(["git", "-C", self.repo_root, "rev-parse", "--git-dir"], env=self.repo_config.env, check=False).returncode == 0:
            run_command(["git", "-C", self.repo_root, "submodule", "update", "--init", "--recursive"], env=self.repo_config.env)

    def print_summary(self) -> None:
        print(
            f"""

Environment preparation completed.

Installed toolchain layers:
1. Base tools: git, curl, archive utilities
2. Native build tools: gcc/g++, cmake, ninja, pkg-config
3. Source-build helpers: autotools, perl, nasm, flex, bison
4. Python toolchain: python3, uv, Conan 2

Suggested next steps:
1. source \"$HOME/.profile\"
2. cd \"{self.repo_root}\"
3. ./builder/hpx/cbuild.py --rebuild --install
4. ./builder/folly/cbuild.py --rebuild --install
5. ./builder/abseil-cpp/cbuild.py --rebuild --install
6. ./builder/nng/cbuild.py --rebuild --install
7. ./builder/zpp/cbuild.py --rebuild
""".rstrip()
        )

    def run(self) -> None:
        self.validate_platform()
        print("==> Updating apt package index")
        run_command([*self.sudo_prefix, "apt-get", "update"], env=self.repo_config.env)

        print("==> Installing base command-line tools")
        self.apt_install(self.base_packages)

        print("==> Installing build toolchain")
        self.apt_install(self.build_packages)

        print("==> Installing source-build helpers for Conan dependencies")
        self.apt_install(self.source_helper_packages)

        print("==> Installing Python tooling")
        self.apt_install(self.python_packages)

        print("==> Ensuring ~/.local/bin is on PATH")
        self.ensure_user_local_bin_on_path()

        print("==> Installing uv")
        self.install_or_upgrade_uv()

        print("==> Installing Conan 2 through uv")
        self.install_or_upgrade_conan()

        print("==> Initializing repository submodules")
        self.update_submodules()

        self.print_summary()