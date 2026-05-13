from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .process import run_command, shell_join


@dataclass(frozen=True)
class RunTarget:
    name: str
    run_name: str
    cmake_file: Path
    build_dir: Path
    args: tuple[str, ...]
    resolved_args: tuple[str, ...]
    executable_path: Path
    working_dir: Path
    cmake_command: tuple[str, ...]


def _strip_line_comment(line: str) -> str:
    in_quote = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if char == "#" and not in_quote:
            return line[:index]
    return line


def _find_call_bodies(text: str, function_name: str) -> list[str]:
    bodies: list[str] = []
    index = 0

    while True:
        found = text.find(function_name, index)
        if found < 0:
            return bodies

        before = text[found - 1] if found > 0 else ""
        after_index = found + len(function_name)
        after = text[after_index] if after_index < len(text) else ""
        if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
            index = after_index
            continue

        open_index = after_index
        while open_index < len(text) and text[open_index].isspace():
            open_index += 1
        if open_index >= len(text) or text[open_index] != "(":
            index = after_index
            continue

        depth = 1
        in_quote = False
        escaped = False
        body_start = open_index + 1
        cursor = body_start
        while cursor < len(text):
            char = text[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = not in_quote
            elif not in_quote and char == "(":
                depth += 1
            elif not in_quote and char == ")":
                depth -= 1
                if depth == 0:
                    bodies.append(text[body_start:cursor])
                    index = cursor + 1
                    break
            cursor += 1
        else:
            return bodies


def _tokenize_cmake_words(body: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0

    while cursor < len(body):
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor >= len(body):
            break

        if body[cursor] == '"':
            cursor += 1
            token_chars: list[str] = []
            escaped = False
            while cursor < len(body):
                char = body[cursor]
                if escaped:
                    token_chars.append(char)
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    cursor += 1
                    break
                else:
                    token_chars.append(char)
                cursor += 1
            tokens.append("".join(token_chars))
            continue

        start = cursor
        while cursor < len(body) and not body[cursor].isspace():
            cursor += 1
        tokens.append(body[start:cursor])

    return tokens


def _replace_known_variables(value: str, variables: dict[str, Path]) -> str:
    resolved = value
    for name, path in variables.items():
        resolved = resolved.replace(f"${{{name}}}", str(path))
    return resolved


def _parse_run_target(cmake_file: Path, source_dir: Path, build_dir: Path, body: str) -> RunTarget | None:
    tokens = _tokenize_cmake_words(body)
    if not tokens:
        return None

    name = tokens[0]
    arg_tokens: Sequence[str]
    if "ARGS" in tokens[1:]:
        args_index = tokens.index("ARGS", 1)
        arg_tokens = tokens[args_index + 1 :]
    else:
        arg_tokens = ()

    relative_dir = cmake_file.parent.relative_to(source_dir)
    working_dir = build_dir / relative_dir
    variables = {
        "PROJECT_SOURCE_DIR": source_dir,
        "CMAKE_SOURCE_DIR": source_dir,
        "CMAKE_CURRENT_SOURCE_DIR": cmake_file.parent,
        "CMAKE_CURRENT_LIST_DIR": cmake_file.parent,
        "CMAKE_CURRENT_BINARY_DIR": working_dir,
    }
    resolved_args = tuple(_replace_known_variables(arg, variables) for arg in arg_tokens)
    run_name = f"run_{name}"
    executable_path = working_dir / name
    cmake_command = ("cmake", "--build", str(build_dir), "--target", run_name)

    return RunTarget(
        name=name,
        run_name=run_name,
        cmake_file=cmake_file,
        build_dir=build_dir,
        args=tuple(arg_tokens),
        resolved_args=resolved_args,
        executable_path=executable_path,
        working_dir=working_dir,
        cmake_command=cmake_command,
    )


def discover_run_targets(source_dir: Path, build_dir: Path) -> list[RunTarget]:
    source_dir = source_dir.resolve()
    build_dir = build_dir.resolve()
    targets: list[RunTarget] = []

    for cmake_file in sorted(source_dir.rglob("CMakeLists.txt")):
        if any(part in {"build", "build_debug", ".git"} for part in cmake_file.relative_to(source_dir).parts[:-1]):
            continue
        raw_text = cmake_file.read_text(encoding="utf-8", errors="ignore")
        text = "\n".join(_strip_line_comment(line) for line in raw_text.splitlines())
        for body in _find_call_bodies(text, "add_run_target"):
            target = _parse_run_target(cmake_file.resolve(), source_dir, build_dir, body)
            if target is not None:
                targets.append(target)

    return targets


def format_run_target_line(target: RunTarget) -> str:
    command = shell_join(target.cmake_command)
    return f"{target.name};\t{command}"


def print_run_targets(targets: Sequence[RunTarget]) -> None:
    for target in targets:
        print(format_run_target_line(target))


def find_run_target(targets: Sequence[RunTarget], name: str) -> RunTarget:
    for target in targets:
        if target.name == name:
            return target
    available = ", ".join(target.name for target in targets) or "<none>"
    raise RuntimeError(f"Unknown run entry: {name}\nAvailable run entries: {available}")


def require_existing_build_tree(build_dir: Path) -> None:
    cache_path = build_dir / "CMakeCache.txt"
    if not cache_path.is_file():
        raise RuntimeError(
            f"CMake build tree not found: {build_dir}\n"
            "Run the project zbuild.py build command first for the selected BUILD_TYPE."
        )


def require_existing_executable(target: RunTarget) -> None:
    if not target.executable_path.is_file():
        raise RuntimeError(
            f"Run executable not found: {target.executable_path}\n"
            "Build the project first for the selected BUILD_TYPE, or use the printed CMake command to build the run target."
        )
    if not os.access(target.executable_path, os.X_OK):
        raise RuntimeError(f"Run executable is not executable: {target.executable_path}")


def run_existing_target(target: RunTarget) -> int:
    require_existing_build_tree(target.build_dir)
    require_existing_executable(target)
    run_command((str(target.executable_path), *target.resolved_args), cwd=target.working_dir)
    return 0


def build_type_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--BUILD_TYPE", dest="build_type", default="Release", choices=("Release", "Debug"))
    return parser
