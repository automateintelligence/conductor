"""``project.json`` and ``run.json`` shapes, the closed state vocabularies, and the legal status
transitions (design §"Project and run identity").

The vocabularies are closed sets because a typo'd status is exactly how an unattended run stops
counting as active without anyone noticing: ``active``, ``checkpointed`` and ``blocked`` count as
active for run-key disambiguation and manual autodev; ``awaiting-team-merge``, ``terminal`` and
``failed`` do not. A run becomes ``failed`` only when a recorded invariant violation leaves no
safe retry — every recoverable stop uses ``blocked``, which ``resume`` can return to ``active``.

``new_run_doc`` deliberately writes every field later plans populate, as ``None`` or an empty
container. Growing the document later would mean each plan reasoning about absent keys; a fixed
shape means ``validate_run`` is the only place that knows the schema.
"""

from __future__ import annotations

import copy

from conductor.core.names import GATE_DIR_PREFIX, derived_names, is_safe_segment
from conductor.core.runkey import is_safe_run_key, parse_generation

SCHEMA_VERSION = 2

RUN_STATUSES = (
    "active",
    "checkpointed",
    "blocked",
    "awaiting-team-merge",
    "terminal",
    "failed",
)
ACTIVE_STATUSES = ("active", "checkpointed", "blocked")
TERMINAL_STATUSES = ("terminal", "failed")
RESUMABLE_STATUSES = ("checkpointed", "blocked", "awaiting-team-merge")
REVIEW_POLICIES = (
    "opposite-required",
    "same-host-fallback-allowed",
    "blocked-pending-opposite-host",
)
IDENTITY_SCHEMES = ("path-hash-v2", "legacy-slug-v1")

# active -> terminal is absent on purpose: only `conductor finish` completes a run, and it runs
# from awaiting-team-merge after proving the final pull request merged.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"checkpointed", "blocked", "awaiting-team-merge", "failed"}),
    "checkpointed": frozenset({"active", "blocked", "failed"}),
    "blocked": frozenset({"active", "failed"}),
    "awaiting-team-merge": frozenset({"active", "blocked", "terminal", "failed"}),
    "terminal": frozenset(),
    "failed": frozenset(),
}

_RUN_REQUIRED = (
    "schema_version",
    "revision",
    "run_key",
    "generation",
    "identity_scheme",
    "spec_path",
    "spec_digest",
    "path_history",
    "status",
    "workstation_id",
    "integration_branch",
    "integration_worktree",
    "gate_dir",
    "phase_branch",
    "phase_worktree",
    "current_phase",
    "phase_ids",
    "plan_digest",
    "ledger_ref",
    "goal_digest",
    "assertion_digest",
    "worker_host",
    "reviewer_host",
    "review_policy",
    "phase_reviews",
    "last_review_head_sha",
    "last_worker_host",
    "last_reviewer_host",
    "dispatches",
    "github",
    "heartbeat",
    "lease",
    "last_reconciled_at",
    "last_checkpoint_at",
    "created_at",
    "updated_at",
    "completed_at",
    "failed_at",
)


def _is_safe_gate_dir(value: object) -> bool:
    """Whether ``value`` is ``assertions/<single-safe-segment>``.

    The segment rule comes from ``names.is_safe_segment`` — the same predicate
    ``paths._safe_slug`` guards the gate path with. It used to be a local regex here, and the two
    had drifted: this one accepted ``assertions/a..b``, which ``paths.resolve_gate`` refuses. A
    record legal to write and impossible to use is worse than either verdict alone, and for a
    ``legacy-slug-v1`` run (exempt from the derived-name cross-check below) these two guards are
    the only structural validation its ``gate_dir`` gets."""
    return (
        isinstance(value, str)
        and value.startswith(GATE_DIR_PREFIX)
        and is_safe_segment(value[len(GATE_DIR_PREFIX) :])
    )


class SchemaError(ValueError):
    """A state document violates the schema or a closed vocabulary."""


def is_active(status: str) -> bool:
    """Whether ``status`` counts as active for run-key disambiguation and manual autodev."""
    return status in ACTIVE_STATUSES


def assert_transition(old: str, new: str) -> None:
    """Raise unless ``old -> new`` is a legal status transition. Same-to-same is always legal so
    a reconcile may rewrite the current status without a special case."""
    if old not in RUN_STATUSES:
        raise SchemaError(
            f"unknown current status {old!r}; expected one of {RUN_STATUSES}"
        )
    if new not in RUN_STATUSES:
        raise SchemaError(
            f"unknown target status {new!r}; expected one of {RUN_STATUSES}"
        )
    if old == new:
        return
    if new not in _TRANSITIONS[old]:
        raise SchemaError(
            f"illegal status transition {old!r} -> {new!r}; legal targets from {old!r} are "
            f"{sorted(_TRANSITIONS[old]) or 'none (final state)'}"
        )


