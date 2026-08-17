# Dual-host Conductor — Plan Roadmap

> **For agentic workers:** This is an INDEX, not an executable plan. Do not implement from this
> file. Each numbered entry below points at (or reserves a filename for) a standalone plan
> document. Execute those with `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`.

**Source design:** `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md`
(approved 2026-08-10, commit `67dcf93`, branch `docs/codex-dual-host-design`)

**Superseding design for Plan 00:**
`docs/superpowers/specs/2026-08-12-conductor-source-decommission-design.md` (owner-approved
2026-08-16, execution deferred). It replaces the relocation approach with a fresh clone plus
staged quarantine — see Plan 00 below.

**Why this is split:** the design spans three repositories and ten subsystems that each produce
working, testable software on their own. A single plan would exceed what one implementer or one
reviewer can hold. Each plan below has its own worktree, branch, commits, tests, and pull request
— which is also what design §"Repository and release sequence" requires.

---

## Global constraints (apply to every plan)

Copy this block verbatim into each plan's `## Global Constraints` section.

- **Host floor:** Claude Code `2.1.224`, Codex CLI `0.147.0`. Manifests, preflight diagnostics,
  and installation documentation publish these minimums.
- **Canonical editable checkout:** `~/programming/conductor`, established by a fresh `git clone`
  — never by moving the existing tree. The old `~/.claude/conductor` is retired in place: `mv` to
  a dated quarantine directory, one-week observation, then removal, with **no symlink left
  behind**. Until then Conductor is consumed as an installed plugin from
  `~/.claude/plugins/cache/`. See the decommission design named above.
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

