# Dual-host Conductor — Plan Roadmap

> **For agentic workers:** This is an INDEX, not an executable plan. Do not implement from this
> file. Each numbered entry below points at (or reserves a filename for) a standalone plan
> document. Execute those with `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`.

**Source design:** `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md`
(approved 2026-08-10, commit `67dcf93`, branch `docs/codex-dual-host-design`)

**Why this is split:** the design spans three repositories and ten subsystems that each produce
working, testable software on their own. A single plan would exceed what one implementer or one
reviewer can hold. Each plan below has its own worktree, branch, commits, tests, and pull request
— which is also what design §"Repository and release sequence" requires.

---

## Global constraints (apply to every plan)

Copy this block verbatim into each plan's `## Global Constraints` section.

- **Host floor:** Claude Code `2.1.224`, Codex CLI `0.147.0`. Manifests, preflight diagnostics,
  and installation documentation publish these minimums.
- **Canonical editable checkout:** `~/programming/conductor`. The old `~/.claude/conductor` is
  quarantined at `~/.claude/conductor.quarantine-2026-08-10` with **no symlink left behind**.
- **Plugin identity:** one public name, `conductor`, in the AutomateIntelligence marketplace.
  No second Codex-specific product identity.
- **Run key format:** `<spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]`.
  Generation 1 omits the `-g<N>` component.
- **Run integration branch:** `conductor/run-<run-key>`.
  **Phase branch:** `conductor/<run-key>/phase-<phase-id>`.
- **Run status vocabulary (exactly these six):** `active`, `checkpointed`, `blocked`,
  `awaiting-team-merge`, `terminal`, `failed`.
- **Review policy vocabulary (exactly these three):** `opposite-required`,
  `same-host-fallback-allowed`, `blocked-pending-opposite-host`.
- **Global lock order:** `migration.lock` (when applicable), then `project.lock` (when
  applicable), then `owner.lock`, then `state.lock`. Multi-run project operations acquire run
  locks in **sorted run-key order**.
- **Lease defaults:** 120 second lease, renewed at least every 30 seconds. A repository may
  lengthen these but may **not** configure a renewal interval greater than one quarter of the
  lease duration.
- **Review freshness:** default maximum review age 24 hours; a review also expires whenever the
  pull-request head changes.
- **Per-fire context budget:** default `0.60` of the host's declared context window.
- **Conductor never merges to the repository default branch.** No merge, squash, rebase,
  force-push, close, base mutation, auto-merge enablement, or merge-queue enrollment of the final
  pull request.
- **Every state write** uses a sibling temporary file, flush, fsync, and atomic replace.
  `run.json` writes additionally require `state.lock` and the current revision; `project.json`
  mutations require `project.lock` and its current revision.
- **Every actionable failure reports:** run key and current state, the failed invariant or
  operation, the affected branch/worktree/pull request/state path, whether any write occurred,
  and the exact inspect/retry/takeover/migrate/recovery command.
- **Tooling gates:** `ruff check . && ruff format --check .`, `pyright .`, `pytest -q`. Python
  3.12.

---

## Plan index

| # | Plan | Repo | Depends on | Status |
| --- | --- | --- | --- | --- |
| 00 | Source relocation and quarantine | conductor (+ workstation) | — | not written |
| 01 | Run identity, project registry, per-run state | conductor | — | **written** |
| 02 | Ownership, leases, takeover, prune, rebind | conductor | 01 | not written |
| 03 | Legacy run migration | conductor | 01, 02 | not written |
| 04 | Host adapter layer and preflight floors | conductor | 01 | not written |
| 05 | Heartbeat, checkpoint, no-compaction policy | conductor | 01, 02, 03, 04 | not written |
| 06 | Branch/worktree/PR model, merge gates, sync phases | conductor | 01, 04 | not written |
| 07 | Reviewer routing, structured review, review debt | conductor | 04, 06 | not written |
| 08 | spec-craft dual-host | spec-craft | — | not written |
| 09 | Packaging and marketplace dual catalogs | conductor, marketplace | 04, 08 | not written |
| 10 | Public messaging and installation smokes | all three | 09 | not written |

Dependency edges are **interface** dependencies: plan N may be written and reviewed before its
dependency merges, but it cannot go green until the interfaces it consumes exist.

---

## Plan 00 — Source relocation and quarantine

**File:** `2026-08-10-plan-00-source-relocation.md`
**Repo:** conductor, plus workstation configuration (cron, hooks, shell, plugin caches)
**Spec sections:** §"Source relocation and quarantine"; §"Repository and release sequence" steps
1–4 and 11; the relocation bullets in §"Integration tests".

