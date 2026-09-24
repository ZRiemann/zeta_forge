"""Side-effect bounded regression coverage for the public build contract."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.build_cli import Product, Project, Request, cli
from zeta_forge.build_ops import remove_generated, run_program
from zeta_forge.cmake_engine import CMakeEngine
from zeta_forge.gradle_engine import GradleEngine
from zeta_forge.rust_engine import RustEngine, RustTarget


def request(action: str, profile: str = "release", **options) -> Request:
    return Request(action, profile, 2, argparse.Namespace(**options))


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = mock.Mock()
        self.engine.describe.return_value = {"scope": "native"}
        self.project = Project(
            "sample",
            (
                Product("app", "native", "application", ("build", "run", "dev", "clean")),
                Product("lib", "native", "library", ("build", "clean")),
            ),
            {"native": self.engine},
            ("app", "lib"),
            run_default="app",
        )

    def invoke(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cli(self.project, args)
        return status, output.getvalue()

    def test_defaults_profiles_and_deduplication(self) -> None:
        self.assertEqual(self.invoke("build")[0], 0)
        req, names = self.engine.execute.call_args.args
        self.assertEqual(names, ("app", "lib"))
        self.assertEqual(
            (req.profile, req.cmake_profile, req.cargo_profile), ("release", "Release", "release")
        )
        self.assertEqual(self.invoke("build", "lib", "lib", "--profile", "debug")[0], 0)
        req, names = self.engine.execute.call_args.args
        self.assertEqual(names, ("lib",))
        self.assertEqual((req.cmake_profile, req.cargo_profile), ("Debug", "dev"))

    def test_invalid_and_removed_syntax(self) -> None:
        for args in (
            ("--rebuild",),
            ("build", "--all"),
            ("build", "--subsystem", "app"),
            ("build", "--BUILD_TYPE=Release"),
            ("frontend", "build"),
            ("build", "--profile", "dev"),
            ("build", "unknown"),
            ("test", "lib"),
            ("run", "app", "lib"),
            ("build", "--", "secret"),
            ("build", "-j", "0"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args)[0], 2)
        self.engine.execute.assert_not_called()

    def test_read_only_modes_do_not_preflight_or_execute(self) -> None:
        for args in (("--help",), ("list",), ("metadata",), ("clean", "--dry-run")):
            self.assertEqual(self.invoke(*args)[0], 0)
        self.engine.execute.assert_not_called()
        self.engine.preflight.assert_not_called()

    def test_run_forwards_without_logging_arguments(self) -> None:
        status, output = self.invoke("run", "--", "--password", "test-secret")
        self.assertEqual(status, 0)
        self.assertNotIn("test-secret", output)
        self.assertEqual(
            self.engine.execute.call_args.args[0].arguments, ("--password", "test-secret")
        )

    def test_all_preflights_precede_any_execution(self) -> None:
        second = mock.Mock()
        second.describe.return_value = {}
        second.preflight.side_effect = RuntimeError("missing tool")
        self.project = Project(
            "two",
            self.project.products + (Product("other", "other", "other", ("build",)),),
            {"native": self.engine, "other": second},
            ("app", "other"),
        )
        self.assertEqual(self.invoke("build")[0], 1)
        self.engine.execute.assert_not_called()
        second.preflight.side_effect = None
        self.engine.execute.side_effect = RuntimeError("failed")
        self.assertEqual(self.invoke("build")[0], 1)
        second.execute.assert_not_called()
        self.engine.execute.side_effect = KeyboardInterrupt
        self.assertEqual(self.invoke("build")[0], 130)


class AdapterTests(unittest.TestCase):
    def test_cmake_selected_targets_tests_and_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = mock.Mock(build_dir=root / "build/Release", source_dir=root)
            builder.repo_config.env = {}
            engine = CMakeEngine(
                root,
                lambda req, names: builder,
                ("app", "lib"),
                test_targets=lambda names: ("unit_tests",),
                components={"lib": ("library",)},
            )
            engine.execute(request("build"), ("app",))
            builder.build.assert_called_once_with(("app",), jobs=2)
            with mock.patch("zeta_forge.cmake_engine.run_command") as run:
                engine.execute(request("test"), ("lib",))
            builder.build.assert_called_with(("unit_tests",), jobs=2)
            self.assertIn("--no-tests=error", run.call_args.args[0])
            engine.execute(request("install"), ("lib",))
            builder.install.assert_called_once_with(("library",))

    def test_cargo_profile_features_and_fullstack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = RustEngine(
                root,
                mock.Mock(),
                {"app": RustTarget("app", root, "fullstack", ("web",))},
                root / "target",
            )
            manager = mock.Mock()
            manager.require_cargo_tool.return_value = "/managed/bin/dx"
            with (
                mock.patch.object(engine, "environment", return_value={}),
                mock.patch("zeta_forge.rust_engine.run_command") as run,
            ):
                engine.build(manager, request("build"), ("app",))
            command = run.call_args.args[0]
            for flag in ("--locked", "--release", "--web", "--fullstack", "--force-sequential"):
                self.assertIn(flag, command)
            with mock.patch.object(engine, "cargo") as cargo:
                engine.validate_code(manager, request("check", "debug"), ("app",))
            commands = [call.args[2:] for call in cargo.call_args_list]
            self.assertEqual(commands[0], ("fmt", "--all", "--", "--check"))
            self.assertEqual(len(commands), 5)
            self.assertTrue(any("server" in cmd for cmd in commands))
            self.assertTrue(any("wasm32-unknown-unknown" in cmd for cmd in commands))
            for cmd in commands[1:]:
                self.assertIn("--locked", cmd)
                self.assertEqual(cmd[cmd.index("--profile") + 1], "dev")
            with mock.patch.object(engine, "cargo") as cargo:
                engine.validate_code(manager, request("test"), ("app",))
            self.assertEqual(cargo.call_count, 1)
            self.assertNotIn("wasm32-unknown-unknown", cargo.call_args.args)

    def test_run_never_builds_and_preserves_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = RustEngine(
                root, mock.Mock(), {"app": RustTarget("app", root)}, root / "target"
            )
            with mock.patch.object(engine, "manager"), mock.patch.object(engine, "build") as build:
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    engine.preflight(request("run"), ("app",))
                with (
                    mock.patch.object(engine, "environment", return_value={}),
                    mock.patch("zeta_forge.rust_engine.run_program") as run,
                ):
                    engine.execute(request("run"), ("app",))
                build.assert_not_called()
                self.assertEqual(run.call_args.kwargs["cwd"], root / "target/release")

    def test_profile_clean_protects_data_installs_and_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = RustEngine(
                root, mock.Mock(), {"app": RustTarget("app", root)}, root / "target"
            )
            for name in (
                "target/debug",
                "target/release",
                "target/dx/app/debug",
                "target/x86_64-unknown-linux-gnu/server-dev",
                "target/wasm32-unknown-unknown/wasm-dev",
                "target/wasm32-unknown-unknown/wasm-release",
                "data",
                "install",
            ):
                (root / name).mkdir(parents=True)
                (root / name / "sentinel").touch()
            engine.clean(request("clean", "debug"))
            self.assertFalse((root / "target/debug").exists())
            for name in (
                "target/release",
                "target/wasm32-unknown-unknown/wasm-release",
                "data",
                "install",
            ):
                self.assertTrue((root / name / "sentinel").is_file())
            self.assertFalse((root / "target/x86_64-unknown-linux-gnu/server-dev").exists())
            self.assertFalse((root / "target/wasm32-unknown-unknown/wasm-dev").exists())
            with self.assertRaises(RuntimeError):
                remove_generated(root, root)
            (root / "link").symlink_to(root / "install", target_is_directory=True)
            with self.assertRaises(RuntimeError):
                remove_generated(root / "link", root)

    def test_gradle_variants_and_apk_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = GradleEngine(root, root / "android", environment=lambda req: {})
            for variant in ("debug", "release"):
                apk = root / "android/app/build/outputs/apk" / variant / "app.apk"
                apk.parent.mkdir(parents=True)
                apk.write_bytes(variant.encode())
            engine.clean(request("clean", "debug"))
            self.assertEqual(
                (root / "android/app/build/outputs/apk/release/app.apk").read_bytes(), b"release"
            )
            self.assertFalse((root / "android/app/build/outputs/apk/debug").exists())
            with mock.patch("zeta_forge.gradle_engine.run_command") as run:
                engine.execute(request("test", "debug"), ("android",))
                self.assertIn(":app:testDebugUnitTest", run.call_args.args[0])
                engine.execute(request("build"), ("android",))
                self.assertIn(":app:assembleRelease", run.call_args.args[0])

    def test_foreground_process_arguments_and_interrupt(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_program(
                [sys.executable, "-c", "import sys; assert sys.argv[1] == 'secret'", "secret"],
                cwd=FORGE,
                env=dict(os.environ),
            )
        self.assertNotIn("secret", output.getvalue())
        process = mock.Mock(pid=9876)
        process.wait.side_effect = [KeyboardInterrupt, 0]
        with (
            mock.patch("zeta_forge.build_ops.subprocess.Popen", return_value=process),
            mock.patch("zeta_forge.build_ops.os.killpg") as kill,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_program([sys.executable], cwd=FORGE, env={})
        self.assertEqual(kill.call_count, 2)


class ForgeEntrypointTests(unittest.TestCase):
    def test_launcher_is_read_only_and_rejects_legacy_flags(self) -> None:
        for args, expected in (
            (("--help",), 0),
            (("list",), 0),
            (("build", "--dry-run"), 0),
            (("test", "--profile", "debug", "--dry-run"), 0),
            (("build", "--all"), 2),
            (("build", "--BUILD_TYPE=Release"), 2),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(FORGE / "zbuild.py"), *args],
                    cwd=FORGE,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
