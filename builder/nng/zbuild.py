#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "common"))

from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs, common_build_argument_parser
from zeta_forge.config import load_repo_config


class NngBuilder(CMakeProjectBuilder):
    uses_conan = False
    reset_conan_on_move = False
    source_watch_patterns = ("CMakeLists.txt", "*.cmake", "*.cmake.in", "*.h", "*.c")

    @property
    def project_name(self) -> str:
        return "NNG"

    @property
    def source_dir(self) -> Path:
        return self.repo_config.source_dir("ZETA_NNG_SRC_DIR")

    @property
    def missing_source_hint(self) -> str:
        return "Set ZETA_NNG_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/nng"

    def conan_install_command(self) -> list[object]:
        raise NotImplementedError

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
            f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
            f"-DCMAKE_CXX_STANDARD={self.repo_config.cxx_standard}",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DNNG_TESTS=OFF",
            "-DNNG_TOOLS=OFF",
            "-DNNG_ENABLE_NNGCAT=OFF",
            "-DNNG_ENABLE_TLS=OFF",
            "-DNNG_ENABLE_HTTP=ON",
        ]


def main() -> int:
    parser = common_build_argument_parser("Build NNG")
    namespace = parser.parse_args()
    args = CommonBuildArgs(build_type=namespace.build_type, install=namespace.install, rebuild=namespace.rebuild)
    repo_config = load_repo_config(Path(__file__))
    NngBuilder(script_path=Path(__file__), repo_config=repo_config, args=args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)