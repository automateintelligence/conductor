"""run.json / project.json shapes and the exact state vocabularies (design §"Project and run
identity").

The vocabularies are closed sets on purpose: a typo'd status is how an unattended run silently
stops counting as active. Transitions are checked too — a run becomes ``failed`` only when a
recorded invariant violation leaves no safe retry, and recoverable stops use ``blocked``."""

from __future__ import annotations

import copy

import pytest

from conductor.core import runkey, schema

NOW = "2026-08-10T12:00:00+00:00"


def _run(**overrides):
    rel = "docs/specs/alpha.md"
    key = runkey.run_key(rel)
    doc = schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=rel,
        workstation_id="0123456789abcdef0123456789abcdef",
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )
    doc.update(overrides)
    return doc


def test_vocabularies_are_exactly_the_design_sets():
    assert schema.RUN_STATUSES == (
        "active",
        "checkpointed",
        "blocked",
        "awaiting-team-merge",
        "terminal",
        "failed",
    )
    assert schema.ACTIVE_STATUSES == ("active", "checkpointed", "blocked")
    assert schema.TERMINAL_STATUSES == ("terminal", "failed")
    assert schema.REVIEW_POLICIES == (
        "opposite-required",
        "same-host-fallback-allowed",
        "blocked-pending-opposite-host",
    )
    assert schema.IDENTITY_SCHEMES == ("path-hash-v2", "legacy-slug-v1")


def test_is_active_classifies_the_three_active_statuses_only():
    assert [s for s in schema.RUN_STATUSES if schema.is_active(s)] == [
        "active",
        "checkpointed",
        "blocked",
    ]


def test_a_new_run_doc_validates_and_defaults_to_opposite_required_review():
    doc = _run()
    assert schema.validate_run(doc) == doc
    assert doc["status"] == "active"
    assert doc["review_policy"] == "opposite-required"
    assert doc["revision"] == 0
    assert doc["identity_scheme"] == "path-hash-v2"


def test_run_doc_carries_every_field_later_plans_populate():
    doc = _run()
    for field in (
        "current_phase",
        "phase_ids",
        "plan_digest",
        "ledger_ref",
        "goal_digest",
        "assertion_digest",
        "worker_host",
        "reviewer_host",
        "phase_reviews",
        "last_review_head_sha",
        "dispatches",
        "github",
        "heartbeat",
        "lease",
        "integration_worktree",
        "phase_branch",
        "phase_worktree",
        "last_reconciled_at",
        "last_checkpoint_at",
        "completed_at",
        "failed_at",
        "path_history",
    ):
        assert field in doc, field


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "running"),
        ("review_policy", "whatever"),
        ("identity_scheme", "v3"),
        ("run_key", "../escape"),
        ("generation", 0),
        ("gate_dir", "/etc/passwd"),
        ("gate_dir", "assertions/../outside"),
        ("integration_branch", ""),
        ("revision", -1),
    ],
)
def test_invalid_run_fields_are_refused(field, value):
    doc = _run(**{field: value})
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


def test_generation_must_agree_with_the_suffix_in_the_run_key():
    doc = _run(generation=2)
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(doc)
    assert "generation" in str(excinfo.value)


def test_a_missing_required_field_is_refused():
    doc = _run()
    del doc["spec_path"]
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


@pytest.mark.parametrize(
    "field", ["spec_path", "spec_digest", "integration_branch", "workstation_id"]
)
@pytest.mark.parametrize("value", [None, "", 64, ["a" * 64]])
def test_the_non_empty_string_fields_are_type_checked(field, value):
    """``spec_digest`` was in the required-field list but was never type-checked, so ``None``
    validated. ``repoint`` then compares it against a computed sha256 and refuses the move as
    "not the same spec" — a content check silently answering the wrong question."""
    doc = _run(**{field: value})
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(doc)
    assert field in str(excinfo.value)


