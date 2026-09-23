"""Small native fixture and staging-policy tests, never third-party builds."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

from builder.deps.project import DepsBuilder
from builder.project import ForgeEngine, project as forge_project
from zeta_forge.build_cli import Product, Project, cli
from zeta_forge.build_ops import relocate_installed_metadata
from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs
from zeta_forge.cmake_engine import CMAKE_ACTIONS, CMakeEngine
from test_build_contract import request


class FixtureBuilder(CMakeProjectBuilder):
    uses_conan = False
    project_name = "fixture"
    missing_source_hint = "temporary fixture"

    @property
    def source_dir(self) -> Path:
        return self.script_dir

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
            f"-DCMAKE_INSTALL_PREFIX={self.repo_config.install_prefix}",
        ]


class NativeDeliveryTests(unittest.TestCase):
    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("cmake", "ninja", "cc")),
        "native fixture tools unavailable",
    )
    def test_selected_install_and_engineering_unit_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-fixture-") as directory:
            root = Path(directory)
            (root / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\nproject(fixture C)\n"
                "add_executable(one main.c)\nadd_executable(two main.c)\n"
                "enable_testing()\nadd_test(NAME correctness COMMAND one)\n"
                "install(TARGETS one RUNTIME DESTINATION bin COMPONENT one)\n"
                "install(TARGETS two RUNTIME DESTINATION bin COMPONENT two)\n",
                encoding="utf-8",
            )
            config = SimpleNamespace(env=dict(os.environ), install_prefix=root / "install")

            def factory(req, names):
                return FixtureBuilder(
                    script_path=root / "zbuild.py",
                    repo_config=config,
                    args=CommonBuildArgs(req.cmake_profile),
                )

            engine = CMakeEngine(
                root,
                factory,
                ("one", "two"),
                test_targets=lambda names: ("one",),
                components={"one": ("one",), "two": ("two",)},
            )
            project = Project(
                "fixture",
                tuple(
                    Product(name, "cmake", "fixture", (*CMAKE_ACTIONS, "install"))
                    for name in ("one", "two")
                ),
                {"cmake": engine},
                ("one",),
            )
            before = sorted(root.rglob("*"))
            self.assertEqual(cli(project, ["rebuild", "one", "--dry-run"]), 0)
            self.assertEqual(sorted(root.rglob("*")), before)
            self.assertEqual(cli(project, ["install", "one", "--profile", "debug"]), 0)
            self.assertTrue((root / "install/bin/one").is_file())
            self.assertFalse((root / "install/bin/two").exists())
            self.assertFalse((root / "build/Debug/two").exists())
            self.assertEqual(cli(project, ["test", "one", "--profile", "debug"]), 0)
            (root / "build/Release").mkdir()
            (root / "build/Release/keep").touch()
            (root / "data").mkdir()
            self.assertEqual(cli(project, ["clean", "one", "--profile", "debug"]), 0)
            self.assertFalse((root / "build/Debug").exists())
            self.assertTrue((root / "build/Release/keep").exists())
            self.assertTrue((root / "install/bin/one").exists())
            self.assertTrue((root / "data").is_dir())

    def test_forge_install_does_not_publish_unselected_dependency(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-install-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            engine.config = replace(engine.config, install_prefix=engine.root / "published")
            folly = mock.Mock(build_dir=engine.root / "folly-build")
            folly.build_dir.mkdir()
            engine.mark_built(request("build"), "folly")
            with (
                mock.patch.object(engine, "builder", return_value=folly) as factory,
                mock.patch("builder.project.relocate_installed_metadata") as relocate,
            ):
                engine.preflight(request("install"), ("folly",))
                engine.execute(request("install"), ("folly",))
            self.assertEqual([call.args[1] for call in factory.call_args_list], ["folly", "folly"])
            folly.build.assert_not_called()
            folly.install.assert_called_once_with(prefix=engine.config.install_prefix)
            relocate.assert_called_once_with(
                folly.build_dir / "install_manifest.txt",
                engine.staging(request("install"), "folly"),
                engine.config.install_prefix,
            )

    def test_forge_install_requires_explicit_successful_builds(self) -> None:
        self.assertEqual(cli(forge_project(ROOT / "zbuild.py"), ["install"]), 2)
        with tempfile.TemporaryDirectory(prefix="zbuild-install-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            builders = {
                name: mock.Mock(build_dir=engine.root / f"{name}-build")
                for name in ("grpc", "hpx")
            }
            builders["grpc"].build_dir.mkdir()
            engine.mark_built(request("build"), "grpc")
            with mock.patch.object(engine, "builder", side_effect=lambda req, name: builders[name]):
                with self.assertRaisesRegex(RuntimeError, "build hpx --profile release"):
                    engine.preflight(request("install"), ("grpc", "hpx"))
            for builder in builders.values():
                builder.install.assert_not_called()

    def test_failed_forge_build_invalidates_completion_stamp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-build-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            nng = mock.Mock(build_dir=engine.root / "nng-build")
            nng.build_dir.mkdir()
            engine.mark_built(request("build"), "nng")
            nng.build.side_effect = RuntimeError("compile failed")
            with mock.patch.object(engine, "builder", return_value=nng):
                with self.assertRaisesRegex(RuntimeError, "compile failed"):
                    engine.execute(request("build"), ("nng",))
            self.assertFalse(engine.build_stamp(request("build"), "nng").exists())

    def test_forge_build_marks_target_after_staging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-build-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            nng = mock.Mock(build_dir=engine.root / "nng-build")
            nng.build_dir.mkdir()
            with mock.patch.object(engine, "builder", return_value=nng):
                engine.execute(request("build"), ("nng",))
                engine.require_built(request("install"), "nng")
            nng.build.assert_called_once_with(jobs=2)
            nng.install.assert_called_once_with(prefix=engine.staging(request("build"), "nng"))

    def test_deps_publish_preserves_other_prefix_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-deps-") as directory:
            root = Path(directory)
            builder = DepsBuilder.__new__(DepsBuilder)
            builder.install_dir = root / "stage"
            builder.args = CommonBuildArgs("Release")
            builder.install_dir.mkdir()
            (builder.install_dir / "BoostConfig.cmake").write_text("new config\n")
            destination = root / "published/lib/cmake/zeta_deps/Release"
            destination.mkdir(parents=True)
            (destination / "keep.txt").write_text("user data\n")
            builder.publish(root / "published")
            self.assertEqual((destination / "BoostConfig.cmake").read_text(), "new config\n")
            self.assertEqual((destination / "keep.txt").read_text(), "user data\n")

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("cmake", "ninja", "cc")),
        "native fixture tools unavailable",
    )
    def test_installed_metadata_uses_publication_prefix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-prefix-") as directory:
            root = Path(directory)
            stage = root / "stage"
            published = root / "published"
            (root / "main.c").write_text("int one(void) { return 1; }\n")
            (root / "library.pc.in").write_text("prefix=@CMAKE_INSTALL_PREFIX@\n")
            (root / "internal.cmake.in").write_text(
                'set(PACKAGE_PREFIX "@CMAKE_INSTALL_PREFIX@")\n'
            )
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\nproject(fixture C)\n"
                "add_library(one STATIC main.c)\n"
                "configure_file(library.pc.in library.pc @ONLY)\n"
                "configure_file(internal.cmake.in internal.cmake @ONLY)\n"
                "install(TARGETS one ARCHIVE DESTINATION lib)\n"
                "install(FILES ${CMAKE_CURRENT_BINARY_DIR}/library.pc DESTINATION lib/pkgconfig)\n"
                "install(FILES ${CMAKE_CURRENT_BINARY_DIR}/internal.cmake "
                "DESTINATION lib/cmake/fixture)\n"
            )
            config = SimpleNamespace(env=dict(os.environ), install_prefix=stage)
            builder = FixtureBuilder(
                script_path=root / "zbuild.py",
                repo_config=config,
                args=CommonBuildArgs("Release"),
            )
            builder.build(jobs=2)
            builder.install(prefix=stage)
            builder.install(prefix=published)
            self.assertEqual(
                (published / "lib/pkgconfig/library.pc").read_text(), f"prefix={stage}\n"
            )
            relocate_installed_metadata(
                builder.build_dir / "install_manifest.txt", stage, published
            )
            self.assertEqual(
                (published / "lib/pkgconfig/library.pc").read_text(), f"prefix={published}\n"
            )
            self.assertEqual(
                (published / "lib/cmake/fixture/internal.cmake").read_text(),
                f'set(PACKAGE_PREFIX "{published}")\n',
            )
            self.assertTrue((published / "lib/libone.a").is_file())

    def test_metadata_relocation_rejects_manifest_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-prefix-") as directory:
            root = Path(directory)
            stage = root / "stage"
            published = root / "published"
            published.mkdir()
            outside = root / "outside.pc"
            outside.write_text(f"prefix={stage}\n")
            manifest = root / "install_manifest.txt"
            manifest.write_text(f"{published}/../outside.pc\n")
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                relocate_installed_metadata(manifest, stage, published)
            self.assertEqual(outside.read_text(), f"prefix={stage}\n")


if __name__ == "__main__":
    unittest.main()