| # | Plan | Repo | Depends on | Plan doc | Code |
| --- | --- | --- | --- | --- | --- |
| 00 | Source decommission (was: relocation) | conductor (+ workstation) | — | superseded by the 2026-08-12 decommission design | execution deferred; all four loss-risk predicates blocked |
| 01 | Run identity, project registry, per-run state | conductor | — | **written** | **merged** (PR #84) |
| 02 | Ownership, leases, takeover, prune, rebind | conductor | 01 | not written | — |
| 03 | Legacy run migration | conductor | 01, 02 | not written | — |
| 04 | Host adapter layer and preflight floors | conductor | 01 | **written** | in progress — `conductor/hosts/` does not exist on `main` |
| 05 | Heartbeat, checkpoint, no-compaction policy | conductor | 01, 02, 03, 04 | not written | — |
| 06 | Branch/worktree/PR model, merge gates, sync phases | conductor | 01, 04 | not written | — |
| 07 | Reviewer routing, structured review, review debt | conductor | 04, 06 | not written | — |
| 08 | spec-craft dual-host | spec-craft | — | not written | — |
| 09 | Packaging and marketplace dual catalogs | conductor, marketplace | 04, 08 | not written | — |
| 10 | Public messaging and installation smokes | all three | 09 | not written | — |

Dependency edges are **interface** dependencies: plan N may be written and reviewed before its
dependency merges, but it cannot go green until the interfaces it consumes exist. **One
exception:** Plan 09 must not *start* before Plan 04 merges — see that entry.

### Verified Codex facts

`docs/reviews/2026-08-12-codex-host-ground-truth.md` is the source of truth for what Codex CLI
actually does, measured against `0.147.0`. Prefer it over the design wherever the two differ; the
design was written before the probe. Two corrections it carries that change plan content:

- **`permission_profile()` is no longer abstract on the Codex side.** The real flags are
  `-s, --sandbox {read-only|workspace-write|danger-full-access}`, `--approve-for-me` (automatic
  approval routing under the workspace-write sandbox), and
  `--dangerously-bypass-approvals-and-sandbox` — the analogue of Claude's
  `--dangerously-skip-permissions`. Codex's sandbox is a graded axis where Claude's posture is a
  mode plus a settings file, so they do **not** map one to one: the adapter defines its own
  posture vocabulary and projects it onto each host rather than passing a shared string through.
- **The design's assumption that Codex has no session continuation is wrong.** `codex exec
  resume`, top-level `codex resume`, and `codex fork` all exist. Ignoring them in favour of
  cold-start reconciliation from durable state is a defensible choice, but it is now an
  **explicit non-goal to be written down with its reason** (Plan 04 does this), not an absence.

---

## Plan 00 — Source decommission (was: source relocation and quarantine)

**File:** `2026-08-10-plan-00-source-decommission.md`
**Repo:** conductor, plus workstation configuration (cron, hooks, shell, plugin caches)
**Governing design:** `docs/superpowers/specs/2026-08-12-conductor-source-decommission-design.md`
— owner-approved 2026-08-16 with both scope decisions confirmed, amended 2026-08-17, **execution
deferred**. It supersedes the relocation approach; Plan 00 becomes a decommission checklist, not
a relocation runbook.
**Spec sections:** the decommission design in full — §"Predicates", §"Execution preconditions",
§"Order of operations", §"Testing". The original design's §"Source relocation and quarantine" and
§"Repository and release sequence" steps 1–4 and 11 now supply only the *release ordering*.

**Approach change.** The checkout is not moved. `~/programming/conductor` is created by a fresh
`git clone`, verified on its own terms before anything at the old path is touched, and
`~/.claude/conductor` is retired in place — `mv` to a dated quarantine directory, one-week
observation, removal only after a final re-run of all six checks. This deletes the old plan's
highest-risk step outright: a fresh clone creates its worktrees from the new root, so nothing is
repointed and nothing can dangle. The four linked worktrees under the old path are retired
explicitly instead, each carrying a recorded outcome — archive, discard, or keep.

**Goal:** retire `~/.claude/conductor` as the canonical checkout, with Conductor consumed as an
installed plugin from `~/.claude/plugins/cache/`, and with nothing lost that exists in no second
place.

**Produces:** `scripts/decommission-check.sh` — the six predicates as one runnable check, exiting
non-zero with the failing list and reporting **loss-risk failures and quiesce failures under
separate headings**, because they mean different things: a quiesce failure is rescheduled, a
loss-risk failure is remediated. Run before quarantine and again after.

**Notes for the writer:** every predicate is a runnable check, not a prose instruction — that
requirement survives the change of approach. Three of them have already had to be corrected once
each, and the design records why: P1 must be built on `git log --branches --not --remotes`, never
on `@{u}`, which cannot see an upstream-less branch — the only case that has ever failed here;
P2 must invoke `git status --porcelain -uall --ignored` and print the `.conductor/` entries it
finds rather than a count, since the ignored paths are precisely the ones GitHub never saw; P5
must run P1's and P2's commands *inside* each linked worktree and fail on any worktree without a
recorded outcome, not merely enumerate them. Merge status decides nothing anywhere in this plan.

> **PRECONDITION — reduced, not removed.** The original form of this gate said: as of 2026-08-10
> the owner has a live Conductor run executing out of `~/.claude/conductor`, and renaming that
> path while a run holds a worktree, a schedule, or a live process under it breaks absolute
> linked-worktree metadata, so worktree registrations dangle and work completes under a
> quarantined path. Not moving the tree removes that specific hazard, but it does not remove the
> gate. The single blocking precondition splits in two:
>
> - **Loss-risk gates are unconditional and must pass before quarantine.** P1 (no commit exists
>   only here), P2 (no untracked *or ignored* content of value), P4 (nothing external names the
>   path), P5 (no worktree holds state with no second copy). They ask one question: would
>   anything be destroyed that exists nowhere else? As of the 2026-08-17 readings **all four are
>   blocked**, which is why execution is deferred. Deletion is the only irreversible step and
>   GitHub recovers only what was pushed to it.
> - **Quiesce is now a declared window, not a standing invariant.** "No durable process rooted
>   here" (P3) and the other activity checks are evaluated inside an announced window immediately
>   before the `mv`; a failed check reschedules the window rather than failing the design. Do not
>   re-tighten P1 or P5 back into absolute activity invariants — a gate that goes red whenever
>   anyone is working in the tree gates nothing.
>
> Plans 01–07 remain unaffected: they are ordinary feature branches developed in `.worktrees/`,
> and they never move, rename, or write to the checkout root.

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

**File:** `2026-08-10-plan-04-host-adapters.md` — **written** (commit `ee8befe`)
**Repo:** conductor
**Spec sections:** §"System architecture"; the host-floor paragraph in §"Packaging and
installation"; adapter/permission/dispatch bullets in §"Unit and contract tests".

**Goal:** introduce `conductor/hosts/{base,proc,claude,codex,cli}.py` implementing the adapter
surface, so no core module contains a Claude slash command, a Codex dollar invocation,
`CLAUDE_PLUGIN_ROOT`, a host-specific permission flag, or an assumption about one installation
directory. Thirty-one production lines across `resume_script.py`, `driver.py`, and `preflight.py`
carry a host assumption today; Plan 04 builds the destination without moving them.

**Produces** — the written plan establishes a **nineteen-member** surface. The design's eleven
capabilities and the twelve methods this roadmap previously listed were both insufficient; this
block is now the plan's, not the roadmap's guess:

```python
# conductor/hosts/base.py
@dataclass(frozen=True)
class DispatchResult:                                          # referenced by the old signature,
    host: str; argv: tuple[str, ...]; returncode: int          #   defined nowhere until now
    result_path: str; result_text: str; truncated: bool; duration_s: float

class HostAdapter(Protocol):
    id: str                                                   # "claude" | "codex"; also the executable basename
    def executable(self) -> str: ...
    def source_root(self) -> str: ...
    def version(self) -> tuple[int, ...]: ...
    def minimum_version(self) -> tuple[int, ...]: ...
    def upgrade_hint(self) -> str: ...
    def native_invocation(self, skill: str) -> str: ...        # "/conductor:autodev" | "$conductor:autodev"
    def launch_prompt(self, skill: str, *, run_key: str | None = None) -> str: ...
    def worker_argv(self, *, state_root: str, run_key: str, project_root: str,
                    posture: str = "supervised") -> list[str]: ...
    def worker_env(self, *, state_root: str, run_key: str,
                   project_root: str) -> dict[str, str]: ...
    def reviewer_argv(self, *, pr: int, head_sha: str, run_key: str, project_root: str,
                      posture: str = "supervised") -> list[str]: ...
    def permission_profile(self, posture: str = "supervised") -> dict: ...
    def validate_permissions(self, profile: dict) -> None: ...
    def process_identity(self, pid: int) -> str: ...           # "<host>:<pid>:<start-ticks>"
    def process_alive(self, identity: str) -> bool: ...
    def processes_under(self, roots: list[str]) -> list[int]: ...
    def install_hooks(self, state_root: str, run_key: str, *, command: list[str]) -> str: ...
    def hook_installed(self, state_root: str, run_key: str) -> bool: ...
    def dispatch_implementation(self, prompt: str, *, timeout: float,
                                result_path: str | None = None,
                                posture: str = "scoped") -> DispatchResult: ...
def load(host_id: str) -> HostAdapter
def opposite(host_id: str) -> str
```

**Why each addition, since the earlier list looked complete:** `upgrade_hint` because preflight
must fail with the documented minimum-version command and nothing else renders one;
`process_identity` because `process_alive(identity)` needs an identity to have been minted, and
Plan 02 storing a bare PID would reintroduce PID reuse; **`processes_under` because
`process_alive` cannot express the double-drive guard** — it asks "is the process I recorded
alive?", while the guard asks "is any process of my host already driving this directory?", with
no prior recording, which is the entire point; `hook_installed` because `install_hooks -> None`
leaves "missing, untrusted, disabled, or ineffective" undetectable; `worker_env` because Claude's
proven worker argv is a bare slash command, so the run key travels in the environment rather than
changing a dispatch path many live fires have proven.

**Corrections the written plan makes to this roadmap and the design:**

- **`install_hooks` takes the command.** The old signature implied the adapter knows what the
  hook should do. It runs Plan 05's checkpoint sequence, which does not exist yet. The caller
  supplies the command; the adapter owns host-native placement and format.
- **`native_invocation` and `launch_prompt` are two methods, not one.** `$conductor:*` is a
  prompting convention, not a host primitive: `$name` is resolved by the *model* reading a
  dispatch table in `~/.codex/AGENTS.md`, so on a machine without one it resolves to nothing.
  Fixing the quoting does not make the launch work, it makes it fail differently. `launch_prompt`
  emits the expansion — an explicit `SKILL.md` path — while `native_invocation` preserves the
  user-facing surface the design describes.
- **`dispatch_implementation` is the out-of-session child-process form.** The design describes an
  in-session subagent mechanism; a Python adapter spawned from cron has no Task-tool API. Whether
  Codex has a native in-session subagent primitive at all is still open.
- **Bounded result collection cannot be symmetric.** Codex has `--output-schema`; Claude has no
  equivalent. Plan 04 delivers bounded *text* with a byte cap and a caller-named `result_path`.
  Structured verdicts are Plan 07's.

**Hard requirements:** adapters launch **argument vectors**, never interpolated shell strings; the
literal `$conductor:*` token must not be shell-expanded *and* must not be relied on as a launch
mechanism; an adapter that cannot dispatch isolated implementation work **fails preflight** — the
orchestrator never absorbs implementation as fallback. Plan 04 ships **unwired**: nothing in
`driver.py`, `resume_script.py`, or `preflight.py` imports it, and those three files stay
byte-identical. Plan 05 is where the adapter first carries load.

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

**Handed off explicitly by written Plan 04, resolve before assuming:** Codex `PreCompact` is
unimplemented and blocks unattended Codex runs — unblocking needs a probe proving the hook
requests a checkpoint and blocks continuation. Claude's hook payload shape is unprobed; Plan 04's
tests assert a round trip through its own reader, not conformance to Claude's schema. And
**whether Codex has a native scheduler was not observed, not confirmed absent** — settle that
before this plan's per-run `heartbeat.sh` assumes OS cron on both hosts.

**Produces:** `conductor/heartbeat/{cli,schedule,checkpoint,marker}.py`, the two verbatim
orchestrator reminders and their anchor contract tests, and the reconciliation evidence
precedence (Git → GitHub → `results.json` → `run.json` → `handoff.md`).

**Hard requirements:** a skipped fire caused by a live lock is a **success**; a checkpoint-only
fire does not advance the phase ledger; `awaiting-team-merge` removes its schedule; `finish`
refuses until authoritative remote metadata proves the final PR merged with the right base, head
SHA, and no review debt; failed commit/push/lease/handoff **blocks** rather than exiting clean.

**Dispatch economics (owner instruction, 2026-08-10).** These bind the prompts Conductor's own
orchestrator sends to its implementation and review subagents, not just this repo's development
process:

- **No full test suites inside a phase.** An implementation subagent runs only the focused tests
  covering the code it changed. The full suite runs once, at the phase pull request — which is
  also where the merge gate already runs it.
- **Never re-run the same suite on the same task.** A reviewer does not re-run tests the
  implementer already ran on identical code; the implementer's report is the test evidence.
- **Scale the model to the task.** Small, well-specified transcription work gets the cheapest
  tier for both implementation and review. Reserve the expensive tiers for architecture,
  concurrency, and the whole-branch review.

The orchestrator prompt contract in design §"Orchestrator context contract" is where these land,
alongside the two verbatim reminders. Note that today's shipped `skills/autodev/SKILL.md` predates
this and should be updated independently rather than waiting for Plan 05 — see the standalone
follow-up below.

**Standalone follow-up (do before Plan 05):** apply the three rules above to the existing
`skills/autodev/SKILL.md` dispatch prose. That is a live behaviour change to a shipped skill, so
it takes a feature branch, a PR, a codex review, and a plugin version bump — not a docs-direct
commit.

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

**Affordances Plan 04 deliberately left here:** Codex's `--output-schema` may be used as a
Codex-only path to structured verdicts — Claude has no equivalent, so the schema must degrade to
Plan 04's bounded text on that side rather than becoming a shared requirement. `codex exec review`
exists but its argument contract was not verified; whether to use it is this plan's decision.

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

**Ordering exception — this plan does not start before Plan 04 merges.** Packaging Conductor for
Codex while the adapter is absent produces a plugin that installs cleanly and then tries to spawn
`claude` at first fire, on a machine that may not have Claude installed. This is the one place the
"write ahead of your dependency" allowance above does not apply. The written Plan 04 also records
that the Codex plugin system is missing from the design entirely — `codex plugin {add, list,
marketplace, remove}`, `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, a `policy`
block Claude has no counterpart for, and `--sparse` file scoping Claude has no mechanism for —
plus that Codex's installed plugin-cache layout is not contractual anywhere. Establishing that
layout is this plan's job.

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
06 → 07 in the canonical clone as soon as decommission precondition 1 (fresh clone established at
`~/programming/conductor`) is done — and, because nothing is being moved, it may equally proceed
from `.worktrees/` under the old path until then. Release order is fixed:

1. Decommission precondition 1 — fresh clone established. Preconditions 2–5 (archive untracked and
   ignored state, give every linked worktree a recorded outcome, clear the stale Codex
   project-trust entry, install and smoke-test the intended plugin version) run in that order
   alongside engineering; the quarantine itself does **not** happen here.
2. Plan 08 — spec-craft dual-host, versioned artifact.
3. Plans 01–07 — Conductor dual-host against that supported spec-craft version.
4. Plan 09 — marketplace catalogs and bundles. Not before Plan 04 has merged.
5. Plan 10 — descriptions and public documentation, after each repository team accepts.
6. Branch-based installation smokes before merge; public installation smokes after merge.
7. Decommission precondition 6 — declare the quiesce window, `mv` to a dated quarantine directory,
   observe for one week, then remove after a final re-run of all six checks against the
   quarantined copy.

**Conductor never merges any of these default-branch pull requests.**
