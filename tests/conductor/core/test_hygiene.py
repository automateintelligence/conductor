"""Repository hygiene before any project-local state is created (design §"Project and run
identity").

Tracked .conductor/ or .worktrees/ paths would put a run's private state — locks, leases,
heartbeat records — into the repository history and onto every collaborator's checkout. Preflight
fails closed with the exact recovery command, then establishes a local git exclude and rechecks
it."""

from __future__ import annotations

import os

import pytest

from conductor.core import hygiene


def test_state_paths_are_the_two_documented_directories():
    assert hygiene.STATE_PATHS == (".conductor", ".worktrees")


def test_a_clean_repository_passes(git_repo):
    hygiene.assert_state_paths_untracked(str(git_repo))
    assert hygiene.tracked_state_paths(str(git_repo)) == []


def test_a_tracked_state_path_fails_closed_with_the_exact_recovery_command(
    git_repo, git
):
    (git_repo / ".conductor").mkdir()
    (git_repo / ".conductor" / "goal.md").write_text("goal\n")
    git(git_repo, "add", "-f", ".conductor/goal.md")
    git(git_repo, "commit", "-qm", "oops")
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(git_repo))
    message = str(excinfo.value)
    assert ".conductor/goal.md" in message
    assert f"git -C {git_repo} rm -r --cached .conductor .worktrees" in message
    assert "No write occurred" in message


def test_a_tracked_worktrees_path_is_caught_too(git_repo, git):
    (git_repo / ".worktrees").mkdir()
    (git_repo / ".worktrees" / "keep.txt").write_text("x\n")
    git(git_repo, "add", "-f", ".worktrees/keep.txt")
    git(git_repo, "commit", "-qm", "oops")
    with pytest.raises(hygiene.TrackedStateError):
        hygiene.assert_state_paths_untracked(str(git_repo))


def test_ensure_local_exclude_makes_both_paths_ignored(git_repo):
    assert not hygiene.is_ignored(str(git_repo), ".conductor/project.json")
    hygiene.ensure_local_exclude(str(git_repo))
    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")
    assert hygiene.is_ignored(str(git_repo), ".worktrees/conductor/x/integration")


def test_ensure_local_exclude_is_idempotent(git_repo):
    hygiene.ensure_local_exclude(str(git_repo))
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    first = open(exclude, encoding="utf-8").read()
    hygiene.ensure_local_exclude(str(git_repo))
    assert open(exclude, encoding="utf-8").read() == first


def test_ensure_local_exclude_preserves_existing_exclude_content(git_repo):
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "w", encoding="utf-8") as fh:
        fh.write("# my own rules\n*.scratch\n")
    hygiene.ensure_local_exclude(str(git_repo))
    body = open(exclude, encoding="utf-8").read()
    assert "*.scratch" in body and "/.conductor/" in body


def test_ensure_local_exclude_is_a_no_op_when_gitignore_already_covers_them(
    git_repo, git
):
    (git_repo / ".gitignore").write_text(".conductor/\n.worktrees/\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ignore run state")
    hygiene.ensure_local_exclude(str(git_repo))
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    assert (
        not os.path.exists(exclude)
        or "/.conductor/" not in open(exclude, encoding="utf-8").read()
    )
    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")


def test_a_non_repository_reports_the_git_failure(tmp_path):
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(tmp_path))
    assert "git ls-files" in str(excinfo.value)
