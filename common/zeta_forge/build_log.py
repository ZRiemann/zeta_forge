"""Stable rendering for build-tool log messages."""

from datetime import datetime


def log(level: str, message: str) -> None:
    print(f"{datetime.now():%m-%d:%H:%M:%S.%f} [{level}] [zbuild] {message}", flush=True)
