from __future__ import annotations

import subprocess
from pathlib import Path

from zeta_forge.conan_session import ForgeConanProjectBuilder

HPX_TOOLCHAIN_BLOCKS = (
    "user_toolchain", "generic_system", "compilers", "android_system", "apple_system",
    "fpic", "arch_flags", "linker_scripts", "rpath_link_flags", "libcxx", "vs_runtime",
    "vs_debugger_environment", "parallel", "extra_flags", "cmake_flags_init",
    "extra_variables", "try_compile", "find_paths", "pkg_config", "rpath", "shared",
    "output_dirs", "variables", "preprocessor",
)


class HpxBuilder(ForgeConanProjectBuilder):
    native_conan_target = "hpx"
    enabled_toolchain_blocks = HPX_TOOLCHAIN_BLOCKS

    @property
    def project_name(self) -> str:
        return "HPX"

    @property
    def source_dir(self) -> Path:
        return self.repo_config.source_dir("ZETA_HPX_SRC_DIR")

    @property
    def missing_source_hint(self) -> str:
        return "Set ZETA_HPX_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd/hpx"

    @property
    def source_git_commit(self) -> str:
        """Read HEAD exactly; HPX's path-filtered log can skip merge commits."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.source_dir), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                env=self.repo_config.env,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"Unable to read the HPX source commit from {self.source_dir}"
            ) from exc
        return result.stdout.strip()

    def configure_dependencies(self) -> list[Path]:
        compat_dir = self.script_dir / "cmake" / "conan_compat" / "packages"
        return [*super().configure_dependencies(), *sorted(compat_dir.glob("*.cmake"))]

    def configure_command(self) -> list[object]:
        source_commit = self.source_git_commit
        return [
            *self.configure_arguments(suppress_dev_warnings=False),
            f"-DCMAKE_TOOLCHAIN_FILE={self.conan_toolchain_file}",
            "-DHPX_WITH_MALLOC=jemalloc",
            "-DHPX_WITH_NETWORKING=ON",
            "-DHPX_WITH_PARCELPORT_MPI=ON",
            f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
            "-DHPX_WITH_FETCH_ASIO=OFF",
            "-DHPX_WITH_FETCH_BOOST=OFF",
            "-DHPX_WITH_FETCH_HWLOC=OFF",
            "-DHPX_WITH_FETCH_STDEXEC=ON",
            "-DHPX_WITH_STDEXEC_TAG=04de75de5807e73a229acf047c6976575e313359",
            f"-DHPX_WITH_GIT_COMMIT={source_commit}",
            f"-DHPX_WITH_GIT_COMMIT_SHORT={source_commit[:7]}",
            "-DHPX_WITH_PKGCONFIG=OFF",
            "-DBUILD_TESTING=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DHPX_WITH_STATIC_LINKING=ON",
            "-DHPX_WITH_TESTS=OFF",
            "-DHPX_WITH_EXAMPLES=OFF",
            "-DHPX_WITH_TOOLS=ON",
            f"-DHPX_WITH_CXX_STANDARD={self.repo_config.cxx_standard}",
        ]
