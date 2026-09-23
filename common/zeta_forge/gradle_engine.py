"""A Gradle Android deliverable; device and deployment workflows stay outside."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from .build_cli import Request, log
from .build_ops import remove_generated, validate_generated_path
from .process import require_command, run_command

GRADLE_ACTIONS = ("doctor", "check", "build", "rebuild", "test", "clean")


class GradleEngine:
    def __init__(
        self, root: Path, directory: Path, *, environment: Callable[[Request], dict[str, str]]
    ) -> None:
        self.root = root
        self.directory = directory
        self.application = directory / "app"
        self.environment = environment

    def validate_request(self, request: Request, names: tuple[str, ...]) -> None:
        pass

    def describe(self, request: Request, names: tuple[str, ...]) -> Mapping[str, object]:
        result: dict[str, object] = {
            "native_engine": "gradle",
            "project": str(self.directory),
            "variant": request.cmake_profile,
            "artifacts": str(self.application / "build/outputs/apk" / request.profile),
        }
        if request.action in {"clean", "rebuild"}:
            result.update(
                clean_scope="Android app build and .cxx intermediates across variants",
                preserved="other profile's final APK directory",
            )
        return result

    def preflight(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action in {"clean", "rebuild"}:
            self.validate_cleanup()
        if request.action == "clean":
            return
        wrapper = self.directory / "gradlew"
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise RuntimeError(f"Gradle wrapper is missing or not executable: {wrapper}")
        require_command("java")
        environment = self.environment(request)
        sdk = environment.get("ANDROID_SDK_ROOT") or environment.get("ANDROID_HOME")
        if not sdk or not Path(sdk).is_dir():
            raise RuntimeError(
                "Android SDK is missing; prepare the Android host environment separately"
            )

    def validate_cleanup(self) -> None:
        for path in (self.application / "build", self.application / ".cxx"):
            validate_generated_path(path, self.root)
        outputs = self.application / "build/outputs/apk"
        if outputs.exists() and any(path.is_symlink() for path in outputs.rglob("*")):
            raise RuntimeError("Android APK outputs must not contain symlinks")

    def clean(self, request: Request) -> None:
        self.validate_cleanup()
        other = "debug" if request.profile == "release" else "release"
        preserved = self.application / "build/outputs/apk" / other
        if preserved.is_symlink() or not preserved.resolve().is_relative_to(self.root.resolve()):
            raise RuntimeError("unsafe Android artifact path")
        # Keep final APKs while invalidating Gradle/NDK shared intermediates.
        temporary = Path(tempfile.mkdtemp(prefix="zbuild-apk-"))
        backup = temporary / other
        try:
            if preserved.is_dir():
                shutil.copytree(preserved, backup)
            try:
                remove_generated(self.application / "build", self.root)
                remove_generated(self.application / ".cxx", self.root)
            finally:
                if backup.exists():
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(backup, preserved, dirs_exist_ok=True)
        except (OSError, RuntimeError, KeyboardInterrupt):
            # If restoration fails, never destroy the last recoverable APK copy.
            log("E", f"Android cleanup failed; recovery backup retained at {temporary}")
            raise
        else:
            shutil.rmtree(temporary)

    def execute(self, request: Request, names: tuple[str, ...]) -> None:
        if request.action == "doctor":
            return
        if request.action in {"clean", "rebuild"}:
            self.clean(request)
            if request.action == "clean":
                return
        variant = request.cmake_profile
        task = {
            "build": f"assemble{variant}",
            "rebuild": f"assemble{variant}",
            "check": f"lint{variant}",
            "test": f"test{variant}UnitTest",
        }[request.action]
        run_command(
            [
                self.directory / "gradlew",
                "--no-daemon",
                f"--max-workers={request.jobs}",
                f":app:{task}",
            ],
            cwd=self.directory,
            env=self.environment(request),
        )
