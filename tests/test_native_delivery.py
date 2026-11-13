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

from test_build_contract import request
from zeta_forge.build_cli import Product, Project, cli
from zeta_forge.build_ops import destdir_path, relocate_installed_metadata
from zeta_forge.cmake_builder import CMakeProjectBuilder, CommonBuildArgs
from zeta_forge.cmake_engine import CMAKE_ACTIONS, CMakeEngine
from zeta_forge.conan_state import write_json
from zeta_forge.native_artifacts import make_receipt

from builder.deps.project import DepsBuilder
from builder.grpc.project import GrpcBuilder
from builder.project import ForgeEngine
from builder.project import project as forge_project


def seed_stage(engine: ForgeEngine, name: str, dependencies: dict[str, str] | None = None) -> Path:
    req = request("build")
    stage = engine.staging(req, name)
    stage.mkdir(parents=True)
    (stage / "old.a").write_bytes(b"old")
    write_json(
        engine.build_stamp(req, name),
        make_receipt(
            stage, name, req.profile, sources=[], conan=None, references=[], dependencies=dependencies or {},
        ),
    )
    return stage


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
    def test_grpc_requires_external_proto_submodule(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grpc-source-") as directory:
            source = Path(directory)
            for dependency in (
                "abseil-cpp", "boringssl-with-bazel", "cares/cares",
                "protobuf", "re2", "zlib",
            ):
                (source / "third_party" / dependency).mkdir(parents=True)
            engine = ForgeEngine(ROOT / "zbuild.py")
            config = replace(
                engine.config,
                source_dirs={**engine.config.source_dirs, "ZETA_GRPC_SRC_DIR": source},
            )
            builder = GrpcBuilder(
                script_path=ROOT / "builder/grpc/project.py",
                repo_config=config,
                args=CommonBuildArgs("Release"),
            )

            with self.assertRaisesRegex(RuntimeError, "third_party/grpc-proto") as error:
                builder.validate()
            self.assertIn("git submodule update --init --recursive 3rd/grpc", str(error.exception))

            (source / "third_party/grpc-proto").mkdir()
            builder.validate()

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
            seed_stage(engine, "grpc")
            grpc = engine.require_built(request("build"), "grpc")
            seed_stage(engine, "folly", {"grpc": grpc["artifact_id"]})
            with (
                mock.patch.object(engine, "builder", side_effect=AssertionError("builder used")),
                mock.patch("builder.project.require_command", side_effect=AssertionError("tool used")),
            ):
                engine.preflight(request("install"), ("folly",))
                with self.assertRaisesRegex(RuntimeError, "explicitly install grpc"):
                    engine.execute(request("install"), ("folly",))
                engine.execute(request("install"), ("grpc",))
                engine.execute(request("install"), ("folly",))
            self.assertTrue((engine.config.install_prefix / "old.a").is_file())

    def test_forge_install_requires_explicit_successful_builds(self) -> None:
        self.assertEqual(cli(forge_project(ROOT / "zbuild.py"), ["install"]), 2)
        with tempfile.TemporaryDirectory(prefix="zbuild-install-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            engine.config = replace(engine.config, install_prefix=engine.root / "published")
            seed_stage(engine, "grpc")
            with mock.patch.object(engine, "builder", side_effect=AssertionError("builder used")):
                with self.assertRaisesRegex(RuntimeError, "build hpx --profile release"):
                    engine.execute(request("install"), ("grpc", "hpx"))
            self.assertFalse((engine.config.install_prefix / "old.a").exists())

    def test_failed_forge_build_invalidates_completion_stamp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-build-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            nng = mock.Mock(build_dir=engine.root / "nng-build")
            nng.build_dir.mkdir()
            seed_stage(engine, "nng")
            nng.build.side_effect = RuntimeError("compile failed")
            with mock.patch.object(engine, "builder", return_value=nng):
                with self.assertRaisesRegex(RuntimeError, "compile failed"):
                    engine.execute(request("build"), ("nng",))
            self.assertFalse(engine.build_stamp(request("build"), "nng").exists())

    def test_forge_build_marks_target_after_staging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zbuild-build-") as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            nng = mock.Mock(build_dir=engine.root / "nng-build", source_dir=engine.root)
            nng.build_dir.mkdir()
            stage = seed_stage(engine, "nng")

            def install(*, prefix: Path, destdir: Path) -> None:
                candidate = destdir_path(destdir, prefix)
                candidate.mkdir(parents=True)
                (candidate / "new.a").write_bytes(b"new")
                (candidate / "link.a").symlink_to("new.a")

            nng.install.side_effect = install
            with mock.patch.object(engine, "builder", return_value=nng):
                engine.execute(request("build"), ("nng",))
                engine.require_built(request("install"), "nng")
            nng.build.assert_called_once_with(jobs=2)
            self.assertEqual(nng.install.call_args.kwargs["prefix"], stage)
            self.assertFalse((stage / "old.a").exists())
            self.assertEqual((stage / "link.a").readlink(), Path("new.a"))
            (stage / "new.a").write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "contents differ"):
                engine.require_built(request("install"), "nng")

    def test_legacy_receipt_and_escaping_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ForgeEngine(ROOT / "zbuild.py")
            engine.root = Path(directory)
            stage = seed_stage(engine, "nng")
            (stage / "escape").symlink_to(engine.root)
            with self.assertRaisesRegex(RuntimeError, "Unsafe native stage symlink"):
                engine.require_built(request("install"), "nng")
            write_json(engine.build_stamp(request("build"), "nng"), {"target": "nng", "profile": "release"})
            with self.assertRaisesRegex(RuntimeError, "build nng --profile release"):
                engine.require_built(request("install"), "nng")

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
