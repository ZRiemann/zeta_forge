from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_rust_workspace as workspace_fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from zeta_forge.rust_patches import tree_digest  # noqa: E402
from zeta_forge.rust_tool_patches import (  # noqa: E402
    install_repaired_tool,
    prepare_tool_source,
    tool_repair,
    verify_tool_install,
)
from zeta_forge.rust_workspace import (  # noqa: E402
    CargoToolUnavailable,
    RustWorkspaceManager,
    load_rust_project,
)


class CargoToolRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.specification = {"name": "sample-cli", "version": "1.2.3", "executable": "sample",
                              "crate": "sample", "crate-version": "=1.2.3",
                              "source-repair": "sample-1.2.3-fixed"}
        self.forge = self.root / "forge"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in {"Cargo.toml": b'[package]\nname="sample-cli"\nversion="1.2.3"\n',
                                  "src/main.rs": b"original"}.items():
                info = tarfile.TarInfo("sample-cli-1.2.3/" + name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        self.payload = buffer.getvalue()
        expected = self.root / "expected"
        (expected / "src").mkdir(parents=True)
        (expected / "Cargo.toml").write_bytes(b'[package]\nname="sample-cli"\nversion="1.2.3"\n')
        (expected / "src/main.rs").write_bytes(b"repaired")
        recipe = {"schema": 1, "package": "sample-cli", "version": "1.2.3",
                  "archive_sha256": hashlib.sha256(self.payload).hexdigest(),
                  "tree_sha256": tree_digest(expected), "upstream": "https://example.com/source",
                  "file": "src/main.rs", "replacements": [{"before": "original", "after": "repaired"}]}
        self.recipe_path = self.forge / "rust/tool-patches/sample-1.2.3-fixed.json"
        self.recipe_path.parent.mkdir(parents=True)
        self.recipe_path.write_text(json.dumps(recipe))
        self.repair = tool_repair(self.forge, self.specification)
        self.environment = {"CARGO_HOME": str(self.root / "cargo"), "HTTP_PROXY": "http://proxy:8118"}
        cache = self.root / "cargo/registry/cache/fixture/sample-cli-1.2.3.crate"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(self.payload)
        self.tool_root = self.root / "install/source" / self.repair.identity
        manifest = self.root / "Cargo.toml"
        manifest.write_text('[workspace.dependencies]\nsample = "=1.2.3"\n')
        self.project = SimpleNamespace(
            catalog=SimpleNamespace(cargo_tools={"sample-cli": (self.specification,)}),
            toolchain_spec={"channel": "1.97.1"}, workspace_dir=self.root, manifest=manifest,
        )
        self.config = SimpleNamespace(forge_root=self.forge, install_prefix=self.root / "prefix",
                                      env=self.environment)
        self.manager = RustWorkspaceManager(self.project, self.config)

    def fake_build(self, command, **kwargs):
        if "install" in command:
            candidate = Path(command[command.index("--root") + 1])
            executable = candidate / "bin/sample"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"compiled repaired tool")
            executable.chmod(0o755)
            self.assertIn("--locked", command)
            self.assertEqual(Path(command[command.index("--path") + 1]), self.repair.source)
            self.assertEqual(kwargs["env"]["HTTP_PROXY"], self.environment["HTTP_PROXY"])
            self.assertTrue(kwargs["env"]["CARGO_TARGET_DIR"].startswith(str(self.forge / "build")))
            search_path = kwargs["env"]["PATH"]
            self.assertEqual(search_path.split(os.pathsep)[-1], str(candidate / "bin"))
            self.assertEqual(search_path.rsplit(os.pathsep, 1)[0], self.environment.get("PATH", os.defpath))
        return subprocess.CompletedProcess(command, 0, stdout="sample 1.2.3\n", stderr="")

    def install(self) -> None:
        install_repaired_tool(self.repair, "1.97.1", "sample", self.tool_root, "cargo",
                              self.forge, self.environment)

    def test_recipe_is_exact_version_bound_and_rejects_unsafe_or_unknown_data(self) -> None:
        self.assertIsNone(tool_repair(self.forge, {"name": "ordinary"}))
        for change in ({"version": "2.0.0"}, {"source-repair": "../escape"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                tool_repair(self.forge, {**self.specification, **change})
        recipe = json.loads(self.recipe_path.read_bytes())
        for change in ({"file": "../escape"}, {"schema": 2}, {"unexpected": True},
                       {"archive_sha256": "invalid"}, {"replacements": []}):
            self.recipe_path.write_text(json.dumps({**recipe, **change}))
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                tool_repair(self.forge, self.specification)

    def test_cached_source_preparation_is_idempotent_and_does_not_edit_cargo_cache(self) -> None:
        with mock.patch("zeta_forge.rust_tool_patches.urlopen") as download:
            prepare_tool_source(self.repair, self.environment)
            prepare_tool_source(self.repair, self.environment)
            download.assert_not_called()
        self.assertEqual((self.repair.source / "src/main.rs").read_bytes(), b"repaired")
        self.assertEqual(next((self.root / "cargo/registry/cache").glob("*/*.crate")).read_bytes(), self.payload)
        (self.repair.source / "injected.rs").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "modified"):
            prepare_tool_source(self.repair, self.environment)

    def test_multifile_recipe_applies_all_files_and_rejects_ambiguous_edits(self) -> None:
        original = json.loads(self.recipe_path.read_bytes())
        expected_manifest = self.root / "expected/Cargo.toml"
        expected_manifest.write_bytes(expected_manifest.read_bytes() + b"# repaired\n")
        recipe = {key: value for key, value in original.items() if key not in ("file", "replacements")}
        recipe.update(schema=2, tree_sha256=tree_digest(self.root / "expected"), files=[
            {"file": "src/main.rs", "replacements": original["replacements"]},
            {"file": "Cargo.toml", "replacements": [{"before": 'version="1.2.3"',
                                                     "after": 'version="1.2.3"\n# repaired'}]},
        ])
        self.recipe_path.write_text(json.dumps(recipe))
        repair = tool_repair(self.forge, self.specification)
        prepare_tool_source(repair, self.environment)
        self.assertEqual((repair.source / "Cargo.toml").read_bytes(), expected_manifest.read_bytes())
        self.assertEqual((repair.source / "src/main.rs").read_bytes(), b"repaired")
        for changes in ({"files": []}, {"files": [*recipe["files"], recipe["files"][0]]},
                        {"file": "src/main.rs"}, {"replacements": original["replacements"]},
                        {"files": [{"file": "../escape", "replacements": original["replacements"]}]}):
            self.recipe_path.write_text(json.dumps({**recipe, **changes}))
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                tool_repair(self.forge, self.specification)

    def test_multifile_mismatch_does_not_publish_partial_source(self) -> None:
        original = json.loads(self.recipe_path.read_bytes())
        recipe = {key: value for key, value in original.items() if key not in ("file", "replacements")}
        recipe.update(schema=2, files=[
            {"file": "src/main.rs", "replacements": original["replacements"]},
            {"file": "Cargo.toml", "replacements": [{"before": "absent", "after": "bad"}]},
        ])
        self.recipe_path.write_text(json.dumps(recipe))
        repair = tool_repair(self.forge, self.specification)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            prepare_tool_source(repair, self.environment)
        self.assertFalse(repair.source.exists())

    def test_installation_does_not_mutate_parent_environment(self) -> None:
        self.environment["PATH"] = os.pathsep.join(("/first", "/second"))
        original = dict(self.environment)
        parent_path = os.environ.get("PATH")
        with mock.patch("zeta_forge.rust_tool_patches.run_command", side_effect=self.fake_build):
            self.install()
        self.assertEqual(self.environment, original)
        self.assertEqual(os.environ.get("PATH"), parent_path)

    def test_install_receipt_rejects_original_binary_changed_recipe_and_toolchain(self) -> None:
        with mock.patch("zeta_forge.rust_tool_patches.run_command", side_effect=self.fake_build):
            self.install()
        executable = self.tool_root / "bin/sample"
        verify_tool_install(self.repair, "1.97.1", executable)
        with self.assertRaisesRegex(RuntimeError, "identity"):
            verify_tool_install(self.repair, "1.98.0", executable)
        recipe = json.loads(self.recipe_path.read_bytes())
        self.recipe_path.write_text(json.dumps({**recipe, "upstream": "https://example.com/revised"}))
        changed = tool_repair(self.forge, self.specification)
        with self.assertRaisesRegex(RuntimeError, "identity"):
            verify_tool_install(changed, "1.97.1", executable)
        executable.write_bytes(b"original CLI with same version")
        with self.assertRaisesRegex(RuntimeError, "identity"):
            verify_tool_install(self.repair, "1.97.1", executable)

    def test_failed_install_preserves_prior_installation_and_cleans_lock(self) -> None:
        self.tool_root.mkdir(parents=True)
        sentinel = self.tool_root / "keep"
        sentinel.write_text("existing")
        with mock.patch("zeta_forge.rust_tool_patches.run_command", side_effect=RuntimeError("build failed")):
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                self.install()
        self.assertEqual(sentinel.read_text(), "existing")
        self.assertFalse(self.tool_root.with_name(self.tool_root.name + ".installing").exists())
        self.assertEqual(list(self.tool_root.parent.glob(".install-*")), [])

    def test_interrupted_publication_restores_previous_installation(self) -> None:
        self.tool_root.mkdir(parents=True)
        (self.tool_root / "keep").write_text("previous")
        rename = Path.rename

        def interrupted(path, target):
            if path.name == "product":
                raise KeyboardInterrupt()
            return rename(path, target)

        with (mock.patch("zeta_forge.rust_tool_patches.run_command", side_effect=self.fake_build),
              mock.patch.object(Path, "rename", interrupted)):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        self.assertEqual((self.tool_root / "keep").read_text(), "previous")
        self.assertFalse(self.tool_root.with_name(self.tool_root.name + ".installing").exists())

    def test_manager_new_projects_share_identity_and_read_only_check_never_installs(self) -> None:
        expected_root = self.manager.cargo_tool_root("sample-cli", "1.2.3")
        self.assertIn("source-" + self.repair.identity, str(expected_root))
        with mock.patch("zeta_forge.rust_tool_patches.urlopen") as download:
            with self.assertRaises(CargoToolUnavailable):
                self.manager.require_cargo_tool("sample-cli")
            download.assert_not_called()
        with (mock.patch("zeta_forge.rust_workspace.shutil.which", return_value="cargo"),
              mock.patch("zeta_forge.rust_tool_patches.run_command", side_effect=self.fake_build) as build,
              mock.patch("zeta_forge.rust_workspace.run_command", side_effect=self.fake_build)):
            first = self.manager.ensure_cargo_tool("sample-cli")
            self.assertEqual(first, self.manager.ensure_cargo_tool("sample-cli"))
            # A distinct project using the same baseline consumes the same tool installation.
            other_project = SimpleNamespace(**{**vars(self.project), "workspace_dir": self.root / "new-project"})
            other = RustWorkspaceManager(other_project, self.config)
            self.assertEqual(first, other.require_cargo_tool("sample-cli"))
            self.assertEqual(sum("install" in call.args[0] for call in build.call_args_list), 1)
        receipt = Path(first).parent.parent / "forge-tool.json"
        receipt.unlink()
        with self.assertRaisesRegex(CargoToolUnavailable, "repair identity"):
            self.manager.require_cargo_tool("sample-cli")

    def test_real_dioxus_capability_automatically_selects_the_shared_repair(self) -> None:
        forge = workspace_fixtures.FORGE_ROOT
        roots = []
        for name in ("first-project", "new-project"):
            root = self.root / name
            workspace_fixtures.RustWorkspaceTests().create_project(root)
            project = load_rust_project(root, forge)
            config = SimpleNamespace(forge_root=forge, install_prefix=self.root / "shared-prefix",
                                     env=self.environment)
            manager = RustWorkspaceManager(project, config)
            target = manager.cargo_tool_root("dioxus-cli", "0.7.10")
            self.assertTrue(target.name.startswith("source-"))
            roots.append(target)
            with mock.patch("zeta_forge.rust_tool_patches.urlopen") as download:
                with self.assertRaisesRegex(CargoToolUnavailable, "fetch"):
                    manager.require_cargo_tool("dioxus-cli")
                download.assert_not_called()
        self.assertEqual(roots[0], roots[1])

    def test_concurrent_install_is_reported_without_compiling(self) -> None:
        lock = self.tool_root.with_name(self.tool_root.name + ".installing")
        lock.mkdir(parents=True)
        with mock.patch("zeta_forge.rust_tool_patches.run_command") as build:
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                self.install()
            build.assert_not_called()
        self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
