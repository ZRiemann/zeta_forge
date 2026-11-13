"""Content identity and sanitized profile evidence for Conan resolution."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .process import run_command
from .state_io import canonical_json as canonical_json
from .state_io import digest_bytes as digest_bytes
from .state_io import digest_json as digest_json
from .state_io import file_digest as file_digest

PROFILE_INCLUDE = re.compile(r"^\s*include\(([^)]+)\)\s*$", re.MULTILINE)
JINJA_INCLUDE = re.compile(r'{%\s*(?:include|import|from|extends)\s+["\']([^"\']+)["\']')
CMAKE_INCLUDE = re.compile(r'^\s*include\(\s*(?:"([^"]+)"|([^\s)]+))', re.IGNORECASE | re.MULTILINE)
TRANSPORT_KEY = re.compile(r"proxy|password|secret|token|credential|auth", re.IGNORECASE)
BUILD_ENV_KEYS = (
    "CC", "CXX", "CPP", "AR", "RANLIB", "LD", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
)
SAFE_CONF = (
    "tools.build:",
    "tools.cmake.cmaketoolchain:",
    "tools.cmake.cmakedeps:",
    "tools.cmake:cmake_program",
)


def sensitive_key(name: str) -> bool:
    return bool(TRANSPORT_KEY.search(name))


def ignored_setting(name: str) -> bool:
    name = name.strip()
    return (
        name.endswith("tools.build:jobs")
        or name.startswith(("core.net:", "core.net."))
        or sensitive_key(name)
    )


def safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: safe_value(item) for key, item in value.items() if not sensitive_key(key)}
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?<=://)[^/\s]+@", "", value)
        if re.search(r"(?:password|token|secret)\s*=", value, re.I):
            return "<redacted>"
    return value


def sanitize_profiles(profiles: object) -> dict[str, object]:
    if not isinstance(profiles, dict) or set(profiles) != {"host", "build"}:
        raise RuntimeError("Unsupported Conan profile JSON; expected host and build profiles")
    result = {}
    for context, profile in profiles.items():
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("settings"), dict)
            or not isinstance(profile.get("conf"), dict)
        ):
            raise RuntimeError(f"Unsupported Conan {context} profile JSON")
        saved = {}
        for section, data in profile.items():
            if section not in {"conf", "build_env", "run_env"}:
                saved[section] = safe_value(data)
        saved["conf"] = {
            key: safe_value(value) if key.startswith(SAFE_CONF) else {"sha256": digest_json(safe_value(value))}
            for key, value in profile["conf"].items()
            if not ignored_setting(key)
        }
        # Effective profile environments can include credentials and shell fragments.
        # Keep build identity via a digest, with transport assignments filtered out.
        for section in ("build_env", "run_env"):
            if section in profile:
                text = str(profile[section])
                content = [
                    line for line in text.splitlines()
                    if not ignored_setting(line.split("=", 1)[0].strip())
                ]
                saved[section] = {"sha256": digest_json(safe_value(content))}
        result[context] = saved
    return result


def conan_probe(command: list[object], cwd: Path, env: dict[str, str]) -> str:
    return run_command(command, cwd=cwd, env=env, capture_output=True).stdout.strip()


def profile_inputs(home: Path, env: Mapping[str, str]) -> dict[str, str | None]:
    global_conf = home / "global.conf"
    defaults = {}
    if global_conf.is_file():
        for line in global_conf.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in {"core:default_profile", "core:default_build_profile"}:
                defaults[key.strip()] = value.strip()
    profiles = home / "profiles"
    pending = [
        profiles / env.get("CONAN_DEFAULT_PROFILE", defaults.get("core:default_profile", "default")),
        profiles / env.get(
            "CONAN_DEFAULT_BUILD_PROFILE", defaults.get("core:default_build_profile", "default"),
        ),
    ]
    identity: dict[str, str | None] = {}
    while pending:
        path = pending.pop().resolve()
        if str(path) in identity:
            continue
        if not path.is_file():
            identity[str(path)] = None
            continue
        content = path.read_text(encoding="utf-8")
        lines = []
        section = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
            elif stripped and not stripped.startswith("#") and not ignored_setting(
                stripped.split("=", 1)[0]
            ):
                if section:
                    lines.append(section)
                    section = ""
                lines.append(safe_value(line))
        identity[str(path)] = digest_json(lines)
        for name in (*PROFILE_INCLUDE.findall(content), *JINJA_INCLUDE.findall(content)):
            relative = path.parent / name.strip()
            pending.append(relative if relative.is_file() else profiles / name.strip())
    for relative in ("settings.yml", "settings_user.yml", "extensions/plugins/profile.py"):
        path = home / relative
        identity[str(path)] = file_digest(path) if path.is_file() else None
    return identity


def toolchain_inputs(toolchains: list[Path]) -> dict[str, str | None]:
    pending = list(toolchains)
    identity: dict[str, str | None] = {}
    while pending:
        path = pending.pop().resolve()
        if str(path) in identity:
            continue
        identity[str(path)] = file_digest(path) if path.is_file() else None
        if path.is_file():
            for quoted, plain in CMAKE_INCLUDE.findall(path.read_text(encoding="utf-8")):
                name = quoted or plain
                name = name.replace("${CMAKE_CURRENT_LIST_DIR}", str(path.parent))
                name = name.replace("${CMAKE_CURRENT_LIST_FILE}", str(path))
                if "$" not in name and ("/" in name or name.endswith(".cmake")):
                    pending.append(path.parent / name)
    return identity


def compiler_inputs(profiles: dict, env: dict[str, str]) -> dict[str, object]:
    commands = {env[key] for key in ("CC", "CXX") if env.get(key)}
    for context in profiles.values():
        settings = context["settings"]
        conf = context["conf"]
        for key, value in conf.items():
            if key.endswith("tools.build:compiler_executables") and isinstance(value, dict):
                commands.update(command for command in value.values() if isinstance(command, str))
        compiler = settings.get("compiler")
        if compiler == "gcc":
            commands.update(("gcc", "g++"))
        elif compiler in {"clang", "apple-clang"}:
            commands.update(("clang", "clang++"))
        elif compiler in {"msvc", "Visual Studio"}:
            commands.add("cl")
    executables = {}
    for command in sorted(commands):
        path = shutil.which(command, path=env.get("PATH"))
        if path is not None:
            resolved = Path(path).resolve()
            executables[str(resolved)] = file_digest(resolved)
    return {
        "executables": executables,
        "build_environment": {key: digest_json(env[key]) for key in BUILD_ENV_KEYS if key in env},
    }
