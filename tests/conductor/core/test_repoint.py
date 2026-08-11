"""Renaming or moving a spec within the repository never silently creates a second run
(design §"Project and run identity").

repoint-spec runs only without a live owner, acquires project.lock, owner.lock, then state.lock,
verifies that the old and new paths describe the same Git rename or approved digest, rejects
mapping collisions, and journals then applies the project.json and run.json updates so recovery
completes or reverses both while retaining the run key and a path-history audit."""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from conductor.core import (
    atomic,
    locks,
    registry,
    repoint,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
)

WORKSTATION = "0123456789abcdef0123456789abcdef"
NOW = "2026-08-10T12:00:00+00:00"
ALPHA = "docs/specs/alpha.md"
MOVED = "docs/specs/archive/alpha.md"


@pytest.fixture
def project(git_repo, git):
    root = str(git_repo)
    state_root = resolve.state_root(root)
    registry.init(
        state_root,
        workstation_id=WORKSTATION,
        repo_identity=resolve.repo_identity(root),
    )
    key = runkey.run_key(ALPHA)
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path=ALPHA,
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest=digest,
            now=NOW,
        ),
    )
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    return root, state_root, key


def _move(git_repo, git, old_rel, new_rel):
    os.makedirs(os.path.dirname(str(git_repo / new_rel)), exist_ok=True)
    git(git_repo, "mv", old_rel, new_rel)


def _register_run(state_root, spec, *, digest="b" * 64):
    """Create and register a second run at ``spec`` so collisions can be exercised."""
    key = runkey.run_key(spec)
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path=spec,
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest=digest,
            now=NOW,
        ),
    )
    registry.update(
        state_root, lambda d: registry.register(d, spec=spec, run_key=key, generation=1)
    )
    return key


def _documents(state_root, run_key):
    """The exact bytes of both state files, so "no write occurred" can be checked literally."""
    return (
        pathlib.Path(registry.registry_path(state_root)).read_bytes(),
        pathlib.Path(runstate.run_path(state_root, run_key)).read_bytes(),
    )


def test_repoint_keeps_the_run_key_and_records_the_path_history(project, git_repo, git):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["run_key"] == key
    assert doc["spec_path"] == MOVED
    assert doc["path_history"] == [ALPHA]
    registry_doc = registry.load(state_root)
    assert registry.current_run_key(registry_doc, MOVED) == key
    assert ALPHA not in registry_doc["specs"]
    assert registry_doc["specs"][MOVED]["path_history"] == [ALPHA]


def test_the_gate_dir_and_integration_branch_are_untouched_by_a_repoint(
    project, git_repo, git
):
    root, state_root, key = project
    before = runstate.load(state_root, key)
    _move(git_repo, git, ALPHA, MOVED)
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["gate_dir"] == before["gate_dir"]
    assert doc["integration_branch"] == before["integration_branch"]


def test_a_digest_match_authorizes_a_repoint_without_a_staged_rename(project, git_repo):
    root, state_root, key = project
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / MOVED).write_bytes((git_repo / ALPHA).read_bytes())
    (git_repo / ALPHA).unlink()
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["spec_path"] == MOVED


def test_an_unrelated_target_is_refused(project, git_repo, git):
    root, state_root, key = project
    (git_repo / "docs" / "specs" / "unrelated.md").write_text("# totally different\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "unrelated")
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root,
            repo_root=root,
            run_key=key,
            new_spec_path="docs/specs/unrelated.md",
        )
    assert "same" in str(excinfo.value).lower()
    assert runstate.load(state_root, key)["spec_path"] == ALPHA


def test_a_mapping_collision_is_refused(project, git_repo, git):
    root, state_root, key = project
    beta_key = runkey.run_key("docs/specs/beta.md")
    runstate.create(
        state_root,
        beta_key,
        schema.new_run_doc(
            run_key=beta_key,
            generation=1,
            spec_path="docs/specs/beta.md",
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{beta_key}",
            gate_dir=f"assertions/{beta_key}",
            spec_digest="b" * 64,
            now=NOW,
        ),
    )
    registry.update(
        state_root,
        lambda d: registry.register(
            d, spec="docs/specs/beta.md", run_key=beta_key, generation=1
        ),
    )
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/beta.md"
        )
    assert beta_key in str(excinfo.value)


def test_a_missing_target_file_is_refused(project):
    root, state_root, key = project
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/ghost.md"
        )
    assert "does not exist" in str(excinfo.value)


def test_a_path_outside_the_repository_is_refused(project):
    root, state_root, key = project
    with pytest.raises(repoint.RepointRefused):
        repoint.repoint(
            state_root,
            repo_root=root,
            run_key=key,
            new_spec_path="../elsewhere/alpha.md",
        )


def test_a_live_owner_blocks_the_repoint(project, git_repo, git):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    holder = os.open(
        runstate.owner_lock_path(state_root, key), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        import fcntl

        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(repoint.RepointRefused) as excinfo:
            repoint.repoint(
                state_root,
                repo_root=root,
                run_key=key,
                new_spec_path=MOVED,
                owner_timeout=0.05,
            )
        assert "owner" in str(excinfo.value).lower()
    finally:
        os.close(holder)
    assert runstate.load(state_root, key)["spec_path"] == ALPHA


def test_a_crash_after_prepare_reverses_both_files(project, git_repo, git, monkeypatch):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)

    class Crash(RuntimeError):
        pass

    monkeypatch.setattr(
        repoint.transaction, "commit", lambda *_a, **_k: (_ for _ in ()).throw(Crash())
    )
    with pytest.raises(Crash):
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    # The journal decides the direction, and at this crash point it says "prepared": both
    # targets still hold their before images, so only the journal distinguishes this state
    # from the crash-after-commit one below.
    assert transaction.pending(state_root) == ["repoint-" + key]
    journal = atomic.read_json(transaction.journal_path(state_root, "repoint-" + key))
    assert journal is not None and journal["state"] == "prepared"
    assert transaction.recover(state_root) == ["repoint-" + key]
    assert runstate.load(state_root, key)["spec_path"] == ALPHA
    assert registry.current_run_key(registry.load(state_root), ALPHA) == key
    assert transaction.pending(state_root) == []


