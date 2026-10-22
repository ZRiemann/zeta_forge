"""Deterministic release, MSRV, network and read-only command contracts."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))
from zeta_forge.build_cli import Product, Project, Request, cli
from zeta_forge.config import load_repo_config
from zeta_forge.rust_releases import (
    SemVer, git_releases, http_text, index_url, json_response, registry_releases, remote_tags,
)
from zeta_forge.rust_updates import RustUpdates, _edge_declarations, query_dependency, select_candidates
from zeta_forge.rust_workspace import load_catalog


def discovered(releases: list[dict]) -> dict:
    return {"releases": releases, "prereleases": [], "unverified_tags": [], "errors": [], "sources": ["official"]}


def context(*toolchains: str) -> dict:
    return {"consumers": [{"toolchain": t} for t in toolchains], "constraints": []}


class ReleaseTests(unittest.TestCase):
    def test_semver_ordering_build_and_prerelease_identifiers(self):
        versions = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                    "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.2", "1.0.10", "2.0.0"]
        self.assertEqual(sorted(reversed(versions), key=SemVer.parse), versions)
        self.assertEqual(SemVer.parse("1.0.0+build.4"), SemVer.parse("1.0.0"))
        for invalid in ("1.0", "01.0.0", "1.0.0-01", "v1.0.0", "1.0.0-"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                SemVer.parse(invalid)

    def test_registry_stable_yanked_and_index_paths(self):
        for name, suffix in (("a", "1/a"), ("ab", "2/ab"), ("abc", "3/a/abc"), ("tokio", "to/ki/tokio")):
            self.assertTrue(index_url(name).endswith(suffix))
        rows = [dict(name="example", vers=version, yanked=yanked, rust_version=msrv)
                for version, yanked, msrv in (("1.0.9", False, "1.60"), ("1.0.10", False, None),
                                              ("2.0.0", True, "1.80"), ("3.0.0-rc.1", False, "1.80"))]
        with mock.patch("zeta_forge.rust_releases.http_text", return_value="\n".join(map(json.dumps, rows))):
            report = registry_releases("example")
        self.assertEqual([r["version"] for r in report["releases"]], ["1.0.9", "1.0.10"])
        with mock.patch("zeta_forge.rust_releases.http_text", return_value="invalid"):
            with self.assertRaisesRegex(RuntimeError, "Invalid crates.io"):
                registry_releases("example")

    def test_msrv_missing_excessive_and_multiple_toolchains(self):
        releases = [dict(version="1.0.1", rust_version="1.65"), dict(version="1.0.2", rust_version=None),
                    dict(version="1.1.0", rust_version="1.90"), dict(version="2.0.0", rust_version="1.99")]
        report = select_candidates("example", {"version": "=1.0.0"}, discovered(releases), context("1.85.0", "1.97.1"), [])
        self.assertEqual(report["toolchain_candidate"]["version"], "1.0.1")
        self.assertEqual(report["latest_stable"]["msrv_status"], "requires_newer_toolchain")
        self.assertEqual(report["compatible_series_update"]["version"], "1.1.0")
        self.assertEqual(report["breaking_update"]["version"], "2.0.0")
        self.assertEqual(report["unverified_msrv_update"]["version"], "1.0.2")
        report = select_candidates("example", {"version": "=1.0.0"}, discovered([releases[1]]), context("1.97.1"), [])
        self.assertIsNone(report["toolchain_candidate"])
        self.assertEqual(report["latest_stable"]["msrv_status"], "needs_verification")

    def test_exact_blockers_are_reported_even_if_msrv_unknown(self):
        info = context("1.97.1")
        info["constraints"] = [{"consumer_is_zetax": False, "exact_version": "2.0.1", "requirement": "=2.0.1",
                                "chain": ["app", "wry@0.53.5", "webkit2gtk@2.0.1"]},
                               {"consumer_is_zetax": True, "exact_version": "2.0.1"}]
        report = select_candidates("webkit2gtk", {"version": "=2.0.1"},
                                   discovered([dict(version="2.0.2", rust_version=None)]), info, [])
        self.assertEqual(len(report["known_blockers"]), 1)
        self.assertEqual(report["known_blockers"][0]["chain"][1], "wry@0.53.5")

    def test_breaking_series_exact_requirement_may_coexist(self):
        info = context("1.97.1")
        info["constraints"] = [{"consumer_is_zetax": False, "exact_version": "1.0.0"}]
        report = select_candidates("example", {"version": "=1.0.0"},
                                   discovered([dict(version="2.0.0", rust_version="1.85")]), info, [])
        self.assertFalse(report["known_blockers"])
        self.assertEqual(report["adoption"], "needs_cargo_resolution_and_delivery_validation")

    def test_requirements_follow_alias_kinds_and_sources(self):
        owner = {"dependencies": [
            dict(name="example", rename="first", req="=1.0.0", source="registry+official", kind=None, target=None),
            dict(name="example", rename="second", req="=2.0.0", source="registry+official", kind=None, target=None),
            dict(name="example", rename="first", req="=3.0.0", source="git+elsewhere", kind=None, target=None),
            dict(name="example", rename="first", req="=4.0.0", source="registry+official", kind="build", target=None),
        ]}
        target = dict(name="example", source="registry+official")
        edge = dict(name="first", dep_kinds=[dict(kind=None, target=None)])
        matches = _edge_declarations(owner, target, edge)
        self.assertEqual([d["req"] for d in matches], ["=1.0.0"])


    def test_http_timeout_missing_and_rate_limit_failures(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("zeta_forge.rust_releases.urlopen", return_value=response) as open_url:
            self.assertEqual(http_text("https://example.com"), "[]")
            self.assertEqual(open_url.call_args.kwargs["timeout"], 20)
        for error in (URLError("offline"), HTTPError("url", 429, "rate limited", None, None), TimeoutError("timeout")):
            with mock.patch("zeta_forge.rust_releases.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError):
                    http_text("https://example.com")
        with mock.patch("zeta_forge.rust_releases.urlopen", side_effect=HTTPError("url", 404, "missing", None, None)):
            self.assertIsNone(http_text("https://example.com", missing_ok=True))
        with mock.patch("zeta_forge.rust_releases.http_text", return_value="{"):
            with self.assertRaisesRegex(RuntimeError, "Invalid JSON"):
                json_response("https://example.com")

    def test_annotated_tags_resolve_and_preserve_environment(self):
        output = "a" * 40 + "\trefs/tags/anng-v1.0.0\n" + "b" * 40 + "\trefs/tags/anng-v1.0.0^{}\n"
        with mock.patch("zeta_forge.rust_releases.run_command", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            tags = remote_tags("https://github.com/nanomsg/nng-rs", {"HTTPS_PROXY": "existing"})
            self.assertEqual(tags["anng-v1.0.0"], "b" * 40)
            self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], "existing")
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
        with mock.patch("zeta_forge.rust_releases.run_command", side_effect=subprocess.TimeoutExpired("git", 20)):
            with self.assertRaisesRegex(RuntimeError, "Git tag query failed"):
                remote_tags("https://example.com", {})

    def git_fixture(self, inherited=False):
        entry = load_catalog(FORGE).dependencies["anng"]
        manifests = {
            "old": '[package]\nname = "anng"\nversion = "0.1.3"\nrust-version = "1.85"\n',
            "current": '[package]\nname = "anng"\nversion = "0.2.0-v2pre.3"\nrust-version = "1.85"\n',
        }
        if inherited:
            manifests["old"] = '[package]\nname = "anng"\nversion.workspace = true\nrust-version.workspace = true\n'
        def read(url, **kwargs):
            if entry["rev"] in url:
                return manifests["current"]
            if url.endswith("/old/Cargo.toml"):
                return '[workspace.package]\nversion = "0.1.3"\nrust-version = "1.85"\n'
            return manifests["old"]
        return entry, manifests, read

    def test_git_tag_only_old_stable_no_downgrade_and_other_crates(self):
        entry, _, read = self.git_fixture(inherited=True)
        tags = {"anng-v0.1.3": "old", "anng-v0.2.0-v2pre.4": "new", "nng-sys-v9.0.0": "other"}
        with mock.patch("zeta_forge.rust_releases.remote_tags", return_value=tags), \
             mock.patch("zeta_forge.rust_releases.json_response", return_value=None), \
             mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
            discovery = git_releases("anng", entry, {})
        report = select_candidates("anng", entry, discovery, context("1.97.1"), [])
        self.assertEqual(report["status"], "current_ahead_of_stable")
        self.assertIsNone(report["toolchain_candidate"])
        self.assertEqual(report["latest_stable"]["version"], "0.1.3")
        self.assertTrue(report["prereleases"][0]["newer_than_current"])
        self.assertFalse(report["errors"])

    def test_git_draft_prerelease_and_mismatched_manifest(self):
        entry, manifests, read = self.git_fixture()
        for release in ({"draft": True, "prerelease": False}, {"draft": False, "prerelease": True}):
            with mock.patch("zeta_forge.rust_releases.remote_tags", return_value={"anng-v0.1.3": "old"}), \
                 mock.patch("zeta_forge.rust_releases.json_response", return_value=release), \
                 mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
                self.assertFalse(git_releases("anng", entry, {})["releases"])
        manifests["old"] = '[package]\nname = "nng-sys"\nversion = "0.1.3"\n'
        with mock.patch("zeta_forge.rust_releases.remote_tags", return_value={"anng-v0.1.3": "old"}), \
             mock.patch("zeta_forge.rust_releases.json_response", return_value=None), \
             mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
            report = git_releases("anng", entry, {})
        self.assertFalse(report["releases"])
        self.assertIn("mismatch", report["unverified_tags"][0]["reason"])

    def test_git_formal_release_and_invalid_manifest_response(self):
        entry, manifests, read = self.git_fixture()
        release = {"draft": False, "prerelease": False, "html_url": "official-release"}
        with mock.patch("zeta_forge.rust_releases.remote_tags", return_value={"anng-v0.1.3": "old"}), \
             mock.patch("zeta_forge.rust_releases.json_response", return_value=release), \
             mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
            report = git_releases("anng", entry, {})
        self.assertEqual(report["releases"][0]["release"], "official-release")
        manifests["old"] = "package = 3"
        with mock.patch("zeta_forge.rust_releases.remote_tags", return_value={"anng-v0.1.3": "old"}), \
             mock.patch("zeta_forge.rust_releases.json_response", return_value=release), \
             mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
            report = git_releases("anng", entry, {})
        self.assertFalse(report["releases"])
        self.assertTrue(report["errors"])
        self.assertTrue(report["unverified_tags"])


    def test_git_release_failure_keeps_other_tag_results(self):
        entry, _, read = self.git_fixture()
        with mock.patch("zeta_forge.rust_releases.remote_tags", return_value={"anng-v0.1.2": "bad", "anng-v0.1.3": "old"}), \
             mock.patch("zeta_forge.rust_releases.json_response", side_effect=[RuntimeError("HTTP 429"), None]), \
             mock.patch("zeta_forge.rust_releases.http_text", side_effect=read):
            report = git_releases("anng", entry, {})
        self.assertEqual(report["releases"][0]["version"], "0.1.3")
        self.assertEqual(report["errors"], ["HTTP 429"])


class UpdateCommandTests(unittest.TestCase):
    def project(self):
        engine = RustUpdates(load_repo_config(FORGE / "zbuild.py"))
        return engine, Project("updates", (Product("rust-updates", "updates", "updates", ("check",)),),
                               {"updates": engine}, ("rust-updates",))

    def test_descriptive_commands_never_query_or_resolve(self):
        engine, project = self.project()
        paths = [FORGE / "rust/catalog.toml", FORGE / "rust/projects.toml"]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        with mock.patch("zeta_forge.rust_updates.query_dependency") as query, \
             mock.patch("zeta_forge.rust_updates.resolved_metadata") as metadata, \
             mock.patch.object(engine, "preflight") as preflight:
            for args in (["--help"], ["list"], ["metadata"], ["check", "--dry-run"]):
                with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli(project, args), 0)
            query.assert_not_called()
            metadata.assert_not_called()
            preflight.assert_not_called()
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_partial_queries_print_all_results_and_fail(self):
        engine, _ = self.project()
        catalog = mock.Mock(dependencies={"good": {"version": "=1.0.0"}, "bad": {"version": "=1.0.0"}})
        def query(name, *args):
            return {"name": name, "status": "current" if name == "good" else "query_failed",
                    "errors": [] if name == "good" else ["HTTP 429"]}
        with mock.patch("zeta_forge.rust_updates.load_catalog", return_value=catalog), \
             mock.patch("zeta_forge.rust_updates.query_dependency", side_effect=query), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                engine.execute(Request("check", "release", 1, argparse.Namespace()), ("rust-updates",))
        report = next(json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{"errors":'))
        self.assertEqual([r["name"] for r in report["results"]], ["bad", "good"])
        self.assertEqual(report["summary"]["errors"], 1)

    def test_missing_offline_cache_preserves_discovery_without_recommendation(self):
        engine, _ = self.project()
        engine.projects = [mock.Mock(manifest=Path("/app/Cargo.toml"), toolchain_spec={"channel": "1.97.1"})]
        catalog = mock.Mock(dependencies={"example": {"version": "=1.0.0"}})
        response = {"name": "example", "status": "update_available", "errors": [],
                    "toolchain_candidate": {"version": "1.0.1"}}
        with mock.patch("zeta_forge.rust_updates.load_catalog", return_value=catalog), \
             mock.patch("zeta_forge.rust_updates.resolved_metadata", side_effect=RuntimeError("prepare cache with ./zbuild.py fetch")), \
             mock.patch("zeta_forge.rust_updates.query_dependency", return_value=response), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "prepare cache"):
                engine.execute(Request("check", "release", 1, argparse.Namespace()), ("rust-updates",))
        report = next(json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{"errors":'))
        self.assertIsNone(report["results"][0]["toolchain_candidate"])
        self.assertEqual(report["results"][0]["consumer_graph_status"], "incomplete")


    def test_newer_version_is_success_and_network_failure_has_no_old_results(self):
        with mock.patch("zeta_forge.rust_updates.registry_releases", return_value=discovered([dict(version="1.0.1", rust_version="1.60")])):
            result = query_dependency("example", {"version": "=1.0.0"}, context("1.97.1"), [], {})
            self.assertEqual(result["status"], "update_available")
            self.assertFalse(result["errors"])
        with mock.patch("zeta_forge.rust_updates.registry_releases", side_effect=RuntimeError("network offline")):
            result = query_dependency("example", {"version": "=1.0.0"}, context("1.97.1"), [], {})
            self.assertEqual(result["status"], "query_failed")
            self.assertEqual(result["current"]["version"], "1.0.0")
            self.assertNotIn("latest_stable", result)


if __name__ == "__main__":
    unittest.main()
