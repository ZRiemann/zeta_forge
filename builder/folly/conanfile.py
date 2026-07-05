import os

from conan import ConanFile
from conan.tools.cmake import cmake_layout


BOOST_VERSION = os.getenv("ZETA_BOOST_VERSION", "1.91.0")
FMT_VERSION = os.getenv("ZETA_FMT_VERSION", "12.1.0")
OPENSSL_VERSION = os.getenv("ZETA_OPENSSL_VERSION", "3.6.2")


class FollyBuildConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"

    requires = (
        f"boost/{BOOST_VERSION}",
        "double-conversion/3.3.0",
        "fast_float/8.1.0",
        f"fmt/{FMT_VERSION}",
        "gflags/2.2.2",
        "glog/0.7.1",
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
        "boost/*:header_only": False,
        "boost/*:shared": False,
        "boost/*:zlib": False,
        "double-conversion/*:shared": False,
        "fmt/*:shared": False,
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
