"""Compile patched CLI watcher methods in a small offline OS-notification harness.

No Dioxus CLI/application build or business-service startup is performed. Requires
an independently acquired pinned CLI archive and cached harness dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE_ROOT / "common"))
from zeta_forge.rust_patches import materialize  # noqa: E402
from zeta_forge.rust_tool_patches import tool_repair  # noqa: E402


def rust_function(source: str, name: str) -> str:
    """Extract these pinned methods verbatim, including their balanced bodies."""
    start = source.index(f"fn {name}(")
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new private report/build directory")
    parser.add_argument("--profile", choices=("debug", "release"), default="debug")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    catalog = tomllib.loads((FORGE_ROOT / "rust/catalog.toml").read_text())
    specification = next(tool for tool in catalog["cargo-tools"] if tool["name"] == "dioxus-cli")
    repair = tool_repair(FORGE_ROOT, specification)
    materialize(repair.recipe, output / "source", args.archive.read_bytes())
    runner = (output / "source/src/serve/runner.rs").read_text()
    workspace = (output / "source/src/workspace.rs").read_text()
    fixture = FORGE_ROOT / "tests/fixtures/dioxus_watch"
    harness = (fixture / "harness.rs").read_text()
    methods = {"DEFAULT_IGNORE_LIST": rust_function(workspace, "default_ignore_list"),
               "WORKSPACE_ROOT": rust_function(workspace, "workspace_root"),
               "ALL_WATCHED_CRATES": rust_function(runner, "all_watched_crates"),
               "FILL_FILEMAP": rust_function(runner, "fill_filemap_from_krate"),
               "WATCH_PATHS": rust_function(runner, "watch_paths"),
               "WATCH_FILESYSTEM": rust_function(runner, "watch_filesystem"),
               "SCOPED_ENTRIES": rust_function(runner, "scoped_watch_entries"),
               "METADATA_PROGRESS": "async " + rust_function(workspace, "metadata_progress")}
    if 'bail!("cargo metadata took too long to respond, try again with --offline")' not in workspace:
        raise RuntimeError("Metadata timeout must retain its error path")
    start = runner.index("if matches!(event.kind, EventKind::Create(_) | EventKind::Modify(ModifyKind::Name(_))) {")
    end = runner.index("\n\n                    for path in event.paths", start)
    methods["ADMIT_NEW_DIRECTORIES"] = runner[start:end]
    for placeholder, code in methods.items():
        harness = harness.replace(f"// @{placeholder}@", code)
    project = output / "harness"
    (project / "src").mkdir(parents=True)
    (project / "src/lib.rs").write_text(harness)
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copyfile(fixture / name, project / name)
    environment = {**os.environ, "CARGO_TARGET_DIR": str(output / "target"),
                   "ZETA_WATCH_FIXTURE_ROOT": str(output / "fixtures")}
    command = ["rustup", "run", "1.97.1", "cargo", "test", "--locked", "--offline",
               "--manifest-path", str(project / "Cargo.toml")]
    if args.profile == "release":
        command.append("--release")
    command.extend(["--", "--test-threads=1"])
    trace = shutil.which("strace")
    trace_path = output / "filesystem.trace"
    if trace:
        command = [trace, "-f", "-e", "trace=openat,getdents64", "-o", str(trace_path), *command]
    subprocess.run(command, env=environment, check=True)
    forbidden = []
    if trace:
        for line in trace_path.read_text().splitlines():
            if str(output / "fixtures") in line and "O_DIRECTORY" in line:
                if re.search(r"/(?:[^/\"]*forbidden[^/\"]*|\.git|target|node_modules)(?:/|\")", line):
                    forbidden.append(line)
    if forbidden:
        raise RuntimeError("Watcher opened excluded fixture directories: " + "\n".join(forbidden[:5]))
    report = {"status": "pass", "profile": args.profile, "recipe_sha256": repair.identity,
              "archive_sha256": repair.recipe["archive_sha256"],
              "patched_tree_sha256": repair.recipe["tree_sha256"], "rust_tests": 6,
              "strace_checked": bool(trace), "forbidden_directory_opens": len(forbidden),
              "full_cli_built": False, "application_started": False}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("ZETA_DIOXUS_WATCH_DONE " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
