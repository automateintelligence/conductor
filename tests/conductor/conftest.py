"""Shared fixtures for the conductor test suite.

Conductor resolves its canonical state root from git plumbing (``--git-common-dir``) so that
starting from a linked worktree finds the same root as the main checkout. Mocking git would test
the mock, so these tests build real, isolated repositories."""

from __future__ import annotations

import os
import subprocess

import pytest


@pytest.fixture
def git_env(tmp_path):
    """Git environment isolated from the developer's global and system configuration."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Conductor Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Conductor Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


@pytest.fixture
def git(git_env):
    """Run a git command inside a repository, raising on failure."""

    def _git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
            timeout=30,
        )

    return _git


@pytest.fixture
def git_repo(tmp_path, git_env, git):
    """A repository on ``main`` with two committed specs."""
    root = tmp_path / "repo"
    (root / "docs" / "specs").mkdir(parents=True)
    (root / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    (root / "docs" / "specs" / "beta.md").write_text("# beta\n")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root
