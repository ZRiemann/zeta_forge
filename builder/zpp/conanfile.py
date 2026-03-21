from conan import ConanFile
from conan.tools.cmake import cmake_layout


class ZppBuildConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"

    requires = (
        "spdlog/1.17.0",
        "gtest/1.17.0",
    )

    default_options = {
        "spdlog/*:header_only": True,
        "gtest/*:build_gmock": False,
        "gtest/*:no_main": False,
    }

    def layout(self):
        cmake_layout(self)
