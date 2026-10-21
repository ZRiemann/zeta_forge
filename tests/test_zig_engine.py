"""Bounded contract checks for the shared Zig adapter; no compiler invocations."""
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
from zeta_forge.zig_engine import ZIG_VERSION, ZigEngine, ZigTarget


def request(action: str, profile: str = "release") -> Request:
    return Request(action, profile, 3, argparse.Namespace())


class ZigContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "build.zig").write_text("// native graph\n")
        (self.root / "build.zig.zon").write_text(".{}\n")
        self.engine = ZigEngine(self.root, {"lib": ZigTarget("lib"), "example": ZigTarget("example", "example")})
        actions = ("doctor", "check", "build", "rebuild", "test", "clean")
        self.project = Project("zig-test", (Product("lib", "zig", "library", actions),
                                Product("example", "zig", "example", actions + ("run", "dev"))),
                               {"zig": self.engine}, ("lib",))

    def invoke(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cli(self.project, args)
        return status, output.getvalue()

    def test_read_only_and_dry_run_do_not_find_or_run_zig(self) -> None:
        with mock.patch("zeta_forge.zig_engine.require_command") as find, mock.patch("zeta_forge.zig_engine.run_program") as run:
            for arguments in (("--help",), ("list",), ("metadata",), ("build", "--dry-run"), ("clean", "--dry-run")):
                self.assertEqual(self.invoke(*arguments)[0], 0)
            find.assert_not_called()
            run.assert_not_called()
        self.assertFalse((self.root / "build").exists())

    def test_profiles_and_native_scope(self) -> None:
        for profile, optimize in (("debug", "Debug"), ("release", "ReleaseFast")):
            with self.subTest(profile=profile), mock.patch("zeta_forge.zig_engine.require_command", return_value="/usr/bin/zig"), mock.patch("zeta_forge.zig_engine.run_program") as run:
                self.engine.execute(request("build", profile), ("lib", "example"))
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ["/usr/bin/zig", "build", "lib", "example"])
                self.assertIn(f"-Doptimize={optimize}", command)
                self.assertIn(str(self.root / "build" / "zig" / profile), command)
                self.assertIn("-j3", command)
                self.assertEqual(run.call_args.kwargs["cwd"], self.root)
        self.assertIn("all components", self.engine.describe(request("test"), ("lib",))["verification_scope"])

    def test_exact_version_and_run_missing_artifact(self) -> None:
        with mock.patch("zeta_forge.zig_engine.require_command", return_value="/usr/bin/zig"), mock.patch("zeta_forge.zig_engine.subprocess.run") as run:
            run.return_value.stdout = "0.17.0-dev.1\n"
            with self.assertRaisesRegex(RuntimeError, "required"):
                self.engine.preflight(request("build"), ("lib",))
            run.return_value.stdout = ZIG_VERSION + "\n"
            self.engine.preflight(request("build"), ("lib",))
            with self.assertRaisesRegex(RuntimeError, "artifact is missing"):
                self.engine.preflight(request("run"), ("example",))

    def test_run_never_builds_and_hides_arguments(self) -> None:
        req = Request("run", "release", 3, argparse.Namespace(), ("private-argument",))
        with mock.patch.object(self.engine, "native") as build, mock.patch("zeta_forge.zig_engine.run_program") as run:
            self.engine.execute(req, ("example",))
            build.assert_not_called()
            self.assertEqual(run.call_args.args[0][1:], ["private-argument"])
        self.assertNotIn("private-argument", str(self.engine.describe(req, ("example",))))

    def test_dev_builds_before_run_and_preserves_environment(self) -> None:
        calls: list[str] = []
        with mock.patch.object(self.engine, "native", side_effect=lambda *args: calls.append("build")), mock.patch("zeta_forge.zig_engine.run_program", side_effect=lambda *args, **kwargs: calls.append("run")) as run, mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://test-proxy:1234"}):
            self.engine.execute(request("dev"), ("example",))
            self.assertEqual(calls, ["build", "run"])
            self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], "http://test-proxy:1234")

    def test_clean_only_selected_profile_and_rejects_symlinks(self) -> None:
        for profile in ("debug", "release"):
            directory = self.engine.profile_dir(request("clean", profile))
            directory.mkdir(parents=True)
            (directory / "marker").write_text(profile)
        self.engine.preflight(request("clean", "debug"), ("lib",))
        self.engine.execute(request("clean", "debug"), ("lib",))
        self.assertFalse(self.engine.profile_dir(request("clean", "debug")).exists())
        self.assertTrue((self.engine.profile_dir(request("clean")) / "marker").is_file())
        self.engine.profile_dir(request("clean", "debug")).symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            self.engine.preflight(request("clean", "debug"), ("lib",))

    def test_cli_failure_exit_codes_and_interruption(self) -> None:
        self.assertEqual(self.invoke("fetch")[0], 2)
        self.assertEqual(self.invoke("run", "lib")[0], 2)
        self.assertEqual(self.invoke("run")[0], 2)
        with mock.patch.object(self.engine, "preflight", side_effect=RuntimeError("wrong toolchain")):
            self.assertEqual(self.invoke("check")[0], 1)
        with mock.patch.object(self.engine, "preflight"), mock.patch.object(self.engine, "execute", side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke("test")[0], 130)

    def test_nonzero_version_command_is_an_actionable_error(self) -> None:
        with mock.patch("zeta_forge.zig_engine.require_command", return_value="/usr/bin/zig"), mock.patch("zeta_forge.zig_engine.subprocess.run", side_effect=subprocess.CalledProcessError(9, ["zig", "version"])):
            with self.assertRaisesRegex(RuntimeError, "version"):
                self.engine.preflight(request("doctor"), ("lib",))


if __name__ == "__main__":
    unittest.main()
