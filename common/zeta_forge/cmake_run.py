"""Read CMake-generated runtime metadata for explicitly registered products."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .run_spec import RunSpec


@dataclass(frozen=True)
class CMakeRunTarget:
    native_target: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", self.native_target):
            raise ValueError("CMake run target must be a native target name, not a path")
        if self.native_target in {".", ".."}:
            raise ValueError("CMake run target must not be a relative path")


def read_cmake_run(
    target: CMakeRunTarget, source_dir: Path, build_dir: Path, configuration: str,
) -> RunSpec:
    if configuration not in {"Debug", "Release"}:
        raise ValueError("unsupported CMake run configuration")
    source_dir = source_dir.resolve()
    build_dir = build_dir.resolve()
    metadata = build_dir / ".zeta-run" / configuration / target.native_target

    def read_field(path: Path) -> str:
        name = path.name
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError(f"oversized CMake run field: {name}")
            with path.open(encoding="utf-8", newline="") as stream:
                value = stream.read()
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                f"CMake run metadata unavailable for {target.native_target}: {path}; "
                "reconfigure this build tree with the current Forge CMake helpers"
            ) from error
        if "\0" in value:
            raise ValueError(f"invalid CMake run field: {name}")
        return value

    def field(name: str) -> str:
        return read_field(metadata / name)

    token_path = build_dir / ".zeta-run" / "configuration-token"
    token = read_field(token_path)
    if not re.fullmatch(r"[0-9a-f]{32}", token) or field("configuration-token") != token:
        raise ValueError("stale CMake run metadata; reconfigure this build tree")
    if field("schema") != "1":
        raise ValueError("unsupported CMake run metadata schema")
    if (field("target") != target.native_target or field("configuration") != configuration
            or Path(field("source")).resolve() != source_dir
            or Path(field("build")).resolve() != build_dir):
        raise ValueError("CMake run metadata identity mismatch; reconfigure this build tree")
    count = field("argc")
    if not count.isascii() or not count.isdecimal() or len(count) > 4 or int(count) > 1024:
        raise ValueError("invalid CMake run argument count")
    executable = Path(field("executable"))
    directory = Path(field("cwd"))
    if not executable.is_absolute() or not directory.is_absolute():
        raise ValueError("CMake run paths must be absolute")
    arguments = tuple(field(f"arg-{i}") for i in range(int(count)))
    if read_field(token_path) != token:
        raise ValueError("CMake configuration changed while resolving run metadata; retry")
    return RunSpec(executable, arguments, directory)
