"""Ownership and package-ID boundaries for offline and resolved audits."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))
import test_rust_workspace as fixtures
from zeta_forge.rust_dependencies import CRATES_IO_SOURCE, resolved_dependency_report
from zeta_forge.rust_workspace import load_catalog, load_rust_project, locked_dependency_report, validate_rust_project


class EdgeTests(unittest.TestCase):
    def metadata(self, root):
        packages = [
            dict(id="app", name="app", version="0.1.0", source=None, manifest_path=str(root / "Cargo.toml")),
            dict(id="good", name="tokio", version="1.54.0", source=CRATES_IO_SOURCE),
            dict(id="other", name="tokio", version="1.54.0", source="git+https://example.com/tokio#" + "a" * 40),
            dict(id="upstream", name="upstream", version="1.0.0", source=CRATES_IO_SOURCE),
        ]
        return dict(workspace_members=["app"], packages=packages, resolve={"nodes": [
            dict(id="app", deps=[dict(name="runtime", pkg="good", dep_kinds=[
                {"kind": None, "target": 'cfg(unix)'}, {"kind": "build", "target": None},
                {"kind": "dev", "target": None}])]),
            dict(id="good", deps=[dict(pkg="upstream")]), dict(id="upstream", deps=[dict(pkg="other")]),
            dict(id="other", deps=[]),
        ]})

    def test_only_actual_direct_identity_is_governed(self):
        root = Path("/workspace/app")
        metadata = self.metadata(root)
        report = resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(set(report), {"schema_version", "direct_edges", "transitive_versions", "coexisting", "errors"})
        self.assertFalse(report["errors"])
        self.assertEqual(report["direct_edges"][0]["alias"], "runtime")
        self.assertEqual(len(report["direct_edges"][0]["dep_kinds"]), 3)
        self.assertIn("other", {p["id"] for p in report["transitive_versions"]})
        metadata["resolve"]["nodes"][0]["deps"][0]["pkg"] = "other"
        self.assertTrue(resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])["errors"])

    def test_other_versions_and_unactivated_optional_edges(self):
        root = Path("/workspace/app")
        metadata = self.metadata(root)
        metadata["packages"][2]["version"] = "0.2.0"
        self.assertFalse(resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])["errors"])
        metadata["resolve"]["nodes"][0]["deps"] = []
        metadata["packages"][0]["dependencies"] = [{"name": "tokio", "optional": True}]
        self.assertFalse(resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])["direct_edges"])

    def test_downloaded_and_third_party_paths_are_not_own_consumers(self):
        root = Path("/workspace/app")
        for source, path in ((None, root / "vendor/lib/Cargo.toml"),
                             (CRATES_IO_SOURCE, root / "download/Cargo.toml"),
                             (None, root.parent / "elsewhere/Cargo.toml")):
            with self.subTest(source=source, path=path):
                metadata = self.metadata(root)
                metadata["packages"][3].update(source=source, manifest_path=str(path))
                report = resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])
                self.assertFalse(report["errors"])
                self.assertEqual(len(report["direct_edges"]), 1)

    def test_own_path_crate_edges_are_checked(self):
        root = Path("/workspace/app")
        metadata = self.metadata(root)
        metadata["packages"][3].update(source=None, manifest_path=str(root / "lib/Cargo.toml"))
        report = resolved_dependency_report(metadata, load_catalog(FORGE).dependencies, [root])
        self.assertTrue(any("upstream -> tokio" in e for e in report["errors"]))

    def test_lock_reference_disambiguation_and_wrong_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures.RustWorkspaceTests().create_project(root)
            project = load_rust_project(root, FORGE)
            lock = root / "rust/Cargo.lock"
            original = lock.read_text()
            duplicate = '\n[[package]]\nname = "tokio"\nversion = "1.54.0"\nsource = "registry+https://example.com/index"\n'
            lock.write_text(original + duplicate)
            self.assertIn("resolves to 2 identities", locked_dependency_report(project)["errors"][0])
            for source, good in ((CRATES_IO_SOURCE, True), ("registry+https://example.com/index", False)):
                lock.write_text(original.replace('dependencies = ["tokio"]',
                                                f'dependencies = ["tokio 1.54.0 ({source})"]') + duplicate)
                report = locked_dependency_report(project)
                self.assertEqual(not report["errors"], good)

    def test_inactive_optional_declaration_is_still_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures.RustWorkspaceTests().create_project(root)
            member = root / "rust/crate/Cargo.toml"
            member.write_text(member.read_text().replace('tokio.workspace = true', 'tokio = { workspace = true, optional = true }'))
            lock = root / "rust/Cargo.lock"
            lock.write_text(lock.read_text().replace('dependencies = ["tokio"]', 'dependencies = []'))
            validate_rust_project(load_rust_project(root, FORGE))
            manifest = root / "rust/Cargo.toml"
            manifest.write_text(manifest.read_text().replace('=1.54.0', '=1.53.1'))
            with self.assertRaisesRegex(RuntimeError, "not approved"):
                validate_rust_project(load_rust_project(root, FORGE))

    def test_recursive_path_crate_cannot_hide_bad_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures.RustWorkspaceTests().create_project(root)
            helper = root / "rust/helper"
            helper.mkdir()
            (helper / "Cargo.toml").write_text('[package]\nname = "helper"\nversion = "0.1.0"\n[dependencies]\ntokio = "1"\n')
            member = root / "rust/crate/Cargo.toml"
            member.write_text(member.read_text() + 'helper = { path = "../helper" }\n')
            with self.assertRaisesRegex(RuntimeError, "helper/Cargo.toml.*must inherit"):
                validate_rust_project(load_rust_project(root, FORGE))


if __name__ == "__main__":
    unittest.main()