**Goal:** move the canonical editable checkout from `~/.claude/conductor` to
`~/programming/conductor`, recreate every retained linked worktree from the canonical clone,
repoint every schedule/hook/resume-env/launcher, quarantine the old path, and pass the one-week
removal predicate.

**Produces:** a relocation runbook with machine-checked predicates; a
`conductor doctor relocation` style scan that fails when any cron entry, hook, process,
worktree registration, or runtime config still names the old path.

**Notes for the writer:** this plan is mostly operational, but every predicate in the final-scan
list (design lines 418–427) must be a runnable check, not a prose instruction. The nested-worktree
recreation step (design line 414) is the highest-risk item — absolute linked-worktree metadata
under the renamed root is what breaks.

---

## Plan 01 — Run identity, project registry, per-run state

**File:** `2026-08-10-plan-01-run-identity-registry.md` — **written**
**Repo:** conductor
**Spec sections:** §"Project and run identity" (all); §"Failure handling" paragraphs on atomic
writes, revision guards, project transactions, and lock order; §"Unit and contract tests" bullets
1–8 and 11 (tracked-path recovery).

**Goal:** replace flat `.conductor/` state with the per-run registry + run-key model, so multiple
specs coexist in one repository with independent goals, gates, manifests, baselines, and results,
and so every later plan has one resolver to call.

**Produces (public interfaces later plans consume):**

```python
# conductor/core/runkey.py
normalize_spec_path(repo_root: str, spec_path: str) -> str
path_hash(normalized_spec_path: str) -> str                       # 8 lowercase hex chars
run_key(normalized_spec_path: str, generation: int = 1) -> str
parse_generation(key: str) -> int
is_safe_run_key(key: str) -> bool

# conductor/core/atomic.py
write_atomic(path: str, data: str | bytes, *, mode: int = 0o644) -> None
write_json_atomic(path: str, doc: dict, *, mode: int = 0o644) -> None
read_json(path: str) -> dict | None

# conductor/core/locks.py
LOCK_ORDER: tuple[str, ...]                                       # ("migration","project","owner","state")
class LockOrderError(RuntimeError); class LockTimeout(RuntimeError)
hold(path: str, *, kind: str, run_key: str | None = None, timeout: float = 30.0)  # context manager

# conductor/core/schema.py
SCHEMA_VERSION: int
RUN_STATUSES: tuple[str, ...]; ACTIVE_STATUSES: tuple[str, ...]
REVIEW_POLICIES: tuple[str, ...]; IDENTITY_SCHEMES: tuple[str, ...]
class SchemaError(ValueError)
validate_run(doc: dict) -> dict
validate_project(doc: dict) -> dict
new_run_doc(**kwargs) -> dict
is_active(status: str) -> bool
assert_transition(old: str, new: str) -> None

# conductor/core/workstation.py
workstation_id() -> str

# conductor/core/registry.py
class RegistryMissing(RuntimeError); class RevisionConflict(RuntimeError)
registry_path(state_root: str) -> str
load(state_root: str) -> dict | None
init(state_root: str, *, workstation_id: str, repo_identity: dict) -> dict
commit(state_root: str, doc: dict, *, expect_revision: int) -> dict
update(state_root: str, mutate, *, attempts: int = 5) -> dict
mapping(doc: dict, normalized_spec_path: str) -> dict | None
current_run_key(doc: dict, normalized_spec_path: str) -> str | None
next_generation(doc: dict, normalized_spec_path: str) -> int
find_run(doc: dict, run_key: str) -> tuple[str, dict] | None
register(doc: dict, *, spec: str, run_key: str, generation: int) -> dict
mirror_status(doc: dict, run_key: str, status: str) -> dict
run_keys(doc: dict) -> list[str]

# conductor/core/transaction.py
prepare(state_root: str, txn_id: str, entries: list[dict]) -> str
commit(state_root: str, txn_id: str) -> None
apply(state_root: str, txn_id: str) -> None
recover(state_root: str) -> list[str]

# conductor/core/runstate.py
run_dir(state_root: str, run_key: str) -> str
run_path(state_root: str, run_key: str) -> str
state_lock_path(state_root: str, run_key: str) -> str
owner_lock_path(state_root: str, run_key: str) -> str   # path only; Plan 02 owns the semantics
load(state_root: str, run_key: str) -> dict | None
create(state_root: str, run_key: str, doc: dict) -> dict
commit(state_root: str, run_key: str, doc: dict, *, expect_revision: int) -> dict
update(state_root: str, run_key: str, mutate, *, attempts: int = 5) -> dict
set_status(state_root: str, run_key: str, status: str) -> dict

# conductor/core/resolve.py
class RunAmbiguous(RuntimeError); class RunNotFound(RuntimeError)
class RunResolution(NamedTuple):
    state_root: str; repo_root: str; run_key: str; run_dir: str; run: dict
repo_root(start: str | None = None) -> str
state_root(start: str | None = None) -> str
active_run_keys(state_root: str) -> list[str]
repo_identity(repo_root: str) -> dict
resolve(*, run_key: str | None = None, start: str | None = None) -> RunResolution
gate_for_run(res: RunResolution) -> paths.GateResolution

# conductor/core/hygiene.py
class TrackedStateError(RuntimeError)
assert_state_paths_untracked(repo_root: str) -> None
ensure_local_exclude(repo_root: str) -> None

# conductor/core/repoint.py
class RepointRefused(RuntimeError)
repoint(state_root: str, *, repo_root: str, run_key: str, new_spec_path: str) -> dict

# conductor/paths.py  (modified)
run_gate_dir(repo_root: str, run_key: str) -> str
resolve_gate(repo_root: str | None = None, *, run_key: str | None = None,
             run: dict | None = None) -> GateResolution      # source == "run_key" in key mode
```

