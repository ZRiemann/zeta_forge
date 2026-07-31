from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import RepoConfig, load_repo_config
from .process import run_command


TARGET_ORDER = ["deps", "grpc", "hpx", "folly", "nng"]
TARGET_DEPENDENCIES = {
    "deps": (),
    "grpc": (),
    "hpx": (),
    "folly": ("grpc",),
    "nng": (),
}
TARGET_ALIASES = {
    "conan": "deps",
}


def target_scripts(forge_root: Path) -> dict[str, Path]:
    return {target: forge_root / "builder" / target / "zbuild.py" for target in TARGET_ORDER}


def format_target_lines() -> str:
    lines: list[str] = []
    for target in TARGET_ORDER:
        dependencies = TARGET_DEPENDENCIES[target]
        if dependencies:
            lines.append(f"  {target} (depends on: {', '.join(dependencies)})")
        else:
            lines.append(f"  {target} (independent)")
    lines.append("  all")
    lines.append("  conan (alias for: deps)")
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
        "  ./zbuild.py conan --BUILD_TYPE=Debug --install\n"
        "  ./zbuild.py deps --BUILD_TYPE=Debug --install\n"
        "  ./zbuild.py grpc --rebuild --install\n"
        "  ./zbuild.py hpx --rebuild --install\n"
        "  ./zbuild.py all --BUILD_TYPE=Debug --continue-on-error\n"
        "  ./zbuild.py prepare-debian"
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
    parser = argparse.ArgumentParser(prog="./zbuild.py all", description="Build all configured targets in sequence")
    parser.add_argument("--BUILD_TYPE", dest="build_type", default="Release", choices=("Release", "Debug"))
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser


def normalize_forward_args(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def run_single(target: str, forwarded_args: list[str], repo_config: RepoConfig) -> int:
    target = TARGET_ALIASES.get(target, target)
    script = target_scripts(repo_config.forge_root)[target]
    run_command([sys.executable, script, *normalize_forward_args(forwarded_args)], cwd=repo_config.forge_root)
    return 0


def run_all(namespace: argparse.Namespace, repo_config: RepoConfig) -> int:
    forwarded = [f"--BUILD_TYPE={namespace.build_type}"]
    if namespace.install:
        forwarded.append("--install")
    if namespace.rebuild:
        forwarded.append("--rebuild")

    failures: list[str] = []
    for target in resolve_build_order():
        try:
            run_single(target, forwarded, repo_config)
        except Exception:
            failures.append(target)
            if not namespace.continue_on_error:
                raise

    if failures:
        raise RuntimeError(f"Build failed for targets: {', '.join(failures)}")
    return 0


def run_prepare(forwarded_args: list[str], repo_config: RepoConfig) -> int:
    script = repo_config.forge_root / "common" / "prepare_debian.py"
    run_command([sys.executable, script, *normalize_forward_args(forwarded_args)], cwd=repo_config.forge_root)
    return 0


def main(script_path: Path) -> int:
    parser = build_parser()
    namespace = parser.parse_args()
    repo_config = load_repo_config(script_path)
    scripts = target_scripts(repo_config.forge_root)

    if namespace.command in {None, "-h", "--help"}:
        parser.print_help()
        return 0
    if namespace.command in scripts or namespace.command in TARGET_ALIASES:
        return run_single(namespace.command, namespace.args, repo_config)
    if namespace.command == "all":
        all_namespace = build_all_parser().parse_args(normalize_forward_args(namespace.args))
        return run_all(all_namespace, repo_config)
    if namespace.command == "prepare-debian":
        return run_prepare(namespace.args, repo_config)
    if namespace.command == "list":
        print(format_target_lines())
        return 0
    parser.error(f"Unknown command: {namespace.command}")
    return 2
