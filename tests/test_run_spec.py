"""Explicit native registration and language-neutral execution contracts."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.build_cli import Request
from zeta_forge.cmake_engine import CMakeEngine
from zeta_forge.cmake_run import CMakeRunTarget, read_cmake_run
from zeta_forge.run_spec import RunSpec, launch_run
from zeta_forge.rust_engine import RustEngine, RustTarget
from zeta_forge.zig_engine import ZigEngine, ZigTarget


def request(action: str, profile: str = "release", *args: str) -> Request:
    return Request(action, profile, 2, argparse.Namespace(), args)


@unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
class NativeRunMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="native run ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "sub").mkdir()
        helper = (FORGE / "cmake_util/add_run_target.cmake").as_posix()
        (self.source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.20)
project(run_metadata NONE)
include("{helper}")
option(ENABLE_APP "Enable fixture" ON)
if(ENABLE_APP)
  add_subdirectory(sub)
endif()
''')
        self.script = self.source / "record args.py"
        self.script.write_text('''import json, os, pathlib, sys
pathlib.Path("observed.json").write_text(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"env":os.getenv("RUN_FIXTURE")}))
''')
        (self.source / "sub/CMakeLists.txt").write_text('''
add_executable(native_app IMPORTED GLOBAL)
set_target_properties(native_app PROPERTIES IMPORTED_LOCATION "${PYTHON_EXECUTABLE}")
add_run_target(native_app ARGS "${PROJECT_SOURCE_DIR}/record args.py"
  "argument with spaces" "quote\\\"and\\\\slash" "unicode-中文"
  "$<IF:$<CONFIG:Debug>,debug-value,release-value>" "line-value"
  "semi$<SEMICOLON>colon")
''')

    def configure(self, profile: str, *extra: str) -> Path:
        build = self.root / "output" / profile
        subprocess.run(["cmake", "-S", str(self.source), "-B", str(build),
                        f"-DCMAKE_BUILD_TYPE={profile}", f"-DPYTHON_EXECUTABLE={sys.executable}",
                        *extra], check=True, capture_output=True, text=True, timeout=30)
        return build

    def test_native_values_and_registered_alias_run_without_scanning_or_building(self) -> None:
        for profile in ("Debug", "Release"):
            with self.subTest(profile=profile):
                build = self.configure(profile)
                builder = mock.Mock(source_dir=self.source, build_dir=build)
                builder.repo_config.install_prefix = self.root / "install"
                environment = {**os.environ, "RUN_FIXTURE": "preserved"}
                engine = CMakeEngine(
                    self.source, lambda req, names: builder, ("public_app", "library"),
                    test_targets=lambda names: (),
                    run_targets={"public_app": CMakeRunTarget("native_app")},
                    run_environment=lambda name, root: environment,
                )
                req = request("run", profile.lower(), "--extra", "extra value")
                with (mock.patch("os.walk", side_effect=AssertionError("must not scan")),
                      mock.patch("pathlib.Path.rglob", side_effect=AssertionError("must not glob"))):
                    spec = engine.resolve_run(req, "public_app")
                    self.assertIsInstance(spec, RunSpec)
                    self.assertEqual(spec.executable, Path(sys.executable))
                    self.assertEqual(spec.working_directory, build / "sub")
                    self.assertEqual(spec.arguments[1:], (
                        "argument with spaces", 'quote"and\\slash', "unicode-中文",
                        "debug-value" if profile == "Debug" else "release-value",
                        "line-value", "semi;colon", "--extra", "extra value",
                    ))
                    engine.preflight(req, ("public_app",))
                    engine.execute(req, ("public_app",))
                    metadata = engine.describe(request("metadata", profile.lower()),
                                               ("public_app", "library"))
                self.assertEqual(metadata["executables"], {"public_app": sys.executable})
                observed = json.loads((build / "sub/observed.json").read_text())
                self.assertEqual(observed["args"], list(spec.arguments[1:]))
                self.assertEqual(observed["cwd"], str(spec.working_directory))
                self.assertEqual(observed["env"], "preserved")
                # Native CMake invocation and the new registry must agree on
                # default argv, including generator-expression punctuation.
                subprocess.run(["cmake", "--build", str(build), "--target", "run_native_app"],
                               env=environment, check=True, capture_output=True, timeout=30)
                native = json.loads((build / "sub/observed.json").read_text())
                self.assertEqual(native["args"], list(spec.arguments[1:-2]))
                self.assertEqual(native["cwd"], observed["cwd"])
                builder.build.assert_not_called()
                with self.assertRaisesRegex(ValueError, "registered"):
                    engine.validate_request(request("run"), ("library",))

    def test_missing_disabled_or_mismatched_metadata_never_falls_back(self) -> None:
        build = self.configure("Release", "-DENABLE_APP=OFF")
        with self.assertRaisesRegex(RuntimeError, "reconfigure"):
            read_cmake_run(CMakeRunTarget("native_app"), self.source, build, "Release")
        self.configure("Release", "-DENABLE_APP=ON")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            read_cmake_run(CMakeRunTarget("native_app"), self.root, build, "Release")
        # A moved build tree cannot silently launch values from the previous owner.
        moved = self.root / "copied"
        shutil.copytree(build, moved)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            read_cmake_run(CMakeRunTarget("native_app"), self.source, moved, "Release")
        self.configure("Release", "-DENABLE_APP=OFF")
        with self.assertRaisesRegex(ValueError, "stale"):
            read_cmake_run(CMakeRunTarget("native_app"), self.source, build, "Release")

    def test_invalid_schema_count_and_nul_argument_fail(self) -> None:
        build = self.configure("Release")
        metadata = build / ".zeta-run/Release/native_app"
        for name, bad, valid in (("schema", "2", "1"), ("argc", "9999", "7"),
                                 ("arg-0", "bad\0value", str(self.script))):
            with self.subTest(field=name):
                (metadata / name).write_text(bad)
                with self.assertRaises(ValueError):
                    read_cmake_run(CMakeRunTarget("native_app"), self.source, build, "Release")
                (metadata / name).write_text(valid)

    def test_raw_fields_preserve_newlines_without_translation(self) -> None:
        build = self.configure("Release")
        path = build / ".zeta-run/Release/native_app/arg-1"
        path.write_bytes(b"first\r\nsecond\n")
        spec = read_cmake_run(CMakeRunTarget("native_app"), self.source, build, "Release")
        self.assertEqual(spec.arguments[1], "first\r\nsecond\n")

    @unittest.skipUnless(shutil.which("cc"), "C compiler unavailable")
    def test_custom_native_output_name_and_directory_come_from_cmake(self) -> None:
        (self.source / "main.c").write_text("int main(void) { return 0; }\n")
        with (self.source / "CMakeLists.txt").open("a") as out:
            out.write('''
enable_language(C)
add_executable(real_app main.c)
set_target_properties(real_app PROPERTIES OUTPUT_NAME renamed
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/custom output")
add_run_target(real_app)
''')
        for profile in ("Debug", "Release"):
            build = self.configure(profile)
            spec = read_cmake_run(CMakeRunTarget("real_app"), self.source, build, profile)
            suffix = ".exe" if sys.platform == "win32" else ""
            self.assertEqual(spec.executable, build / "custom output" / f"renamed{suffix}")
            self.assertEqual(spec.arguments, ())
            # Only a tiny private fixture is compiled, never an application stack.
            subprocess.run(["cmake", "--build", str(build), "--target", "real_app"],
                           check=True, capture_output=True, timeout=30)
            launch_run(spec)


class SharedRunSpecTests(unittest.TestCase):
    def test_explicit_empty_registration_never_discovers_runnable_products(self) -> None:
        for registrations in (None, {}):
            engine = CMakeEngine(FORGE, mock.Mock(), ("library",),
                                 test_targets=lambda names: (), run_targets=registrations)
            with (self.subTest(registrations=registrations),
                  mock.patch("os.walk", side_effect=AssertionError("must not scan"))):
                for action in ("run", "dev"):
                    with self.assertRaisesRegex(ValueError, "registered"):
                        engine.validate_request(request(action), ("library",))
                with self.assertRaisesRegex(ValueError, "registered"):
                    engine.resolve_run(request("run"), "library")

    def test_rust_and_zig_resolve_native_variants_without_build_or_source_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = {
                "plain": RustTarget("package", root / "source", artifact_name="renamed"),
                "example": RustTarget("package", root / "source", example="demo"),
                "web": RustTarget("web", root / "source", "dioxus", ("web",)),
                "server": RustTarget("server", root / "source", "fullstack", ("web",)),
            }
            engine = RustEngine(root, mock.Mock(), targets, root / "target",
                                application_environment=lambda req, name: {"APP_ENV": name})
            zig = ZigEngine(root, {"demo": ZigTarget("native-step", "demo-bin")})
            for profile in ("debug", "release"):
                req = request("run", profile, "--extra", "value with spaces")
                web = engine.artifact(req, "web")
                web.mkdir(parents=True)
                with (mock.patch.object(engine, "environment", return_value={"BASE": "retained"}),
                      mock.patch("os.walk", side_effect=AssertionError("must not scan"))):
                    plain = engine.resolve_run(req, "plain", manager=mock.Mock())
                    example = engine.resolve_run(req, "example", manager=mock.Mock())
                    browser = engine.resolve_run(req, "web", manager=mock.Mock())
                    server = engine.resolve_run(req, "server", manager=mock.Mock())
                    native_zig = zig.resolve_run(req, "demo")
                suffix = ".exe" if sys.platform == "win32" else ""
                self.assertEqual(plain.executable, root / f"target/{profile}/renamed")
                self.assertEqual(plain.arguments, req.arguments)
                self.assertEqual(plain.working_directory, root / f"target/{profile}")
                self.assertEqual(example.executable, root / f"target/{profile}/examples/demo{suffix}")
                self.assertEqual(example.working_directory, root / "source")
                self.assertEqual(browser.executable, Path(sys.executable))
                self.assertEqual(browser.arguments[-2:], req.arguments)
                self.assertEqual(browser.working_directory, web)
                self.assertEqual(server.environment["IP"], "127.0.0.1")
                self.assertEqual(server.environment["PORT"], "8080")
                self.assertEqual(server.environment["APP_ENV"], "server")
                self.assertEqual(server.environment["BASE"], "retained")
                self.assertEqual(plain.environment["APP_ENV"], "plain")
                self.assertEqual(native_zig.executable, root / f"build/zig/{profile}/bin/demo-bin")
                self.assertEqual(native_zig.arguments, req.arguments)
                self.assertEqual(native_zig.working_directory, root)

    def test_common_runner_preserves_values_without_logging_arguments(self) -> None:
        private_argument = "private argument"
        spec = RunSpec(Path(sys.executable),
                       ("-c", "import sys; assert sys.argv[1] == 'private argument'", private_argument),
                       FORGE, dict(os.environ))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            launch_run(spec)
        self.assertNotIn(private_argument, output.getvalue())

    def test_native_target_registration_rejects_paths(self) -> None:
        for name in ("", "..", "a/b", "a\\b", "a\0b", "/tmp/app"):
            with self.assertRaises(ValueError):
                CMakeRunTarget(name)


if __name__ == "__main__":
    unittest.main()
