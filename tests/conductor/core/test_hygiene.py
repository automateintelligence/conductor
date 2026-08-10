"""Repository hygiene before any project-local state is created (design §"Project and run
identity").

Tracked .conductor/ or .worktrees/ paths would put a run's private state — locks, leases,
heartbeat records — into the repository history and onto every collaborator's checkout. Preflight
fails closed with the exact recovery command, then establishes a local git exclude and rechecks
it."""

from __future__ import annotations

import os
import subprocess

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
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    hygiene.ensure_local_exclude(str(git_repo))
    first = open(exclude, encoding="utf-8").read()
    # Discriminating, not just stable: a no-op implementation would also produce byte-identical
    # reads across both calls (git's own info/exclude template, untouched). Assert the lines
    # were actually written, and that the second call adds no duplicate of them.
    assert first.count("/.conductor/") == 1
    assert first.count("/.worktrees/") == 1
    hygiene.ensure_local_exclude(str(git_repo))
    second = open(exclude, encoding="utf-8").read()
    assert second.count("/.conductor/") == 1
    assert second.count("/.worktrees/") == 1


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
    # This is the discriminating assertion: a no-op implementation also leaves .conductor/
    # ignored (the committed .gitignore already covers it), so checking is_ignored() alone here
    # cannot fail. Only "nothing was written to info/exclude" proves the no-op branch was taken.
    assert (
        not os.path.exists(exclude)
        or "/.conductor/" not in open(exclude, encoding="utf-8").read()
    )


def test_a_non_repository_reports_the_git_failure(tmp_path):
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(tmp_path))
    assert "git ls-files" in str(excinfo.value)


def test_a_missing_git_binary_fails_closed_with_the_message_contract(
    monkeypatch, git_repo
):
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(hygiene.subprocess, "run", _raise)
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(git_repo))
    message = str(excinfo.value)
    assert "git" in message
    assert str(git_repo) in message


def test_is_ignored_raises_rather_than_reporting_not_ignored_on_a_git_error(
    monkeypatch, git_repo
):
    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            [], returncode=128, stdout="", stderr="fatal: boom"
        )

    monkeypatch.setattr(hygiene.subprocess, "run", _fake_run)
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.is_ignored(str(git_repo), ".conductor/project.json")
    assert "check-ignore" in str(excinfo.value)


def test_ensure_local_exclude_raises_closed_when_the_recheck_still_finds_them_not_ignored(
    git_repo, git
):
    # Write the exclude rules first, then commit a .gitignore that negates them. Working-tree
    # .gitignore files are consulted after .git/info/exclude and the last matching pattern wins,
    # so this genuinely un-ignores both probes without touching info/exclude again — exercising
    # the post-write recheck failure the brief calls the load-bearing branch of this function.
    hygiene.ensure_local_exclude(str(git_repo))
    (git_repo / ".gitignore").write_text("!.conductor/\n!.worktrees/\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "negate the local exclude")
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.ensure_local_exclude(str(git_repo))
    message = str(excinfo.value)
    assert ".conductor/project.json" in message
    assert ".worktrees/conductor/probe/integration" in message
    assert exclude in message
