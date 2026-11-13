"""Pure planning for Forge's native deliverables."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zeta_forge.build_cli import Request

NATIVE_ACTIONS = ("doctor", "check", "build", "rebuild", "clean", "install")
BUILD_ACTIONS = frozenset(("check", "build", "rebuild"))


@dataclass(frozen=True)
class TargetSpec:
    name: str
    prerequisites: tuple[str, ...] = ()
    uses_conan: bool = False


TARGETS = (
    TargetSpec("deps", uses_conan=True),
    TargetSpec("grpc"),
    TargetSpec("hpx", uses_conan=True),
    TargetSpec("folly", prerequisites=("grpc",), uses_conan=True),
    TargetSpec("nng"),
)


def staging_directory(root: Path, profile: str, target: str) -> Path:
    return root / "build" / "staging" / profile / target


@dataclass(frozen=True)
class NativePlan:
    action: str
    profile: str
    native_profile: str
    jobs: int
    root: Path
    install_prefix: Path | None
    selected: tuple[str, ...]
    order: tuple[str, ...]
    clean_scope: tuple[str, ...]
    publish_scope: tuple[str, ...]
    conan_scope: tuple[str, ...]

    @property
    def build_scope(self) -> tuple[str, ...]:
        return self.order if self.action in BUILD_ACTIONS else ()

    def build_directory(self, target: str) -> Path:
        return self.root / "builder" / target / "build" / self.native_profile

    def staging(self, target: str) -> Path:
        return staging_directory(self.root, self.profile, target)


def plan_native(
    request: Request,
    names: tuple[str, ...],
    root: Path,
    install_prefix: Path,
    *,
    targets: tuple[TargetSpec, ...] = TARGETS,
) -> NativePlan:
    action = request.action
    if action not in (*NATIVE_ACTIONS, "list", "metadata"):
        raise ValueError(f"unsupported Forge native action: {action}")
    specs = {target.name: target for target in targets}
    if len(specs) != len(targets):
        raise ValueError("duplicate Forge native target specification")
    selected = tuple(dict.fromkeys(names))
    unknown = set(selected) - specs.keys()
    if unknown:
        raise ValueError(f"unknown Forge native target: {', '.join(sorted(unknown))}")

    ordered: dict[str, None] = {}
    visiting: list[str] = []
    include_prerequisites = action in BUILD_ACTIONS or action == "doctor"

    def visit(name: str) -> None:
        if name in ordered:
            return
        if name in visiting:
            raise ValueError(f"Forge native prerequisite cycle: {' -> '.join((*visiting, name))}")
        if name not in specs:
            raise ValueError(f"unknown prerequisite {name} for Forge native target {visiting[-1]}")
        visiting.append(name)
        if include_prerequisites:
            for prerequisite in specs[name].prerequisites:
                visit(prerequisite)
        visiting.pop()
        ordered[name] = None

    for name in specs:
        if name in selected:
            visit(name)
    order = tuple(ordered)
    is_install = action == "install"
    return NativePlan(
        action=action,
        profile=request.profile,
        native_profile=request.cmake_profile,
        jobs=request.jobs,
        root=root,
        install_prefix=install_prefix if is_install else None,
        selected=selected,
        order=order,
        clean_scope=selected if action in {"clean", "rebuild"} else (),
        publish_scope=order if is_install else (),
        conan_scope=tuple(name for name in order if specs[name].uses_conan)
        if include_prerequisites
        else (),
    )
