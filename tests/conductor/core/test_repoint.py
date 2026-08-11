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
# Long enough that editing one line leaves git's default 50% rename similarity satisfied, so a
# rename that also changed the spec is still detected as a rename.
LONG_SPEC = "# alpha\n\nline one\nline two\nline three\nline four\nline five\n"


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


def _register_run(state_root, spec, *, generation=1, digest="b" * 64):
    """Create and register a run at ``spec``, so collisions and multi-generation mappings can be
    exercised."""
    key = runkey.run_key(spec, generation)
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=generation,
            spec_path=spec,
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest=digest,
            now=NOW,
        ),
    )
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=spec, run_key=key, generation=generation),
    )
    return key


def _finish(state_root, run_key):
    """Drive a run to terminal in both the record and the registry mirror, which is what a spec
    path with several generations looks like in practice."""
    runstate.set_status(state_root, run_key, "awaiting-team-merge")
    runstate.set_status(state_root, run_key, "terminal")
    registry.update(
        state_root, lambda d: registry.mirror_status(d, run_key, "terminal")
    )


def _documents(state_root, *run_keys):
    """The exact bytes of every state file involved, so "no write occurred" can be checked
    literally rather than semantically."""
    return (
        pathlib.Path(registry.registry_path(state_root)).read_bytes(),
        tuple(
            pathlib.Path(runstate.run_path(state_root, key)).read_bytes()
            for key in run_keys
        ),
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
    # A repoint is a mutation, so it stamps updated_at exactly as runstate.commit would have,
    # and the stamp is what landed on disk rather than only in the returned copy.
    assert doc["updated_at"] != before["updated_at"]
    assert doc["updated_at"] == runstate.load(state_root, key)["updated_at"]


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
    before = _documents(state_root, key, beta_key)
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/beta.md"
        )
    assert beta_key in str(excinfo.value)
    assert repr(key) in str(excinfo.value)
    assert _documents(state_root, key, beta_key) == before
    assert transaction.pending(state_root) == []


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
    before = _documents(state_root, key)
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=ALPHA)
    assert doc["path_history"] == []
    # Byte-identical documents and no journal: a revision bump, a re-stamped updated_at or a
    # rewritten mapping would all show up here, which comparing the return value against a
    # re-read of the same file cannot.
    assert _documents(state_root, key) == before
    assert transaction.pending(state_root) == []


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
    assert repr(key) in str(excinfo.value)
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


def test_a_staged_rename_authorizes_a_repoint_whose_content_also_changed(
    project, git_repo, git
):
    """The one case no digest can cover: the spec was edited in the same move, so the recorded
    digest matches nothing on disk and only rename detection can authorize the repoint."""
    root, state_root, key = project
    (git_repo / ALPHA).write_text(LONG_SPEC)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "grow alpha")
    _move(git_repo, git, ALPHA, MOVED)
    (git_repo / MOVED).write_text(LONG_SPEC.replace("line two", "line two, revised"))
    moved_digest = hashlib.sha256((git_repo / MOVED).read_bytes()).hexdigest()
    assert moved_digest != runstate.load(state_root, key)["spec_digest"]

    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["spec_path"] == MOVED
    assert doc["path_history"] == [ALPHA]


def test_a_rename_that_was_already_committed_is_still_recognized(
    project, git_repo, git
):
    """`git mv` followed by `git commit` is a normal thing to do before repointing, and it leaves
    the working tree clean — so the working-tree diff sees nothing and only history does."""
    root, state_root, key = project
    (git_repo / ALPHA).write_text(LONG_SPEC)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "grow alpha")
    _move(git_repo, git, ALPHA, MOVED)
    (git_repo / MOVED).write_text(LONG_SPEC.replace("line two", "line two, revised"))
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "move and edit alpha")
    assert git(git_repo, "diff", "-M", "--name-status", "HEAD").stdout == ""
    assert (
        hashlib.sha256((git_repo / MOVED).read_bytes()).hexdigest()
        != runstate.load(state_root, key)["spec_digest"]
    )

    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["spec_path"] == MOVED


def test_a_refusal_does_not_advise_git_mv_once_the_old_path_is_gone(project, git_repo):
    """Every refusal owes an exact recovery command, and `git mv <old> <new>` stops being one the
    moment the old path no longer exists."""
    root, state_root, key = project
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / MOVED).write_text("# a different spec entirely\n")
    (git_repo / ALPHA).unlink()
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    message = str(excinfo.value)
    assert "git mv" not in message
    assert "no longer exists" in message
    assert f"conductor run new {MOVED}" in message


