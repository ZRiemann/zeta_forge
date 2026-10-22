"""Registered sibling dependencies retain source and dependency identity checks."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import test_rust_workspace as fixtures

from zeta_forge.rust_workspace import RustProject, load_rust_project, validate_rust_project, locked_dependency_report

FORGE_ROOT = fixtures.FORGE_ROOT


class LocalDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.forge, self.consumer, self.library = base / "forge", base / "consumer", base / "library"
        (self.forge / "rust/toolchains").mkdir(parents=True)
        shutil.copy(FORGE_ROOT / "rust/catalog.toml", self.forge / "rust/catalog.toml")
        shutil.copy(FORGE_ROOT / "rust/toolchains/1.97.1.toml", self.forge / "rust/toolchains/1.97.1.toml")
        self.registry = self.forge / "rust/projects.toml"
        self.registry.write_text('schema = 1\n[[projects]]\npath = "consumer"\nlocal-dependencies = ["library"]\n[[projects]]\npath = "library"\n')
        for root in (self.consumer, self.library):
            fixtures.RustWorkspaceTests().create_project(root)
        manifest = self.library / "rust/crate/Cargo.toml"
        manifest.write_text(manifest.read_text().replace('name = "sample"', 'name = "helper"'))
        self.manifest = self.consumer / "rust/Cargo.toml"
        self.manifest.write_text(self.manifest.read_text() + 'helper = { path = "../../library/rust/crate", version = "=0.1.0" }\n')
        member = self.consumer / "rust/crate/Cargo.toml"
        member.write_text(member.read_text() + 'helper.workspace = true\n')
        lock = self.consumer / "rust/Cargo.lock"
        lock.write_text(lock.read_text().replace('dependencies = ["tokio"]', 'dependencies = ["tokio", "helper"]') + '\n[[package]]\nname = "helper"\nversion = "0.1.0"\ndependencies = ["tokio"]\n')

    def project(self) -> RustProject:
        return load_rust_project(self.consumer, self.forge)

    def test_authorized_sibling_and_its_direct_edges_are_audited(self) -> None:
        project = self.project()
        validate_rust_project(project)
        edges = locked_dependency_report(project)["direct_edges"]
        self.assertTrue(any(edge["consumer"]["name"] == "helper" and edge["dependency"]["name"] == "tokio" for edge in edges))
        manifest = self.library / "rust/Cargo.toml"
        manifest.write_text(manifest.read_text().replace('=1.54.0', '=1.53.1'))
        with self.assertRaisesRegex(RuntimeError, "not approved"):
            validate_rust_project(project)

    def test_unregistered_package_and_version_are_rejected(self) -> None:
        original = self.manifest.read_text()
        for old, new, message in [('=0.1.0', '=0.2.0', 'exact package version'), ('helper =', 'wrong =', 'wrong package name')]:
            with self.subTest(new=new):
                self.manifest.write_text(original.replace(old, new))
                with self.assertRaisesRegex(RuntimeError, message):
                    validate_rust_project(self.project())
        self.manifest.write_text(original)
        self.registry.write_text(self.registry.read_text().replace('local-dependencies = ["library"]', 'local-dependencies = []'))
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            validate_rust_project(self.project())

    def test_aliases_work_but_missing_sibling_lock_identity_fails(self) -> None:
        self.manifest.write_text(self.manifest.read_text().replace('helper = {', 'renamed = { package = "helper",'))
        member = self.consumer / "rust/crate/Cargo.toml"
        member.write_text(member.read_text().replace('helper.workspace', 'renamed.workspace'))
        validate_rust_project(self.project())
        lock = self.consumer / "rust/Cargo.lock"
        lock.write_text(lock.read_text().replace('name = "helper"\nversion = "0.1.0"', 'name = "helper"\nversion = "0.2.0"'))
        with self.assertRaisesRegex(RuntimeError, "lockfile has 0 consumer identities"):
            validate_rust_project(self.project())

    def test_symlink_and_third_party_escapes_are_rejected(self) -> None:
        outside = Path(self.directory.name) / 'outside'
        outside.mkdir()
        link = self.library / 'rust/link'
        link.symlink_to(outside, target_is_directory=True)
        original = self.manifest.read_text()
        for target in ['../../library/rust/link', '../../library/vendor/helper']:
            with self.subTest(target=target):
                self.manifest.write_text(original.replace('../../library/rust/crate', target))
                with self.assertRaisesRegex(RuntimeError, 'escapes'):
                    validate_rust_project(self.project())


if __name__ == '__main__':
    unittest.main()
