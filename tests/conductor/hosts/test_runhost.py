"""Which host does THIS run launch? (A1 — the launcher's one input.)

The scheduler fires a generated shell driver. Before it can render that driver it has to know
whether the run is Claude-hosted or Codex-hosted, and the answer has to survive a reboot, so it
is durable state next to `.conductor/run_branch`, not a process-local guess.

The default is `claude` and that is deliberate: every run recorded before this module existed
has no `.conductor/host`, and those runs must keep launching exactly as they do today. A file
that EXISTS but does not name a supported host is a different situation — something wrote it —
and it fails loud rather than falling back, because "silently launched the wrong agent" is the
one outcome no operator can debug from a cron log.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conductor.hosts import base, runhost

# Literal, not `base.HOST_IDS`: parametrizing over the value under test lets a falsifier that
# shrinks HOST_IDS DELETE cases instead of failing them. The equality test below is what ties
# this tuple to the production vocabulary, and it fails loudly when they diverge.
HOSTS = ("claude", "codex")


def test_the_host_matrix_covers_exactly_the_supported_hosts():
    assert HOSTS == base.HOST_IDS


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".conductor").mkdir(parents=True)
    return str(root)


def test_an_unrecorded_run_resolves_claude_so_existing_runs_never_change_host(project):
    """Every run installed before A1 has no host file. Those drivers must keep firing claude."""
    assert not os.path.exists(runhost.host_file(project))
    assert runhost.resolve(project) == "claude"


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_recorded_host_round_trips_through_the_durable_file(host_id, project):
    path = runhost.record(project, host_id)
    assert path == runhost.host_file(project)
    assert runhost.resolve(project) == host_id


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_environment_override_wins_over_the_recorded_file(
    host_id, project, monkeypatch
):
    """An operator moving a run between hosts must not have to hand-edit state."""
    runhost.record(project, base.opposite(host_id))
    monkeypatch.setenv(runhost.HOST_ENV, host_id)
    assert runhost.resolve(project) == host_id


def test_a_garbage_environment_override_is_refused_and_never_silently_claude(
    project, monkeypatch
):
    monkeypatch.setenv(runhost.HOST_ENV, "gemini")
    with pytest.raises(base.UnknownHost) as excinfo:
        runhost.resolve(project)
    message = str(excinfo.value)
    assert "gemini" in message
    assert runhost.HOST_ENV in message


def test_a_garbage_recorded_host_is_refused_and_names_the_file(project):
    with open(runhost.host_file(project), "w", encoding="utf-8") as f:
        f.write("clade\n")
    with pytest.raises(base.UnknownHost) as excinfo:
        runhost.resolve(project)
    message = str(excinfo.value)
    assert "clade" in message
    assert runhost.host_file(project) in message


def test_an_empty_host_file_is_refused_rather_than_read_as_unrecorded(project):
    """A truncated write is not the same as a run that predates the file. Falling back to
    claude here would launch the wrong agent on a Codex machine and log nothing about it."""
    with open(runhost.host_file(project), "w", encoding="utf-8") as f:
        f.write("   \n")
    with pytest.raises(base.UnknownHost):
        runhost.resolve(project)


@pytest.mark.parametrize("host_id", HOSTS)
def test_recording_tolerates_surrounding_whitespace_on_read(host_id, project):
    with open(runhost.host_file(project), "w", encoding="utf-8") as f:
        f.write(f"  {host_id}\n\n")
    assert runhost.resolve(project) == host_id


def test_recording_an_unsupported_host_is_refused_before_anything_is_written(project):
    with pytest.raises(base.UnknownHost):
        runhost.record(project, "gemini")
    assert not os.path.exists(runhost.host_file(project))


@pytest.mark.parametrize("host_id", HOSTS)
def test_adapter_returns_the_recorded_hosts_adapter(host_id, project):
    runhost.record(project, host_id)
    assert runhost.adapter(project).id == host_id


def test_recording_creates_the_state_directory_when_it_is_absent(tmp_path):
    root = str(tmp_path / "fresh")
    os.mkdir(root)
    runhost.record(root, "codex")
    assert runhost.resolve(root) == "codex"


# ---- the run worktree is the same run (P1-B) ---------------------------------------


@pytest.fixture
def linked_worktree(tmp_path):
    """A main checkout plus a linked worktree of it — the topology every run uses.

    `/conductor:start` step 5b puts the worker in `.worktrees/run-<slug>`, and the generated
    driver exports that worktree as `CONDUCTOR_HOME`. So every consumer of the host — preflight,
    plan-lint, the merge gate — asks from INSIDE the worktree, while `driver install` recorded
    from the main checkout.
    """
    main = tmp_path / "main"
    main.mkdir()
    for cmd in (
        ["git", "init", "-q", str(main)],
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "root"],
        [
            "git",
            "-C",
            str(main),
            "worktree",
            "add",
            "-q",
            str(tmp_path / "wt"),
            "-b",
            "run",
        ],
    ):
        subprocess.run(cmd, check=True, timeout=30)
    return str(main), str(tmp_path / "wt")


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_linked_worktree_resolves_the_same_host_as_the_main_checkout(
    host_id, linked_worktree, monkeypatch
):
    """One run, one host. Resolving per-directory gave a Codex run claude preflight roots, a
    claude plan-lint needle and the wrong review marker — from inside its own worktree."""
    monkeypatch.delenv(runhost.HOST_ENV, raising=False)
    main, worktree = linked_worktree
    runhost.record(main, host_id)
    assert runhost.resolve(worktree) == host_id


@pytest.mark.parametrize("host_id", HOSTS)
def test_recording_from_inside_the_worktree_lands_in_the_main_checkout(
    host_id, linked_worktree, monkeypatch
):
    """The write side has to agree with the read side, or a worktree grows its own recording
    that the main checkout — and the next reconcile from it — never sees."""
    monkeypatch.delenv(runhost.HOST_ENV, raising=False)
    main, worktree = linked_worktree
    runhost.record(worktree, host_id)
    assert not os.path.exists(os.path.join(worktree, ".conductor", "host"))
    assert runhost.resolve(main) == host_id


def test_a_path_outside_any_repository_still_resolves_against_itself(
    tmp_path, monkeypatch
):
    """Not every caller is in a git repo — the tmp-dir callers in this file are not, and neither
    is a project someone has not run `git init` in yet. Those keep the literal-path behaviour
    rather than failing or reaching for an unrelated ancestor repository."""
    monkeypatch.delenv(runhost.HOST_ENV, raising=False)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    runhost.record(str(plain), "codex")
    assert os.path.isfile(os.path.join(str(plain), ".conductor", "host"))
    assert runhost.resolve(str(plain)) == "codex"
