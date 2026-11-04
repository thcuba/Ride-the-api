"""deploy/update.sh never leaves new code with old dependencies.

A failed ``pip install`` after the pull used to leave the tree on the new
commit: the next restart (a reboot, a crash) would load it with the old
dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash"
)

# Stands in for .venv/bin/python: "pip install -e <dir>" fails while the
# checked-out tree carries a BROKEN marker (a release whose deps do not install).
FAKE_PYTHON = """#!/usr/bin/env bash
target="${@: -1}"
[[ -f "$target/BROKEN" ]] && exit 1
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(message)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    _git(repo, "push", "-q", "origin", "HEAD:main")
    return _git(repo, "rev-parse", "HEAD")


def test_failed_dependency_install_rolls_the_code_back(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    good = _commit(seed, "app.txt", "v1")

    install = tmp_path / "install"
    _git(tmp_path, "clone", "-q", str(origin), str(install))
    python = install / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    (install / ".git" / "info" / "exclude").write_text(".venv/\nVERSION\nupdate.log\n")

    _commit(seed, "BROKEN", "v2 with dependencies that do not install")

    result = subprocess.run(
        ["bash", str(UPDATE_SH), "--dir", str(install), "--restart", "none"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert _git(install, "rev-parse", "HEAD") == good
    assert not (install / "BROKEN").exists()
    assert good.startswith((install / "VERSION").read_text().strip())  # abbreviated sha
