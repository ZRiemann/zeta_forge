"""Link the Rust probe against OpenSSL and zlib from the published deps config."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "common"))

from zeta_forge.conan_openssl import _cmake_value, read_openssl_manifest
from zeta_forge.process import require_command, run_command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path, help="published Forge Release prefix")
    parser.add_argument("output", type=Path, help="path for the Rust test executable")
    args = parser.parse_args()
    openssl = read_openssl_manifest(args.prefix)
    configs = args.prefix / "lib" / "cmake" / "zeta_deps" / "Release"
    matches = sorted(configs.glob("ZLIB-release-*-data.cmake"))
    if len(matches) != 1:
        parser.error(f"expected one published zlib data file under {configs}")
    zlib = Path(_cmake_value(matches[0], "zlib_PACKAGE_FOLDER_RELEASE")) / "lib" / "libz.a"
    if not zlib.is_file():
        parser.error(f"published zlib archive is missing: {zlib}")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        require_command("rustc"), "--edition=2021",
        "-L", f"native={openssl / 'lib'}", "-C", f"link-arg={zlib}",
        HERE / "openssl.rs", "-o", output,
    ])
    run_command([output])


if __name__ == "__main__":
    main()
