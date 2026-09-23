from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

FORGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE_ROOT / "common"))
sys.path.insert(0, str(FORGE_ROOT))

from zeta_forge.conan_openssl import (  # noqa: E402
    package_from_generators,
    read_openssl_manifest,
    write_openssl_manifest,
)
from zeta_forge.rust_workspace import (  # noqa: E402
    _native_environment,
)

from builder.deps.project import DepsBuilder  # noqa: E402


class ConanOpensslTests(unittest.TestCase):
    def create_package(self, root: Path) -> Path:
        package_dir = root / "package"
        for relative in ("include/openssl/ssl.h", "lib/libssl.a", "lib/libcrypto.a"):
            path = package_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return package_dir

    def install_manifest(self, root: Path, package_dir: Path) -> None:
        install_dir = root / "prefix/lib/cmake/zeta_deps/Release"
        install_dir.mkdir(parents=True)
        write_openssl_manifest(install_dir, "3.6.2", package_dir)

    def test_generated_metadata_produces_matching_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = self.create_package(root)
            generators = root / "generators"
            generators.mkdir()
            (generators / "OpenSSL-release-x86_64-data.cmake").write_text(
                f'set(openssl_PACKAGE_FOLDER_RELEASE "{package_dir}")\n', encoding="utf-8"
            )
            (generators / "OpenSSLConfigVersion.cmake").write_text(
                'set(PACKAGE_VERSION "3.6.2")\n', encoding="utf-8"
            )
            version, discovered = package_from_generators(generators, "Release")
            self.assertEqual((version, discovered), ("3.6.2", package_dir))
            self.install_manifest(root, discovered)
            self.assertEqual(read_openssl_manifest(root / "prefix"), package_dir)

            builder = DepsBuilder(
                script_path=FORGE_ROOT / "builder/deps/project.py",
                repo_config=SimpleNamespace(install_prefix=root / "installed"),
                args=SimpleNamespace(build_type="Release"),
            )
            builder.generators_dir = generators
            with (
                mock.patch.object(builder, "install_rapidjson_config"),
                mock.patch.object(builder, "install_boost_findboost_compat"),
            ):
                builder.install()
            self.assertEqual(read_openssl_manifest(root / "installed"), package_dir)

    def test_missing_manifest_and_library_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "install deps --profile release"):
                read_openssl_manifest(root / "prefix")
            package_dir = self.create_package(root)
            self.install_manifest(root, package_dir)
            (package_dir / "lib/libcrypto.a").unlink()
            with self.assertRaisesRegex(RuntimeError, "libcrypto.a"):
                read_openssl_manifest(root / "prefix")

    def test_native_environment_uses_one_conan_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = self.create_package(root)
            self.install_manifest(root, package_dir)
            project = SimpleNamespace(native_dependencies=("openssl-conan",))
            config = SimpleNamespace(env={"TEST_ENV": "1"}, install_prefix=root / "prefix")
            environment = _native_environment(project, config)
            self.assertEqual(environment["OPENSSL_DIR"], str(package_dir))
            self.assertEqual(environment["OPENSSL_LIB_DIR"], str(package_dir / "lib"))
            self.assertEqual(environment["OPENSSL_INCLUDE_DIR"], str(package_dir / "include"))
            self.assertEqual(environment["OPENSSL_STATIC"], "1")
            self.assertEqual(environment["OPENSSL_NO_VENDOR"], "1")
            self.assertEqual(environment["TEST_ENV"], "1")

    def test_conflicting_overrides_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = self.create_package(root)
            self.install_manifest(root, package_dir)
            project = SimpleNamespace(native_dependencies=("openssl-conan",))
            for name in ("OPENSSL_DIR", "OPENSSL_LIBS", "X86_64_UNKNOWN_LINUX_GNU_OPENSSL_LIB_DIR"):
                config = SimpleNamespace(env={name: "/usr"}, install_prefix=root / "prefix")
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, name):
                    _native_environment(project, config)


if __name__ == "__main__":
    unittest.main()
