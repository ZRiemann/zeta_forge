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
from typing import Sequence

from .build_contract import Engine as Engine
from .build_contract import Product as Product
from .build_contract import Project as Project
from .build_contract import Request as Request
from .build_invocation import describe_invocation, execute_invocation, invocation_steps
from .build_log import log as log

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
            "--dry-run for the execution/cleanup scope. Prepare host packages and "
            "toolchains separately; Rust deliveries prepare approved Cargo tools."
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
    """Validate and prepare the entire plan before executing engineering units."""
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
        if project.validate_request is not None:
            project.validate_request(request, selected)
        steps = invocation_steps(project.engines, selected, request)
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
        for step in steps:
            try:
                info = step.engine.describe(request, step.names)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"zbuild: {error}", file=sys.stderr)
                return 1
            native = info.get(
                "source", info.get("project", info.get("target_dir", info.get("build_dirs", step.key)))
            )
            print(f"  native project [{step.key}]: {native}")
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
        plan = describe_invocation(steps, request)
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
        for _ in execute_invocation(steps, request):
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
