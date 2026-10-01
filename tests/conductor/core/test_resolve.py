"""Canonical state root and run-key resolution (design §"Project and run identity").

Conductor resolves one canonical state root from the repository's Git common directory, so
starting from a linked worktree still finds that same root. When an invocation carries a run key,
that key alone determines the run: legacy .conductor/run_branch, legacy .conductor/goal.md, and
ambient gate environment variables are ignored rather than consulted as fallback. Without a key,
resolution succeeds only when exactly one active run exists."""

from __future__ import annotations

import os
import subprocess

import pytest

from conductor.core import (
    locks,
    registry,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
)

WORKSTATION = "0123456789abcdef0123456789abcdef"
NOW = "2026-08-10T12:00:00+00:00"

# schema forbids active -> terminal outright: only `conductor finish` completes a run, and it does
# so from awaiting-team-merge. A helper that assumed the direct hop would raise SchemaError, so
# statuses that are not reachable from active in one step name the legal path they take.
_STATUS_PATH = {"terminal": ("awaiting-team-merge", "terminal")}


def _run_doc(spec, key):
    return schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=spec,
        workstation_id=WORKSTATION,
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )


def _make_run(state_root, spec, *, status="active"):
    key = runkey.run_key(spec)
    runstate.create(state_root, key, _run_doc(spec, key))
    registry.update(
        state_root, lambda d: registry.register(d, spec=spec, run_key=key, generation=1)
    )
    if status != "active":
        for step in _STATUS_PATH.get(status, (status,)):
            runstate.set_status(state_root, key, step)
        registry.update(state_root, lambda d: registry.mirror_status(d, key, status))
    return key


def _pending_create(state_root, spec, txn_id):
    """Leave a run creation committed but unapplied, exactly as a crash between
    ``transaction.commit`` and ``transaction.apply`` leaves it: the journal holds the after
    images while ``project.json`` and ``run.json`` still hold their before images."""
    key = runkey.run_key(spec)
    before = registry.load(state_root)
    # schema.clone is a DEEP copy on purpose: registry.register appends to nested lists, so a
    # shallow copy would alias them, the before image would equal the after image, and this test
    # would pass whether recovery ran forward, backward, or not at all.
    after = registry.register(
        schema.clone(before), spec=spec, run_key=key, generation=1
    )
    after["revision"] = before["revision"] + 1
    transaction.prepare(
        state_root,
        txn_id,
        [
            {
                "path": registry.registry_path(state_root),
                "before": before,
                "after": after,
            },
            {
                "path": runstate.run_path(state_root, key),
                "before": None,
                "after": _run_doc(spec, key),
            },
        ],
    )
    transaction.commit(state_root, txn_id)
    return key


@pytest.fixture
def project(git_repo):
    root = str(git_repo)
    state_root = resolve.state_root(root)
    registry.init(
        state_root,
        workstation_id=WORKSTATION,
        repo_identity=resolve.repo_identity(root),
    )
    return root, state_root


