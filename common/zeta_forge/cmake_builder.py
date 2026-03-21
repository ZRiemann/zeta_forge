from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .config import RepoConfig
from .process import cpu_count, run_command


@dataclass(frozen=True)
class CommonBuildArgs:
    build_type: str
    install: bool
    rebuild: bool


def common_build_argument_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--BUILD_TYPE", dest="build_type", default="Release", choices=("Release", "Debug"))
    parser.add_argument("--install", action="store_true", dest="install")
    parser.add_argument("--rebuild", action="store_true", dest="rebuild")
    return parser


def cmake_bool(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


class CMakeProjectBuilder:
    source_watch_patterns: tuple[str, ...] = ("CMakeLists.txt", "*.cmake", "*.cmake.in")
    source_prune_dirs: tuple[str, ...] = ("build",)
    uses_conan: bool = True
    reset_conan_on_move: bool = True

    def __init__(self, *, script_path: Path, repo_config: RepoConfig, args: CommonBuildArgs) -> None:
        self.script_path = script_path.resolve()
        self.script_dir = self.script_path.parent
        self.repo_config = repo_config
        self.args = args
        self.build_root = self.script_dir / "build"
        self.build_dir = self.build_root / args.build_type
        self.conan_root = self.build_dir / "conan"
        self.conan_generators_dir = self.conan_root / "build" / args.build_type / "generators"
        self.conan_toolchain_file = self.conan_generators_dir / "conan_toolchain.cmake"
        self.conan_stamp = self.build_dir / ".conan.stamp"
        self.configure_stamp = self.build_dir / ".configure.stamp"

    @property
    def project_name(self) -> str:
        raise NotImplementedError

    @property
    def source_dir(self) -> Path:
        raise NotImplementedError

    @property
    def missing_source_hint(self) -> str:
        raise NotImplementedError

    def validate(self) -> None:
        if not self.source_dir.is_dir():
            raise RuntimeError(f"{self.project_name} source directory not found: {self.source_dir}\n{self.missing_source_hint}")

    def conan_input_files(self) -> Sequence[Path]:
        return [self.script_dir / "conanfile.py"]

    def configure_dependencies(self) -> Sequence[Path]:
        if not self.uses_conan:
            return []
        return [self.conan_toolchain_file]

    def conan_install_command(self) -> Sequence[object]:
        raise NotImplementedError

    def configure_command(self) -> Sequence[object]:
        raise NotImplementedError

    def detect_moved_build_dir(self) -> bool:
        cache_path = self.build_dir / "CMakeCache.txt"
        if not cache_path.is_file():
            return False
        for line in cache_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("CMAKE_CACHEFILE_DIR:INTERNAL="):
                cached_value = Path(line.split("=", 1)[1]).resolve()
                return cached_value != self.build_dir.resolve()
        return False

    def clear_cmake_state(self) -> None:
        cache_path = self.build_dir / "CMakeCache.txt"
        cmake_files = self.build_dir / "CMakeFiles"
        if cache_path.exists():
            cache_path.unlink()
        if cmake_files.exists():
            shutil.rmtree(cmake_files)

    def clear_conan_state(self) -> None:
        if self.conan_root.exists():
            shutil.rmtree(self.conan_root)
        if self.conan_stamp.exists():
            self.conan_stamp.unlink()

    def reset_moved_build_state(self) -> None:
        if self.reset_conan_on_move and self.uses_conan:
            self.clear_conan_state()
        if self.configure_stamp.exists():
            self.configure_stamp.unlink()
        self.clear_cmake_state()

    def should_run_conan(self) -> bool:
        if not self.uses_conan:
            return False
        if self.args.rebuild or not self.conan_stamp.is_file():
            return True
        if not self.conan_toolchain_file.is_file():
            return True
        stamp_mtime = self.conan_stamp.stat().st_mtime_ns
        for path in self.conan_input_files():
            if path.exists() and path.stat().st_mtime_ns > stamp_mtime:
                return True
        return False

    def source_tree_is_newer(self) -> bool:
        if not self.configure_stamp.is_file():
            return True
        stamp_mtime = self.configure_stamp.stat().st_mtime_ns
        prune_dirs = set(self.source_prune_dirs)
        for path in self.source_dir.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(self.source_dir).parts
            if any(part in prune_dirs for part in relative_parts[:-1]):
                continue
            if not any(path.match(pattern) for pattern in self.source_watch_patterns):
                continue
            if path.stat().st_mtime_ns > stamp_mtime:
                return True
        return False

    def should_configure(self) -> bool:
        if self.args.rebuild or not self.configure_stamp.is_file():
            return True
        stamp_mtime = self.configure_stamp.stat().st_mtime_ns
        for path in self.configure_dependencies():
            if path.exists() and path.stat().st_mtime_ns > stamp_mtime:
                return True
        return self.source_tree_is_newer()

    def ensure_build_directory(self) -> None:
        self.build_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        self.validate()
        moved_build_dir = self.detect_moved_build_dir()

        if self.args.rebuild and self.build_dir.exists():
            shutil.rmtree(self.build_dir)

        self.ensure_build_directory()

        if moved_build_dir:
            self.reset_moved_build_state()

        if self.should_run_conan():
            run_command(["conan", "profile", "detect", "--force"], env=self.repo_config.env, check=False)
            run_command(self.conan_install_command(), cwd=self.script_dir, env=self.repo_config.env)
            self.conan_stamp.touch()

        if self.args.rebuild:
            self.clear_cmake_state()

        if self.should_configure():
            run_command(self.configure_command(), cwd=self.script_dir, env=self.repo_config.env)
            self.configure_stamp.touch()

        run_command(["cmake", "--build", self.build_dir, "--parallel", str(cpu_count())], env=self.repo_config.env)

        if self.args.install:
            run_command(["cmake", "--install", self.build_dir], env=self.repo_config.env)