def test_a_crash_after_commit_rolls_both_files_forward(
    project, git_repo, git, monkeypatch
):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)

    class Crash(RuntimeError):
        pass

    monkeypatch.setattr(
        repoint.transaction, "apply", lambda *_a, **_k: (_ for _ in ()).throw(Crash())
    )
    with pytest.raises(Crash):
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    # Same on-disk state as the crash-after-prepare case — commit only flips the journal — so
    # the opposite recovery outcome below is attributable to the journal state alone.
    assert runstate.load(state_root, key)["spec_path"] == ALPHA
    journal = atomic.read_json(transaction.journal_path(state_root, "repoint-" + key))
    assert journal is not None and journal["state"] == "committed"
    assert transaction.recover(state_root) == ["repoint-" + key]
    assert runstate.load(state_root, key)["spec_path"] == MOVED
    assert registry.current_run_key(registry.load(state_root), MOVED) == key
    assert transaction.pending(state_root) == []


def test_repointing_to_the_same_path_is_a_no_op(project):
    root, state_root, key = project
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=ALPHA)
    assert doc["path_history"] == []
    assert doc["revision"] == runstate.load(state_root, key)["revision"]


def test_repoint_takes_the_locks_in_the_documented_order(
    project, git_repo, git, monkeypatch
):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    order: list[str] = []
    original = locks.hold

    def spy(path, *, kind, **kwargs):
        order.append(kind)
        return original(path, kind=kind, **kwargs)

    monkeypatch.setattr(repoint.locks, "hold", spy)
    repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert order == ["project", "owner", "state"]


def test_a_terminal_mapping_at_the_target_path_is_refused_rather_than_overwritten(
    project, git_repo, git
):
    """A finished run at the destination still owns that path.

    ``registry.current_run_key`` reports ``None`` for a fully terminal mapping, so a collision
    check that consults it alone finds no collision, replaces the mapping wholesale and silently
    drops the finished run's generation history — the registry stops listing that run at all."""
    root, state_root, key = project
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / MOVED).write_text("# an earlier, finished run lived here\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "archived spec")
    archived = _register_run(state_root, MOVED)
    runstate.set_status(state_root, archived, "awaiting-team-merge")
    runstate.set_status(state_root, archived, "terminal")
    registry.update(
        state_root, lambda d: registry.mirror_status(d, archived, "terminal")
    )
    assert registry.current_run_key(registry.load(state_root), MOVED) is None
    # Content identity holds — the digest authorizes the move — so only the collision check
    # stands between this repoint and the archived run's history.
    (git_repo / MOVED).write_bytes((git_repo / ALPHA).read_bytes())
    (git_repo / ALPHA).unlink()

    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert archived in str(excinfo.value)
    after = registry.load(state_root)
    assert [g["run_key"] for g in after["specs"][MOVED]["generations"]] == [archived]
    assert registry.current_run_key(after, ALPHA) == key
    assert runstate.load(state_root, key)["spec_path"] == ALPHA


def test_a_run_record_that_disagrees_with_the_registry_is_refused(
    project, git_repo, git
):
    """The registry names the path the run is being moved FROM, and ``run.json`` is supposed to
    agree. When they diverge, repointing would write a path history the run never had and erase
    the evidence of the divergence, so it fails closed and names both paths."""
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    runstate.update(
        state_root, key, lambda d: {**d, "spec_path": "docs/specs/other.md"}
    )
    before = runstate.load(state_root, key)
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    message = str(excinfo.value)
    assert ALPHA in message and "docs/specs/other.md" in message
    assert runstate.load(state_root, key) == before
    assert registry.current_run_key(registry.load(state_root), ALPHA) == key


def test_every_refusal_writes_nothing_and_leaves_no_journal(project, git_repo, git):
    """Each refusal must land before the transaction starts: both documents byte-identical
    afterwards, and no journal left behind for a later entry point to roll forward."""
    root, state_root, key = project
    (git_repo / "docs" / "specs" / "unrelated.md").write_text("# totally different\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "unrelated")
    before = _documents(state_root, key)
    for target in (
        "docs/specs/ghost.md",  # missing target
        "../elsewhere/alpha.md",  # outside the repository
        "docs/specs/unrelated.md",  # not the same spec
    ):
        with pytest.raises(repoint.RepointRefused):
            repoint.repoint(
                state_root, repo_root=root, run_key=key, new_spec_path=target
            )
        assert _documents(state_root, key) == before
        assert transaction.pending(state_root) == []

    holder = os.open(
        runstate.owner_lock_path(state_root, key), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        import fcntl

        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(repoint.RepointRefused):  # live owner
            repoint.repoint(
                state_root,
                repo_root=root,
                run_key=key,
                new_spec_path="docs/specs/beta.md",
                owner_timeout=0.05,
            )
    finally:
        os.close(holder)
    assert _documents(state_root, key) == before
    assert transaction.pending(state_root) == []


def test_an_unregistered_run_key_is_refused(project):
    root, state_root, _key = project
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key="ghost-deadbeef", new_spec_path=ALPHA
        )
    assert "ghost-deadbeef" in str(excinfo.value)
    assert "no write occurred" in str(excinfo.value)