def new_project_doc(*, workstation_id: str, repo_identity: dict) -> dict:
    """A fresh registry with no spec mappings."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "workstation_id": workstation_id,
        "workstation_history": [],
        "repo_identity": dict(repo_identity),
        "specs": {},
    }


def new_run_doc(
    *,
    run_key: str,
    generation: int,
    spec_path: str,
    workstation_id: str,
    integration_branch: str,
    gate_dir: str,
    spec_digest: str,
    now: str,
    identity_scheme: str = "path-hash-v2",
) -> dict:
    """A fresh run record. Fields later plans own are present and empty so the shape never
    changes underneath them."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_key": run_key,
        "generation": generation,
        "identity_scheme": identity_scheme,
        "spec_path": spec_path,
        "spec_digest": spec_digest,
        "path_history": [],
        "status": "active",
        "workstation_id": workstation_id,
        "integration_branch": integration_branch,
        "integration_worktree": None,
        "gate_dir": gate_dir,
        "phase_branch": None,
        "phase_worktree": None,
        "current_phase": None,
        "phase_ids": [],
        "plan_digest": None,
        "ledger_ref": None,
        "goal_digest": None,
        "assertion_digest": None,
        "worker_host": None,
        "reviewer_host": None,
        "review_policy": "opposite-required",
        "phase_reviews": [],
        "last_review_head_sha": None,
        "last_worker_host": None,
        "last_reviewer_host": None,
        "dispatches": [],
        "github": {"issue": None, "phase_prs": {}, "final_pr": None},
        "heartbeat": {"schedule_id": None, "process_identity": None},
        "lease": {"owner": None, "expires_at": None, "renewed_at": None},
        "last_reconciled_at": None,
        "last_checkpoint_at": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "failed_at": None,
    }


def _require_int(doc: dict, field: str, minimum: int) -> int:
    value = doc.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SchemaError(f"{field} must be an integer >= {minimum}, got {value!r}")
    return value


def validate_run(doc: dict) -> dict:
    """Return ``doc`` unchanged if it is a legal run record, else raise ``SchemaError``."""
    if not isinstance(doc, dict):
        raise SchemaError(f"run document must be a mapping, got {type(doc).__name__}")
    missing = [field for field in _RUN_REQUIRED if field not in doc]
    if missing:
        raise SchemaError(
            f"run document is missing required field(s): {', '.join(missing)}"
        )
    _require_int(doc, "schema_version", 1)
    _require_int(doc, "revision", 0)
    generation = _require_int(doc, "generation", 1)
    key = doc["run_key"]
    if not isinstance(key, str) or not is_safe_run_key(key):
        raise SchemaError(f"run_key {key!r} is not a safe single path/ref segment")
    if parse_generation(key) != generation:
        raise SchemaError(
            f"generation {generation} disagrees with run_key {key!r} "
            f"(the key encodes generation {parse_generation(key)})"
        )
    if doc["identity_scheme"] not in IDENTITY_SCHEMES:
        raise SchemaError(
            f"identity_scheme {doc['identity_scheme']!r}; expected one of {IDENTITY_SCHEMES}"
        )
    if doc["status"] not in RUN_STATUSES:
        raise SchemaError(f"status {doc['status']!r}; expected one of {RUN_STATUSES}")
    if doc["review_policy"] not in REVIEW_POLICIES:
        raise SchemaError(
            f"review_policy {doc['review_policy']!r}; expected one of {REVIEW_POLICIES}"
        )
    for field in ("spec_path", "spec_digest", "integration_branch", "workstation_id"):
        value = doc[field]
        if not isinstance(value, str) or not value:
            raise SchemaError(f"{field} must be a non-empty string, got {value!r}")
    gate_dir = doc["gate_dir"]
    if not _is_safe_gate_dir(gate_dir):
        raise SchemaError(
            f"gate_dir {gate_dir!r} must be 'assertions/<single-safe-segment>' relative to the "
            "repository root"
        )
    if doc["identity_scheme"] == "path-hash-v2":
        # A path-hash-v2 run's identity is fully derived from run_key, so gate_dir and
        # integration_branch must match that derived form exactly — a mismatch means the
        # recorded identity and the on-disk paths have silently diverged. legacy-slug-v1 runs
        # (Plan 03 migration) deliberately retain their pre-migration branch/gate names, so
        # this cross-check does not apply to them; only the safety checks above do.
        names = derived_names(key)
        if gate_dir != names.gate_dir:
            raise SchemaError(
                f"gate_dir {gate_dir!r} does not match the run_key-derived path "
                f"{names.gate_dir!r} required for identity_scheme 'path-hash-v2'"
            )
        if doc["integration_branch"] != names.integration_branch:
            raise SchemaError(
                f"integration_branch {doc['integration_branch']!r} does not match the "
                f"run_key-derived branch {names.integration_branch!r} required for identity_scheme "
                "'path-hash-v2'"
            )
    for field in ("path_history", "phase_ids", "phase_reviews", "dispatches"):
        if not isinstance(doc[field], list):
            raise SchemaError(
                f"{field} must be a list, got {type(doc[field]).__name__}"
            )
    for field in ("github", "heartbeat", "lease"):
        if not isinstance(doc[field], dict):
            raise SchemaError(
                f"{field} must be a mapping, got {type(doc[field]).__name__}"
            )
    return doc


