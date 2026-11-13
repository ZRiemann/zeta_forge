"""Shared build values and adapter hooks, independent of the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

if TYPE_CHECKING:
    from argparse import ArgumentParser


@dataclass(frozen=True)
class Product:
    name: str
    engine: str
    description: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class Request:
    action: str
    profile: str
    jobs: int
    options: object
    arguments: tuple[str, ...] = ()

    @property
    def cmake_profile(self) -> str:
        return {"release": "Release", "debug": "Debug"}[self.profile]

    @property
    def cargo_profile(self) -> str:
        return {"release": "release", "debug": "dev"}[self.profile]


class Engine(Protocol):
    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]: ...
    def validate_request(self, request: Request, names: tuple[str, ...]) -> None: ...
    def preflight(self, request: Request, names: tuple[str, ...]) -> None: ...
    def execute(self, request: Request, names: tuple[str, ...]) -> None: ...


@dataclass(frozen=True)
class Project:
    name: str
    products: tuple[Product, ...]
    engines: Mapping[str, Engine]
    defaults: tuple[str, ...]
    run_default: str | None = None
    action_defaults: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    extra_actions: tuple[str, ...] = ()
    add_options: Callable[[ArgumentParser], None] | None = None
    validate_request: Callable[[Request, tuple[Product, ...]], None] | None = None
