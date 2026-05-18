#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.cmake_builder import CommonBuildArgs, common_build_argument_parser
from zeta_forge.config import RepoConfig, load_repo_config
from zeta_forge.process import run_command


class DepsBuilder:
    def __init__(self, *, script_path: Path, repo_config: RepoConfig, args: CommonBuildArgs) -> None:
        self.script_path = script_path.resolve()
        self.script_dir = self.script_path.parent
        self.repo_config = repo_config
        self.args = args
        self.build_dir = self.script_dir / "build" / args.build_type
        self.conan_root = self.build_dir / "conan"
        self.generators_dir = self.conan_root / "build" / args.build_type / "generators"
        self.install_dir = self.repo_config.install_prefix / "lib" / "cmake" / "zeta_deps" / args.build_type

    @property
    def conanfile(self) -> Path:
        return self.script_dir / "conanfile.py"

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
        run_command(["conan", "profile", "detect", "--force"], env=self.repo_config.env, check=False)
        run_command(
            [
                "conan",
                "install",
                self.conanfile,
                f"--output-folder={self.conan_root}",
                "--build=missing",
                "-s",
                f"build_type={self.args.build_type}",
                "-s",
                f"compiler.cppstd={self.repo_config.cxx_standard}",
                "-c",
                "tools.cmake.cmaketoolchain:generator=Ninja",
            ],
            cwd=self.script_dir,
            env=self.repo_config.env,
        )

    def install(self) -> None:
        if not self.generators_dir.is_dir():
            raise RuntimeError(
                f"Conan generators directory not found: {self.generators_dir}\n"
                "Run zeta_forge deps before installing the dependency environment."
            )

        if self.install_dir.exists():
            shutil.rmtree(self.install_dir)
        self.install_dir.mkdir(parents=True, exist_ok=True)

        for source in sorted(self.generators_dir.iterdir()):
            if source.is_file() and source.suffix == ".cmake":
                shutil.copy2(source, self.install_dir / source.name)

        self.install_rapidjson_config()
        self.install_boost_findboost_compat()

        print(f"Installed zeta deps CMake package files to: {self.install_dir}")

    def install_boost_findboost_compat(self) -> None:
        """Append FindBoost compatibility variables to the Conan-generated BoostConfig.cmake.

        CMake 3.28+ FindBoost checks Boost_CONTEXT_FOUND, Boost_FILESYSTEM_FOUND, etc.
        (uppercase) to verify component availability.  Boost 1.70+'s own BoostConfig.cmake
        sets these variables, but Conan CMakeDeps only creates Boost::<component> targets
        without the corresponding Boost_<COMPONENT>_FOUND variables.  Without this patch,
        find_package(Boost COMPONENTS context filesystem ...) always fails at the
        find_package_handle_standard_args check even though all targets and library files
        are present in the Conan package folder.
        """
        boost_config = self.install_dir / "BoostConfig.cmake"
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
                '  if(TARGET Boost::${_zeta_short})',
                '    set(Boost_${_zeta_upper}_FOUND TRUE)',
                '  endif()',
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

    def install_rapidjson_config(self) -> None:
        rapidjson_source_dir = self.repo_config.source_dir("ZETA_RAPIDJSON_SRC_DIR")
        rapidjson_include_dir = rapidjson_source_dir / "include"
        config_file = self.install_dir / "RapidJSONConfig.cmake"
        version_file = self.install_dir / "RapidJSONConfigVersion.cmake"

        config_file.write_text(
            "\n".join(
                [
                    'set(RapidJSON_FOUND TRUE)',
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

    def run(self) -> None:
        self.validate()
        if self.args.rebuild and self.build_dir.exists():
            shutil.rmtree(self.build_dir)
        self.run_conan()
        if self.args.install:
            self.install()


def main() -> int:
    parser = common_build_argument_parser("Build shared ZetaX Conan dependency environment")
    namespace = parser.parse_args()
    args = CommonBuildArgs(build_type=namespace.build_type, install=namespace.install, rebuild=namespace.rebuild)
    repo_config = load_repo_config(Path(__file__))
    DepsBuilder(script_path=Path(__file__), repo_config=repo_config, args=args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