**Explicitly out of scope (owned by later plans):** owner.lock lease semantics and takeover (02),
legacy migration (03), adapters (04), heartbeat/schedules (05), branches/worktrees/PRs (06),
reviews (07). Plan 01 creates `owner.lock` *files* and acquires them in the correct order for
`repoint-spec`, but treats a busy lock as a flat refusal — it does not interpret leases.

---

## Plan 02 — Ownership, leases, takeover, prune, rebind

**File:** `2026-08-10-plan-02-ownership-takeover.md`
**Repo:** conductor
**Spec sections:** §"Ownership, locking, and takeover" (all); the lease/marker paragraphs in
§"Failure handling"; §"Unit and contract tests" bullets on lock/lease/PID reuse, orphan refusal,
prune ordering, cross-machine refusal, and rebind.

**Goal:** make `owner.lock` the single execution-ownership authority — recording run key, host,
wrapper process identity, launched host process identity, lease, and heartbeat identity — and
implement the seven-step compare-and-swap takeover, orphan prune, and workstation rebind.

**Consumes from 01:** `locks.hold`, `runstate.update/commit`, `registry.update`,
`resolve.resolve`, `workstation.workstation_id`.

**Produces:**

```python
# conductor/core/ownership.py
class OwnerBusy(RuntimeError); class OwnerAmbiguous(RuntimeError); class OwnerOrphaned(RuntimeError)
class OwnerRecord(NamedTuple):
    run_key: str; host: str; wrapper_identity: str; host_identity: str | None
    lease_expires_at: str; heartbeat_id: str
acquire(state_root, run_key, *, host, wrapper_identity, lease_seconds=120)  # context manager
renew(state_root, run_key, *, wrapper_identity) -> OwnerRecord
read(state_root, run_key) -> OwnerRecord | None
prove_exited(record: OwnerRecord) -> bool
takeover(state_root, run_key, *, new_host, wrapper_identity, launch) -> dict
prune(state_root, run_key, *, terminate_orphan: str | None = None) -> dict

# conductor/core/rebind.py
rebind(state_root, *, confirm_prior_workstation_quiesced: str) -> dict
```

**Key traps to encode as tests:** expiry is *necessary but never sufficient*; a dead wrapper with
a live launched host is orphaned, not stale; the wrapper holds `owner.lock` across the launched
host's whole lifetime so there is no release window; takeover invalidates a posted review whose
reviewer host equals the new worker host; rebind installs **no** schedules.

---

## Plan 03 — Legacy run migration

**File:** `2026-08-10-plan-03-legacy-migration.md`
**Repo:** conductor
**Spec sections:** §"Legacy run migration" (all); the migration bullets in §"Unit and contract
tests" and §"End-to-end matrix".

**Goal:** convert the existing flat `.conductor/{goal.md,run_branch,resume-env.sh}` state into the
per-run layout under `migration.lock`, adopting the legacy shared slug as the run key with
`generation=1` and `identity_scheme=legacy-slug-v1`, preserving exact branch names, worktrees,
pull requests, and frozen assertions.

**Consumes from 01:** the whole `conductor/core` surface, especially `identity_scheme` handling in
`schema.validate_run` and `paths.resolve_gate`. **Consumes from 02:** ownership records so a
migrated run gets a coherent (empty) owner state.

