#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs, common_build_argument_parser
from zeta_forge.config import load_repo_config


class GrpcBuilder(CMakeProjectBuilder):
    uses_conan = False
    reset_conan_on_move = False
    source_watch_patterns = ("CMakeLists.txt", "*.cmake", "*.cmake.in")
    source_prune_dirs = ("build", "cmake/build")

    @property
    def project_name(self) -> str:
        return "gRPC"

    @property
    def source_dir(self) -> Path:
        return self.repo_config.source_dir("ZETA_GRPC_SRC_DIR")

    @property
    def missing_source_hint(self) -> str:
        return "Set ZETA_GRPC_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd/grpc"

    def validate(self) -> None:
        super().validate()
        required_dirs = [
            "third_party/abseil-cpp",
            "third_party/boringssl-with-bazel",
            "third_party/cares/cares",
            "third_party/protobuf",
            "third_party/re2",
            "third_party/zlib",
        ]
        missing = [path for path in required_dirs if not (self.source_dir / path).is_dir()]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(
                f"gRPC nested submodules are missing: {joined}\n"
                "Initialize them with: git submodule update --init --recursive 3rd/grpc"
            )

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
            "-DBUILD_TESTING=OFF",
            "-DgRPC_INSTALL=ON",
            "-DgRPC_BUILD_TESTS=OFF",
            "-DgRPC_BUILD_CODEGEN=ON",
            "-DgRPC_BUILD_GRPC_CPP_PLUGIN=ON",
            "-DgRPC_BUILD_GRPCPP_OTEL_PLUGIN=OFF",
            "-DgRPC_DOWNLOAD_ARCHIVES=OFF",
            "-DgRPC_ABSL_PROVIDER=module",
            "-DgRPC_PROTOBUF_PROVIDER=module",
            "-DgRPC_CARES_PROVIDER=module",
            "-DgRPC_RE2_PROVIDER=module",
            "-DgRPC_ZLIB_PROVIDER=module",
            "-DgRPC_SSL_PROVIDER=module",
            "-DgRPC_BENCHMARK_PROVIDER=none",
            "-DABSL_ENABLE_INSTALL=ON",
            "-DABSL_BUILD_TESTING=OFF",
            "-DABSL_BUILD_TEST_HELPERS=OFF",
            "-Dprotobuf_INSTALL=ON",
            "-Dprotobuf_BUILD_TESTS=OFF",
            "-Dprotobuf_BUILD_CONFORMANCE=OFF",
            "-Dprotobuf_BUILD_EXAMPLES=OFF",
            "-DCARES_BUILD_TESTS=OFF",
            "-DCARES_BUILD_CONTAINER_TESTS=OFF",
            "-DRE2_BUILD_TESTING=OFF",
        ]

    def install_boringssl_fallback(self) -> None:
        include_src = self.source_dir / "third_party" / "boringssl-with-bazel" / "src" / "include"
        include_dst = self.repo_config.install_prefix / "include"
        lib_dst = self.repo_config.install_prefix / "lib"
        lib_dst.mkdir(parents=True, exist_ok=True)

        if include_src.is_dir():
            shutil.copytree(include_src / "openssl", include_dst / "openssl", dirs_exist_ok=True)

        for lib_name in ("libssl.a", "libcrypto.a"):
            installed = lib_dst / lib_name
            if installed.exists():
                continue
            candidates = list(self.build_dir.rglob(lib_name))
            if not candidates:
                raise RuntimeError(f"Unable to find built BoringSSL archive {lib_name} under {self.build_dir}")
            shutil.copy2(candidates[0], installed)

    def run(self) -> None:
        super().run()
        if self.args.install:
            self.install_boringssl_fallback()


def main() -> int:
    parser = common_build_argument_parser("Build gRPC")
    namespace = parser.parse_args()
    args = CommonBuildArgs(build_type=namespace.build_type, install=namespace.install, rebuild=namespace.rebuild)
    repo_config = load_repo_config(Path(__file__))
    GrpcBuilder(script_path=Path(__file__), repo_config=repo_config, args=args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
