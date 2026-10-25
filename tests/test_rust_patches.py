from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from zeta_forge.rust_patches import (  # noqa: E402
    PATCH_NAME, materialize, patch_environment, patch_recipe, prepare_patches,
    tree_digest, verify_resolved_patches, verify_source,
)


class RustSourcePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = SimpleNamespace(forge_root=self.root / "forge", env={"CARGO_HOME": str(self.root / "cargo")})
        self.project = SimpleNamespace(source_patches=(PATCH_NAME,))
        self.payload = self.archive({"Cargo.toml": b'[package]\nname="wry"\nversion="0.53.5"\n',
                                     "src/lib.rs": b"strong-reference"})
        expected = self.root / "expected"
        (expected / "src").mkdir(parents=True)
        (expected / "Cargo.toml").write_bytes(b'[package]\nname="wry"\nversion="0.53.5"\n')
        (expected / "src/lib.rs").write_bytes(b"weak-reference")
        self.recipe = {"schema": 1, "package": "wry", "version": "0.53.5",
                       "archive_sha256": hashlib.sha256(self.payload).hexdigest(),
                       "tree_sha256": tree_digest(expected), "file": "src/lib.rs",
                       "replacements": [{"before": "strong-reference", "after": "weak-reference"}]}
        recipe_path = self.config.forge_root / "rust/patches" / f"{PATCH_NAME}.json"
        recipe_path.parent.mkdir(parents=True)
        recipe_path.write_text(json.dumps(self.recipe))
        self.recipe, self.source = patch_recipe(self.config.forge_root, self.project.source_patches)

    def archive(self, files: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo("wry-0.53.5/" + name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    def prepare_cached(self) -> None:
        cache = self.root / "cargo/registry/cache/test/wry-0.53.5.crate"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(self.payload)
        prepare_patches(self.project, self.config)

    def test_only_explicit_supported_repair_is_accepted(self) -> None:
        for names in (("other",), (PATCH_NAME, PATCH_NAME)):
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                patch_recipe(self.config.forge_root, names)
        self.project.source_patches = ()
        self.assertEqual(patch_environment(self.project, self.config), {})

    def test_missing_source_is_not_acquired_during_read_only_checks(self) -> None:
        with mock.patch("zeta_forge.rust_patches.urlopen") as download:
            with self.assertRaisesRegex(RuntimeError, "fetch"):
                patch_environment(self.project, self.config)
            download.assert_not_called()
        self.assertFalse(self.source.exists())

    def test_verified_cached_archive_is_materialized_without_cache_edits(self) -> None:
        with mock.patch("zeta_forge.rust_patches.urlopen") as download:
            self.prepare_cached()
            download.assert_not_called()
        self.assertEqual((self.source / "src/lib.rs").read_text(), "weak-reference")
        self.assertEqual((self.root / "cargo/registry/cache/test/wry-0.53.5.crate").read_bytes(), self.payload)
        self.assertEqual(patch_environment(self.project, self.config), patch_environment(self.project, self.config))

    def test_tampering_or_extra_source_is_rejected_and_preserved(self) -> None:
        self.prepare_cached()
        extra = self.source / "injected.rs"
        extra.write_text("modified")
        for operation in (lambda: patch_environment(self.project, self.config),
                          lambda: prepare_patches(self.project, self.config)):
            with self.assertRaisesRegex(RuntimeError, "modified"):
                operation()
        self.assertEqual(extra.read_text(), "modified")

    def test_wrong_archive_and_path_traversal_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            materialize(self.recipe, self.source, b"invalid")
        unsafe = self.archive({"../escape": b"bad"})
        recipe = {**self.recipe, "archive_sha256": hashlib.sha256(unsafe).hexdigest()}
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            materialize(recipe, self.source, unsafe)
        self.assertFalse(self.source.exists())

    def test_symlink_and_changed_patch_input_are_rejected(self) -> None:
        recipe = {**self.recipe, "replacements": [{"before": "absent", "after": "bad"}]}
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            materialize(recipe, self.source, self.payload)
        self.prepare_cached()
        (self.source / "link").symlink_to(self.root / "expected")
        with self.assertRaisesRegex(RuntimeError, "Symlink"):
            verify_source(self.recipe, self.source)

    def test_cargo_home_preserves_user_config_and_cache_lock_identity(self) -> None:
        base = self.root / "cargo"
        base.mkdir()
        settings = base / "config.toml"
        settings.write_text('[net]\noffline = true\n')
        self.prepare_cached()
        environment = patch_environment(self.project, self.config)
        home = Path(environment["CARGO_HOME"])
        self.assertIn(str(settings), (home / "config.toml").read_text())
        self.assertEqual((home / ".package-cache").resolve(), base / ".package-cache")
        self.assertEqual(settings.read_text(), '[net]\noffline = true\n')

    def test_unknown_overrides_and_modified_managed_config_fail_closed(self) -> None:
        self.prepare_cached()
        home = Path(patch_environment(self.project, self.config)["CARGO_HOME"])
        (home / "config.toml").write_text("[patch.crates-io]\n")
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            patch_environment(self.project, self.config)
        (self.root / "cargo/config.toml").write_text('paths = ["/other"]\n')
        with self.assertRaisesRegex(RuntimeError, "overrides"):
            patch_environment(self.project, self.config)

    def test_resolved_graph_must_use_the_exact_managed_manifest(self) -> None:
        self.prepare_cached()
        package = {"name": "wry", "version": "0.53.5", "manifest_path": str(self.source / "Cargo.toml")}
        verify_resolved_patches({"packages": [package]}, self.project, self.config)
        for packages in ([], [package, package], [{**package, "manifest_path": "/other/Cargo.toml"}],
                         [{**package, "version": "0.53.6"}]):
            with self.assertRaisesRegex(RuntimeError, "bypasses"):
                verify_resolved_patches({"packages": packages}, self.project, self.config)


if __name__ == "__main__":
    unittest.main()
