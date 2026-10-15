"""HPX source provenance must survive Git history simplification."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "common"))

from builder.hpx.project import HpxBuilder
from zeta_forge.cmake_builder import CommonBuildArgs
from zeta_forge.config import load_repo_config


@unittest.skipUnless(shutil.which("git"), "Git unavailable")
class HpxBuilderTests(unittest.TestCase):
    def builder(self, source: Path) -> HpxBuilder:
        config = load_repo_config(ROOT / "zbuild.py")
        config = replace(
            config, source_dirs={**config.source_dirs, "ZETA_HPX_SRC_DIR": source}
        )
        return HpxBuilder(
            script_path=ROOT / "builder/hpx/project.py",
            repo_config=config,
            args=CommonBuildArgs("Release"),
        )

    def test_configuration_records_head_even_without_path_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hpx-git-") as directory:
            source = Path(directory)

            def git(*arguments: str) -> str:
                return subprocess.run(
                    [
                        "git", "-C", str(source),
                        "-c", "user.name=HPX fixture",
                        "-c", "user.email=hpx-fixture@example.invalid",
                        "-c", "commit.gpgsign=false", *arguments,
                    ],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()

            git("init")
            (source / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
            git("add", "CMakeLists.txt")
            git("commit", "-m", "Source snapshot")
            git("commit", "--allow-empty", "-m", "No path changes")
            head = git("rev-parse", "HEAD")
            self.assertNotEqual(head, git("log", "--pretty=%H", "-1", "."))

            command = self.builder(source).configure_command()
            self.assertIn(f"-DHPX_WITH_GIT_COMMIT={head}", command)
            self.assertIn(f"-DHPX_WITH_GIT_COMMIT_SHORT={head[:7]}", command)

    def test_missing_source_repository_has_contextual_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hpx-no-git-") as directory:
            with self.assertRaisesRegex(RuntimeError, "Unable to read the HPX source commit"):
                self.builder(Path(directory)).configure_command()


if __name__ == "__main__":
    unittest.main()
