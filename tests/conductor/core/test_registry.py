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
    with pytest.raises(schema.SchemaError):
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
    after = registry.register(dict(doc), spec=ALPHA, run_key=key, generation=1)
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-repoint",
        [{"path": registry.registry_path(state_root), "before": doc, "after": after}],
    )
    transaction.commit(state_root, "txn-repoint")
    refreshed = registry.update(state_root, lambda d: d)
    assert registry.current_run_key(refreshed, ALPHA) == key
    assert transaction.pending(state_root) == []


def test_the_mutate_callback_cannot_alter_the_on_disk_snapshot(state_root):
    def mutate(doc):
        doc["specs"]["docs/specs/ghost.md"] = {"generations": [], "current": None}
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        registry.update(state_root, mutate)
    assert registry.load(state_root)["specs"] == {}
