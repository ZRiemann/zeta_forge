"""Shared invocation policy for Forge's native Conan consumers."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from functools import cached_property
from pathlib import Path

from . import conan_boost, conan_policy, conan_state
from .build_log import log
from .cmake_builder import CMakeProjectBuilder, CommonBuildArgs
from .conan_inputs import (
    compiler_inputs,
    conan_probe,
    profile_inputs,
    sanitize_profiles,
    toolchain_inputs,
)
from .config import RepoConfig
from .process import run_command
from .state_io import digest_json, file_digest, write_json, writer_lock

POLICY_SNAPSHOT = "native-policy.json"


def profile_flags(
    *,
    build_type: str,
    cxx_standard: str,
    jobs: int | None,
    user_toolchain: Path | None = None,
    enabled_blocks: tuple[str, ...] = (),
) -> list[str]:
    command = [
        "-s",
        f"build_type={build_type}",
        "-s",
        f"compiler.cppstd={cxx_standard}",
        "-c",
        "tools.cmake.cmaketoolchain:generator=Ninja",
    ]
    if jobs is not None:
        command.extend(("-c:a", f"tools.build:jobs={jobs}"))
    if user_toolchain is not None:
        toolchains = json.dumps([str(user_toolchain)])
        command.extend(("-c", f"tools.cmake.cmaketoolchain:user_toolchain={toolchains}"))
    if enabled_blocks:
        blocks = json.dumps(enabled_blocks)
        command.extend(("-c", f"tools.cmake.cmaketoolchain:enabled_blocks={blocks}"))
    return command


def materialize_recipe(
    target: str, config: RepoConfig, boost_reference: str, directory: Path
) -> Path:
    policy = conan_policy.resolve_policy(target, config.env, boost_reference)
    recipe = directory / "conanfile.py"
    shutil.copyfile(config.builder_dir / "conanfile.py", recipe)
    (directory / POLICY_SNAPSHOT).write_text(
        json.dumps(asdict(policy), sort_keys=True) + "\n", encoding="utf-8"
    )
    return recipe


def install_native(
    target: str,
    config: RepoConfig,
    args: CommonBuildArgs,
    output_folder: Path,
    *,
    user_toolchain: Path | None = None,
    enabled_blocks: tuple[str, ...] = (),
) -> None:
    ConanSession(target, config, args, output_folder, user_toolchain, enabled_blocks).ensure_generators()


class ConanSession:
    def __init__(
        self,
        target: str,
        config: RepoConfig,
        args: CommonBuildArgs,
        output_folder: Path,
        user_toolchain: Path | None = None,
        enabled_blocks: tuple[str, ...] = (),
    ) -> None:
        self.target = target
        self.config = config
        self.args = args
        self.output_folder = output_folder
        self.cwd = config.builder_dir / target
        self.flags = profile_flags(
            build_type=args.build_type,
            cxx_standard=config.cxx_standard,
            jobs=args.jobs,
            user_toolchain=user_toolchain,
            enabled_blocks=enabled_blocks,
        )
        self.state_root = config.forge_root / "build" / "conan-state" / target / args.build_type.lower()
        self.generators = output_folder / "build" / args.build_type / "generators"
        self.output_receipt = output_folder / ".forge-conan-receipt.json"

    def probe(self, *args: str) -> str:
        return conan_probe(["conan", *args], self.cwd, self.config.env)

    def run(self, *args: object) -> None:
        run_command(["conan", *args], cwd=self.cwd, env=self.config.env)

    @cached_property
    def inputs(self) -> dict:
        env = self.config.env
        boost = conan_policy.package_version("boost", env)
        policy = conan_policy.resolve_policy(self.target, env, f"boost/{boost}")
        files = [
            self.config.builder_dir / "conanfile.py",
            Path(__file__),
            Path(conan_policy.__file__),
            Path(conan_boost.__file__),
            Path(conan_state.__file__),
            Path(__file__).with_name("conan_inputs.py"),
            Path(__file__).with_name("state_io.py"),
        ]
        managed_boost = {}
        if boost == conan_policy.MANAGED_BOOST_VERSION:
            patch_dir = self.config.builder_dir / "boost"
            files.extend((patch_dir / conan_boost.RECIPE_PATCH, patch_dir / conan_boost.SOURCE_PATCH))
            managed_boost = {
                "upstream_recipe": conan_boost.UPSTREAM_RECIPE,
                "openssl_version": conan_policy.package_version("openssl", env),
            }
        profiles = json.loads(self.probe("profile", "show", "--format=json", *self.flags))
        sanitized = sanitize_profiles(profiles)
        toolchains = []
        for context in profiles.values():
            for key, value in context["conf"].items():
                if key.endswith("tools.cmake.cmaketoolchain:user_toolchain") and isinstance(value, list):
                    toolchains.extend(self.cwd / path for path in value if isinstance(path, str))
                elif key.endswith("tools.cmake.cmaketoolchain:toolchain_file") and isinstance(value, str):
                    toolchains.append(self.cwd / value)
        return {
            "schema": conan_state.SCHEMA,
            "target": self.target,
            "profile": self.args.build_type,
            "policy": asdict(policy),
            "managed_boost": managed_boost,
            "profiles": sanitized,
            "profile_files": profile_inputs(Path(self.probe("config", "home")), env),
            "recipe_files": {
                str(path.relative_to(self.config.builder_dir))
                if path.is_relative_to(self.config.builder_dir)
                else f"zeta_forge/{path.name}": file_digest(path) if path.is_file() else None
                for path in files
            },
            "toolchain_files": toolchain_inputs(toolchains),
            "tools": {
                "conan": self.probe("--version"),
                "generator": "Ninja",
                "compiler": compiler_inputs(profiles, env),
            },
        }

    @cached_property
    def fingerprint(self) -> str:
        return digest_json(self.inputs)

    @property
    def state_dir(self) -> Path:
        return self.state_root / self.fingerprint

    def output_matches(self, generation: tuple[str, Path, dict] | None) -> bool:
        if generation is None or not self.output_receipt.is_file():
            return False
        try:
            receipt = conan_state.read_json(self.output_receipt)
        except RuntimeError:
            return False
        return (
            receipt == {
                "schema": conan_state.SCHEMA,
                "generation": generation[0],
                "input_fingerprint": self.fingerprint,
                "output_folder": str(self.output_folder.resolve()),
            }
            and conan_state.generator_inventory(self.generators) == generation[2]["generators"]
        )

    def needs_generators(self) -> bool:
        if self.args.refresh_conan_lock:
            return True
        return not self.output_matches(conan_state.active_generation(self.state_dir))

    def ensure_generators(self) -> None:
        with writer_lock(self.state_root / ".writer.lock"):
            try:
                previous = conan_state.active_generation(self.state_dir)
            except RuntimeError:
                if not self.args.refresh_conan_lock:
                    raise
                previous = None
            if not self.args.refresh_conan_lock and self.output_matches(previous):
                log("I", f"conan {self.target}: reuse generators and lock {self.fingerprint[:12]}")
                return
            self.resolve(previous)

    def resolve(self, previous: tuple[str, Path, dict] | None) -> None:
        if self.generators.is_symlink():
            raise RuntimeError(f"Unsafe Conan generator directory: {self.generators}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.generators.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.generators.parent, prefix=".forge-conan-") as directory:
            work = Path(directory)
            reference = conan_boost.prepare_boost_recipe(self.config)
            recipe = materialize_recipe(self.target, self.config, reference, work)
            if previous is not None and not self.args.refresh_conan_lock:
                lock = previous[1]
                log("I", f"conan {self.target}: regenerate using retained lock {self.fingerprint[:12]}")
            else:
                lock = work / "candidate.lock"
                reason = "refresh lock" if self.args.refresh_conan_lock else "resolve new inputs"
                log("I", f"conan {self.target}: {reason} {self.fingerprint[:12]}")
                update = ("--update",) if self.args.refresh_conan_lock else ()
                self.run(
                    "lock", "create", recipe, "--build=missing", *self.flags, *update,
                    "--lockfile=", f"--lockfile-out={lock}",
                )
                conan_state.validate_lock(lock)
            self.install_locked(recipe, lock, work, previous)

    def install_locked(
        self, recipe: Path, lock: Path, work: Path, previous: tuple[str, Path, dict] | None
    ) -> None:
        backup = work / "old-generators"
        if self.generators.exists():
            self.generators.rename(backup)
        graph_path = work / "graph.json"
        try:
            self.run(
                "install", recipe, f"--output-folder={self.output_folder}", "--build=missing",
                *self.flags, f"--lockfile={lock}", "--format=json", f"--out-file={graph_path}",
            )
            graph = conan_state.graph_summary(graph_path)
            inventory = conan_state.generator_inventory(self.generators)
            if not inventory:
                raise RuntimeError(f"Conan did not produce a complete generator inventory: {self.generators}")
            receipt = {
                "schema": conan_state.SCHEMA,
                "input_fingerprint": self.fingerprint,
                "inputs": self.inputs,
                "lock_sha256": file_digest(lock),
                "graph": graph,
                "graph_sha256": digest_json(graph),
                "generators": inventory,
            }
            generation = conan_state.store_generation(self.state_dir, lock, receipt)
            write_json(self.output_receipt, {
                "schema": conan_state.SCHEMA,
                "generation": generation,
                "input_fingerprint": self.fingerprint,
                "output_folder": str(self.output_folder.resolve()),
            })
            write_json(
                self.state_dir / "active.json",
                {"schema": conan_state.SCHEMA, "generation": generation},
            )
        except BaseException:
            if self.generators.is_symlink():
                self.generators.unlink()
            elif self.generators.exists():
                shutil.rmtree(self.generators)
            if backup.exists():
                backup.rename(self.generators)
            raise
        if self.args.refresh_conan_lock and previous is not None:
            before = {(node["context"], node["ref"]): node for node in previous[2]["graph"]}
            after = {(node["context"], node["ref"]): node for node in graph}
            changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
            log("I", f"conan {self.target}: activated refreshed graph with {len(changed)} changed nodes")
            for context, reference in changed:
                log("I", f"conan {self.target}: changed {context} {reference}")


class ForgeConanProjectBuilder(CMakeProjectBuilder):
    """Adapt the shared CMake hooks to Forge's native Conan session."""

    native_conan_target: str
    enabled_toolchain_blocks: tuple[str, ...] = ()

    @property
    def user_toolchain(self) -> Path:
        return self.script_dir / "cmake" / "conan_user_toolchain.cmake"

    @cached_property
    def conan_session(self) -> ConanSession:
        return ConanSession(
            self.native_conan_target,
            self.repo_config,
            self.args,
            self.conan_root,
            self.user_toolchain,
            self.enabled_toolchain_blocks,
        )

    def should_run_conan(self) -> bool:
        return self.conan_session.needs_generators()

    def run_conan(self) -> None:
        self.conan_session.ensure_generators()
