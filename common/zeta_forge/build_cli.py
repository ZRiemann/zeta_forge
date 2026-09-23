"""The action-first CLI shared by ZetaX project launchers.

Products select native engineering units; native tools still own their DAGs.
Project construction, descriptions and argument validation must be read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Protocol, Sequence


ACTIONS = (
    "list",
    "doctor",
    "metadata",
    "check",
    "build",
    "rebuild",
    "test",
    "run",
    "dev",
    "clean",
    "install",
    "fetch",
)


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
    options: argparse.Namespace
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
    add_options: Callable[[argparse.ArgumentParser], None] | None = None


def log(level: str, message: str) -> None:
    print(f"{datetime.now():%m-%d:%H:%M:%S.%f} [{level}] [zbuild] {message}", flush=True)


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def parser_for(project: Project) -> argparse.ArgumentParser:
    install_help = (
        " Install requires explicit deliverables."
        if project.action_defaults.get("install") == ()
        else ""
    )
    parser = argparse.ArgumentParser(
        prog="./zbuild.py",
        allow_abbrev=False,
        description=f"Build, validate and use {project.name} deliverables.",
        epilog=(
            "Defaults: release profile; project deliverables unless named explicitly. "
            "run uses existing artifacts. dev builds and runs once unless its native "
            "engine supplies a development watcher. Use list for capabilities and "
            "--dry-run for the execution/cleanup scope. Prepare host tools separately."
            + install_help
        ),
    )
    parser.add_argument("command", choices=(*ACTIONS, *project.extra_actions), nargs="?")
    parser.add_argument("targets", nargs="*", help="deliverable names shown by list")
    parser.add_argument("--profile", choices=("release", "debug"), default="release")
    parser.add_argument("--dry-run", action="store_true", help="show the plan without executing it")
    parser.add_argument("-j", "--jobs", type=positive_int, default=max(1, os.cpu_count() or 1))
    if project.add_options:
        project.add_options(parser)
    return parser


def select(project: Project, action: str, supplied: Sequence[str]) -> tuple[Product, ...]:
    catalog = {product.name: product for product in project.products}
    if len(catalog) != len(project.products):
        raise ValueError("duplicate deliverable names in project definition")
    if supplied:
        names = tuple(dict.fromkeys(supplied))
    elif action == "list":
        names = tuple(catalog)
    elif action in {"run", "dev"}:
        if project.run_default is None:
            raise ValueError("an explicit runnable deliverable is required")
        names = (project.run_default,)
    else:
        names = project.action_defaults.get(action, project.defaults)
    if not names:
        if action == "install":
            raise ValueError("install requires at least one explicit deliverable")
        raise ValueError(f"no deliverables defined for {action}")
    if action in {"run", "dev"} and len(names) != 1:
        raise ValueError(f"{action} accepts exactly one deliverable")
    selected = []
    for name in names:
        if name not in catalog:
            raise ValueError(f"unknown deliverable: {name}")
        product = catalog[name]
        if action not in {"list", "metadata"} and action not in product.actions:
            raise ValueError(f"{action} is not supported by {name}")
        selected.append(product)
    return tuple(selected)


def cli(project: Project, argv: Sequence[str] | None = None) -> int:
    """Validate the entire plan before executing the first engineering unit."""
    parser = parser_for(project)
    arguments = list(sys.argv[1:] if argv is None else argv)
    program_arguments: tuple[str, ...] = ()
    if "--" in arguments:
        separator = arguments.index("--")
        program_arguments = tuple(arguments[separator + 1 :])
        arguments = arguments[:separator]
    try:
        options = parser.parse_intermixed_args(arguments)
        if options.command is None:
            parser.print_help()
            return 0
        if program_arguments and options.command not in {"run", "dev"}:
            parser.error("program arguments apply only to run/dev")
        selected = select(project, options.command, options.targets)
        request = Request(
            options.command, options.profile, options.jobs, options, program_arguments
        )
        grouped: dict[str, list[str]] = {}
        for product in selected:
            grouped.setdefault(product.engine, []).append(product.name)
        steps = [(key, tuple(names)) for key, names in grouped.items()]
        for key, names in steps:
            project.engines[key].validate_request(request, names)
    except ValueError as error:
        print(f"zbuild: {error}", file=sys.stderr)
        return 2
    except SystemExit as error:
        return int(error.code)

    if request.action == "list":
        for product in selected:
            default = " default" if product.name in project.defaults else ""
            print(f"{product.name} [{product.engine}]{default}: {product.description}")
            print(f"  actions: {', '.join(product.actions)}")
        for key, names in steps:
            try:
                info = project.engines[key].describe(request, names)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"zbuild: {error}", file=sys.stderr)
                return 1
            native = info.get(
                "source", info.get("project", info.get("target_dir", info.get("build_dirs", key)))
            )
            print(f"  native project [{key}]: {native}")
        print(f"default targets: {', '.join(project.defaults)}")
        for action, targets in project.action_defaults.items():
            default = ", ".join(targets) if targets else "(explicit selection required)"
            print(f"default {action}: {default}")
        print(f"default run: {project.run_default or '(explicit selection required)'}")
        return 0

    started = time.monotonic()
    succeeded = 0
    status = 0
    try:
        plan = [
            dict(engine=key, targets=names, **project.engines[key].describe(request, names))
            for key, names in steps
        ]
        if request.action == "metadata":
            print(
                json.dumps(
                    dict(project=project.name, profile=request.profile, steps=plan), indent=2
                )
            )
            return 0
        for step in plan:
            log("I", json.dumps(step))
        if options.dry_run:
            return 0
        for key, names in steps:
            project.engines[key].preflight(request, names)
        for index, (key, names) in enumerate(steps, 1):
            log(
                "I",
                f"step={index}/{len(steps)} action={request.action} engine={key} profile={request.profile}",
            )
            project.engines[key].execute(request, names)
            succeeded += 1
    except KeyboardInterrupt:
        status = 130
        log("E", "interrupted")
    except (OSError, RuntimeError, ValueError) as error:
        status = 1
        log("E", str(error))
    finally:
        print(
            f"ZETA_BUILD_DONE status={status} action={request.action} profile={request.profile} "
            f"succeeded={succeeded} failed={int(status != 0)} "
            f"elapsed_seconds={time.monotonic() - started:.3f}",
            file=sys.stderr if request.action == "metadata" else sys.stdout,
            flush=True,
        )
    return status
