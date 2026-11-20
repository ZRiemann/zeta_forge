from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from zeta_forge import zig_toolchain as zig


class ZigToolchainTest(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(
            mock.patch.object(zig.platform, "machine", return_value="x86_64")
        )
        self.version = zig.ZIG_VERSION
        stack.enter_context(mock.patch.object(zig.shutil, "which", return_value=None))
        self.archive = self.home / "release.tar.xz"
        self.write_archive(self.version)
        self.asset = {
            "tarball": f"https://ziglang.org/download/{self.version}/zig-x86_64-linux-{self.version}.tar.xz",
            "shasum": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        }
        self.index = {
            "master": {"version": "0.11.0-dev.1"},
            "99.10.2": {"x86_64-linux": self.asset},
            self.version: {"x86_64-linux": self.asset},
            "0.9.1": {},
        }
        self.context = zig.ZigInstallContext(
            home=self.home,
            local_bin=self.home / ".local" / "bin",
            fetch_text=mock.Mock(side_effect=lambda _: json.dumps(self.index)),
            download_binary=mock.Mock(
                side_effect=lambda _, target: shutil.copyfile(self.archive, target)
            ),
            run_subprocess=mock.Mock(side_effect=self.probe),
        )
        self.destination = (
            self.home / ".local" / "opt" / "zig" / f"{self.version}-x86_64-linux"
        )
        self.launcher = self.context.local_bin / "zig"

    def write_archive(
        self, version: str, extra_member: tarfile.TarInfo | None = None
    ) -> None:
        with tarfile.open(self.archive, "w:xz") as archive:
            for name, content, mode in (
                ("package/zig", version.encode(), 0o755),
                ("package/lib/std/std.zig", b"standard library", 0o644),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = mode
                archive.addfile(member, io.BytesIO(content))
            if extra_member is not None:
                archive.addfile(extra_member, io.BytesIO(b""))

    def probe(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.assertEqual(command[1:], ["version"])
        return subprocess.CompletedProcess(
            command, 0, Path(command[0]).read_text(encoding="utf-8"), ""
        )

    def make_previous_installation(self, directory: Path | None = None) -> Path:
        previous = (
            directory if directory is not None else self.destination.parent / "older"
        )
        previous.mkdir(parents=True)
        binary = previous / "zig"
        binary.write_text("0.9.1", encoding="utf-8")
        binary.chmod(0o755)
        self.context.local_bin.mkdir(parents=True)
        self.launcher.symlink_to(binary)
        return binary

    def refresh_checksum(self) -> None:
        self.asset["shasum"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_only_the_forge_version_is_selected(self) -> None:
        release = zig.resolve_release(json.dumps(self.index), "x86_64")
        self.assertEqual(release.version, self.version)
        self.assertEqual(release.url, self.asset["tarball"])

    def test_linux_architecture_aliases(self) -> None:
        for machine, architecture in (
            ("arm64", "aarch64"),
            ("i686", "x86"),
            ("ppc64le", "powerpc64le"),
            ("armv7l", "arm"),
        ):
            with self.subTest(machine=machine):
                index = {self.version: {f"{architecture}-linux": self.asset}}
                self.assertEqual(
                    zig.resolve_release(json.dumps(index), machine).target,
                    f"{architecture}-linux",
                )

    def test_pinned_release_without_host_binary_is_an_error(self) -> None:
        self.index[self.version] = {"aarch64-linux": self.asset}
        with self.assertRaisesRegex(
            RuntimeError, "no official binary for x86_64-linux"
        ):
            zig.install(self.context)
        self.context.download_binary.assert_not_called()

    def test_invalid_index_and_asset_metadata_are_rejected(self) -> None:
        for index in ("not JSON", "[]", '{"master": {}}'):
            with (
                self.subTest(index=index),
                self.assertRaises((RuntimeError, TypeError)),
            ):
                zig.resolve_release(index, "x86_64")
        for url, checksum in (
            ("http://ziglang.org/download/zig.tar.xz", "0" * 64),
            ("https://example.org/zig.tar.xz", "0" * 64),
            (self.asset["tarball"], "missing"),
        ):
            with self.subTest(url=url, checksum=checksum):
                index = {
                    self.version: {"x86_64-linux": {"tarball": url, "shasum": checksum}}
                }
                with self.assertRaises(RuntimeError):
                    zig.resolve_release(json.dumps(index), "x86_64")

    def test_upgrade_installs_complete_package_and_keeps_previous_version(self) -> None:
        previous = self.make_previous_installation()
        zig.install(self.context)
        self.assertEqual(self.launcher.resolve(), self.destination / "zig")
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), self.version)
        self.assertTrue((self.destination / "lib" / "std" / "std.zig").is_file())
        self.assertEqual(previous.read_text(encoding="utf-8"), "0.9.1")
        self.context.fetch_text.assert_called_once_with(zig.DOWNLOAD_INDEX_URL)
        self.context.download_binary.assert_called_once()
        self.assertFalse(list(self.destination.parent.glob(".install-*")))

        # Rerunning checks upstream again, but reuses a healthy downloaded version.
        zig.install(self.context)
        self.assertEqual(self.context.fetch_text.call_count, 1)
        self.context.download_binary.assert_called_once()

    def test_checksum_failure_keeps_previous_installation(self) -> None:
        previous = self.make_previous_installation()
        self.asset["shasum"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            zig.install(self.context)
        self.assertEqual(self.launcher.resolve(), previous)
        self.assertFalse(self.destination.exists())

    def test_download_failure_keeps_previous_installation(self) -> None:
        previous = self.make_previous_installation()
        self.context.download_binary.side_effect = OSError("connection lost")
        with self.assertRaisesRegex(OSError, "connection lost"):
            zig.install(self.context)
        self.assertEqual(self.launcher.resolve(), previous)
        self.assertFalse(self.destination.exists())

    def test_wrong_version_keeps_previous_installation(self) -> None:
        previous = self.make_previous_installation()
        self.write_archive("0.9.0")
        self.refresh_checksum()
        with self.assertRaisesRegex(RuntimeError, "version validation"):
            zig.install(self.context)
        self.assertEqual(self.launcher.resolve(), previous)
        self.assertFalse(self.destination.exists())

    def test_unsafe_archive_members_are_rejected(self) -> None:
        for name, kind in (
            ("../../outside", tarfile.REGTYPE),
            ("package/link", tarfile.SYMTYPE),
        ):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "/tmp/outside"
                self.write_archive(self.version, member)
                self.refresh_checksum()
                with self.assertRaisesRegex(RuntimeError, "Unsafe Zig archive member"):
                    zig.install(self.context)
                self.assertFalse(self.launcher.exists())
                self.assertFalse(self.destination.exists())

    def test_broken_managed_version_is_repaired(self) -> None:
        self.make_previous_installation(self.destination)
        zig.install(self.context)
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), self.version)
        self.context.download_binary.assert_called_once()

    def test_missing_standard_library_is_repaired(self) -> None:
        zig.install(self.context)
        shutil.rmtree(self.destination / "lib")
        zig.install(self.context)
        self.assertTrue((self.destination / "lib" / "std" / "std.zig").is_file())
        self.assertEqual(self.context.download_binary.call_count, 2)

    def test_launcher_failure_restores_replaced_directory(self) -> None:
        previous = self.make_previous_installation(self.destination)
        with (
            mock.patch.object(
                Path, "replace", side_effect=OSError("cannot switch launcher")
            ),
            self.assertRaisesRegex(OSError, "cannot switch launcher"),
        ):
            zig.install(self.context)
        self.assertEqual(self.launcher.resolve(), previous)
        self.assertEqual(previous.read_text(encoding="utf-8"), "0.9.1")


if __name__ == "__main__":
    unittest.main()
