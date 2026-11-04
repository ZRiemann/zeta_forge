"""Language-neutral description of one foreground application invocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .build_ops import run_program


@dataclass(frozen=True)
class RunSpec:
    executable: Path
    arguments: tuple[str, ...]
    working_directory: Path
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.environment is not None:
            object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


def launch_run(spec: RunSpec) -> None:
    """Reuse the existing process-group and interruption owner; do not log arguments."""
    run_program(
        [spec.executable, *spec.arguments],
        cwd=spec.working_directory,
        env=dict(spec.environment) if spec.environment is not None else None,
    )