def test_repo_root_is_the_main_checkout_from_a_linked_worktree(git_repo, git, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    assert resolve.repo_root(str(linked)) == os.path.realpath(str(git_repo))
    assert resolve.state_root(str(linked)) == resolve.state_root(str(git_repo))


def test_state_root_is_dot_conductor_under_the_main_checkout(git_repo):
    assert resolve.state_root(str(git_repo)) == os.path.join(
        os.path.realpath(str(git_repo)), ".conductor"
    )


def test_repo_root_falls_back_to_conductor_home_when_no_start_is_given(
    git_repo, monkeypatch
):
    """A cron fire calls in with no start path. CONDUCTOR_HOME names the project and must win over
    the process cwd, which under cron is not the project at all."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(git_repo))
    assert resolve.repo_root() == os.path.realpath(str(git_repo))
    assert resolve.state_root() == os.path.join(
        os.path.realpath(str(git_repo)), ".conductor"
    )


def test_repo_root_takes_an_explicit_empty_start_literally(
    git_repo, tmp_path, monkeypatch
):
    """`resume_script.main` hands --project straight to main_root with no or-chain, so an empty
    --project must NOT be quietly replaced by CONDUCTOR_HOME: uninstall-cron would then compute a
    marker for a different project, find no matching crontab lines, and exit 0 while the target
    project's cron lines kept firing. Passed through as given, git resolves it against the process
    cwd — outside a repository that is a CalledProcessError, which resume_script.main:455-462
    already turns into an actionable message."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(git_repo))
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    with pytest.raises(subprocess.CalledProcessError):
        resolve.repo_root("")


def test_repo_identity_records_the_root_commit(git_repo):
    identity = resolve.repo_identity(str(git_repo))
    assert identity["root_commit"] and len(identity["root_commit"]) == 40
    assert "origin_url" in identity


def test_an_explicit_run_key_resolves_regardless_of_ambient_files(project, monkeypatch):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    other = _make_run(state_root, "docs/specs/beta.md")
    os.makedirs(os.path.join(root, ".conductor"), exist_ok=True)
    with open(
        os.path.join(root, ".conductor", "run_branch"), "w", encoding="utf-8"
    ) as fh:
        fh.write("conductor/run-something-else\n")
    with open(os.path.join(root, ".conductor", "goal.md"), "w", encoding="utf-8") as fh:
        fh.write("docs/specs/gamma.md\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked")
    resolution = resolve.resolve(run_key=key, start=root)
    assert resolution.run_key == key
    assert resolution.run["spec_path"] == "docs/specs/alpha.md"
    assert resolution.run_dir == runstate.run_dir(state_root, key)
    assert (
        resolve.resolve(run_key=other, start=root).run["spec_path"]
        == "docs/specs/beta.md"
    )


def test_an_unknown_explicit_run_key_fails_with_the_listing_command(project):
    root, _ = project
    with pytest.raises(resolve.RunNotFound) as excinfo:
        resolve.resolve(run_key="not-a-run-0badf00d", start=root)
    assert "conductor run list --all" in str(excinfo.value)


def test_no_key_resolves_when_exactly_one_run_is_active(project):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    _make_run(state_root, "docs/specs/beta.md", status="terminal")
    assert resolve.resolve(start=root).run_key == key


def test_no_key_with_two_active_runs_fails_listing_the_keys_and_commands(project):
    root, state_root = project
    first = _make_run(state_root, "docs/specs/alpha.md")
    second = _make_run(state_root, "docs/specs/beta.md")
    with pytest.raises(resolve.RunAmbiguous) as excinfo:
        resolve.resolve(start=root)
    message = str(excinfo.value)
    assert first in message and second in message
    assert f"--run {first}" in message and f"--run {second}" in message


def test_no_key_with_no_active_run_fails_with_the_creation_command(project):
    root, state_root = project
    _make_run(state_root, "docs/specs/alpha.md", status="terminal")
    with pytest.raises(resolve.RunNotFound) as excinfo:
        resolve.resolve(start=root)
    assert "conductor run new" in str(excinfo.value)


def test_checkpointed_and_blocked_count_as_active_but_awaiting_team_merge_does_not(
    project,
):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    for status in ("checkpointed", "blocked"):
        runstate.set_status(state_root, key, status)
        assert resolve.active_run_keys(state_root) == [key]
        runstate.set_status(state_root, key, "active")
    runstate.set_status(state_root, key, "awaiting-team-merge")
    assert resolve.active_run_keys(state_root) == []


def test_active_run_keys_reads_run_json_not_the_registry_mirror(project):
    """The registry status is a mirror; run.json is authoritative. A stale mirror must not make a
    terminal run look active or an active run look gone."""
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    registry.update(state_root, lambda d: registry.mirror_status(d, key, "terminal"))
    assert resolve.active_run_keys(state_root) == [key]


def test_recover_pending_finishes_a_committed_transaction_and_names_what_it_handled(
    project,
):
    """A crash between transaction.commit() and apply leaves project.json and run.json holding
    their BEFORE images, so a run that is already committed is invisible on disk. recover_pending
    rolls the journal forward and reports which transactions it settled."""
    _, state_root = project
    key = _pending_create(state_root, "docs/specs/alpha.md", "txn-create-alpha")
    assert registry.run_keys(registry.load(state_root)) == []
    assert runstate.load(state_root, key) is None

    assert resolve.recover_pending(state_root) == ["txn-create-alpha"]
    assert transaction.pending(state_root) == []
    assert registry.run_keys(registry.load(state_root)) == [key]
    assert runstate.load(state_root, key)["spec_path"] == "docs/specs/alpha.md"
    assert resolve.recover_pending(state_root) == []


def test_active_run_keys_sees_the_run_only_after_the_entry_point_recovers(project):
    """active_run_keys is a leaf READ: it must not recover, because recovery takes project.lock
    and a caller may already hold a lock. Until the entry point calls recover_pending, a
    committed-but-unapplied run is legitimately not there."""
    _, state_root = project
    key = _pending_create(state_root, "docs/specs/alpha.md", "txn-create-alpha")
    assert resolve.active_run_keys(state_root) == []
    assert transaction.pending(state_root) == ["txn-create-alpha"]

    resolve.recover_pending(state_root)
    assert resolve.active_run_keys(state_root) == [key]


def test_an_explicit_run_key_sees_the_run_only_after_the_entry_point_recovers(project):
    """The explicit-key path reads run.json directly rather than through active_run_keys, and is
    a leaf read for the same reason. Once the entry point has recovered, it resolves."""
    root, state_root = project
    key = _pending_create(state_root, "docs/specs/beta.md", "txn-create-beta")
    with pytest.raises(resolve.RunNotFound):
        resolve.resolve(run_key=key, start=root)

    resolve.recover_pending(state_root)
    assert resolve.resolve(run_key=key, start=root).run["spec_path"] == (
        "docs/specs/beta.md"
    )


def test_the_readers_take_no_locks_so_they_are_safe_to_call_under_one(project):
    """The guard against reintroducing recovery — or any other lock — into the read path. A
    takeover or a repoint resolves while holding owner.lock; a project.lock taken underneath that
    is a lock-order violation (locks.LOCK_ORDER), and one that would only fire when a journal
    happened to be pending, i.e. during crash recovery of an unattended run."""
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    # A journal deliberately left pending: recovery only takes project.lock when one exists, so a
    # guard run against an empty journal directory would pass no matter what the readers did.
    _pending_create(state_root, "docs/specs/beta.md", "txn-create-beta")
    with locks.hold(
        runstate.owner_lock_path(state_root, key), kind="owner", run_key=key
    ):
        assert resolve.active_run_keys(state_root) == [key]
        assert resolve.resolve(run_key=key, start=root).run_key == key
        assert resolve.resolve(start=root).run_key == key
    assert transaction.pending(state_root) == ["txn-create-beta"]


def test_a_run_that_vanishes_mid_resolution_names_the_path_and_the_way_out(
    project, monkeypatch
):
    """The one-active-run branch re-reads the record active_run_keys just loaded. If it is gone,
    that is a real race, and it gets the same actionable failure as every other branch here rather
    than a bare AssertionError — which `python -O` would strip away entirely."""
    root, state_root = project
    monkeypatch.setattr(resolve, "active_run_keys", lambda _root: ["ghost-0badf00d"])
    with pytest.raises(resolve.RunNotFound) as excinfo:
        resolve.resolve(start=root)
    message = str(excinfo.value)
    assert "ghost-0badf00d" in message
    assert runstate.run_path(state_root, "ghost-0badf00d") in message
    assert "no write occurred" in message
    assert "conductor run list --all" in message


def test_resume_script_main_root_delegates_to_the_same_resolver(
    git_repo, git, tmp_path
):
    from conductor import resume_script

    linked = tmp_path / "linked2"
    git(git_repo, "worktree", "add", "-q", "-b", "side2", str(linked))
    assert resume_script.main_root(str(linked)) == resolve.repo_root(str(linked))


# --- the run a WORKTREE belongs to (usage ingest, conductor review) ------------------------


def _checkout(git, repo, tmp_path, name: str, branch: str):
    """A linked worktree of ``repo`` with a new ``branch`` checked out."""
    path = tmp_path / name
    git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return str(path)


def test_run_for_worktree_takes_the_one_active_run_without_a_binding(project):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    assert resolve.run_for_worktree(root).run_key == key


def test_run_for_worktree_picks_the_run_whose_branch_the_worktree_has_checked_out(
    project, git, tmp_path
):
    root, state_root = project
    alpha = _make_run(state_root, "docs/specs/alpha.md")
    beta = _make_run(state_root, "docs/specs/beta.md")
    wt_alpha = _checkout(git, root, tmp_path, "wt-a", f"conductor/run-{alpha}")
    wt_beta = _checkout(git, root, tmp_path, "wt-b", f"conductor/run-{beta}")
    assert resolve.run_for_worktree(wt_alpha).run_key == alpha
    assert resolve.run_for_worktree(wt_beta).run_key == beta


def test_run_for_worktree_picks_the_run_that_records_the_worktree(
    project, git, tmp_path
):
    root, state_root = project
    _make_run(state_root, "docs/specs/alpha.md")
    beta = _make_run(state_root, "docs/specs/beta.md")
    phase = _checkout(git, root, tmp_path, "phase", "some-phase-branch")
    runstate.update(state_root, beta, lambda d: {**d, "phase_worktree": phase})
    found = resolve.run_for_worktree(phase)
    assert found.run_key == beta
    assert found.state_root == state_root


def test_run_for_worktree_finds_a_run_awaiting_the_teams_merge(project, git, tmp_path):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md", status="awaiting-team-merge")
    wt = _checkout(git, root, tmp_path, "wt", f"conductor/run-{key}")
    assert resolve.run_for_worktree(wt).run_key == key


def test_run_for_worktree_ignores_a_terminal_run_bound_to_the_worktree(
    project, git, tmp_path
):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md", status="terminal")
    wt = _checkout(git, root, tmp_path, "wt", f"conductor/run-{key}")
    with pytest.raises(resolve.RunNotFound):
        resolve.run_for_worktree(wt)


def test_run_for_worktree_with_no_bound_run_among_several_is_ambiguous(
    project, git, tmp_path
):
    """Nothing binds the worktree, so the bare resolution decides — and two active runs are
    ambiguous."""
    root, state_root = project
    alpha = _make_run(state_root, "docs/specs/alpha.md")
    beta = _make_run(state_root, "docs/specs/beta.md")
    wt = _checkout(git, root, tmp_path, "wt", "unrelated")
    with pytest.raises(resolve.RunAmbiguous) as excinfo:
        resolve.run_for_worktree(wt)
    assert alpha in str(excinfo.value) and beta in str(excinfo.value)


def test_run_for_worktree_prefers_the_bound_run_over_the_sole_active_one(
    project, git, tmp_path
):
    """Run A's final fire moved A to awaiting-team-merge while B is the only active run: A's
    worktree still means A, never B."""
    root, state_root = project
    alpha = _make_run(state_root, "docs/specs/alpha.md", status="awaiting-team-merge")
    beta = _make_run(state_root, "docs/specs/beta.md")
    wt = _checkout(git, root, tmp_path, "wt-a", f"conductor/run-{alpha}")
    assert resolve.run_for_worktree(wt).run_key == alpha
    assert (
        resolve.run_for_worktree(root).run_key == beta
    )  # unbound: the sole active run


def test_run_for_worktree_with_an_unbound_checkout_takes_the_one_active_run(
    project, git, tmp_path
):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    wt = _checkout(git, root, tmp_path, "wt", "unrelated")
    assert resolve.run_for_worktree(wt).run_key == key


def test_run_for_worktree_with_two_bound_runs_is_ambiguous(project, git, tmp_path):
    root, state_root = project
    alpha = _make_run(state_root, "docs/specs/alpha.md")
    beta = _make_run(state_root, "docs/specs/beta.md")
    wt = _checkout(git, root, tmp_path, "wt", "shared")
    for key in (alpha, beta):
        runstate.update(state_root, key, lambda d: {**d, "integration_worktree": wt})
    with pytest.raises(resolve.RunAmbiguous) as excinfo:
        resolve.run_for_worktree(wt)
    assert alpha in str(excinfo.value) and beta in str(excinfo.value)


def test_a_same_branch_checkout_of_another_repository_is_not_bound(
    project, git, git_env, tmp_path
):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    run = runstate.load(state_root, key)
    assert run is not None
    other = tmp_path / "other"
    subprocess.run(
        ["git", "init", "-q", "-b", run["integration_branch"], str(other)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    unbound = resolve.worktree_unbound(root, str(other), run)
    assert unbound is not None and unbound.foreign
    mine = _checkout(git, root, tmp_path, "mine", run["integration_branch"])
    assert resolve.worktree_unbound(root, mine, run) is None
