"""project.json — one registry per project (design §"Project and run identity").

Records the state schema, its own monotonic revision, stable repository identity,
normalized-spec-path-to-run-key mappings, and the workstation that owns this project-local state.
Each spec-path mapping is an ordered generation list with at most one nonterminal run designated
current.

Mutations are guarded by project.lock plus a revision compare-and-swap: a stale writer re-reads
and retries rather than replacing a newer value."""

from __future__ import annotations

import pytest

from conductor.core import registry, runkey, schema, transaction

WORKSTATION = "0123456789abcdef0123456789abcdef"
IDENTITY = {"root_commit": "abc123", "origin_url": "git@example.invalid:x/y.git"}
ALPHA = "docs/specs/alpha.md"
BETA = "docs/specs/beta.md"


@pytest.fixture
def state_root(tmp_path):
    root = str(tmp_path / ".conductor")
    registry.init(root, workstation_id=WORKSTATION, repo_identity=IDENTITY)
    return root


def test_load_returns_none_before_init(tmp_path):
    assert registry.load(str(tmp_path / ".conductor")) is None


def test_init_is_idempotent_and_does_not_bump_the_revision(tmp_path):
    root = str(tmp_path / ".conductor")
    first = registry.init(root, workstation_id=WORKSTATION, repo_identity=IDENTITY)
    second = registry.init(root, workstation_id="different", repo_identity={})
    assert first == second
    assert second["revision"] == 0
    assert second["workstation_id"] == WORKSTATION


def test_register_maps_a_spec_to_its_first_generation(state_root):
    key = runkey.run_key(ALPHA)
    doc = registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    assert doc["revision"] == 1
    assert registry.current_run_key(doc, ALPHA) == key
    assert registry.run_keys(doc) == [key]
    assert registry.find_run(doc, key) == (ALPHA, doc["specs"][ALPHA]["generations"][0])


def test_next_generation_is_one_for_an_unmapped_spec(state_root):
    assert registry.next_generation(registry.load(state_root), ALPHA) == 1


def test_next_generation_follows_the_highest_recorded_generation(state_root):
    key = runkey.run_key(ALPHA)
    doc = registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    doc = registry.update(
        state_root, lambda d: registry.mirror_status(d, key, "terminal")
    )
    assert registry.next_generation(doc, ALPHA) == 2
    doc = registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=f"{key}-g2", generation=2),
    )
    assert registry.current_run_key(doc, ALPHA) == f"{key}-g2"
    assert registry.next_generation(doc, ALPHA) == 3


def test_registering_a_second_nonterminal_generation_is_refused(state_root):
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    # Matched on the count check's own phrase: `register` also moves `current` onto the new
    # generation, so an unmatched `pytest.raises(SchemaError)` here passes on the `current`
    # consistency check too and proves nothing about the invariant this test is named for.
    with pytest.raises(schema.SchemaError, match="at most one is allowed"):
        registry.update(
            state_root,
            lambda d: registry.register(
                d, spec=ALPHA, run_key=f"{key}-g2", generation=2
            ),
        )


def test_mirror_status_moves_current_when_a_run_becomes_terminal(state_root):
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    doc = registry.update(
        state_root, lambda d: registry.mirror_status(d, key, "terminal")
    )
    assert registry.current_run_key(doc, ALPHA) is None


def test_two_specs_hold_independent_mappings(state_root):
    alpha_key, beta_key = runkey.run_key(ALPHA), runkey.run_key(BETA)
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=alpha_key, generation=1),
    )
    doc = registry.update(
        state_root,
        lambda d: registry.register(d, spec=BETA, run_key=beta_key, generation=1),
    )
    assert registry.run_keys(doc) == sorted([alpha_key, beta_key])
    assert registry.current_run_key(doc, ALPHA) == alpha_key
    assert registry.current_run_key(doc, BETA) == beta_key


def test_commit_with_a_stale_revision_is_refused_and_writes_nothing(state_root):
    stale = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1),
    )
    with pytest.raises(registry.RevisionConflict) as excinfo:
        registry.commit(
            state_root,
            registry.register(
                stale, spec=BETA, run_key=runkey.run_key(BETA), generation=1
            ),
            expect_revision=0,
        )
    assert "revision" in str(excinfo.value)
    assert registry.run_keys(registry.load(state_root)) == [key]


