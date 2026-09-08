from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


FORGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE_ROOT / "common"))

from zeta_forge.rust_workspace import load_rust_project, validate_rust_project  # noqa: E402


class RustWorkspaceTests(unittest.TestCase):
    def create_project(self, root: Path) -> None:
        (root / "rust" / "crate" / "src").mkdir(parents=True)
        (root / "zeta-rust.toml").write_text(
            "schema = 1\n"
            'baseline = "2026.09"\n'
            'manifest = "rust/Cargo.toml"\n'
            'toolchain = "rust/rust-toolchain.toml"\n'
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
            '[workspace.metadata.zeta-forge]\nbaseline = "2026.09"\n'
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
            self.assertEqual(project.baseline.cargo_tools["dioxus-cli"]["version"], "0.7.10")

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

    def test_target_dependency_must_use_workspace_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_project(root)
            manifest = root / "rust" / "crate" / "Cargo.toml"
            with manifest.open("a", encoding="utf-8") as stream:
                stream.write('\n[target."cfg(unix)".dependencies]\ntokio = "1"\n')
            with self.assertRaisesRegex(RuntimeError, "must inherit"):
                validate_rust_project(load_rust_project(root, FORGE_ROOT))


if __name__ == "__main__":
    unittest.main()
