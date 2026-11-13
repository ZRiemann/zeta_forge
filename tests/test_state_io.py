"""Regression fixtures for persistent state bytes and atomic replacement."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.state_io import digest_json, file_digest, read_json, write_json


class StateIOTests(unittest.TestCase):
    def test_phase_two_canonical_utf8_and_digest(self) -> None:
        value = {"z": ["中文", True], "a": {"value": 1}}
        canonical = '{"a":{"value":1},"z":["中文",true]}'.encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            write_json(path, value)
            self.assertEqual(path.read_bytes(), canonical + b"\n")
            self.assertEqual(digest_json(value), hashlib.sha256(canonical).hexdigest())
            self.assertEqual(file_digest(path), hashlib.sha256(canonical + b"\n").hexdigest())
            self.assertEqual(read_json(path), value)

    def test_failed_replace_preserves_original_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_bytes(b'{"generation":1}\n')
            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_json(path, {"generation": 2})
            self.assertEqual(path.read_bytes(), b'{"generation":1}\n')
            self.assertEqual(list(path.parent.iterdir()), [path])
