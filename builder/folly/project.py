from __future__ import annotations

from pathlib import Path

from zeta_forge.conan_session import ForgeConanProjectBuilder


class FollyBuilder(ForgeConanProjectBuilder):
    native_conan_target = "folly"

    @property
    def project_name(self) -> str:
        return "Folly"

    @property
    def source_dir(self) -> Path:
        return self.repo_config.source_dir("ZETA_FOLLY_SRC_DIR")

    @property
    def missing_source_hint(self) -> str:
        return "Set ZETA_FOLLY_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd/folly"

    def configure_command(self) -> list[object]:
        install_prefix = self.repo_config.install_prefix
        return [
            *self.configure_arguments(suppress_dev_warnings=True),
            f"-DCMAKE_TOOLCHAIN_FILE={self.conan_toolchain_file}",
            f"-DCMAKE_PREFIX_PATH={install_prefix};{self.repo_config.env.get('ZETA_GRPC_STAGE', install_prefix)}",
            f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
            f"-DCMAKE_CXX_STANDARD={self.repo_config.cxx_standard}",
            f"-DZLIB_ROOT={self.repo_config.env.get('ZETA_GRPC_STAGE', install_prefix)}",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DBOOST_LINK_STATIC=ON",
            "-DBUILD_TESTS=OFF",
            "-DBUILD_BENCHMARKS=OFF",
            "-DPYTHON_EXTENSIONS=OFF",
        ]
