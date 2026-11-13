"""Keep Forge capability imports directed, including function-local imports."""

from __future__ import annotations

import ast
import os
import unittest
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImportScanner(ast.NodeVisitor):
    def __init__(self, package: str) -> None:
        self.package = package
        self.runtime: set[str] = set()
        self.type_only: set[str] = set()
        self.type_checking = False

    def visit_If(self, node: ast.If) -> None:
        if ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            previous = self.type_checking
            self.type_checking = True
            for child in node.body:
                self.visit(child)
            self.type_checking = previous
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        imports = self.type_only if self.type_checking else self.runtime
        imports.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = node.module or ""
        if node.level:
            target = resolve_name("." * node.level + target, self.package)
        imports = self.type_only if self.type_checking else self.runtime
        imports.add(target)
        imports.update(f"{target}.{alias.name}" for alias in node.names)


def module_sources() -> dict[str, Path]:
    sources = {}
    for directory, base in ((ROOT / "common/zeta_forge", ROOT / "common"),
                            (ROOT / "builder", ROOT)):
        for current, subdirs, files in os.walk(directory):
            subdirs[:] = [name for name in subdirs if name not in {"build", "__pycache__"}]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = Path(current) / filename
                relative = path.relative_to(base)
                if path.stem == "__init__":
                    name = ".".join(relative.parent.parts)
                else:
                    name = ".".join(relative.with_suffix("").parts)
                sources[name] = path
    return sources


class BuildArchitectureTests(unittest.TestCase):
    def test_runtime_dependencies_are_acyclic_and_stay_below_adapters(self) -> None:
        sources = module_sources()
        graph: dict[str, set[str]] = {}
        for name, path in sources.items():
            package = name if path.stem == "__init__" else name.rpartition(".")[0]
            scanner = ImportScanner(package)
            scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
            graph[name] = scanner.runtime & sources.keys()
            self.assertNotIn("zeta_forge.build_cli", graph[name], name)

        adapters = {name for name in sources if name.endswith("_engine")}
        entrypoints = adapters | {"builder.project", "zeta_forge.rust_audit", "zeta_forge.rust_updates"}
        for name in sources.keys() - entrypoints:
            self.assertFalse(graph[name] & adapters, (name, graph[name] & adapters))
            if name.startswith("zeta_forge."):
                self.assertFalse(
                    {edge for edge in graph[name] if edge.startswith("builder.")}, name,
                )

        visited: set[str] = set()
        path: list[str] = []

        def visit(module: str) -> None:
            self.assertNotIn(module, path, " -> ".join((*path, module)))
            if module in visited:
                return
            path.append(module)
            for dependency in sorted(graph[module]):
                visit(dependency)
            path.pop()
            visited.add(module)

        for name in sorted(graph):
            visit(name)

    def test_scanner_resolves_package_and_deferred_imports(self) -> None:
        scanner = ImportScanner("zeta_forge")
        scanner.visit(ast.parse(
            "from . import native_transaction as tx\n"
            "if TYPE_CHECKING:\n"
            "    from .rust_project import RustProject\n"
            "def later():\n"
            "    from .build_cli import Request\n"
        ))
        self.assertIn("zeta_forge.native_transaction", scanner.runtime)
        self.assertIn("zeta_forge.build_cli", scanner.runtime)
        self.assertIn("zeta_forge.rust_project", scanner.type_only)
        self.assertNotIn("zeta_forge.rust_project", scanner.runtime)
