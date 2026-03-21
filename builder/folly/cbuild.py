#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "common"))

from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs, common_build_argument_parser
from zeta_forge.config import load_repo_config


class FollyBuilder(CMakeProjectBuilder):
    @property
    def project_name(self) -> str:
        return "Folly"

    @property
    def source_dir(self) -> Path:
        return self.repo_config.source_dir("ZETA_FOLLY_SRC_DIR")

    @property
    def missing_source_hint(self) -> str:
        return "Set ZETA_FOLLY_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/folly"

    @property
    def user_toolchain(self) -> Path:
        return self.script_dir / "cmake" / "conan_user_toolchain.cmake"

    def conan_input_files(self) -> list[Path]:
        return [self.script_dir / "conanfile.py", self.user_toolchain]

    def conan_install_command(self) -> list[object]:
        return [
            "conan",
            "install",
            self.script_dir / "conanfile.py",
            f"--output-folder={self.conan_root}",
            "--build=missing",
            "-s",
            f"build_type={self.args.build_type}",
            "-s",
            f"compiler.cppstd={self.repo_config.cxx_standard}",
            "-c",
            "tools.cmake.cmaketoolchain:generator=Ninja",
            "-c",
            f"tools.cmake.cmaketoolchain:user_toolchain=[\"{self.user_toolchain}\"]",
        ]

    def configure_command(self) -> list[object]:
        return [
            "cmake",
            "-S",
            self.source_dir,
            "-B",
            self.build_dir,
            "-G",
            "Ninja",
            "-Wno-dev",
            f"-DCMAKE_BUILD_TYPE={self.args.build_type}",
            f"-DCMAKE_TOOLCHAIN_FILE={self.conan_toolchain_file}",
            f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
            f"-DCMAKE_CXX_STANDARD={self.repo_config.cxx_standard}",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DBOOST_LINK_STATIC=ON",
            "-DBUILD_TESTS=OFF",
            "-DBUILD_BENCHMARKS=OFF",
            "-DPYTHON_EXTENSIONS=OFF",
        ]


def main() -> int:
    parser = common_build_argument_parser("Build Folly")
    namespace = parser.parse_args()
    args = CommonBuildArgs(build_type=namespace.build_type, install=namespace.install, rebuild=namespace.rebuild)
    repo_config = load_repo_config(Path(__file__))
    FollyBuilder(script_path=Path(__file__), repo_config=repo_config, args=args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

