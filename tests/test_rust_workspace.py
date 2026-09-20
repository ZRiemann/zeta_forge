from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


FORGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.rust_workspace import (  # noqa: E402
    RustWorkspaceManager,
    load_baseline,
    load_rust_project,
    validate_rust_project,
)


BASELINE_ID = "1.97.1-r1"


class RustWorkspaceTests(unittest.TestCase):
    def create_baseline(
        self,
        forge_root: Path,
        identifier: str,
        *,
        rust_version: str,
        revision: int,
    ) -> Path:
        path = forge_root / "rust" / "baselines" / f"{identifier}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[baseline]\n"
            "schema = 1\n"
            f'id = "{identifier}"\n'
            f'rust-version = "{rust_version}"\n'
            f"revision = {revision}\n"
            'released = "2026-09-20"\n'
            "[toolchain]\n"
            f'channel = "{rust_version}"\n'
            'profile = "minimal"\n'
            'components = ["rustfmt", "clippy"]\n'
            "[groups]\n"
            "[group-requirements]\n"
            "[cargo-tools]\n"
            "[dependencies]\n",
            encoding="utf-8",
        )
        return path

    def create_project(self, root: Path) -> None:
        (root / "rust" / "crate" / "src").mkdir(parents=True)
        (root / "zeta-rust.toml").write_text(
            "schema = 1\n"
            f'baseline = "{BASELINE_ID}"\n'
            'manifest = "rust/Cargo.toml"\n'
            'toolchain = "rust/rust-toolchain.toml"\n'
            'default-profile = "release"\n'
            'dependency-groups = ["foundation", "async-runtime", "native-nng"]\n'
            'native-dependencies = []\n',
            encoding="utf-8",
        )
        (root / "rust" / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.97.1"\nprofile = "minimal"\n'
            'components = ["rustfmt", "clippy"]\n',
            encoding="utf-8",
        )
        (root / "rust" / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\nresolver = "2"\n'
            f'[workspace.metadata.zeta-forge]\nbaseline = "{BASELINE_ID}"\n'
            '[workspace.dependencies]\n'
            'thiserror = "=2.0.20"\n'
            'tokio = { version = "=1.53.1", features = ["macros", "rt-multi-thread", "sync", "time"] }\n'
            'anng = { git = "https://github.com/nanomsg/nng-rs", '
            'rev = "a474ee0272d18f20c360837e2f602dc53ae3b9ec", '
            'default-features = false, features = ["tokio"] }\n',
            encoding="utf-8",
        )
        (root / "rust" / "crate" / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\nthiserror.workspace = true\n',
            encoding="utf-8",
        )
        (root / "rust" / "crate" / "src" / "lib.rs").write_text("", encoding="utf-8")
        (root / "rust" / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")

    def test_valid_project_uses_forge_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            project = load_rust_project(root, FORGE_ROOT)
            validate_rust_project(project)
            self.assertEqual(project.default_profile, "release")
            self.assertEqual(project.baseline.identifier, BASELINE_ID)
            self.assertEqual(project.baseline.rust_version, "1.97.1")
            self.assertEqual(project.baseline.revision, 1)
            self.assertEqual(project.baseline.cargo_tools["dioxus-cli"]["version"], "0.7.10")
            self.assertEqual(project.baseline.groups["dioxus-web-fullstack"], ("dioxus",))
            self.assertEqual(
                project.baseline.group_requirements["dioxus-web-fullstack"],
                {
                    "cargo-tools": ("dioxus-cli",),
                    "rust-targets": ("wasm32-unknown-unknown",),
                },
            )

    def create_manager(self, root: Path) -> RustWorkspaceManager:
        project = load_rust_project(root, FORGE_ROOT)
        repo_config = mock.Mock()
        repo_config.env = {"TEST_ENV": "1"}
        repo_config.install_prefix = root / "install"
        return RustWorkspaceManager(project, repo_config)

    def test_selects_exact_baseline_from_multiple_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            forge_root = Path(directory)
            self.create_baseline(
                forge_root,
                "1.97.1-r1",
                rust_version="1.97.1",
                revision=1,
            )
            self.create_baseline(
                forge_root,
                "1.98.0-r1",
                rust_version="1.98.0",
                revision=1,
            )

            baseline = load_baseline(forge_root, "1.98.0-r1")

            self.assertEqual(baseline.identifier, "1.98.0-r1")
            self.assertEqual(baseline.rust_version, "1.98.0")

    def test_legacy_or_unknown_baseline_lists_available_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            config = root / "zeta-rust.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(BASELINE_ID, "2026.09"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                r"Unknown Rust baseline '2026\.09'.*available: 1\.97\.1-r1",
            ):
                load_rust_project(root, FORGE_ROOT)

    def test_baseline_metadata_must_match_identifier(self) -> None:
        cases = (
            ('id = "1.97.1-r1"', 'id = "1.97.1-r2"', "id does not match"),
            (
                'rust-version = "1.97.1"',
                'rust-version = "1.98.0"',
                "version does not match",
            ),
            ("revision = 1", "revision = 2", "revision does not match"),
            (
                'channel = "1.97.1"',
                'channel = "1.98.0"',
                "toolchain channel does not match",
            ),
            (
                'released = "2026-09-20"',
                'released = "2026-99-99"',
                "Invalid Rust baseline release date",
            ),
        )
        for original, replacement, expected_error in cases:
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                forge_root = Path(directory)
                path = self.create_baseline(
                    forge_root,
                    BASELINE_ID,
                    rust_version="1.97.1",
                    revision=1,
                )
                path.write_text(
                    path.read_text(encoding="utf-8").replace(original, replacement),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(RuntimeError, expected_error):
                    load_baseline(forge_root, BASELINE_ID)

    def test_project_toolchain_and_workspace_metadata_must_match_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            toolchain = root / "rust" / "rust-toolchain.toml"
            toolchain.write_text(
                toolchain.read_text(encoding="utf-8").replace("1.97.1", "1.98.0"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "toolchain does not match"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

            toolchain.write_text(
                toolchain.read_text(encoding="utf-8").replace("1.98.0", "1.97.1"),
                encoding="utf-8",
            )
            manifest = root / "rust" / "Cargo.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(BASELINE_ID, "1.97.1-r2"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "metadata is missing or stale"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_cargo_tool_uses_baseline_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            executable = manager.cargo_tool_root / "bin" / "dx"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
            completed = subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout="dioxus 0.7.10\n",
            )
            with mock.patch(
                "zeta_forge.rust_workspace.run_command",
                return_value=completed,
            ) as run:
                resolved = manager.require_cargo_tool("dioxus-cli")

            self.assertEqual(resolved, str(executable))
            self.assertEqual(run.call_args.args[0], [executable, "--version"])

    def test_cargo_tool_does_not_fall_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            with (
                mock.patch(
                    "zeta_forge.rust_workspace.shutil.which",
                    return_value="/usr/local/bin/dx",
                ) as which,
                self.assertRaisesRegex(RuntimeError, "missing or not executable") as raised,
            ):
                manager.require_cargo_tool("dioxus-cli")

            which.assert_not_called()
            self.assertIn(f"--root {manager.cargo_tool_root}", str(raised.exception))

    def test_cargo_tool_rejects_non_executable_or_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            executable = manager.cargo_tool_root / "bin" / "dx"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "missing or not executable"):
                manager.require_cargo_tool("dioxus-cli")

            executable.chmod(0o755)
            completed = subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout="dioxus 0.8.0\n",
            )
            with (
                mock.patch(
                    "zeta_forge.rust_workspace.run_command",
                    return_value=completed,
                ),
                self.assertRaisesRegex(RuntimeError, "must be version 0.7.10") as raised,
            ):
                manager.require_cargo_tool("dioxus-cli")

            self.assertIn(f"--root {manager.cargo_tool_root}", str(raised.exception))

    def test_member_cannot_declare_independent_dependency_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manifest = root / "rust" / "crate" / "Cargo.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "thiserror.workspace = true", 'thiserror = "2"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "must inherit"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_project_cannot_select_unknown_dependency_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            config = root / "zeta-rust.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace('"foundation"', '"unknown"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Unknown Rust dependency group"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_project_rejects_unsafe_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            config = root / "zeta-rust.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace("release", "../release"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Unsupported characters"):
                load_rust_project(root, FORGE_ROOT)

    def test_target_dependency_must_use_workspace_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manifest = root / "rust" / "crate" / "Cargo.toml"
            with manifest.open("a", encoding="utf-8") as stream:
                stream.write('\n[target."cfg(unix)".dependencies]\ntokio = "1"\n')
            with self.assertRaisesRegex(RuntimeError, "must inherit"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_build_uses_project_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            completed = subprocess.CompletedProcess(args=(), returncode=0)
            with (
                mock.patch.object(manager, "tool_command", return_value=["cargo"]),
                mock.patch(
                    "zeta_forge.rust_workspace.run_command",
                    return_value=completed,
                ) as run,
            ):
                status = manager.build()

            self.assertEqual(status, 0)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "cargo",
                    "build",
                    "--workspace",
                    "--locked",
                    "--profile",
                    "release",
                    "--manifest-path",
                    manager.project.manifest,
                ],
            )

    def test_rebuild_cleans_only_workspace_profile_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            completed = subprocess.CompletedProcess(args=(), returncode=0)
            with (
                mock.patch.object(manager, "tool_command", return_value=["cargo"]),
                mock.patch(
                    "zeta_forge.rust_workspace.run_command",
                    return_value=completed,
                ) as run,
            ):
                status = manager.rebuild("dev")

            self.assertEqual(status, 0)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                commands[0],
                [
                    "cargo",
                    "clean",
                    "--workspace",
                    "--profile",
                    "dev",
                    "--manifest-path",
                    manager.project.manifest,
                ],
            )
            self.assertEqual(
                commands[1][1:7],
                [
                    "build",
                    "--workspace",
                    "--locked",
                    "--profile",
                    "dev",
                    "--manifest-path",
                ],
            )

    def test_check_test_and_run_forward_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.create_manager(root)
            completed = subprocess.CompletedProcess(args=(), returncode=0)
            with (
                mock.patch.object(manager, "tool_command", return_value=["cargo"]),
                mock.patch(
                    "zeta_forge.rust_workspace.run_command",
                    return_value=completed,
                ) as run,
            ):
                self.assertEqual(manager.check("dev"), 0)
                self.assertEqual(manager.test("dev"), 0)
                self.assertEqual(manager.run("sample", profile="dev"), 0)

            commands = [call.args[0] for call in run.call_args_list]
            profiled_commands = [command for command in commands if command[1] != "fmt"]
            self.assertEqual(len(profiled_commands), 4)
            for command in profiled_commands:
                profile_index = command.index("--profile")
                self.assertEqual(command[profile_index + 1], "dev")


if __name__ == "__main__":
    unittest.main()
