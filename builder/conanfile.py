"""Standalone Forge native consumer recipe with a declared policy snapshot."""

import json
from pathlib import Path

from conan import ConanFile
from conan.errors import ConanException
from conan.tools.cmake import cmake_layout

snapshot = Path(__file__).with_name("native-policy.json")
try:
    policy = json.loads(snapshot.read_text(encoding="utf-8"))
except (OSError, ValueError) as error:
    raise ConanException(
        f"Cannot load Forge native Conan policy snapshot {snapshot}. "
        "Run the corresponding Forge zbuild command to prepare the recipe."
    ) from error


class ForgeNativeConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"
    exports = "native-policy.json"
    requires = tuple(policy["requires"])
    default_options = policy["default_options"]

    def layout(self):
        cmake_layout(self)
