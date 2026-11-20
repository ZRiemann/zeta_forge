from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from zeta_forge import host_toolchains


class RustPreparationTest(unittest.TestCase):
    def test_install_pins_components_and_targets_without_changing_default(self) -> None:
        spec = {
            "channel": "1.97.1",
            "profile": "minimal",
            "components": ["rustfmt", "clippy"],
        }
        project = SimpleNamespace(
            toolchain_spec=spec,
            toolchain=Path("rust-toolchain.toml"),
            capabilities=("wasm",),
            catalog=SimpleNamespace(
                capabilities={"wasm": {"rust-targets": ["wasm32-unknown-unknown"]}}
            ),
        )
        with (
            mock.patch.object(
                host_toolchains, "load_rust_project", return_value=project
            ),
            mock.patch.object(
                host_toolchains, "read_project_toml", return_value={"toolchain": spec}
            ),
            mock.patch.object(
                host_toolchains, "ensure_rustup", return_value=Path("/fixture/rustup")
            ),
            mock.patch.object(
                host_toolchains, "rust_requirements_ready", side_effect=(False, True)
            ),
            mock.patch.object(host_toolchains.subprocess, "run") as run,
        ):
            host_toolchains.ensure_rust_toolchain(Path("forge"), Path("consumer"))
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "/fixture/rustup",
                "toolchain",
                "install",
                "1.97.1",
                "--profile",
                "minimal",
                "--no-self-update",
                "--no-update",
                "--component",
                "rustfmt",
                "--component",
                "clippy",
                "--target",
                "wasm32-unknown-unknown",
            ],
        )
        self.assertEqual(len(run.call_args_list), 3)
