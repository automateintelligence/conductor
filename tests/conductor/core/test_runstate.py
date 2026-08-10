"""Per-run state: <project>/.conductor/runs/<run-key>/run.json (design §"Project and run
identity").

Every mutation is a read-modify-write guarded by the short-lived state mutex and the state
revision. A stale writer re-reads and retries rather than replacing a newer value; atomic replace
prevents torn files, the revision prevents lost updates."""

from __future__ import annotations

import os

import pytest

from conductor.core import runkey, runstate, schema

WORKSTATION = "0123456789abcdef0123456789abcdef"
ALPHA = "docs/specs/alpha.md"
NOW = "2026-08-10T12:00:00+00:00"


def _doc(key):
    return schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=ALPHA,
        workstation_id=WORKSTATION,
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )


@pytest.fixture
def run(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    runstate.create(state_root, key, _doc(key))
    return state_root, key


def test_paths_are_namespaced_under_the_run_key(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    assert runstate.run_dir(state_root, key) == os.path.join(state_root, "runs", key)
    assert runstate.run_path(state_root, key).endswith(f"runs/{key}/run.json")
    assert runstate.state_lock_path(state_root, key).endswith(f"runs/{key}/state.lock")
    assert runstate.owner_lock_path(state_root, key).endswith(f"runs/{key}/owner.lock")


def test_an_unsafe_run_key_never_reaches_the_filesystem(tmp_path):
    with pytest.raises(ValueError):
        runstate.run_dir(str(tmp_path / ".conductor"), "../escape")


def test_load_returns_none_for_an_unknown_run(tmp_path):
    assert runstate.load(str(tmp_path / ".conductor"), runkey.run_key(ALPHA)) is None


def test_create_writes_a_validated_record_and_refuses_to_overwrite(run):
    state_root, key = run
    assert runstate.load(state_root, key)["run_key"] == key
    with pytest.raises(runstate.RunExists):
        runstate.create(state_root, key, _doc(key))


def test_create_refuses_a_document_whose_key_disagrees(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    with pytest.raises(ValueError):
        runstate.create(state_root, runkey.run_key("docs/specs/beta.md"), _doc(key))


def test_update_bumps_the_revision_and_refreshes_updated_at(run):
    state_root, key = run
    doc = runstate.update(state_root, key, lambda d: {**d, "current_phase": "phase-1"})
    assert doc["revision"] == 1
    assert doc["current_phase"] == "phase-1"
    assert doc["updated_at"] != NOW


def test_commit_with_a_stale_revision_is_refused_and_writes_nothing(run):
    state_root, key = run
    stale = runstate.load(state_root, key)
    runstate.update(state_root, key, lambda d: {**d, "current_phase": "phase-1"})
    with pytest.raises(runstate.RevisionConflict):
        runstate.commit(
            state_root, key, {**stale, "current_phase": "phase-9"}, expect_revision=0
        )
    assert runstate.load(state_root, key)["current_phase"] == "phase-1"


def test_update_re_reads_and_retries_after_a_concurrent_write(run):
    state_root, key = run
    calls = {"n": 0}

    def mutate(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            current = runstate.load(state_root, key)
            runstate.commit(
                state_root,
                key,
                {**current, "ledger_ref": "#42"},
                expect_revision=current["revision"],
            )
        return {**doc, "current_phase": "phase-1"}

    result = runstate.update(state_root, key, mutate)
    assert calls["n"] == 2
    assert result["revision"] == 2
    assert result["ledger_ref"] == "#42"
    assert result["current_phase"] == "phase-1"


def test_an_invalid_mutation_is_refused_before_it_reaches_disk(run):
    state_root, key = run
    with pytest.raises(schema.SchemaError):
        runstate.update(state_root, key, lambda d: {**d, "status": "running"})
    assert runstate.load(state_root, key)["status"] == "active"


def test_set_status_enforces_the_transition_table(run):
    state_root, key = run
    assert (
        runstate.set_status(state_root, key, "checkpointed")["status"] == "checkpointed"
    )
    assert runstate.set_status(state_root, key, "active")["status"] == "active"
    assert runstate.set_status(state_root, key, "awaiting-team-merge")["status"] == (
        "awaiting-team-merge"
    )
    assert runstate.set_status(state_root, key, "terminal")["status"] == "terminal"
    with pytest.raises(schema.SchemaError):
        runstate.set_status(state_root, key, "active")


def test_set_status_stamps_the_completion_and_failure_timestamps(run):
    state_root, key = run
    runstate.set_status(state_root, key, "awaiting-team-merge")
    doc = runstate.set_status(state_root, key, "terminal")
    assert doc["completed_at"] and doc["failed_at"] is None
    state_root2, key2 = state_root, runkey.run_key("docs/specs/beta.md")
    other = _doc(key2)
    other["spec_path"] = "docs/specs/beta.md"
    other["integration_branch"] = f"conductor/run-{key2}"
    other["gate_dir"] = f"assertions/{key2}"
    runstate.create(state_root2, key2, other)
    failed = runstate.set_status(state_root2, key2, "failed")
    assert failed["failed_at"] and failed["completed_at"] is None


def test_update_on_a_missing_run_names_the_listing_command(tmp_path):
    with pytest.raises(runstate.RunMissing) as excinfo:
        runstate.update(
            str(tmp_path / ".conductor"), runkey.run_key(ALPHA), lambda d: d
        )
    assert "conductor run list --all" in str(excinfo.value)
