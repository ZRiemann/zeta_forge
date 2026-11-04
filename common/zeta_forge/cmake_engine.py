"""Project-owned CMake configurations behind the shared public CLI."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from .build_cli import Request
from .build_ops import remove_generated, validate_generated_path
from .cmake_builder import CMakeProjectBuilder
from .cmake_run import CMakeRunTarget, read_cmake_run
from .process import require_command, run_command
from .run_spec import RunSpec, launch_run

CMAKE_ACTIONS = ("doctor", "check", "build", "rebuild", "test", "clean")


class CMakeEngine:
    def __init__(
        self,
        root: Path,
        factory: Callable[[Request, tuple[str, ...]], CMakeProjectBuilder],
        products: tuple[str, ...],
        *,
        test_targets: Callable[[tuple[str, ...]], tuple[str, ...]],
        native_targets: Callable[[tuple[str, ...]], tuple[str, ...]] = lambda names: names,
        components: Mapping[str, tuple[str, ...]] | None = None,
        run_environment: Callable[[str, Path], dict[str, str]] | None = None,
        run_targets: Mapping[str, CMakeRunTarget] | None = None,
    ) -> None:
        self.root = root
        self.factory = factory
        self.products = products
        self.test_targets = test_targets
        self.native_targets = native_targets
        self.components = components or {}
        self.run_environment = run_environment
        self.run_targets = dict(run_targets or {})

    def resolve_run(self, request: Request, name: str) -> RunSpec:
        if name not in self.run_targets:
            raise ValueError(f"CMake product has no registered run target: {name}")
        builder = self.factory(request, (name,))
        spec = read_cmake_run(
            self.run_targets[name], builder.source_dir, builder.build_dir, request.cmake_profile
        )
        environment = (self.run_environment(name, builder.source_dir)
                       if self.run_environment else os.environ.copy())
        return replace(spec, arguments=(*spec.arguments, *request.arguments), environment=environment)

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action in {"run", "dev"}:
            if len(names) != 1 or names[0] not in self.run_targets:
                raise ValueError("run/dev requires one explicitly registered CMake run target")
        if request.action == "install" and any(name not in self.components for name in names):
            raise ValueError("selected deliverable has no CMake install component")

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        builder = self.factory(request, names)
        result: dict[str, object] = {
            "native_engine": "cmake",
            "source": str(builder.source_dir),
            "build_dir": str(builder.build_dir),
            "native_profile": request.cmake_profile,
            "native_targets": self.native_targets(names),
        }
        if request.action == "metadata":
            result["executables"] = {
                name: str(read_cmake_run(self.run_targets[name], builder.source_dir,
                                        builder.build_dir, request.cmake_profile).executable)
                for name in names if name in self.run_targets
            }
            result["install_components"] = {name: self.components.get(name, ()) for name in names}
            result["dependency_prefix"] = str(builder.repo_config.install_prefix)
        if request.action in {"clean", "rebuild"}:
            result.update(clean_scope="native engineering unit", affected=self.products)
        if request.action == "test":
            result.update(
                test_scope="configured CTest correctness suite",
                test_targets=self.test_targets(names),
            )
        if request.action == "check":
            result["checks"] = ["CMake configure and selected-target compiler checks"]
        if request.action == "install":
            result["install_prefix"] = str(builder.repo_config.install_prefix)
            result["components"] = list(dict.fromkeys(c for n in names for c in self.components[n]))
        return result

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        builder = self.factory(request, names)
        if request.action in {"clean", "rebuild"}:
            validate_generated_path(builder.build_dir, self.root)
        if request.action == "clean":
            return
        if request.action == "run":
            spec = self.resolve_run(request, names[0])
            if not spec.executable.is_file():
                raise RuntimeError(f"built executable is missing: {spec.executable}")
        else:
            require_command("cmake")
            require_command("ninja")
            builder.validate()
        if request.action == "dev" and self.run_environment:
            self.run_environment(names[0], builder.source_dir)

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        builder = self.factory(request, names)
        if request.action == "doctor":
            return  # The read-only preflight is the doctor operation.
        if request.action in {"clean", "rebuild"}:
            remove_generated(builder.build_dir, self.root)
            if request.action == "clean":
                return
        if request.action in {"build", "rebuild", "check", "dev", "install"}:
            targets = self.native_targets(names)
            # Interface libraries have no native build rule. Their owning project
            # supplies concrete prerequisites instead of inventing a fake target.
            builder.build(targets, jobs=request.jobs)
        elif request.action == "test":
            builder.build(self.test_targets(names), jobs=request.jobs)
            run_command(
                [
                    "ctest",
                    "--test-dir",
                    builder.build_dir,
                    "--output-on-failure",
                    "--no-tests=error",
                ],
                env=builder.repo_config.env,
            )
        if request.action == "install":
            components = tuple(dict.fromkeys(c for name in names for c in self.components[name]))
            builder.install(components)
        if request.action in {"run", "dev"}:
            launch_run(self.resolve_run(request, names[0]))
