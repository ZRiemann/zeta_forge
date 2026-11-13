"""Tests for native selection, preview and execution scopes without builds."""

from __future__ import annotations

import contextlib
import io
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
from zeta_forge.build_cli import cli
from zeta_forge.build_ops import destdir_path

from builder.native_engine import NativeTarget
from builder.native_plan import TargetSpec, plan_native
from builder.project import NATIVE_TARGETS, ForgeEngine
from builder.project import project as forge_project


class NativePlanTests(unittest.TestCase):
    def test_sixth_target_uses_registered_builder_for_staging_and_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = mock.Mock(source_dir=root)

            def install(*, prefix: Path, destdir: Path) -> None:
                candidate = destdir_path(destdir, prefix)
                candidate.mkdir(parents=True)
                (candidate / "fixture.a").write_bytes(b"fixture")

            builder.install.side_effect = install
            factory = mock.Mock(return_value=builder)
            targets = (*NATIVE_TARGETS, NativeTarget(TargetSpec("fixture"), factory))
            project = forge_project(ROOT / "zbuild.py", targets=targets)
            engine = project.engines["native"]
            engine.root = root
            engine.config = replace(engine.config, install_prefix=root / "published")
            self.assertEqual(project.defaults[-1], "fixture")
            output = io.StringIO()
            with (
                mock.patch("builder.native_engine.require_command"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli(project, ["build", "fixture", "--dry-run"]), 0)
                factory.assert_not_called()
                self.assertEqual(cli(project, ["build", "fixture"]), 0)
            self.assertIn('"order": ["fixture"]', output.getvalue())
            self.assertEqual(factory.call_args.kwargs["script_path"], root / "builder/fixture/project.py")
            self.assertEqual(engine.require_built(request("build"), "fixture")["target"], "fixture")
            factory.reset_mock()
            with mock.patch("builder.native_engine.require_command",
                            side_effect=AssertionError("tool used")):
                self.assertEqual(cli(project, ["install", "fixture"]), 0)
            factory.assert_not_called()
            self.assertEqual((root / "published/fixture.a").read_bytes(), b"fixture")

    def test_build_clean_and_publish_scopes_are_distinct(self) -> None:
        prefix = ROOT / "test-prefix"
        rebuild = plan_native(request("rebuild", "debug"), ("folly",), ROOT, prefix, targets=tuple(target.spec for target in NATIVE_TARGETS))
        self.assertEqual(rebuild.build_scope, ("grpc", "folly"))
        self.assertEqual(rebuild.clean_scope, ("folly",))
        self.assertEqual(rebuild.publish_scope, ())
        self.assertEqual(rebuild.build_directory("folly"), ROOT / "builder/folly/build/Debug")
        self.assertEqual(rebuild.staging("folly"), ROOT / "build/staging/debug/folly")

        install = plan_native(request("install"), ("folly",), ROOT, prefix, targets=tuple(target.spec for target in NATIVE_TARGETS))
        self.assertEqual(install.publish_scope, ("folly",))
        self.assertEqual(install.build_scope, ())
        self.assertEqual(install.clean_scope, ())
        self.assertEqual(install.install_prefix, prefix)

    def test_invalid_action_target_and_prerequisite_graph(self) -> None:
        prefix = ROOT / "test-prefix"
        with self.assertRaisesRegex(ValueError, "unsupported Forge native action"):
            plan_native(request("run"), ("folly",), ROOT, prefix, targets=tuple(target.spec for target in NATIVE_TARGETS))
        with self.assertRaisesRegex(ValueError, "unknown Forge native target"):
            plan_native(request("build"), ("missing",), ROOT, prefix, targets=tuple(target.spec for target in NATIVE_TARGETS))
        with self.assertRaisesRegex(ValueError, "unknown prerequisite missing"):
            plan_native(
                request("build"), ("one",), ROOT, prefix,
                targets=(TargetSpec("one", ("missing",)),),
            )
        with self.assertRaisesRegex(ValueError, "one -> two -> one"):
            plan_native(
                request("build"), ("one",), ROOT, prefix,
                targets=(TargetSpec("one", ("two",)), TargetSpec("two", ("one",))),
            )

    def test_preview_agrees_with_rebuild_without_widening_cleanup(self) -> None:
        project = forge_project(ROOT / "zbuild.py")
        engine = project.engines["native"]
        self.assertIsInstance(engine, ForgeEngine)
        with tempfile.TemporaryDirectory() as directory:
            engine.root = Path(directory)
            rebuild = request("rebuild", "debug")
            names = ("folly",)
            plan = engine.plan(rebuild, names)
            for profile in ("release", "debug"):
                other_plan = engine.plan(request("rebuild", profile), names)
                for name in ("grpc", "folly"):
                    other_plan.staging(name).mkdir(parents=True)
                    other_plan.build_directory(name).mkdir(parents=True)
                    (other_plan.staging(name) / "keep").touch()
            before = sorted(engine.root.rglob("*"))
            with (
                mock.patch.object(engine, "builder", side_effect=AssertionError("builder used")),
                mock.patch("builder.native_engine.require_command", side_effect=AssertionError("tool used")),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(cli(project, ["rebuild", "folly", "--profile", "debug", "--dry-run"]), 0)
            self.assertIn('"order": ["grpc", "folly"]', stdout.getvalue())
            self.assertIn('"clean_scope": ["folly"]', stdout.getvalue())
            self.assertEqual(sorted(engine.root.rglob("*")), before)

            with mock.patch.object(engine, "stage_build") as stage_build:
                engine.execute(rebuild, names)
            self.assertEqual([call.args[2] for call in stage_build.call_args_list], list(plan.order))
            self.assertTrue(plan.build_directory("grpc").exists())
            self.assertTrue((plan.staging("grpc") / "keep").exists())
            self.assertFalse(plan.build_directory("folly").exists())
            self.assertFalse((plan.staging("folly") / "keep").exists())
            self.assertTrue((engine.root / "build/staging/release/folly/keep").exists())


if __name__ == "__main__":
    unittest.main()