def test_recoverable_and_unrecoverable_transitions():
    schema.assert_transition("active", "checkpointed")
    schema.assert_transition("active", "blocked")
    schema.assert_transition("checkpointed", "active")
    schema.assert_transition("blocked", "active")
    schema.assert_transition("active", "awaiting-team-merge")
    schema.assert_transition("awaiting-team-merge", "blocked")
    schema.assert_transition("awaiting-team-merge", "terminal")
    schema.assert_transition("active", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("terminal", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("failed", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("active", "terminal")


def test_a_run_key_paired_with_an_unrelated_spec_path_is_refused():
    """The scheme's claim is that the key IS a function of the spec path. Cross-checking only
    ``gate_dir``/``integration_branch`` against ``derived_names(run_key)`` is circular — it proves
    the key agrees with names derived from itself, which any key does. This document is internally
    consistent in exactly that way and still has to be refused."""
    key = runkey.run_key("docs/specs/alpha.md")
    doc = _run(
        run_key=key,
        spec_path="docs/specs/beta.md",
        gate_dir=f"assertions/{key}",
        integration_branch=f"conductor/run-{key}",
    )
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(doc)
    assert "docs/specs/beta.md" in str(excinfo.value)


def test_a_repointed_run_keeps_a_key_derived_from_a_path_in_its_history():
    """``repoint`` deliberately keeps the run key when a spec is renamed, so a live run's gate
    directory and integration branch do not move under it. The key therefore derives from the path
    the run was CREATED at, which `path_history` still records — so the check is membership across
    every path the document declares, not equality with the current one."""
    original = "docs/specs/alpha.md"
    key = runkey.run_key(original)
    doc = _run(
        run_key=key,
        spec_path="docs/specs/renamed.md",
        path_history=[original],
        gate_dir=f"assertions/{key}",
        integration_branch=f"conductor/run-{key}",
    )
    assert schema.validate_run(doc) == doc
    # ...but an empty history leaves nothing the key can derive from, so it is refused.
    with pytest.raises(schema.SchemaError):
        schema.validate_run(_run(run_key=key, spec_path="docs/specs/renamed.md"))


@pytest.mark.parametrize(
    "spec_path",
    ["/abs/alpha.md", "../outside.md", "docs/./alpha.md", "docs//alpha.md", "docs/"],
)
def test_a_run_spec_path_must_be_normalized(spec_path):
    """The run key is a hash of exactly this string, so two spellings of one file are two
    identities — and a path that escapes the repository keys a run on content no other checkout
    can reproduce."""
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(_run(spec_path=spec_path))
    assert "spec_path" in str(excinfo.value) or "derived" in str(excinfo.value)


@pytest.mark.parametrize(
    "spec_path", ["/abs/alpha.md", "../outside.md", "docs/./alpha.md", "docs//alpha.md"]
)
def test_a_project_specs_key_must_be_normalized(spec_path):
    """``specs`` keys are validated too, not just their values. ``docs/./alpha.md`` and
    ``docs/alpha.md`` are one file but two mapping keys, and each would be allowed its own
    nonterminal generation — the one-active-run-per-spec rule counts per key."""
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"][spec_path] = {
        "generations": [
            {
                "run_key": runkey.run_key("docs/specs/alpha.md"),
                "generation": 1,
                "status": "active",
            }
        ],
        "current": runkey.run_key("docs/specs/alpha.md"),
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_project(doc)
    assert "normalized" in str(excinfo.value)


def test_project_doc_validates_and_allows_one_nonterminal_generation():
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={
            "root_commit": "abc",
            "origin_url": "git@example.invalid:x/y.git",
        },
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [
            {"run_key": key, "generation": 1, "status": "terminal"},
            {"run_key": f"{key}-g2", "generation": 2, "status": "active"},
        ],
        "current": f"{key}-g2",
        "path_history": [],
    }
    assert schema.validate_project(doc) == doc


def test_two_nonterminal_generations_for_one_spec_are_refused():
    """``current`` names the FIRST nonterminal generation on purpose, and the assertion names the
    count check's own phrase.

    Both matter. With ``current`` set to the other generation the ``current``-consistency check
    (which runs after this one) also refuses — and its message likewise contains "nonterminal" —
    so the test passed whether or not the count check fired at all: raising the bound to ``> 99``
    left it green. Here the only rule the document breaks is "at most one nonterminal
    generation"."""
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [
            {"run_key": key, "generation": 1, "status": "active"},
            {"run_key": f"{key}-g2", "generation": 2, "status": "blocked"},
        ],
        "current": key,
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_project(doc)
    message = str(excinfo.value)
    assert "at most one is allowed" in message
    assert "2 nonterminal generations" in message
    assert f"{key}, {key}-g2" in message  # both offenders named, in order


def test_current_must_name_the_nonterminal_generation():
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [{"run_key": key, "generation": 1, "status": "active"}],
        "current": None,
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError):
        schema.validate_project(doc)


def test_validate_does_not_mutate_its_input():
    doc = _run()
    before = copy.deepcopy(doc)
    schema.validate_run(doc)
    assert doc == before


def test_duplicate_generation_numbers_for_one_spec_are_refused():
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [
            {"run_key": "alpha-11111111", "generation": 1, "status": "terminal"},
            {"run_key": "beta-22222222", "generation": 1, "status": "active"},
        ],
        "current": "beta-22222222",
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_project(doc)
    assert "duplicate" in str(excinfo.value)


def test_path_hash_v2_gate_dir_must_match_the_run_key_derived_path():
    doc = _run(gate_dir="assertions/totally-different-hash1234")
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


def test_path_hash_v2_integration_branch_must_match_the_run_key_derived_branch():
    doc = _run(integration_branch="conductor/run-totally-different-hash1234")
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


def test_legacy_slug_v1_run_keeps_its_recorded_gate_dir_and_branch():
    doc = _run(
        run_key="self-enforcement-1a2b3c4d",
        identity_scheme="legacy-slug-v1",
        gate_dir="assertions/legacy-gate",
        integration_branch="conductor/run-2026-07-05-self-enforcement",
    )
    assert schema.validate_run(doc) == doc