**Produces:** `conductor/core/migrate.py` with `detect(state_root) -> LegacyState | None`,
`migrate(state_root, *, dry_run=False) -> MigrationReport`, and a journal under
`.conductor/migrations/`.

**Hard requirements:** migration runs at `start` and `heartbeat` entry **before any per-run lock**
and **never** from inside a launched host's autodev; `status` detects but never migrates;
migration never regenerates expectations, assertion specs, assertion tests, or the frozen
definition of done; the first `gate verify` after migration must not produce an ambient-dodge
failure.

---

## Plan 04 — Host adapter layer and preflight floors

**File:** `2026-08-10-plan-04-host-adapters.md`
**Repo:** conductor
**Spec sections:** §"System architecture"; the host-floor paragraph in §"Packaging and
installation"; adapter/permission/dispatch bullets in §"Unit and contract tests".

**Goal:** introduce `conductor/hosts/{base,claude,codex}.py` implementing the eleven adapter
capabilities, so no core module contains a Claude slash command, a Codex dollar invocation,
`CLAUDE_PLUGIN_ROOT`, a host-specific permission flag, or an assumption about one installation
directory.

**Produces:**

```python
# conductor/hosts/base.py
class HostAdapter(Protocol):
    id: str                                                   # "claude" | "codex"
    def executable(self) -> str: ...
    def source_root(self) -> str: ...
    def version(self) -> tuple[int, ...]: ...
    def minimum_version(self) -> tuple[int, ...]: ...
    def worker_argv(self, *, state_root: str, run_key: str, project_root: str) -> list[str]: ...
    def reviewer_argv(self, *, pr: int, head_sha: str, run_key: str) -> list[str]: ...
    def native_invocation(self, skill: str) -> str: ...        # "/conductor:autodev" | "$conductor:autodev"
    def permission_profile(self) -> dict: ...
    def validate_permissions(self, profile: dict) -> None: ...
    def process_alive(self, identity: str) -> bool: ...
    def install_hooks(self, state_root: str, run_key: str) -> None: ...
    def dispatch_implementation(self, prompt: str, *, timeout: float) -> DispatchResult: ...
def load(host_id: str) -> HostAdapter
def opposite(host_id: str) -> str
```

**Hard requirements:** adapters launch **argument vectors**, never interpolated shell strings;
Codex's `$conductor:*` invocation is literal prompt text and must not be shell-expanded; an
adapter that cannot dispatch isolated implementation work **fails preflight** — the orchestrator
never absorbs implementation as fallback.

---

## Plan 05 — Heartbeat, checkpoint, no-compaction policy

**File:** `2026-08-10-plan-05-heartbeat-checkpoint.md`
**Repo:** conductor
**Spec sections:** §"Heartbeat and autodev"; §"Orchestrator context contract"; §"Context
exhaustion and no-compaction policy"; the corresponding test bullets.

**Goal:** replace the single-project `resume-autodev.sh` + crontab pair with per-run
`heartbeat.sh` + `heartbeat.json` + `heartbeat.log`, add `conductor heartbeat|resume|finish`, and
implement the eight-step checkpoint sequence with `compaction.marker` fencing.

**Consumes:** 01 (state), 02 (owner.lock lifetime), 03 (migrate-before-lock at the entry point),
04 (launch, hooks, context telemetry).

**Produces:** `conductor/heartbeat/{cli,schedule,checkpoint,marker}.py`, the two verbatim
orchestrator reminders and their anchor contract tests, and the reconciliation evidence
precedence (Git → GitHub → `results.json` → `run.json` → `handoff.md`).

**Hard requirements:** a skipped fire caused by a live lock is a **success**; a checkpoint-only
fire does not advance the phase ledger; `awaiting-team-merge` removes its schedule; `finish`
refuses until authoritative remote metadata proves the final PR merged with the right base, head
SHA, and no review debt; failed commit/push/lease/handoff **blocks** rather than exiting clean.

---

## Plan 06 — Branch/worktree/PR model, merge gates, sync phases

**File:** `2026-08-10-plan-06-branch-pr-model.md`
**Repo:** conductor
**Spec sections:** §"Branch, worktree, and pull-request model"; the dispatch-attribution
paragraph of §"Orchestrator context contract"; the merge/sync/final-PR test bullets.

**Goal:** move `conductor/{branches,merge_cmd,merge_gate}.py` onto run keys, add per-phase
worktrees, make default-branch resolution **fail closed** (removing today's fail-open-to-`main`
behaviour in `conductor/branches.py:91`), enforce phase-PR base equality, protect the final PR,
and implement plan-external `sync-<n>` phases with exact-parent attribution.

