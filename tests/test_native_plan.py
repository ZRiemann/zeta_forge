"""Tests for native selection, preview and execution scopes without builds."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

from test_build_contract import request
from zeta_forge.build_cli import cli

from builder.native_plan import TargetSpec, plan_native
from builder.project import ForgeEngine
from builder.project import project as forge_project


class NativePlanTests(unittest.TestCase):
    def test_build_clean_and_publish_scopes_are_distinct(self) -> None:
        prefix = ROOT / "test-prefix"
        rebuild = plan_native(request("rebuild", "debug"), ("folly",), ROOT, prefix)
        self.assertEqual(rebuild.build_scope, ("grpc", "folly"))
        self.assertEqual(rebuild.clean_scope, ("folly",))
        self.assertEqual(rebuild.publish_scope, ())
        self.assertEqual(rebuild.build_directory("folly"), ROOT / "builder/folly/build/Debug")
        self.assertEqual(rebuild.staging("folly"), ROOT / "build/staging/debug/folly")

        install = plan_native(request("install"), ("folly",), ROOT, prefix)
        self.assertEqual(install.publish_scope, ("folly",))
        self.assertEqual(install.build_scope, ())
        self.assertEqual(install.clean_scope, ())
        self.assertEqual(install.install_prefix, prefix)

    def test_invalid_action_target_and_prerequisite_graph(self) -> None:
        prefix = ROOT / "test-prefix"
        with self.assertRaisesRegex(ValueError, "unsupported Forge native action"):
            plan_native(request("run"), ("folly",), ROOT, prefix)
        with self.assertRaisesRegex(ValueError, "unknown Forge native target"):
            plan_native(request("build"), ("missing",), ROOT, prefix)
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
                mock.patch("builder.project.require_command", side_effect=AssertionError("tool used")),
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