def test_every_generation_of_the_mapping_moves_with_the_registry(
    project, git_repo, git
):
    """The registry moves a spec path's whole entry, so every generation's record has to move in
    the same transaction — a sibling left behind would claim the old path while the registry
    reports the new one, which is the divergence repoint itself refuses to operate on."""
    root, state_root, first = project
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    _finish(state_root, first)
    second = _register_run(state_root, ALPHA, generation=2, digest=digest)
    _move(git_repo, git, ALPHA, MOVED)

    doc = repoint.repoint(
        state_root, repo_root=root, run_key=second, new_spec_path=MOVED
    )
    assert doc["run_key"] == second and doc["spec_path"] == MOVED
    sibling = runstate.load(state_root, first)
    assert sibling["spec_path"] == MOVED
    assert sibling["path_history"] == [ALPHA]
    assert sibling["status"] == "terminal"
    registry_doc = registry.load(state_root)
    assert ALPHA not in registry_doc["specs"]
    assert registry.find_run(registry_doc, first)[0] == MOVED
    assert registry.find_run(registry_doc, second)[0] == MOVED
    assert [g["run_key"] for g in registry_doc["specs"][MOVED]["generations"]] == [
        first,
        second,
    ]
    assert registry.current_run_key(registry_doc, MOVED) == second


def test_a_divergent_sibling_generation_refuses_the_whole_repoint(
    project, git_repo, git
):
    root, state_root, first = project
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    _finish(state_root, first)
    second = _register_run(state_root, ALPHA, generation=2, digest=digest)
    runstate.update(
        state_root, first, lambda d: {**d, "spec_path": "docs/specs/other.md"}
    )
    _move(git_repo, git, ALPHA, MOVED)
    before = _documents(state_root, first, second)

    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=second, new_spec_path=MOVED)
    message = str(excinfo.value)
    assert f"run {first!r} disagrees" in message
    assert f"run {second!r}" in message
    assert "docs/specs/other.md" in message
    assert _documents(state_root, first, second) == before
    assert transaction.pending(state_root) == []


def test_multi_run_locks_are_taken_in_sorted_run_key_order(
    project, git_repo, git, monkeypatch
):
    """Design §"Failure handling": multi-run project operations take run locks in sorted run-key
    order. All owner locks precede all state locks, which is also the only order
    ``locks._check_order`` permits once a state lock is held."""
    root, state_root, first = project
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    _finish(state_root, first)
    second = _register_run(state_root, ALPHA, generation=2, digest=digest)
    _move(git_repo, git, ALPHA, MOVED)
    assert sorted((second, first)) == [first, second]
    order: list[tuple[str, str | None]] = []
    original = locks.hold

    def spy(path, *, kind, run_key=None, **kwargs):
        order.append((kind, run_key))
        return original(path, kind=kind, run_key=run_key, **kwargs)

    monkeypatch.setattr(repoint.locks, "hold", spy)
    repoint.repoint(state_root, repo_root=root, run_key=second, new_spec_path=MOVED)
    assert order == [
        ("project", None),
        ("owner", first),
        ("owner", second),
        ("state", first),
        ("state", second),
    ]


def test_a_live_owner_on_a_sibling_generation_blocks_the_repoint(
    project, git_repo, git
):
    """A terminal sibling is not necessarily ownerless, and its record moves too, so its owner
    lock is held like any other."""
    root, state_root, first = project
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    _finish(state_root, first)
    second = _register_run(state_root, ALPHA, generation=2, digest=digest)
    _move(git_repo, git, ALPHA, MOVED)
    before = _documents(state_root, first, second)
    holder = os.open(
        runstate.owner_lock_path(state_root, first), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        import fcntl

        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(repoint.RepointRefused) as excinfo:
            repoint.repoint(
                state_root,
                repo_root=root,
                run_key=second,
                new_spec_path=MOVED,
                owner_timeout=0.05,
            )
    finally:
        os.close(holder)
    message = str(excinfo.value)
    assert f"run {first!r} has a live owner" in message
    assert f"--run {second}" in message
    assert _documents(state_root, first, second) == before
    assert transaction.pending(state_root) == []


def test_an_unregistered_run_key_is_refused(project):
    root, state_root, _key = project
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key="ghost-deadbeef", new_spec_path=ALPHA
        )
    assert "ghost-deadbeef" in str(excinfo.value)
    assert "no write occurred" in str(excinfo.value)
