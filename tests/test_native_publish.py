"""Filesystem-only checks for staged publication and journal recovery."""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "common"))

from zeta_forge import native_transaction as tx
from zeta_forge.conan_state import write_json, writer_lock
from zeta_forge.native_artifacts import RECEIPT_NAME, make_receipt
from zeta_forge.native_publish import compatible_package, publish


class NativePublishTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.prefix = self.root / "prefix"
        self.state = self.prefix / tx.STATE_PATH

    def stage(self, target: str, files: dict[str, bytes], profile: str = "release") -> tuple[str, str, Path]:
        stage = self.root / "build" / "staging" / profile / target
        if stage.exists():
            shutil.rmtree(stage)
        for relative, data in files.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.replace(b"<stage>", str(stage).encode()))
        write_json(stage / RECEIPT_NAME, make_receipt(
            stage, target, profile, sources=[], conan=None, references=[], dependencies={},
        ))
        return target, profile, stage

    def install(self, *stages: tuple[str, str, Path]) -> None:
        publish(self.prefix, self.root, stages)

    def test_legacy_conflict_does_not_write_and_identical_file_is_adopted(self) -> None:
        existing = self.prefix / "lib/a"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"legacy")
        stage = self.stage("nng", {"lib/a": b"new", "lib/b": b"added"})
        with self.assertRaisesRegex(RuntimeError, "Native publication conflict"):
            self.install(stage)
        self.assertEqual(existing.read_bytes(), b"legacy")
        self.assertFalse((self.prefix / "lib/b").exists())
        self.assertFalse((self.state / "owners.json").exists())
        existing.write_bytes(b"new")
        self.install(stage)
        self.assertIn("release:nng", tx.ownership_index(self.state)["owners"])

    def test_shared_owner_and_obsolete_tampered_files(self) -> None:
        self.install(
            self.stage("grpc", {"lib/shared": b"same"}),
            self.stage("nng", {"lib/shared": b"same", "lib/obsolete": b"owned", "lib/changed": b"owned"}),
        )
        (self.prefix / "lib/changed").write_bytes(b"user")
        with self.assertRaisesRegex(RuntimeError, "Conflicting native owners"):
            self.install(self.stage("nng", {"lib/shared": b"conflict"}))
        self.install(self.stage("nng", {"lib/new": b"new"}))
        self.assertEqual((self.prefix / "lib/shared").read_bytes(), b"same")
        self.assertFalse((self.prefix / "lib/obsolete").exists())
        self.assertEqual((self.prefix / "lib/changed").read_bytes(), b"user")

    def test_profiles_must_agree_on_shared_destination_files(self) -> None:
        self.install(self.stage("nng", {"lib/a": b"release"}))
        with self.assertRaisesRegex(RuntimeError, "Conflicting native owners"):
            self.install(self.stage("nng", {"lib/a": b"debug"}, "debug"))

    def test_relocation_and_unresolved_staging_reference(self) -> None:
        path = "lib/pkgconfig/nng.pc"
        self.install(self.stage("nng", {path: b"prefix=<stage>\n"}))
        self.assertEqual((self.prefix / path).read_text(), f"prefix={self.prefix}\n")
        unknown = self.root / "build" / "staging" / "release" / "other"
        bad = self.stage("nng", {path: f"prefix={unknown}\n".encode()})
        with self.assertRaisesRegex(RuntimeError, "Unresolved Forge"):
            self.install(bad)
        self.assertEqual((self.prefix / path).read_text(), f"prefix={self.prefix}\n")

    def test_install_rolls_back_before_and_during_receipt_commit(self) -> None:
        for failure_path in ("lib/a", "share/zeta_forge/native/owners.json"):
            with self.subTest(failure_path=failure_path):
                old = self.stage("nng", {"lib/a": b"old", "lib/b": b"removed"})
                self.install(old)
                original_index = tx.ownership_index(self.state)
                replacement = self.stage("nng", {"lib/a": b"new", "lib/c": b"added"})
                replace_entry = tx.replace_entry
                failed = False

                def fail(source, destination):
                    nonlocal failed
                    if destination == self.prefix / failure_path and not failed:
                        failed = True
                        if failure_path == "lib/a":
                            replace_entry(source, destination)
                        raise OSError("injected publication failure")
                    replace_entry(source, destination)

                with mock.patch.object(tx, "replace_entry", side_effect=fail):
                    with self.assertRaisesRegex(OSError, "injected"):
                        self.install(replacement)
                self.assertEqual(tx.ownership_index(self.state), original_index)
                self.assertEqual((self.prefix / "lib/a").read_bytes(), b"old")
                self.assertEqual((self.prefix / "lib/b").read_bytes(), b"removed")
                self.assertFalse((self.prefix / "lib/c").exists())
                self.assertFalse((self.state / "pending").exists())

    def test_recovery_uses_receipt_commit_as_transaction_boundary(self) -> None:
        stage = self.stage("nng", {"lib/a": b"old"})
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.install(stage)
                path = self.prefix / "lib/a"
                candidate = self.root / "candidate"
                candidate.write_bytes(b"new")
                journal = self.state / "pending"
                journal.mkdir()
                index = tx.ownership_index(self.state)
                index["transaction"] = uuid.uuid4().hex
                index["owners"]["release:nng"]["files"]["lib/a"] = tx.file_identity(candidate)
                tx.prepare_transaction(
                    self.prefix, journal,
                    {"lib/a": (tx.file_identity(path), tx.file_identity(candidate), candidate)},
                    index,
                )
                if committed:
                    tx.apply_transaction(self.prefix, self.state, journal)
                else:
                    tx.replace_entry(journal / "new/lib/a", path)
                tx.recover_transaction(self.prefix, self.state)
                self.assertEqual(path.read_bytes(), b"new" if committed else b"old")
                self.assertFalse(journal.exists())

    def test_prefix_writer_lock_serializes_publication(self) -> None:
        stage = self.stage("nng", {"lib/a": b"data"})
        started = threading.Event()
        finished = threading.Event()
        errors = []

        def worker():
            started.set()
            try:
                self.install(stage)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with writer_lock(self.state / "writer.lock"):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
            self.assertFalse((self.prefix / "lib/a").exists())
        thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])

    def test_folly_boost_override_preserves_other_abi_inputs(self) -> None:
        base = {
            "ref": "boost/1.91.0#recipe",
            "settings": {"compiler.version": "15"},
            "options": {"zlib": "True", "shared": "False"},
            "package_id": "base",
            "package_revision": "one",
        }
        folly = {**base, "options": {"zlib": "False", "shared": "False"}, "package_id": "folly"}
        self.assertTrue(compatible_package("boost", base, folly, {"deps", "folly"}))
        changed = {**folly, "options": {"zlib": "False", "shared": "True"}}
        self.assertFalse(compatible_package("boost", base, changed, {"deps", "folly"}))


if __name__ == "__main__":
    unittest.main()
