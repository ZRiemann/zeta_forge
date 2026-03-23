#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "common"))

from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs, cmake_bool, common_build_argument_parser
from zeta_forge.config import load_repo_config


@dataclass(frozen=True)
class ZppBuildArgs(CommonBuildArgs):
	build_tests: bool
	build_examples: bool
	build_hpx_examples: bool


class ZppBuilder(CMakeProjectBuilder):
	source_prune_dirs = ("build", "build_debug")

	@property
	def project_name(self) -> str:
		return "zpp"

	@property
	def typed_args(self) -> ZppBuildArgs:
		return self.args  # type: ignore[return-value]

	@property
	def source_dir(self) -> Path:
		return self.repo_config.source_dir("ZETA_ZPP_SRC_DIR")

	@property
	def taskflow_source_dir(self) -> Path:
		return self.repo_config.source_dir("ZETA_TASKFLOW_SRC_DIR")

	@property
	def rapidjson_source_dir(self) -> Path:
		return self.repo_config.source_dir("ZETA_RAPIDJSON_SRC_DIR")

	@property
	def missing_source_hint(self) -> str:
		return "Set ZETA_ZPP_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/zpp"

	@property
	def folly_conan_generators_dir(self) -> Path:
		return self.repo_config.builder_dir / "folly" / "build" / self.args.build_type / "conan" / "build" / self.args.build_type / "generators"

	@property
	def folly_cmake_dir(self) -> Path:
		return self.repo_config.install_prefix / "lib" / "cmake" / "folly"

	def validate(self) -> None:
		super().validate()
		if not self.taskflow_source_dir.is_dir():
			raise RuntimeError(
				f"Taskflow source directory not found: {self.taskflow_source_dir}\n"
				"Set ZETA_TASKFLOW_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/taskflow"
			)
		if not (self.rapidjson_source_dir / "include" / "rapidjson").is_dir():
			raise RuntimeError(
				f"RapidJSON source directory not found or invalid: {self.rapidjson_source_dir}\n"
				"Set ZETA_RAPIDJSON_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/rapidjson"
			)
		if self.typed_args.build_hpx_examples and not self.typed_args.build_examples:
			raise RuntimeError("--with-hpx-examples requires examples to be enabled")

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
		]

	def configure_command(self) -> list[object]:
		cmake_prefix_path = ";".join([str(self.repo_config.install_prefix), str(self.folly_conan_generators_dir)])
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
			f"-DCMAKE_PREFIX_PATH={cmake_prefix_path}",
			f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
			f"-DCMAKE_CXX_STANDARD={self.repo_config.cxx_standard}",
			f"-Dfolly_DIR={self.folly_cmake_dir}",
			f"-DRAPIDJSON_ROOT={self.rapidjson_source_dir / 'include'}",
			f"-DTASKFLOW_ROOT={self.taskflow_source_dir}",
			"-DZPP_USE_CONAN=ON",
			f"-DZPP_BUILD_TESTS={cmake_bool(self.typed_args.build_tests)}",
			f"-DZPP_BUILD_EXAMPLES={cmake_bool(self.typed_args.build_examples)}",
			f"-DZPP_BUILD_HPX_EXAMPLES={cmake_bool(self.typed_args.build_hpx_examples)}",
		]


def main() -> int:
	parser = common_build_argument_parser("Build zpp")
	parser.add_argument("--no-tests", action="store_true")
	parser.add_argument("--no-examples", action="store_true")
	parser.add_argument("--with-hpx-examples", action="store_true")
	namespace = parser.parse_args()
	args = ZppBuildArgs(
		build_type=namespace.build_type,
		install=namespace.install,
		rebuild=namespace.rebuild,
		build_tests=not namespace.no_tests,
		build_examples=not namespace.no_examples,
		build_hpx_examples=namespace.with_hpx_examples,
	)
	repo_config = load_repo_config(Path(__file__))
	ZppBuilder(script_path=Path(__file__), repo_config=repo_config, args=args).run()
	return 0


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except Exception as exc:
		print(exc, file=sys.stderr)
		raise SystemExit(1)
