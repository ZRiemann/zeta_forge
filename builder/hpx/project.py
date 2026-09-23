from __future__ import annotations

from pathlib import Path

from zeta_forge.cmake_builder import CMakeProjectBuilder


class HpxBuilder(CMakeProjectBuilder):
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
    def user_toolchain(self) -> Path:
        return self.script_dir / "cmake" / "conan_user_toolchain.cmake"

    def conan_input_files(self) -> list[Path]:
        return [self.script_dir / "conanfile.py", self.user_toolchain]

    def configure_dependencies(self) -> list[Path]:
        compat_dir = self.script_dir / "cmake" / "conan_compat" / "packages"
        return [*super().configure_dependencies(), *sorted(compat_dir.glob("*.cmake"))]

    def conan_install_command(self) -> list[object]:
        enabled_blocks = 'tools.cmake.cmaketoolchain:enabled_blocks=["user_toolchain", "generic_system", "compilers", "android_system", "apple_system", "fpic", "arch_flags", "linker_scripts", "rpath_link_flags", "libcxx", "vs_runtime", "vs_debugger_environment", "parallel", "extra_flags", "cmake_flags_init", "extra_variables", "try_compile", "find_paths", "pkg_config", "rpath", "shared", "output_dirs", "variables", "preprocessor"]'
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
            f'tools.cmake.cmaketoolchain:user_toolchain=["{self.user_toolchain}"]',
            "-c",
            enabled_blocks,
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
            f"-DCMAKE_BUILD_TYPE={self.args.build_type}",
            f"-DCMAKE_TOOLCHAIN_FILE={self.conan_toolchain_file}",
            "-DHPX_WITH_MALLOC=jemalloc",
            "-DHPX_WITH_NETWORKING=ON",
            "-DHPX_WITH_PARCELPORT_MPI=ON",
            f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
            "-DHPX_WITH_FETCH_ASIO=OFF",
            "-DHPX_WITH_FETCH_BOOST=OFF",
            "-DHPX_WITH_FETCH_HWLOC=OFF",
            "-DHPX_WITH_FETCH_STDEXEC=ON",
            "-DHPX_WITH_STDEXEC_TAG=05e3f5ec59655fee2536fc3116e5ffb4927ae955",
            "-DHPX_WITH_PKGCONFIG=OFF",
            "-DBUILD_TESTING=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DHPX_WITH_STATIC_LINKING=ON",
            "-DHPX_WITH_TESTS=OFF",
            "-DHPX_WITH_EXAMPLES=OFF",
            "-DHPX_WITH_TOOLS=ON",
            f"-DHPX_WITH_CXX_STANDARD={self.repo_config.cxx_standard}",
        ]
