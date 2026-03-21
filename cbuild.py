#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "common"))

from zeta_forge.config import load_repo_config
from zeta_forge.process import run_command


TARGET_SCRIPTS = {
    "hpx": REPO_ROOT / "builder" / "hpx" / "cbuild.py",
    "folly": REPO_ROOT / "builder" / "folly" / "cbuild.py",
    "abseil-cpp": REPO_ROOT / "builder" / "abseil-cpp" / "cbuild.py",
    "nng": REPO_ROOT / "builder" / "nng" / "cbuild.py",
    "zpp": REPO_ROOT / "builder" / "zpp" / "cbuild.py",
}
TARGET_ORDER = ["hpx", "folly", "abseil-cpp", "nng", "zpp"]
TARGET_DEPENDENCIES = {
    "hpx": (),
    "folly": (),
    "abseil-cpp": (),
    "nng": (),
    "zpp": ("hpx", "folly", "abseil-cpp", "nng"),
}


def format_target_lines() -> str:
    lines: list[str] = []
    for target in TARGET_ORDER:
        dependencies = TARGET_DEPENDENCIES[target]
        if dependencies:
            lines.append(f"  {target} (depends on: {', '.join(dependencies)})")
        else:
            lines.append(f"  {target} (independent)")
    lines.append("  all")
    lines.append("  prepare-debian")
    lines.append("  list")
    return "\n".join(lines)


def resolve_build_order() -> list[str]:
    remaining = {target: set(dependencies) for target, dependencies in TARGET_DEPENDENCIES.items()}
    resolved: list[str] = []

    while remaining:
        ready = [target for target in TARGET_ORDER if target in remaining and not remaining[target]]
        if not ready:
            unresolved = ", ".join(sorted(remaining))
            raise RuntimeError(f"Circular or unresolved target dependencies: {unresolved}")

        for target in ready:
            resolved.append(target)
            remaining.pop(target)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)

    return resolved


def help_epilog() -> str:
    build_order = " -> ".join(resolve_build_order())
    return (
        "Targets:\n"
        f"{format_target_lines()}\n\n"
        "Dependency-aware all order:\n"
        f"  {build_order}\n\n"
        "Examples:\n"
        "  ./cbuild.py hpx --rebuild --install\n"
        "  ./cbuild.py zpp --rebuild --no-tests\n"
        "  ./cbuild.py all --BUILD_TYPE=Debug --continue-on-error\n"
        "  ./cbuild.py prepare-debian -- --python-version 3.12"
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified zeta_forge build dispatcher",
        epilog=help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", help="Target name, all, prepare-debian, or list")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def build_all_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="./cbuild.py all", description="Build all configured targets in sequence")
    parser.add_argument("--BUILD_TYPE", dest="build_type", default="Release", choices=("Release", "Debug"))
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser


def print_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()


def normalize_forward_args(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def run_single(target: str, forwarded_args: list[str], repo_root: Path) -> int:
    script = TARGET_SCRIPTS[target]
    run_command([sys.executable, script, *normalize_forward_args(forwarded_args)], cwd=repo_root)
    return 0


def run_all(namespace: argparse.Namespace, repo_root: Path) -> int:
    forwarded = [f"--BUILD_TYPE={namespace.build_type}"]
    if namespace.install:
        forwarded.append("--install")
    if namespace.rebuild:
        forwarded.append("--rebuild")

    failures: list[str] = []
    for target in resolve_build_order():
        try:
            run_single(target, forwarded, repo_root)
        except Exception:
            failures.append(target)
            if not namespace.continue_on_error:
                raise

    if failures:
        raise RuntimeError(f"Build failed for targets: {', '.join(failures)}")
    return 0


def run_prepare(forwarded_args: list[str], repo_root: Path) -> int:
    script = repo_root / "common" / "prepare_debian.py"
    run_command([sys.executable, script, *normalize_forward_args(forwarded_args)], cwd=repo_root)
    return 0


def main() -> int:
    parser = build_parser()
    namespace = parser.parse_args()
    repo_config = load_repo_config(Path(__file__))

    if namespace.command in {None, "-h", "--help"}:
        print_help(parser)
        return 0
    if namespace.command in TARGET_SCRIPTS:
        return run_single(namespace.command, namespace.args, repo_config.repo_root)
    if namespace.command == "all":
        all_namespace = build_all_parser().parse_args(normalize_forward_args(namespace.args))
        return run_all(all_namespace, repo_config.repo_root)
    if namespace.command == "prepare-debian":
        return run_prepare(namespace.args, repo_config.repo_root)
    if namespace.command == "list":
        print(format_target_lines())
        return 0
    parser.error(f"Unknown command: {namespace.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
