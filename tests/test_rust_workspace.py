from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FORGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.rust_workspace import (  # noqa: E402
    RustWorkspaceManager,
    load_catalog,
    load_rust_project,
    load_toolchain,
    validate_rust_project,
)


class RustWorkspaceTests(unittest.TestCase):
    def create_project(self, root: Path) -> None:
        crate = root / "rust" / "crate"
        (crate / "src").mkdir(parents=True)
        (root / "zeta-rust.toml").write_text(
            'schema = 2\nmanifest = "rust/Cargo.toml"\n'
            'toolchain = "rust/rust-toolchain.toml"\n'
            'capabilities = ["dioxus-web-fullstack"]\n'
            'native-dependencies = []\n', encoding="utf-8"
        )
        (root / "rust" / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.97.1"\nprofile = "minimal"\n'
            'components = ["rustfmt", "clippy"]\n', encoding="utf-8"
        )
        (root / "rust" / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\nresolver = "2"\n'
            '[workspace.dependencies]\n'
            'tokio = { version = "=1.53.1", features = ["macros"] }\n'
            'dioxus = { version = "=0.7.10", default-features = false, features = ["lib"] }\n'
            'anng = { git = "https://github.com/nanomsg/nng-rs", '
            'rev = "a474ee0272d18f20c360837e2f602dc53ae3b9ec", '
            'default-features = false, features = ["tokio"] }\n'
            'rusqlite = { version = "=0.40.2", features = ["bundled"] }\n',
            encoding="utf-8"
        )
        (crate / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\ntokio.workspace = true\n', encoding="utf-8"
        )
        (crate / "src" / "lib.rs").write_text("", encoding="utf-8")
        (root / "rust" / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")

    def manager(self, root: Path) -> RustWorkspaceManager:
        config = mock.Mock()
        config.env = {}
        config.install_prefix = root / "install"
        return RustWorkspaceManager(load_rust_project(root, FORGE_ROOT), config)

    def replace(self, root: Path, old: str, new: str) -> None:
        manifest = root / "rust" / "Cargo.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def test_parallel_approved_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            validate_rust_project(load_rust_project(root, FORGE_ROOT))
            self.assertEqual(len(load_catalog(FORGE_ROOT).dependencies["tokio"]), 2)
            self.replace(root, "=1.53.1", "=1.52.1")
            validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_unapproved_version_git_source_and_revision(self) -> None:
        cases = (
            ("=1.53.1", "=1.54.0"),
            ("a474ee0272d18f20c360837e2f602dc53ae3b9ec", "0" * 40),
            ("https://github.com/nanomsg/nng-rs", "https://example.com/other"),
        )
        for old, new in cases:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.create_project(root)
                self.replace(root, old, new)
                with self.assertRaisesRegex(RuntimeError, "not approved"):
                    validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_required_features_and_native_source_constraint(self) -> None:
        cases = (
            ('features = ["bundled"]', 'features = []'),
            ('features = ["tokio"]', 'features = []'),
            ('default-features = false, features = ["tokio"]', 'features = ["tokio"]'),
        )
        for old, new in cases:
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.create_project(root)
                self.replace(root, old, new)
                with self.assertRaisesRegex(RuntimeError, "not approved"):
                    validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_member_inheritance_and_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            member = root / "rust" / "crate" / "Cargo.toml"
            member.write_text(member.read_text(encoding="utf-8").replace(
                "tokio.workspace = true", 'tokio = "=1.53.1"'
            ), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must inherit"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))
            toolchain = root / "rust" / "rust-toolchain.toml"
            toolchain.write_text(toolchain.read_text(encoding="utf-8").replace("1.97.1", "1.98.0"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not approved"):
                load_rust_project(root, FORGE_ROOT)

    def test_member_unknown_workspace_dependency_and_local_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            member = root / "rust" / "crate" / "Cargo.toml"
            member.write_text(member.read_text(encoding="utf-8").replace(
                "tokio.workspace = true", "unknown.workspace = true"
            ), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must inherit"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))
            member.write_text(member.read_text(encoding="utf-8").replace(
                "unknown.workspace = true", 'local = { path = "../../../../outside" }'
            ), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "escapes project root"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_catalog_duplicate_and_invalid_git_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rust").mkdir()
            source = (FORGE_ROOT / "rust" / "catalog.toml").read_text(encoding="utf-8")
            catalog = root / "rust" / "catalog.toml"
            catalog.write_text(source + '\n[[dependencies]]\nname = "tokio"\nversion = "=1.53.1"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Duplicate"):
                load_catalog(root)
            catalog.write_text(source.replace("a474ee0272d18f20c360837e2f602dc53ae3b9ec", "short"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "full revision"):
                load_catalog(root)

    def test_toolchain_filename_and_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "rust" / "toolchains"
            folder.mkdir(parents=True)
            (folder / "1.97.1.toml").write_text('[toolchain]\nchannel = "1.98.0"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match filename"):
                load_toolchain(root, "1.97.1")
            self.create_project(root)
            config = root / "zeta-rust.toml"
            config.write_text(config.read_text(encoding="utf-8").replace("dioxus-web-fullstack", "unknown"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Unknown Rust capability"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_old_schema_and_workspace_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            config = root / "zeta-rust.toml"
            config.write_text(config.read_text(encoding="utf-8").replace("schema = 2", "schema = 1"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Unsupported Rust project schema"):
                load_rust_project(root, FORGE_ROOT)
            config.write_text(config.read_text(encoding="utf-8").replace("schema = 1", "schema = 2"), encoding="utf-8")
            manifest = root / "rust" / "Cargo.toml"
            manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[workspace.metadata.zeta-forge]\nbaseline = "old"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Obsolete"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))

    def test_cargo_tool_path_and_no_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.manager(root)
            tool_root = manager.cargo_tool_root("dioxus-cli", "0.7.10")
            self.assertIn("rust-tools/1.97.1/dioxus-cli/0.7.10", str(tool_root))
            with mock.patch("zeta_forge.rust_workspace.shutil.which") as which:
                with self.assertRaisesRegex(RuntimeError, "missing or not executable"):
                    manager.require_cargo_tool("dioxus-cli")
                which.assert_not_called()
            executable = tool_root / "bin" / "dx"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
            completed = subprocess.CompletedProcess(args=(), returncode=0, stdout="dioxus 0.7.10\n")
            with mock.patch("zeta_forge.rust_workspace.run_command", return_value=completed):
                self.assertEqual(manager.require_cargo_tool("dioxus-cli"), str(executable))
            wrong = subprocess.CompletedProcess(args=(), returncode=0, stdout="dioxus 0.8.0\n")
            with mock.patch("zeta_forge.rust_workspace.run_command", return_value=wrong):
                with self.assertRaisesRegex(RuntimeError, "must be version 0.7.10"):
                    manager.require_cargo_tool("dioxus-cli")

    def test_cargo_tool_install_is_idempotent_and_repairs_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.manager(root)
            executable = manager.cargo_tool_root("dioxus-cli", "0.7.10") / "bin" / "dx"
            version = "0.7.10"
            installs: list[list[str]] = []

            def run(args, **_kwargs):
                nonlocal version
                if "install" in args:
                    installs.append([str(arg) for arg in args])
                    executable.parent.mkdir(parents=True, exist_ok=True)
                    executable.write_text("", encoding="utf-8")
                    executable.chmod(0o755)
                    version = "0.7.10"
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=f"dioxus {version}\n"
                )

            with (
                mock.patch("zeta_forge.rust_workspace.shutil.which", return_value="/cargo"),
                mock.patch("zeta_forge.rust_workspace.run_command", side_effect=run),
            ):
                self.assertEqual(manager.ensure_cargo_tool("dioxus-cli"), str(executable))
                self.assertEqual(manager.ensure_cargo_tool("dioxus-cli"), str(executable))
                self.assertEqual(len(installs), 1)
                self.assertIn("--locked", installs[0])
                self.assertIn("--force", installs[0])
                self.assertEqual(installs[0][-1], str(executable.parent.parent))
                version = "0.8.0"
                self.assertEqual(manager.ensure_cargo_tool("dioxus-cli"), str(executable))
                self.assertEqual(len(installs), 2)

    def test_cargo_tool_install_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.manager(root)
            with (
                mock.patch("zeta_forge.rust_workspace.shutil.which", return_value="/cargo"),
                mock.patch("zeta_forge.rust_workspace.run_command", side_effect=RuntimeError("install failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "install failed"):
                    manager.ensure_cargo_tool("dioxus-cli")

    def test_web_target_and_desktop_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manager = self.manager(root)
            completed = subprocess.CompletedProcess(args=(), returncode=0, stdout="rustc 1.97.1\n")
            with mock.patch("zeta_forge.rust_workspace.run_command", return_value=completed):
                with mock.patch.object(manager, "require_cargo_tool"), mock.patch.object(manager, "require_rust_target") as target:
                    manager.doctor()
                    target.assert_called_once_with("wasm32-unknown-unknown")
                    target.reset_mock()
                    manager.doctor(application_tools=False)
                    target.assert_not_called()
                config = root / "zeta-rust.toml"
                config.write_text(config.read_text(encoding="utf-8").replace("dioxus-web-fullstack", "dioxus-desktop"), encoding="utf-8")
                desktop = self.manager(root)
                with mock.patch.object(desktop, "require_cargo_tool"), mock.patch.object(desktop, "require_rust_target") as target:
                    desktop.doctor()
                    target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
