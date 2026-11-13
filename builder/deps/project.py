from __future__ import annotations

import shutil
from pathlib import Path

from zeta_forge.cmake_builder import CommonBuildArgs
from zeta_forge.conan_openssl import (
    package_from_generators,
    write_openssl_manifest,
)
from zeta_forge.conan_session import install_native
from zeta_forge.config import RepoConfig


def is_cmake_package_file(path: Path) -> bool:
    name = path.name
    return path.is_file() and not path.is_symlink() and (
        name in {"cmakedeps_macros.cmake", "conandeps_legacy.cmake"}
        or name.endswith((
            "Config.cmake", "ConfigVersion.cmake", "Targets.cmake",
            "-config.cmake", "-config-version.cmake", "-data.cmake",
        ))
        or (name.endswith(".cmake") and (name.startswith("Find") or "-Target-" in name))
    )


class DepsBuilder:
    def __init__(
        self, *, script_path: Path, repo_config: RepoConfig, args: CommonBuildArgs
    ) -> None:
        self.script_path = script_path.resolve()
        self.script_dir = self.script_path.parent
        self.repo_config = repo_config
        self.args = args
        self.build_dir = self.script_dir / "build" / args.build_type
        self.conan_root = self.build_dir / "conan"
        self.generators_dir = self.conan_root / "build" / args.build_type / "generators"
        self.install_dir = (
            self.repo_config.install_prefix / "lib" / "cmake" / "zeta_deps" / args.build_type
        )

    @property
    def conanfile(self) -> Path:
        return self.repo_config.builder_dir / "conanfile.py"

    def validate(self) -> None:
        if not self.conanfile.is_file():
            raise RuntimeError(f"zeta deps Conan recipe not found: {self.conanfile}")
        rapidjson_source_dir = self.repo_config.source_dir("ZETA_RAPIDJSON_SRC_DIR")
        if not (rapidjson_source_dir / "include" / "rapidjson").is_dir():
            raise RuntimeError(
                f"RapidJSON source directory not found or invalid: {rapidjson_source_dir}\n"
                "Set ZETA_RAPIDJSON_SRC_DIR to a local checkout or initialize the zeta_forge submodule with: "
                'git -C "$ZETAX_ROOT/zeta_forge" submodule update --init --recursive 3rd/rapidjson'
            )

    def run_conan(self) -> None:
        install_native("deps", self.repo_config, self.args, self.conan_root)

    def install(self, *, prefix: Path | None = None) -> None:
        if not self.generators_dir.is_dir():
            raise RuntimeError(
                f"Conan generators directory not found: {self.generators_dir}\n"
                "Build the deps deliverable before installing the dependency environment."
            )

        openssl_version, openssl_package = package_from_generators(
            self.generators_dir, self.args.build_type
        )

        install_dir = self.install_dir if prefix is None else (
            prefix / "lib" / "cmake" / "zeta_deps" / self.args.build_type
        )
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        for source in sorted(self.generators_dir.iterdir()):
            if is_cmake_package_file(source):
                shutil.copy2(source, install_dir / source.name)

        self.install_rapidjson_config(install_dir)
        self.install_boost_findboost_compat(install_dir)
        write_openssl_manifest(install_dir, openssl_version, openssl_package)

        print(f"Installed zeta deps CMake package files to: {install_dir}")

    def install_boost_findboost_compat(self, install_dir: Path) -> None:
        """Append FindBoost compatibility variables to the Conan-generated BoostConfig.cmake.

        CMake 3.28+ FindBoost checks Boost_CONTEXT_FOUND, Boost_FILESYSTEM_FOUND, etc.
        (uppercase) to verify component availability.  Boost 1.70+'s own BoostConfig.cmake
        sets these variables, but Conan CMakeDeps only creates Boost::<component> targets
        without the corresponding Boost_<COMPONENT>_FOUND variables.  Without this patch,
        find_package(Boost COMPONENTS context filesystem ...) always fails at the
        find_package_handle_standard_args check even though all targets and library files
        are present in the Conan package folder.
        """
        boost_config = install_dir / "BoostConfig.cmake"
        if not boost_config.exists():
            return
        compat_block = "\n".join(
            [
                "",
                "# --- zeta_forge compat: set Boost_<COMPONENT>_FOUND (uppercase) ---",
                "# CMake's FindBoost module checks Boost_CONTEXT_FOUND etc. which Boost 1.70's",
                "# own BoostConfig.cmake sets but Conan CMakeDeps does not.  Iterate the",
                "# component names list populated by the Conan data file and set the variable",
                "# for every component whose Boost::<name> target is defined.",
                "foreach(_zeta_boost_comp IN LISTS boost_COMPONENT_NAMES)",
                '  string(REPLACE "Boost::" "" _zeta_short "${_zeta_boost_comp}")',
                '  string(TOUPPER "${_zeta_short}" _zeta_upper)',
                "  if(TARGET Boost::${_zeta_short})",
                "    set(Boost_${_zeta_upper}_FOUND TRUE)",
                "  endif()",
                "endforeach()",
                "unset(_zeta_boost_comp)",
                "unset(_zeta_short)",
                "unset(_zeta_upper)",
                "# --- end zeta_forge compat ---",
                "",
            ]
        )
        with boost_config.open("a", encoding="utf-8") as f:
            f.write(compat_block)

    def install_rapidjson_config(self, install_dir: Path) -> None:
        rapidjson_source_dir = self.repo_config.source_dir("ZETA_RAPIDJSON_SRC_DIR")
        rapidjson_include_dir = rapidjson_source_dir / "include"
        config_file = install_dir / "RapidJSONConfig.cmake"
        version_file = install_dir / "RapidJSONConfigVersion.cmake"

        config_file.write_text(
            "\n".join(
                [
                    "set(RapidJSON_FOUND TRUE)",
                    f'set(RAPIDJSON_INCLUDE_DIRS "{rapidjson_include_dir}")',
                    f'set(RapidJSON_INCLUDE_DIRS "{rapidjson_include_dir}")',
                    "",
                    "if(NOT TARGET rapidjson::rapidjson)",
                    "  add_library(rapidjson::rapidjson INTERFACE IMPORTED)",
                    "  set_target_properties(rapidjson::rapidjson PROPERTIES",
                    '    INTERFACE_INCLUDE_DIRECTORIES "${RAPIDJSON_INCLUDE_DIRS}"',
                    "  )",
                    "endif()",
                    "",
                    "if(NOT TARGET rapidjson)",
                    "  add_library(rapidjson INTERFACE IMPORTED)",
                    "  set_target_properties(rapidjson PROPERTIES",
                    "    INTERFACE_LINK_LIBRARIES rapidjson::rapidjson",
                    "  )",
                    "endif()",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        version_file.write_text(
            "\n".join(
                [
                    'set(PACKAGE_VERSION "1.1.0-zeta")',
                    "set(PACKAGE_VERSION_COMPATIBLE TRUE)",
                    "set(PACKAGE_VERSION_EXACT FALSE)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
