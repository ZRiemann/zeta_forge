"""Exercise Forge's native Conan commands and retained resolutions."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
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
from zeta_forge.conan_inputs import profile_inputs, sanitize_profiles, toolchain_inputs
from zeta_forge.conan_policy import resolve_policy
from zeta_forge.conan_session import ConanSession, materialize_recipe

from builder.project import ForgeEngine
from builder.project import project as forge_project


class NativeConanSessionTests(unittest.TestCase):
    def test_forge_targets_share_settings_and_keep_toolchain_configuration(self) -> None:
        engine = ForgeEngine(ROOT / "zbuild.py")
        req = request("build", "debug")
        for target in ("deps", "hpx", "folly"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                builder = engine.builder(req, target)
                with mock.patch.object(ConanSession, "ensure_generators", autospec=True) as ensure:
                    builder.run_conan()
                session = ensure.call_args.args[0]
                self.assertIs(session.config.env, builder.repo_config.env)
                self.assertEqual(session.cwd, ROOT / "builder" / target)
                self.assertEqual(session.output_folder, builder.conan_root)
                self.assertIn("build_type=Debug", session.flags)
                self.assertIn(f"compiler.cppstd={engine.config.cxx_standard}", session.flags)
                self.assertIn(("-c:a", "tools.build:jobs=2"), zip(session.flags, session.flags[1:]))

                boost_ref = "boost/1.91.0@zetax/forge#test-revision"
                recipe = materialize_recipe(target, session.config, boost_ref, Path(directory))
                snapshot = json.loads(recipe.with_name("native-policy.json").read_text())
                self.assertIn(boost_ref, snapshot["requires"])
                self.assertEqual("boost/*:zlib" in snapshot["default_options"], target == "folly")
                conf = dict(
                    arg.split("=", 1)
                    for option, arg in zip(session.flags, session.flags[1:])
                    if option == "-c"
                )
                toolchain = "tools.cmake.cmaketoolchain:user_toolchain"
                blocks = "tools.cmake.cmaketoolchain:enabled_blocks"
                if target == "deps":
                    self.assertNotIn(toolchain, conf)
                else:
                    self.assertEqual(json.loads(conf[toolchain]), [str(builder.user_toolchain)])
                if target == "hpx":
                    self.assertIn("find_paths", json.loads(conf[blocks]))
                else:
                    self.assertNotIn(blocks, conf)

    def test_policy_and_file_inputs(self) -> None:
        env = {
            "ZETA_FMT_VERSION": "11.2.0",
            "ZETA_SQLITE_VERSION": "3.50.1",
            "ZETA_OPENSSL_VERSION": "3.5.0",
        }
        policy = resolve_policy("deps", env, "boost/1.90.0")
        self.assertIn("fmt/11.2.0", policy.requires)
        self.assertIn("sqlite3/3.50.1", policy.requires)
        self.assertIn("openssl/3.5.0", policy.requires)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            profiles.mkdir()
            default = profiles / "default"
            default.write_text("include(common)\n[settings]\ncompiler=gcc\n")
            common = profiles / "common"
            common.write_text("[settings]\ncompiler.version=15\n")
            identity = profile_inputs(root, {})
            default.write_text(
                default.read_text() + "[conf]\ntools.build:jobs = 8\n[buildenv]\nHTTPS_PROXY=x\n"
            )
            self.assertEqual(profile_inputs(root, {}), identity)
            info = common.stat()
            common.write_text("[settings]\ncompiler.version=14\n")
            os.utime(common, ns=(info.st_atime_ns, info.st_mtime_ns))
            self.assertNotEqual(profile_inputs(root, {}), identity)
            common.unlink()
            self.assertIsNone(profile_inputs(root, {})[str(common)])

            toolchain = root / "toolchain.cmake"
            include = root / "extra options.cmake"
            toolchain.write_text('include("${CMAKE_CURRENT_LIST_DIR}/extra options.cmake")\n')
            missing = toolchain_inputs([toolchain])
            self.assertIsNone(missing[str(include)])
            include.write_text("set(CMAKE_CXX_FLAGS -g)\n")
            self.assertNotEqual(toolchain_inputs([toolchain]), missing)

    def test_jobs_and_proxy_values_do_not_change_effective_profile_identity(self) -> None:
        def profiles(jobs: int, proxy: str) -> dict:
            profile = {
                "settings": {"compiler": "gcc"},
                "conf": {"tools.build:jobs": jobs, "core.net.http:proxies": {"http": proxy}},
                "build_env": f"CC=gcc\nHTTPS_PROXY={proxy}",
            }
            return {"host": profile, "build": profile}

        original = sanitize_profiles(profiles(2, "http://user:password@a"))
        self.assertEqual(original, sanitize_profiles(profiles(4, "http://user:new@b")))
        self.assertNotIn("password", json.dumps(original))

    def test_session_fingerprint_tracks_policy_cppstd_and_patch_content(self) -> None:
        config = ForgeEngine(ROOT / "zbuild.py").config
        args = ForgeEngine(ROOT / "zbuild.py").builder(request("build"), "deps").args
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = root / "builder"
            shutil.copytree(ROOT / "builder/boost", builder / "boost")
            shutil.copyfile(ROOT / "builder/conanfile.py", builder / "conanfile.py")
            config = replace(config, builder_dir=builder, env={})
            def fingerprint(current_config, current_args=args):
                session = ConanSession("deps", current_config, current_args, root / "output")
                host = {"settings": {"compiler.cppstd": current_config.cxx_standard}, "conf": {}}
                build = {"settings": {}, "conf": {}}
                probe = (json.dumps({"host": host, "build": build}), str(root), "Conan version 2.34.0")
                with mock.patch.object(session, "probe", side_effect=probe):
                    return session.fingerprint

            initial = fingerprint(config)
            self.assertEqual(fingerprint(config, replace(args, jobs=8)), initial)
            self.assertNotEqual(fingerprint(replace(config, cxx_standard="23")), initial)
            self.assertNotEqual(fingerprint(replace(config, env={"ZETA_FMT_VERSION": "11.2.0"})), initial)
            patch = builder / "boost/cobalt-openssl-b2.patch"
            before = patch.stat()
            patch.write_bytes(patch.read_bytes() + b"\n")
            os.utime(patch, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertNotEqual(fingerprint(config), initial)

    def test_refresh_preview_and_incompatible_actions(self) -> None:
        project = forge_project(ROOT / "zbuild.py")
        output = io.StringIO()
        with (
            mock.patch.object(ConanSession, "probe", side_effect=AssertionError("tool invoked")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(cli(project, ["build", "deps", "--refresh-conan-lock", "--dry-run"]), 0)
            for action in (
                ["install", "deps"], ["doctor", "deps"], ["metadata", "deps"],
                ["build", "nng"], ["check", "rust-deps"], ["test", "build-tools"],
            ):
                self.assertEqual(cli(project, [*action, "--refresh-conan-lock"]), 2)
        self.assertIn('"refresh_conan_lock": ["deps"]', output.getvalue())

    def test_generators_reuse_lock_and_failed_refresh_preserves_prior_generation(self) -> None:
        config = ForgeEngine(ROOT / "zbuild.py").config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(config, forge_root=root)
            args = ForgeEngine(ROOT / "zbuild.py").builder(request("build"), "deps").args
            output = root / "output"
            graph = {"graph": {"error": None, "nodes": {
                "1": {"ref": "example/1.0#recipe", "context": "host", "package_id": "id"},
            }}}
            failures = False

            def session(refresh=False):
                value = ConanSession("deps", config, replace(args, refresh_conan_lock=refresh), output)
                value.__dict__["inputs"] = {"fixture": True}
                value.run = mock.Mock(side_effect=run_conan)
                return value

            def run_conan(*command):
                if command[0] == "lock":
                    lock = Path(next(arg.split("=", 1)[1] for arg in command if str(arg).startswith("--lockfile-out=")))
                    lock.write_text(json.dumps({
                        "version": "0.5", "requires": ["example/1.0#recipe"],
                        "build_requires": [], "python_requires": [], "config_requires": [],
                    }))
                    return
                generators = output / "build" / "Release" / "generators"
                generators.mkdir(parents=True)
                (generators / "conan_toolchain.cmake").write_text("partial" if failures else "good")
                if failures:
                    raise RuntimeError("fixture install failed")
                path = Path(next(arg.split("=", 1)[1] for arg in command if str(arg).startswith("--out-file=")))
                path.write_text(json.dumps(graph))

            with mock.patch(
                "zeta_forge.conan_session.conan_boost.prepare_boost_recipe", return_value="boost/1.91.0"
            ):
                initial = session()
                initial.ensure_generators()
                pointer = initial.state_dir / "active.json"
                original = pointer.read_bytes()
                self.assertEqual(initial.run.call_count, 2)

                cached = session()
                cached.ensure_generators()
                cached.run.assert_not_called()

                shutil.rmtree(output)
                rebuilt = session()
                rebuilt.ensure_generators()
                self.assertEqual(rebuilt.run.call_count, 1)
                self.assertEqual(rebuilt.run.call_args.args[0], "install")
                self.assertEqual(pointer.read_bytes(), original)

                failures = True
                refreshed = session(refresh=True)
                with self.assertRaisesRegex(RuntimeError, "fixture install failed"):
                    refreshed.ensure_generators()
                self.assertIn("--update", refreshed.run.call_args_list[0].args)
                self.assertEqual(pointer.read_bytes(), original)
                self.assertEqual(
                    (refreshed.generators / "conan_toolchain.cmake").read_text(), "good"
                )
                self.assertFalse(session().needs_generators())
                failures = False
                graph["graph"]["nodes"]["1"]["prev"] = "new-binary"
                session(refresh=True).ensure_generators()
                self.assertNotEqual(pointer.read_bytes(), original)
                self.assertEqual(len(list((initial.state_dir / "generations").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
