"""CMake preparation hook compatibility and native argument ordering."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

from test_build_contract import request
from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs
from zeta_forge.config import load_repo_config

from builder.project import ForgeEngine


class CMakeBuilderTests(unittest.TestCase):
    def test_grpc_prepares_without_conan_and_fills_only_missing_boringssl_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = root
            engine.config = replace(engine.config, source_dirs={
                **engine.config.source_dirs, "ZETA_GRPC_SRC_DIR": root / "grpc",
            })
            builder = engine.builder(request("build"), "grpc")
            for relative in (
                "abseil-cpp", "boringssl-with-bazel", "cares/cares", "grpc-proto",
                "protobuf", "re2", "zlib",
            ):
                (builder.source_dir / "third_party" / relative).mkdir(parents=True)
            with (
                mock.patch("zeta_forge.cmake_builder.run_command"),
                mock.patch.object(builder, "run_conan", side_effect=AssertionError("Conan invoked")),
            ):
                builder.prepare()
            self.assertFalse(builder.conan_stamp.exists())

            headers = builder.source_dir / "third_party/boringssl-with-bazel/src/include/openssl"
            headers.mkdir(parents=True)
            (headers / "fixture.h").write_text("fixture")
            (builder.build_dir / "libcrypto.a").write_bytes(b"fallback")
            prefix = root / "install"
            (prefix / "lib").mkdir(parents=True)
            (prefix / "lib/libssl.a").write_bytes(b"installed")
            builder.install_boringssl_fallback(prefix)
            self.assertEqual((prefix / "lib/libssl.a").read_bytes(), b"installed")
            self.assertEqual((prefix / "lib/libcrypto.a").read_bytes(), b"fallback")
            self.assertTrue((prefix / "include/openssl/fixture.h").is_file())

    def test_external_builder_hooks_keep_order_and_install_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            order: list[str] = []

            class LegacyBuilder(CMakeProjectBuilder):
                source_dir = root

                def validate(self) -> None:
                    order.append("validate")

                def should_run_conan(self) -> bool:
                    order.append("check-conan")
                    return True

                def run_conan(self) -> None:
                    order.append("conan")

                def configure_command(self) -> list[str]:
                    order.append("configure")
                    return ["legacy-cmake", "-DFLAG=first", "-DFLAG=last"]

            config = load_repo_config(ROOT / "zbuild.py")
            builder = LegacyBuilder(
                script_path=root / "project.py", repo_config=config, args=CommonBuildArgs("Debug"),
            )
            with mock.patch("zeta_forge.cmake_builder.run_command") as run:
                builder.prepare()
                builder.install(("core", "tools"), prefix=root / "prefix")
            self.assertEqual(order, ["validate", "check-conan", "conan", "configure"])
            self.assertTrue(builder.conan_stamp.is_file())
            self.assertEqual(run.call_args_list[0].args[0], [
                "legacy-cmake", "-DFLAG=first", "-DFLAG=last",
            ])
            self.assertEqual([call.args[0][-2:] for call in run.call_args_list[1:]],
                             [["--component", "core"], ["--component", "tools"]])

    def test_profile_paths_and_warning_flags_preserve_order(self) -> None:
        engine = ForgeEngine(ROOT / "zbuild.py")
        with tempfile.TemporaryDirectory(prefix="cmake config ") as directory:
            engine.root = Path(directory)
            engine.config = replace(engine.config, cxx_standard="23")
            for profile in ("debug", "release"):
                req = request("build", profile)
                for name in ("grpc", "nng", "folly"):
                    with self.subTest(profile=profile, target=name):
                        builder = engine.builder(req, name)
                        args = builder.configure_command()
                        self.assertEqual(args[:9], [
                            "cmake", "-S", builder.source_dir, "-B", builder.build_dir,
                            "-G", "Ninja", "-Wno-dev", f"-DCMAKE_BUILD_TYPE={profile.title()}",
                        ])
                        prefix = engine.staging(req, name)
                        install = f"-DCMAKE_INSTALL_PREFIX={prefix}"
                        if name == "folly":
                            grpc = engine.staging(req, "grpc")
                            self.assertEqual(args[9:14], [
                                f"-DCMAKE_TOOLCHAIN_FILE={builder.conan_toolchain_file}",
                                f"-DCMAKE_PREFIX_PATH={prefix};{grpc}", install,
                                "-DCMAKE_CXX_STANDARD=23", f"-DZLIB_ROOT={grpc}",
                            ])
                        else:
                            self.assertEqual(args[9:12], [
                                install, "-DCMAKE_CXX_STANDARD=23", "-DBUILD_SHARED_LIBS=OFF",
                            ])
