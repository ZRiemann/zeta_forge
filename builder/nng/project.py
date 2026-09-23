from __future__ import annotations

from pathlib import Path

from zeta_forge.cmake_builder import CMakeProjectBuilder


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
        return "Set ZETA_NNG_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd/nng"

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