def validate_project(doc: dict) -> dict:
    """Return ``doc`` unchanged if it is a legal registry, else raise ``SchemaError``.

    Enforces the design's central mapping rule: each spec path holds an ordered generation list
    with **at most one nonterminal run**, and ``current`` names exactly that run (or is ``None``
    when every generation is terminal)."""
    if not isinstance(doc, dict):
        raise SchemaError(
            f"project document must be a mapping, got {type(doc).__name__}"
        )
    for field in (
        "schema_version",
        "revision",
        "workstation_id",
        "repo_identity",
        "specs",
    ):
        if field not in doc:
            raise SchemaError(f"project document is missing required field {field!r}")
    _require_int(doc, "schema_version", 1)
    _require_int(doc, "revision", 0)
    if not isinstance(doc["workstation_id"], str) or not doc["workstation_id"]:
        raise SchemaError(
            f"workstation_id must be a non-empty string, got {doc['workstation_id']!r}"
        )
    if not isinstance(doc["repo_identity"], dict):
        raise SchemaError("repo_identity must be a mapping")
    if not isinstance(doc.get("workstation_history"), list):
        raise SchemaError("workstation_history must be a list")
    specs = doc["specs"]
    if not isinstance(specs, dict):
        raise SchemaError("specs must be a mapping of normalized spec path -> mapping")
    seen: dict[str, str] = {}
    for spec_path, mapping in specs.items():
        if not isinstance(mapping, dict):
            raise SchemaError(f"specs[{spec_path!r}] must be a mapping")
        generations = mapping.get("generations")
        if not isinstance(generations, list) or not generations:
            raise SchemaError(
                f"specs[{spec_path!r}].generations must be a non-empty list"
            )
        if not isinstance(mapping.get("path_history"), list):
            raise SchemaError(f"specs[{spec_path!r}].path_history must be a list")
        numbers = []
        nonterminal = []
        for entry in generations:
            if not isinstance(entry, dict):
                raise SchemaError(
                    f"specs[{spec_path!r}].generations entries must be mappings"
                )
            key = entry.get("run_key")
            if not isinstance(key, str) or not is_safe_run_key(key):
                raise SchemaError(f"specs[{spec_path!r}] has unsafe run_key {key!r}")
            if key in seen:
                raise SchemaError(
                    f"run_key {key!r} is mapped by both {seen[key]!r} and {spec_path!r}"
                )
            seen[key] = spec_path
            generation = entry.get("generation")
            if not isinstance(generation, int) or generation < 1:
                raise SchemaError(
                    f"specs[{spec_path!r}] has invalid generation {generation!r}"
                )
            if parse_generation(key) != generation:
                raise SchemaError(
                    f"specs[{spec_path!r}] run_key {key!r} disagrees with generation {generation}"
                )
            numbers.append(generation)
            status = entry.get("status")
            if status not in RUN_STATUSES:
                raise SchemaError(f"specs[{spec_path!r}] has status {status!r}")
            if status not in TERMINAL_STATUSES:
                nonterminal.append(key)
        if len(numbers) != len(set(numbers)):
            duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
            raise SchemaError(
                f"specs[{spec_path!r}].generations has duplicate generation number(s) "
                f"{duplicates}; each generation must appear at most once"
            )
        if numbers != sorted(numbers):
            raise SchemaError(
                f"specs[{spec_path!r}].generations must be in ascending order"
            )
        if len(nonterminal) > 1:
            raise SchemaError(
                f"specs[{spec_path!r}] has {len(nonterminal)} nonterminal generations "
                f"({', '.join(nonterminal)}); at most one is allowed"
            )
        current = mapping.get("current")
        expected = nonterminal[0] if nonterminal else None
        if current != expected:
            raise SchemaError(
                f"specs[{spec_path!r}].current is {current!r} but the nonterminal generation is "
                f"{expected!r}"
            )
    return doc


def clone(doc: dict) -> dict:
    """A deep copy, so a caller's mutate callback cannot alter the on-disk snapshot in place."""
    return copy.deepcopy(doc)
