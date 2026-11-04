"""Workspace integration of explicit native products; no application services."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.build_cli import Request, cli

CONSUMERS = ("zpp", "zgent", "zeta_agent", "zeta_vault")
SOURCES = {name: FORGE.parent / name / "builder" / f"{name}.py" for name in CONSUMERS}
ZPP_EXAMPLES = (
    "zpp_core", "folly_examples", "inspector_cli", "inspector_svr", "folly_server",
    "example_hpx_basic", "zpp_hpx_exec_pipeline", "zpp_hpx_exec_scheduler",
    "example_hpx_taskflow", "zpp_hpx_basic", "publisher_example", "subscriber_example",
    "pub_sub_device_example", "engine", "pusher_example", "puller_example",
    "requester_example", "replier_example", "pair1_listen_example", "pair1_dial_example",
    "zpp_taskflow",
)
RUNNABLE = {
    "zpp": ZPP_EXAMPLES,
    "zgent": ("zgent",),
    "zeta_agent": ("zeta_agent_cli",),
    "zeta_vault": ("zeta_vault_server", "zeta_vault_ctl", "zeta_vault_c_example"),
}


def request(action: str, profile: str, *arguments: str) -> Request:
    return Request(action, profile, 2, argparse.Namespace(), arguments)


@unittest.skipUnless(all(path.is_file() for path in SOURCES.values()),
                     "explicit CMake consumer sibling checkouts unavailable")
class CMakeConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="registered consumers ")
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.projects = {}
        self.modules = {}
        for name, path in SOURCES.items():
            module_name = f"_run_contract_{name}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            self.addCleanup(sys.modules.pop, module_name, None)
            spec.loader.exec_module(module)
            root = self.workspace / name
            root.mkdir()
            environment = {**os.environ, "ZETAX_ROOT": str(self.workspace),
                           "RUN_FIXTURE": "preserved"}
            config = SimpleNamespace(
                env=environment, forge_root=FORGE, install_prefix=self.workspace / "install",
                cxx_standard="20", source_dir=lambda key, source=root: source,
            )
            with mock.patch.object(module, "load_repo_config", return_value=config):
                self.projects[name] = module.project(root / "zbuild.py")
            self.modules[name] = module

    def test_public_products_preserve_names_and_never_walk_sources(self) -> None:
        for name, project in self.projects.items():
            with self.subTest(consumer=name):
                engine = project.engines["cmake"]
                self.assertEqual(tuple(engine.run_targets), RUNNABLE[name])
                runnable = tuple(product.name for product in project.products
                                 if "run" in product.actions)
                self.assertEqual(runnable, RUNNABLE[name])
                output = io.StringIO()
                with (mock.patch("os.walk", side_effect=AssertionError("must not walk")),
                      mock.patch("pathlib.Path.rglob", side_effect=AssertionError("must not glob")),
                      mock.patch("subprocess.Popen", side_effect=AssertionError("must not launch")),
                      contextlib.redirect_stdout(output)):
                    self.assertEqual(cli(project, ["list"]), 0)
                    for profile in ("debug", "release"):
                        self.assertEqual(cli(project, ["run", runnable[0], "--profile", profile,
                                                       "--dry-run", "--", "private argument"]), 0)
                self.assertNotIn("private argument", output.getvalue())
                self.assertFalse((self.workspace / "logs").exists())
                self.assertFalse((self.workspace / name / "build").exists())
                for product in project.products:
                    if product.name not in engine.run_targets:
                        for action in ("run", "dev"):
                            with self.assertRaisesRegex(ValueError, "registered"):
                                engine.validate_request(request(action, "release"), (product.name,))

    def test_zpp_native_module_options_remain_selected_by_product(self) -> None:
        engine = self.projects["zpp"].engines["cmake"]
        cases = {
            "zpp_core": (True, False, False, False, False, False),
            "folly_examples": (True, False, False, True, False, False),
            "publisher_example": (True, False, False, False, True, False),
            "example_hpx_taskflow": (True, True, True, False, False, True),
            "zpp_taskflow": (True, False, False, False, False, True),
            "zpp_hpx": (False, False, True, False, False, False),
            "zpp": (False, False, False, False, False, False),
        }
        for profile in ("debug", "release"):
            for name, expected in cases.items():
                with self.subTest(profile=profile, product=name):
                    args = engine.factory(request("build", profile), (name,)).typed_args
                    self.assertEqual((args.build_examples, args.build_hpx_examples,
                                      args.build_hpx_module, args.build_folly_module,
                                      args.build_nng_module, args.build_taskflow_module), expected)
                    self.assertEqual(args.build_type, profile.title())

    def configure_fixture(self, name: str, profile: str) -> None:
        root = self.workspace / name
        runtime = root / "runtime"
        runtime.mkdir(exist_ok=True)
        (root / "record.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path('observed.json').write_text(json.dumps({"
            "'argv':sys.argv[1:], 'cwd':os.getcwd(), 'env':os.getenv('RUN_FIXTURE')}))\n",
            encoding="utf-8",
        )
        helper = FORGE / "cmake_util/add_run_target.cmake"
        (root / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.20)\nproject(run_fixture NONE)\n'
            f'include("{helper.as_posix()}")\nadd_subdirectory(runtime)\n', encoding="utf-8",
        )
        declarations = []
        for target in RUNNABLE[name]:
            declarations.append(
                f'add_executable({target} IMPORTED GLOBAL)\n'
                f'set_target_properties({target} PROPERTIES IMPORTED_LOCATION "${{PYTHON}}")\n'
                f'add_run_target({target} ARGS "${{PROJECT_SOURCE_DIR}}/record.py" '
                f'"{target}" "default with spaces" "$<CONFIG>")\n'
            )
        (runtime / "CMakeLists.txt").write_text("".join(declarations), encoding="utf-8")
        subprocess.run(
            ["cmake", "-S", str(root), "-B", str(root / "build" / profile.title()),
             f"-DCMAKE_BUILD_TYPE={profile.title()}", f"-DPYTHON={sys.executable}"],
            check=True, capture_output=True, text=True, timeout=30,
        )

    @unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
    def test_private_native_metadata_reaches_every_consumer_in_both_profiles(self) -> None:
        for name, project in self.projects.items():
            engine = project.engines["cmake"]
            for profile in ("debug", "release"):
                self.configure_fixture(name, profile)
                req = request("run", profile, "--operator", "private words")
                builder = engine.factory(req, (RUNNABLE[name][0],))
                with (mock.patch.object(type(builder), "build",
                                        side_effect=AssertionError("run must not build")),
                      mock.patch("os.walk", side_effect=AssertionError("must not walk")),
                      mock.patch("pathlib.Path.rglob", side_effect=AssertionError("must not glob")),
                      mock.patch.dict(os.environ, {"RUN_FIXTURE": "preserved"}),
                      contextlib.redirect_stdout(io.StringIO())):
                    for target in RUNNABLE[name]:
                        with self.subTest(consumer=name, profile=profile, target=target):
                            engine.preflight(req, (target,))
                            engine.execute(req, (target,))
                            directory = builder.build_dir / "runtime"
                            observed = json.loads((directory / "observed.json").read_text())
                            self.assertEqual(observed["argv"], [target, "default with spaces",
                                                              profile.title(), *req.arguments])
                            self.assertEqual(observed["cwd"], str(directory))
                            self.assertEqual(observed["env"], "preserved")
            if name == "zgent":
                self.assertTrue((self.workspace / "logs/zgent").is_dir())

    @unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
    def test_zgent_uses_shared_dev_order_and_interrupt_status(self) -> None:
        project = self.projects["zgent"]
        engine = project.engines["cmake"]
        for profile in ("debug", "release"):
            self.configure_fixture("zgent", profile)
            builder = engine.factory(request("dev", profile), ("zgent",))
            order = []
            with (mock.patch.object(type(builder), "build",
                                    side_effect=lambda *args, **kwargs: order.append("build")),
                  mock.patch("zeta_forge.run_spec.run_program",
                             side_effect=lambda *args, **kwargs: order.append("run")) as run):
                engine.execute(request("dev", profile, "forwarded"), ("zgent",))
            self.assertEqual(order, ["build", "run"])
            self.assertEqual(run.call_args.args[0][-1], "forwarded")
            self.assertEqual(run.call_args.kwargs["env"]["ZETAX_ROOT"], str(self.workspace))
            with (mock.patch("zeta_forge.run_spec.run_program", side_effect=KeyboardInterrupt),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(cli(project, ["run", "--profile", profile]), 130)


if __name__ == "__main__":
    unittest.main()
