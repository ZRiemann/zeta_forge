"""Dependency identity, audit coverage and fail-before-side-effects contracts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

import test_rust_workspace as fixtures
from zeta_forge.build_cli import Product, Project, Request, cli
from zeta_forge.config import load_repo_config
from zeta_forge.rust_audit import RustDependencyAudit, registered_projects, resolved_project
from zeta_forge.rust_dependencies import CRATES_IO_SOURCE, compatibility_series, dependency_report, package_identity, resolved_dependency_report
from zeta_forge.rust_engine import RustEngine, RustTarget
from zeta_forge.rust_workspace import load_catalog, load_rust_project, validate_rust_project


def request(action: str) -> Request:
    return Request(action, "release", 1, argparse.Namespace())


def create_project(root: Path) -> None:
    fixtures.RustWorkspaceTests().create_project(root)


def edge(name, version, source):
    return {"consumer": {"id": "sample", "name": "sample"}, "manifest": "Cargo.toml",
            "dependency": package_identity(dict(name=name, version=version, source=source))}


class GovernanceTests(unittest.TestCase):
    def test_caret_series_and_build_metadata(self) -> None:
        self.assertEqual(compatibility_series("1.54.0"), compatibility_series("1.2.3"))
        self.assertNotEqual(compatibility_series("0.2.1"), compatibility_series("0.3.0"))
        self.assertNotEqual(compatibility_series("0.0.1"), compatibility_series("0.0.2"))
        self.assertEqual(compatibility_series("1.0.0+build.3"), (1,))
        for invalid in ("invalid", "1.2", "-1.2.3"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                compatibility_series(invalid)

    def test_lock_version_source_and_git_revision_drift(self) -> None:
        catalog = load_catalog(FORGE).dependencies
        rev = catalog["anng"]["rev"]
        good_git = f"git+{catalog['anng']['git']}?rev={rev}#{rev}"
        cases = (
            ("tokio", "1.53.1", CRATES_IO_SOURCE),
            ("tokio", "1.54.0", "registry+https://example.com/index"),
            ("tokio", "1.54.0", None),
            ("anng", "0.1.0", good_git.replace(f"#{rev}", "#" + "0" * 40)),
        )
        for name, version, source in cases:
            with self.subTest(name=name, source=source):
                report = dependency_report(
                    [dict(name=name, version=version, source=source)], catalog, direct_edges=[edge(name, version, source)]
                )
                self.assertTrue(report["errors"])
                self.assertIn("required=", report["errors"][0])
        self.assertFalse(dependency_report(
            [dict(name="anng", version="0.1.0", source=good_git)], catalog, direct_edges=[edge("anng", "0.1.0", good_git)]
        )["errors"])

    def test_transitive_coexistence_is_informational(self) -> None:
        records = [
            dict(name="thiserror", version="1.0.69", source=CRATES_IO_SOURCE),
            dict(name="thiserror", version="2.0.20", source=CRATES_IO_SOURCE),
            dict(name="unknown", version="0.1.0", source=CRATES_IO_SOURCE),
            dict(name="unknown", version="0.2.0", source=CRATES_IO_SOURCE),
        ]
        report = dependency_report(records, load_catalog(FORGE).dependencies)
        self.assertFalse(report["errors"])
        self.assertEqual([r["name"] for r in report["coexisting"]], ["thiserror", "unknown"])
        self.assertEqual(report["transitive_versions"][0]["version"], "1.0.69")

    def test_transitive_versions_are_informational_but_direct_drift_is_fatal(self) -> None:
        records = [dict(name="libc", version="0.2.189", source=CRATES_IO_SOURCE)]
        catalog = load_catalog(FORGE).dependencies
        report = dependency_report(records, catalog)
        self.assertFalse(report["errors"])
        self.assertEqual(report["transitive_versions"][0]["name"], "libc")
        report = dependency_report(records, catalog, direct_edges=[edge("libc", "0.2.189", CRATES_IO_SOURCE)])
        self.assertTrue(report["errors"])
        self.assertFalse(report["transitive_versions"])

    def test_catalog_rejects_old_schema_and_distinct_versions_or_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rust").mkdir()
            path = root / "rust/catalog.toml"
            source = (FORGE / "rust/catalog.toml").read_text()
            for extra in (
                '\n[[dependencies]]\nname = "tokio"\nversion = "=1.53.1"\n',
                '\n[[dependencies]]\nname = "tokio"\ngit = "https://example.com/tokio"\nrev = "' + "0" * 40 + '"\n',
            ):
                path.write_text(source + extra)
                with self.assertRaisesRegex(RuntimeError, "Duplicate.*tokio"):
                    load_catalog(root)
            path.write_text(source.replace("schema = 2", "schema = 1", 1))
            with self.assertRaisesRegex(RuntimeError, "Unsupported Rust catalog schema"):
                load_catalog(root)

    def test_root_package_and_target_dependencies_must_inherit(self) -> None:
        for section in ("dependencies", "dev-dependencies", "build-dependencies",
                        "target.'cfg(unix)'.dependencies"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                create_project(root)
                manifest = root / "rust/Cargo.toml"
                with manifest.open("a") as stream:
                    stream.write('\n[package]\nname = "root"\nversion = "0.1.0"\n')
                    stream.write(f'\n[{section}]\ntokio = "=1.54.0"\n')
                with self.assertRaisesRegex(RuntimeError, "must inherit"):
                    validate_rust_project(load_rust_project(root, FORGE))

    def test_patch_replace_and_local_shadowing_are_rejected(self) -> None:
        for relative in ("rust/Cargo.toml", "rust/crate/Cargo.toml"):
            for section in ("patch.crates-io", "replace"):
                with self.subTest(relative=relative, section=section), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    create_project(root)
                    with (root / relative).open("a") as stream:
                        stream.write(f'\n[{section}]\n')
                    with self.assertRaisesRegex(RuntimeError, "overrides are forbidden"):
                        validate_rust_project(load_rust_project(root, FORGE))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_project(root)
            local = root / "rust/fake"
            local.mkdir()
            (local / "Cargo.toml").write_text('[package]\nname = "tokio"\nversion = "1.54.0"\n')
            with (root / "rust/Cargo.toml").open("a") as stream:
                stream.write('\nfake = { path = "fake" }\n')
            with self.assertRaisesRegex(RuntimeError, "shadows a Forge crate"):
                validate_rust_project(load_rust_project(root, FORGE))

    def test_source_repairs_reject_ancestor_or_member_cargo_overrides(self) -> None:
        for relative in (".cargo/config.toml", "rust/crate/.cargo/config.toml"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                create_project(root)
                project = replace(load_rust_project(root, FORGE), source_patches=("wry-0.53.5-gtk-ipc-weak",))
                configuration = root / relative
                configuration.parent.mkdir(parents=True)
                configuration.write_text('[patch.crates-io]\nwry = { path="/other" }\n')
                with self.assertRaisesRegex(RuntimeError, "source overrides"):
                    validate_rust_project(project)

    def test_source_repair_requires_its_locked_target_even_for_transitive_crates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_project(root)
            project = replace(load_rust_project(root, FORGE), source_patches=("wry-0.53.5-gtk-ipc-weak",))
            with self.assertRaisesRegex(RuntimeError, "locked Wry"):
                validate_rust_project(project)
            with (root / "rust/Cargo.lock").open("a") as lock:
                lock.write('\n[[package]]\nname="wry"\nversion="0.53.5"\n')
            validate_rust_project(project)

    def test_registry_alias_and_missing_approved_lock_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_project(root)
            manifest = root / "rust/Cargo.toml"
            manifest.write_text(manifest.read_text().replace(
                'tokio = { version', 'runtime = { package = "tokio", version'
            ))
            member = root / "rust/crate/Cargo.toml"
            member.write_text(member.read_text().replace("tokio.workspace", "runtime.workspace"))
            project = load_rust_project(root, FORGE)
            validate_rust_project(project)
            lockfile = root / "rust/Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace('version = "1.54.0"', 'version = "0.2.0"'))
            with self.assertRaisesRegex(RuntimeError, "tokio.*required="):
                validate_rust_project(project)

    def test_capability_and_tool_selection_use_actual_package_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_project(root)
            manifest = root / "rust/Cargo.toml"
            manifest.write_text(manifest.read_text().replace(
                'dioxus = { version', 'ui = { package = "dioxus", version'
            ))
            validate_rust_project(load_rust_project(root, FORGE))

    def test_preflight_drift_prevents_prepare_cleanup_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_project(root)
            lockfile = root / "rust/Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace("1.54.0", "1.53.1"))
            config = load_repo_config(FORGE / "zbuild.py")
            engine = RustEngine(root, config, {"sample": RustTarget("sample", root / "rust")}, root / "build")
            project = Project("sample", (Product("sample", "rust", "sample", ("rebuild",)),), {"rust": engine}, ("sample",))
            with mock.patch.object(engine, "prepare") as prepare, mock.patch.object(engine, "execute") as execute:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli(project, ["rebuild"]), 1)
                prepare.assert_not_called()
                execute.assert_not_called()
            with mock.patch.object(engine, "manager") as manager:
                engine.preflight(request("clean"), ("sample",))
                manager.assert_not_called()


class AuditTests(unittest.TestCase):
    def make_workspace(self, root: Path):
        forge = root / "zeta_forge"
        shutil.copytree(FORGE / "rust", forge / "rust")
        create_project(root / "sample")
        (forge / "rust/projects.toml").write_text('schema = 1\n[[projects]]\npath = "sample"\n')
        return replace(load_repo_config(FORGE / "zbuild.py"), forge_root=forge, zetax_root=root)

    def test_registry_missing_unregistered_duplicate_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_workspace(root)
            self.assertEqual(len(registered_projects(config)), 1)
            create_project(root / "new")
            with self.assertRaisesRegex(RuntimeError, "Unregistered.*new"):
                registered_projects(config)
            registry = config.forge_root / "rust/projects.toml"
            for source, error in (
                ('schema = 1\n[[projects]]\npath = "missing"\n', "missing"),
                ('schema = 1\n[[projects]]\npath = "../outside"\n', "sibling"),
                ('schema = 1\n[[projects]]\npath = "sample"\n[[projects]]\npath = "sample"\n', "Duplicate"),
            ):
                registry.write_text(source)
                with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                    registered_projects(config)

    def test_extra_workspace_and_read_only_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_workspace(root)
            extra = root / "sample/tests/consumer"
            extra.mkdir(parents=True)
            (extra / "Cargo.toml").write_text('[workspace]\n[package]\nname = "consumer"\nversion = "0.1.0"\n')
            (extra / "Cargo.lock").write_text('version = 4\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n')
            registry = config.forge_root / "rust/projects.toml"
            registry.write_text('schema = 1\n[[projects]]\npath = "sample"\nadditional-manifests = ["tests/consumer/Cargo.toml"]\n')
            engine = RustDependencyAudit(config)
            project = Project("audit", (Product("rust-deps", "audit", "audit", ("doctor", "check")),), {"audit": engine}, ("rust-deps",))
            def snapshot():
                return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
            before = snapshot()
            with mock.patch("zeta_forge.rust_audit.run_command") as run:
                for args in (["--help"], ["list"], ["metadata"], ["check", "--dry-run"], ["doctor"]):
                    with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(cli(project, args), 0)
                    if args == ["metadata"]:
                        self.assertEqual(len(json.loads(output.getvalue())["steps"][0]["consumers"]), 2)
                run.assert_not_called()
            self.assertEqual(before, snapshot())

    def test_resolved_report_chains_and_offline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_workspace(root)
            project = registered_projects(config)[0]
            metadata = {
                "workspace_members": ["sample"],
                "packages": [
                    dict(id="sample", name="sample", version="0.1.0", source=None, manifest_path=str(project.manifest.parent / "crate/Cargo.toml")),
                    dict(id="tokio", name="tokio", version="1.54.0", source=CRATES_IO_SOURCE),
                ],
                "resolve": {"nodes": [dict(id="sample", deps=[dict(pkg="tokio")]), dict(id="tokio", deps=[])]},
            }
            result = subprocess.CompletedProcess([], 0, json.dumps(metadata), "")
            with mock.patch("zeta_forge.rust_audit.run_command", return_value=result) as run:
                report = resolved_project(project, config)
                self.assertEqual(report["direct_edges"][0]["chain"], ["sample@0.1.0", "tokio@1.54.0"])
                self.assertIn("--locked", run.call_args.args[0])
                self.assertIn("--offline", run.call_args.args[0])
                self.assertNotIn("--no-deps", run.call_args.args[0])
            result = subprocess.CompletedProcess([], 101, "", "package not cached")
            with mock.patch("zeta_forge.rust_audit.run_command", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "owning project's ./zbuild.py fetch"):
                    resolved_project(project, config)


if __name__ == "__main__":
    unittest.main()
