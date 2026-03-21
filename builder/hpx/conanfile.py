import os

from conan import ConanFile
from conan.tools.cmake import cmake_layout


BOOST_VERSION = os.getenv("ZETA_BOOST_VERSION", "1.90.0")
JEMALLOC_VERSION = os.getenv("ZETA_JEMALLOC_VERSION", "5.3.0")


class HpxBuildConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"

    requires = (
        f"boost/{BOOST_VERSION}",
        "asio/1.30.2",
        "hwloc/2.12.2",
        f"jemalloc/{JEMALLOC_VERSION}",
    )

    default_options = {
        "boost/*:header_only": False,
        "boost/*:shared": False,
        "asio/*:shared": False,
        "hwloc/*:shared": False,
        "jemalloc/*:shared": False,
    }

    def layout(self):
        cmake_layout(self)