def test_update_re_reads_and_retries_after_a_concurrent_write(state_root):
    """The first mutate call sees revision 0; a concurrent writer lands revision 1 underneath it;
    update must re-read and apply the mutation on top of the newer value, not replace it."""
    beta_key = runkey.run_key(BETA)
    calls = {"n": 0}

    def mutate(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            registry.commit(
                state_root,
                registry.register(
                    registry.load(state_root),
                    spec=ALPHA,
                    run_key=runkey.run_key(ALPHA),
                    generation=1,
                ),
                expect_revision=0,
            )
        return registry.register(doc, spec=BETA, run_key=beta_key, generation=1)

    result = registry.update(state_root, mutate)
    assert calls["n"] == 2
    assert result["revision"] == 2
    assert registry.run_keys(result) == sorted([runkey.run_key(ALPHA), beta_key])


def test_update_gives_up_after_the_attempt_budget(state_root):
    def mutate(doc):
        registry.commit(
            state_root,
            registry.load(state_root),
            expect_revision=registry.load(state_root)["revision"],
        )
        return doc

    with pytest.raises(registry.RevisionConflict):
        registry.update(state_root, mutate, attempts=2)


def test_update_on_a_missing_registry_names_the_init_path(tmp_path):
    with pytest.raises(registry.RegistryMissing) as excinfo:
        registry.update(str(tmp_path / ".conductor"), lambda d: d)
    assert "conductor run new" in str(excinfo.value)


def test_commit_completes_an_unfinished_transaction_before_reading(state_root):
    """Design line 450: every project entry point completes or reverses an unfinished transaction
    before reading mappings, so a crash cannot leave a silently split identity."""
    doc = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    # Use deep copy so before and after genuinely differ; shallow copy would alias nested dicts.
    after = registry.register(schema.clone(doc), spec=ALPHA, run_key=key, generation=1)
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-repoint",
        [{"path": registry.registry_path(state_root), "before": doc, "after": after}],
    )
    transaction.commit(state_root, "txn-repoint")
    refreshed = registry.update(state_root, lambda d: d)
    # After recovery-triggering update, assert ALPHA registration IS present
    # (only true if journal rolled forward, not backward).
    assert registry.current_run_key(refreshed, ALPHA) == key
    assert transaction.pending(state_root) == []


def test_init_completes_an_unfinished_transaction_before_reading(tmp_path):
    """init() must call transaction.recover() before reading, so a crash between
    transaction.commit() and apply does not leave init() silently returning stale state.
    No other registry call should precede init() here — if any other call triggers
    recovery first, this test passes whether or not init() was fixed."""
    state_root = str(tmp_path / ".conductor")
    # Create and initialize the registry at revision 0.
    first = registry.init(
        state_root, workstation_id=WORKSTATION, repo_identity=IDENTITY
    )
    assert first["revision"] == 0

    # Build a transaction that registers ALPHA with revision 1, leaving it committed but unapplied.
    doc = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    # Use deep copy so before and after genuinely differ; shallow copy would alias nested dicts.
    after = registry.register(schema.clone(doc), spec=ALPHA, run_key=key, generation=1)
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-register-alpha",
        [{"path": registry.registry_path(state_root), "before": doc, "after": after}],
    )
    transaction.commit(state_root, "txn-register-alpha")

    # Call init() again — it must recover the pending transaction before reading.
    recovered = registry.init(
        state_root, workstation_id=WORKSTATION, repo_identity=IDENTITY
    )
    # Assert the returned document already contains the ALPHA registration from the recovered transaction.
    assert registry.current_run_key(recovered, ALPHA) == key
    assert transaction.pending(state_root) == []


def test_a_refusal_after_recovery_does_not_claim_no_write_occurred(state_root):
    """``commit`` recovers before it validates, and ``recover`` WRITES — after-images land and
    the journal is removed, irreversibly. A refusal that follows it therefore cannot print the
    house "no write occurred" phrase: project.json genuinely changed and the journal is genuinely
    gone, so an operator told nothing happened would go looking for a document that has moved.

    ``registry.commit`` stands in for all three recover-then-refuse sites (the other two are
    ``run_cmd.cmd_new`` and ``repoint.repoint``); all three take the phrase from the one
    ``transaction.write_status``."""
    key = runkey.run_key(ALPHA)
    before = registry.load(state_root)
    assert before is not None
    after = registry.register(
        schema.clone(before), spec=ALPHA, run_key=key, generation=1
    )
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-register-alpha",
        [
            {
                "path": registry.registry_path(state_root),
                "before": before,
                "after": after,
            }
        ],
    )
    transaction.commit(state_root, "txn-register-alpha")

    # Stale expectation: recovery moves the revision 0 -> 1, so the CAS refuses.
    with pytest.raises(registry.RevisionConflict) as excinfo:
        registry.commit(state_root, schema.clone(before), expect_revision=0)
    message = str(excinfo.value)
    assert "txn-register-alpha" in message
    assert "no further write occurred" in message
    assert "no write occurred" not in message
    # And the write the message admits to really did happen.
    recovered = registry.load(state_root)
    assert recovered is not None
    assert registry.current_run_key(recovered, ALPHA) == key
    assert transaction.pending(state_root) == []


def test_a_refusal_with_nothing_recovered_keeps_the_plain_phrase(state_root):
    """The control: no journal pending, so the honest phrase is the plain one. Without this a
    fix for the case above could simply print the qualified phrase unconditionally."""
    doc = registry.load(state_root)
    assert doc is not None
    with pytest.raises(registry.RevisionConflict) as excinfo:
        registry.commit(state_root, doc, expect_revision=99)
    assert "no write occurred" in str(excinfo.value)


def test_the_mutate_callback_cannot_alter_the_on_disk_snapshot(state_root):
    def mutate(doc):
        doc["specs"]["docs/specs/ghost.md"] = {"generations": [], "current": None}
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        registry.update(state_root, mutate)
    assert registry.load(state_root)["specs"] == {}
