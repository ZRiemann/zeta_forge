"""Plan and run an invocation without depending on command-line parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

from .build_contract import Engine, Product, Request
from .build_log import log


@dataclass(frozen=True)
class InvocationStep:
    key: str
    engine: Engine
    names: tuple[str, ...]


def invocation_steps(
    engines: Mapping[str, Engine], products: tuple[Product, ...], request: Request,
) -> tuple[InvocationStep, ...]:
    grouped: dict[str, list[str]] = {}
    for product in products:
        grouped.setdefault(product.engine, []).append(product.name)
    steps = tuple(InvocationStep(key, engines[key], tuple(names)) for key, names in grouped.items())
    for step in steps:
        step.engine.validate_request(request, step.names)
    return steps


def describe_invocation(
    steps: tuple[InvocationStep, ...], request: Request,
) -> list[dict[str, object]]:
    return [
        dict(engine=step.key, targets=step.names, **step.engine.describe(request, step.names))
        for step in steps
    ]


def execute_invocation(
    steps: tuple[InvocationStep, ...], request: Request,
) -> Iterator[InvocationStep]:
    """Yield completed steps. Preparation is an optional adapter capability."""
    for step in steps:
        step.engine.preflight(request, step.names)
    for step in steps:
        prepare = getattr(step.engine, "prepare", None)
        if prepare is not None:
            prepare(request, step.names)
    for index, step in enumerate(steps, 1):
        log(
            "I",
            f"step={index}/{len(steps)} action={request.action} engine={step.key} profile={request.profile}",
        )
        step.engine.execute(request, step.names)
        yield step
