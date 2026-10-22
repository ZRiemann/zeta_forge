#!/usr/bin/env python3
"""Resolve and test real zust with Trader's governed Serde/Tokio declarations.

Run explicitly after preparing the registered consumers' dependency caches.
Only a temporary Cargo workspace and Forge build outputs are written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.build_cli import log
from zeta_forge.config import load_repo_config
from zeta_forge.process import run_command
from zeta_forge.rust_audit import registered_projects
from zeta_forge.rust_dependencies import (
    resolved_dependency_report,
)
from zeta_forge.rust_workspace import load_catalog


CONSUMER_SOURCE = '''#[derive(serde::Serialize, serde::Deserialize)]
struct Record {
    value: u32,
}

#[test]
fn shared_dependencies_work_together() {
    let _config = z::log::Config::default();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("create runtime");
    runtime.block_on(async {
        tokio::task::yield_now().await;
        let json = serde_json::to_string(&Record { value: 42 }).expect("serialize");
        let record: Record = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(record.value, 42);
    });
}
'''


def snapshot(paths: list[Path]) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def metadata_command(command: list[str], root: Path, environment: dict[str, str]) -> dict[str, object]:
    result = run_command(command, cwd=root, env=environment, check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Consumer resolution failed: {(result.stderr or result.stdout).strip()}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    started = time.monotonic()
    config = load_repo_config(FORGE / "zbuild.py")
    catalog = load_catalog(FORGE)
    projects = registered_projects(config)
    tracked_inputs = [path for project in projects for path in (
        project.manifest, project.manifest.with_name("Cargo.lock"), project.toolchain
    )]
    before = snapshot(tracked_inputs)
    status = 1
    try:
        trader = next(project for project in projects if project.root.name == "zeta_trader")
        zust = next(project for project in projects if project.root.name == "zust")
        declarations = tomllib.loads(trader.manifest.read_text())["workspace"]["dependencies"]
        channel = str(trader.toolchain_spec["channel"])
        with tempfile.TemporaryDirectory(prefix="zetax-rust-consumer-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            lines = [
                '[workspace]', '[package]', 'name = "zetax-governed-consumer"',
                'version = "0.0.0"', 'edition = "2024"', 'publish = false',
                '[dependencies]',
                'z = { package = "zust", path = ' + json.dumps(str(zust.root)) + ', features = ["log"] }',
            ]
            for name in ("serde", "serde_json", "tokio"):
                value = declarations[name]
                if isinstance(value, str):
                    lines.append(f'{name} = {json.dumps(value)}')
                else:
                    fields = ', '.join(f'{key} = {json.dumps(item)}' for key, item in value.items())
                    lines.append(f'{name} = {{ {fields} }}')
            (root / "Cargo.toml").write_text("\n".join(lines) + "\n")
            (root / "src/lib.rs").write_text(CONSUMER_SOURCE)
            # Reuse the governed application's locked identities; explicitly
            # prepare only the disposable fixture's new root package.
            (root / "Cargo.lock").write_bytes(trader.manifest.with_name("Cargo.lock").read_bytes())
            environment = dict(config.env, CARGO_TARGET_DIR=str(FORGE / "build/rust-consumer"))
            command = ["rustup", "run", channel, "cargo"]
            metadata_command([*command, "metadata", "--format-version", "1", "--offline"],
                             root, environment)
            metadata = metadata_command(
                [*command, "metadata", "--format-version", "1", "--locked", "--offline"],
                root, environment,
            )
            report = resolved_dependency_report(metadata, catalog.dependencies, [root, zust.root])
            if report["errors"]:
                raise RuntimeError("Consumer dependency drift: " + "; ".join(report["errors"]))
            for name in ("serde", "tokio"):
                matches = {edge["dependency"]["id"] for edge in report["direct_edges"]
                           if edge["dependency"]["name"] == name}
                if len(matches) != 1:
                    raise RuntimeError(f"Consumer must resolve exactly one {name}: {matches}")
            print(json.dumps(report, sort_keys=True))
            run_command([*command, "test", "--locked", "--offline"], cwd=root, env=environment)
            status = 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        log("E", str(error))
    finally:
        if before != snapshot(tracked_inputs):
            log("E", "Consumer test changed a registered manifest, lockfile or toolchain")
            status = 1
        print(f"ZETA_RUST_CONSUMER_DONE status={status} passed={int(status == 0)} "
              f"failed={int(status != 0)} elapsed_seconds={time.monotonic() - started:.3f}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