**Consumes:** 01 (run resolution), 04 (adapter dispatch IDs).

**Behaviour change to call out in the plan:** `branches.default_branch()` currently fails **open**
to `main`. Design line 220 requires the opposite — unresolved default branch refuses **every**
automated merge. This is a deliberate inversion, and the existing assertion `a10-default-branch`
must be re-derived, not deleted.

---

## Plan 07 — Reviewer routing, structured review, review debt

**File:** `2026-08-10-plan-07-reviewer-routing.md`
**Repo:** conductor
**Spec sections:** §"Reviewer routing"; the review bullets in §"Unit and contract tests" and
§"End-to-end matrix".

**Goal:** default reviewer is always the opposite host; verdicts are structured, tied to the exact
head SHA, expiring on head change or after 24 hours; `require_opposite_review=true` by default;
same-host fallback records review debt with a **stored** required debt-reviewer host that survives
a takeover; outstanding debt blocks the final PR.

**Consumes:** 04 (reviewer launch), 06 (phase PR + merge gate hook point).

**Produces:** `conductor/reviewers/{policy,verdict,debt}.py` and the verdict JSON schema
(reviewer host, verdict, findings, reviewed head SHA, timestamp, prompt/schema version).

---

## Plan 08 — spec-craft dual-host

**File:** `2026-08-10-plan-08-spec-craft-dual-host.md`
**Repo:** automateintelligence/spec-craft
**Spec sections:** §"spec-craft compatibility"; the spec-craft bullets in §"Unit and contract
tests" and §"Installation smoke tests".

**Goal:** add `.codex-plugin/plugin.json`, replace the Claude-specific `$ARGUMENTS` assumption with
host-neutral input resolution from the invocation text, show both `/spec-craft:*` and
`$spec-craft:*` examples, and keep spec-craft standalone and Conductor-agnostic.

**Independent of every other plan.** Ships first per design §"Repository and release sequence"
step 5.

---

## Plan 09 — Packaging and marketplace dual catalogs

**File:** `2026-08-10-plan-09-packaging-marketplace.md`
**Repos:** automateintelligence/conductor, automateintelligence/marketplace
**Spec sections:** §"Packaging and installation"; §"Installation smoke tests".

**Goal:** add `.codex-plugin/plugin.json` to conductor; publish
`.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json` in the marketplace repo;
generate `plugins/conductor` and `plugins/spec-craft` bundles from immutable tags with recorded
source commit and version, verified against source in CI.

**Hard requirement to encode as a CI gate:** Codex has no plugin-level `dependencies` field, so
the Codex marketplace marks spec-craft `policy.installation=INSTALLED_BY_DEFAULT`. Release CI
must validate the catalog with the supported Codex CLI, add the marketplace in an isolated
configuration, and prove a fresh session discovers spec-craft **before** Conductor is installed.
A failed probe blocks publication.

---

## Plan 10 — Public messaging and installation smokes

**File:** `2026-08-10-plan-10-public-messaging.md`
**Repos:** all three
**Spec sections:** §"Public messaging"; §"Installation smoke tests".

**Goal:** update every surface in design lines 385–393 to name Claude Code and Codex, add
public-text guards against unintended Claude-only claims, and run the eight installation smokes in
isolated test users or disposable environments.

**Note:** GitHub repository descriptions are release-owner metadata, not PR-managed files. The
plan must produce the exact approved text plus an API verification step in the release checklist,
not an attempt to change them in a commit. Marketplace-wide wording must not regress Bubo to a
Claude-only description.

---

## Release sequencing (design §"Repository and release sequence")

Engineering order and release order differ. Engineering may proceed 01 → 02 → 03 → 04 → 05 →
06 → 07 in the canonical clone as soon as Plan 00 step 1 (canonical clone established) is done.
Release order is fixed:

1. Plan 00 steps 1–4 — canonical clone, quiesce, worktree recreation, quarantine rename.
2. Plan 08 — spec-craft dual-host, versioned artifact.
3. Plans 01–07 — Conductor dual-host against that supported spec-craft version.
4. Plan 09 — marketplace catalogs and bundles.
5. Plan 10 — descriptions and public documentation, after each repository team accepts.
6. Branch-based installation smokes before merge; public installation smokes after merge.
7. Plan 00 step 11 — quarantine removal after the one-week observation period and final scan.

**Conductor never merges any of these default-branch pull requests.**
