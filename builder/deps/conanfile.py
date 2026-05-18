import os

from conan import ConanFile
from conan.tools.cmake import cmake_layout


FMT_VERSION = os.getenv("ZETA_FMT_VERSION", "12.1.0")
SPDLOG_VERSION = os.getenv("ZETA_SPDLOG_VERSION", "1.17.0")
BOOST_VERSION = os.getenv("ZETA_BOOST_VERSION", "1.91.0")
OPENSSL_VERSION = os.getenv("ZETA_OPENSSL_VERSION", "3.6.2")


class ZetaDepsConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"

    requires = (
        # --- common deps ---
        f"fmt/{FMT_VERSION}",
        f"spdlog/{SPDLOG_VERSION}",
        "gtest/1.17.0",
        # --- boost (required by folly and hpx) ---
        f"boost/{BOOST_VERSION}",
        # --- folly transitive deps ---
        # These must be in zeta_deps so that installed folly-config.cmake can
        # call find_package(gflags), find_package(glog), etc. when downstream
        # projects (zpp, zeta_trader) consume folly via find_package(Folly).
        "gflags/2.2.2",
        "glog/0.7.1",
        "double-conversion/3.3.0",
        "fast_float/8.1.0",
        "libevent/2.1.12",
        "bzip2/1.0.8",
        "xz_utils/5.8.1",
        "lz4/1.10.0",
        "zstd/1.5.7",
        "snappy/1.2.1",
        "libsodium/1.0.20",
        f"openssl/{OPENSSL_VERSION}",
    )

    default_options = {
        "fmt/*:shared": False,
        "spdlog/*:header_only": True,
        "gtest/*:build_gmock": False,
        "gtest/*:no_main": False,
        "boost/*:header_only": False,
        "boost/*:shared": False,
        "double-conversion/*:shared": False,
        "gflags/*:shared": False,
        "glog/*:shared": False,
        "glog/*:with_unwind": False,
        "libevent/*:shared": False,
        "libevent/*:with_openssl": False,
        "bzip2/*:shared": False,
        "xz_utils/*:shared": False,
        "lz4/*:shared": False,
        "zstd/*:shared": False,
        "snappy/*:shared": False,
        "libsodium/*:shared": False,
        "openssl/*:shared": False,
    }

    def layout(self):
        cmake_layout(self)
