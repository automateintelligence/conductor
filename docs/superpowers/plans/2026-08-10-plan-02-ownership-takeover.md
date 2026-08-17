# Ownership, Leases, Takeover, Prune, and Rebind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-run execution ownership an explicit, durable, liveness-checked record — so that two Conductor fires over the same run can never both execute its autodev prologue, so a dead worker's run can be recovered without guessing, and so a run can move between hosts and between workstations under one compare-and-swap rather than under an operator's hope.

**Architecture:** `owner.lock` is a short-lived `flock` mutex; the record it guards is a sibling `owner.json` written with Plan 01's atomic writer. Exclusion is decided by the *record* (identity + lease + liveness), not by how long anyone holds the mutex — because Conductor has two execution tiers and only one of them has a wrapper process that can hold a file descriptor for a whole fire. `run.json`'s `lease` and `heartbeat` blocks stay diagnostic mirrors refreshed under `state.lock`. Takeover, prune, and rebind are three read-modify-write operations over that record, differing only in what they must prove before they write.

**Tech Stack:** Python 3.12 standard library only (`contextlib`, `dataclasses`, `datetime`, `json`, `os`, `signal`, `time`), pytest, ruff, pyright. Linux `/proc` via Plan 04's `conductor.hosts` package.

**Source design:** `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md` §"Ownership, locking, and takeover" (lines 182–202), the lease/marker paragraphs of §"Failure handling" (lines 450–452), and the §"Unit and contract tests" bullets on lock/lease/PID reuse (line 492), orphan refusal and prune ordering (line 493), rebind (line 494), and permission profiles and bypass non-transfer (line 499).

**Inherited constraints:** `docs/reviews/2026-08-10-plan-01-residuals.md` §"Constraints Plan 02 must honour". Read that file before Task 1. It is not optional context; three of this plan's tasks exist only because of it.

**Roadmap:** `docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md` — this is Plan 02 of 11, reserved at roadmap line 531. **Plan 02 is Track B** (improvement work following the Codex-capable release); it is not a prerequisite for any Track A item, and no Track A item names it.

---

## Dependency status — the roadmap's table is wrong about this plan

The roadmap dependency table (line 73) says Plan 02 depends on **01 only**. That is false, and Plan 04's own front matter says so from the other side: *"Plan 02's `ownership.prove_exited(record)` will call this plan's `process_alive(identity)`, so the identity format defined in Task 7 is an interface Plan 02 consumes. Plan 02 must not invent a second identity string."*

**Plan 02 depends on Plan 01 and Plan 04.** Correct the table when this plan merges.

The practical consequence for whoever executes this:

| Tasks | Executable when |
| --- | --- |
| 1, 2, 3 | **Now.** Plan 01 is merged (PR #84). These touch only `conductor/core`. |
| 4–10 | **After Plan 04 merges.** `conductor/hosts/` does not exist on `main` today (roadmap line 75). |

Tasks 4–10 take a `HostAdapter` by dependency injection (`probe=`), so their *tests* run against fakes. Their *default* argument imports `conductor.hosts.base`, and that import is what does not resolve until Plan 04 lands. Do not work around this by writing a second liveness implementation inside `conductor/core`. A duplicate identity format is precisely the PID-reuse hole the design spends a paragraph closing.

**The Track A restructure partially unblocks this, but not completely.** Track A's **A1** creates `conductor/hosts/` early with a subset of Plan 04's protocol — including *"a process-liveness probe for the double-drive guard"* (roadmap line 236) — and the roadmap states A1 is *"a subset, not a competitor; Plan 04 extends A1's module rather than replacing it."* So Tasks 4, 5, 7, 9, and 10 may become executable once A1 ships, provided A1's probe exposes `process_identity` and `process_alive` under those names. **Task 6 is not unblocked by A1:** roadmap line 239 puts `validate_permissions` explicitly in Plan 04, and Task 6 is a contract test *about* it. Check A1's shipped surface before assuming; if the names differ, the correct move is to have Plan 04 reconcile them, never to add an adapter method from this plan.

---

## The central question: run lease versus phase claim

This is the most important section in the plan. Get it wrong and the repository has either a duplicated working mechanism or two sources of truth about who owns what.

### What already exists, verified on `main`

`ledger/claim.py:67` implements a phase-level checkout mediated entirely by GitHub:

```python
def claim(repo, n, worker, now_ts, ttl, gh) -> bool:
    if not eligible(gh.issue_state(repo, n)):
        return False
    gh.assign(repo, n, worker)
    confirm = gh.issue_state(repo, n)
    if confirm["assignees"] != [worker]:      # lost the race
        gh.unassign(repo, n, worker)          # back off; no status/lease touched yet
        return False
    gh.set_labels(repo, n, add=["status:in-progress"], remove=[...])
    renew_lease(repo, n, worker, now_ts, gh)
    return True
```

`skills/autodev/SKILL.md:108` consumes it: *"If False, back off and re-pick."* `ledger/reconcile.py:43,62,71,74,83` interprets the resulting lease and label state, including `stale-lease-reclaim` at `reconcile.py:49` — which deliberately resets the retry counter so a reclaimed phase is not punished for its predecessor's death.

This is optimistic concurrency with a GitHub-mediated compare-and-swap. It works, it is tested, and it already prevents two workers from *executing the same phase* on either scheduling tier.

### The design does not settle the relationship — so this plan does

Design §"Ownership, locking, and takeover" never mentions the ledger claim. It asserts *"A run has exactly one worker owner"* without saying what that implies for a mechanism that already assigns work at a finer grain. Nothing in §"Failure handling" resolves it either. **This is a genuine gap in the design document, recorded here rather than silently resolved.**

**Recommendation, adopted by this plan: two layers, different scopes, neither subsuming the other, with a defined precedence.**

| | Phase claim (`ledger/claim.py`) | Run lease (`owner.json`, this plan) |
| --- | --- | --- |
| Protects | one phase *issue*, and the ledger mutations around it | one *run*'s local shared state on one workstation |
| Resource contended | the GitHub issue's assignee field | `.conductor/runs/<key>/run.json`, the run branch's `index.lock`, `assertions/<slug>/run/results.json`, the handoff, the schedule entry |
| Medium | GitHub API | local filesystem: `flock` + a record |
| Participants | any worker anywhere, including another machine | processes on **this** workstation only |
| Granularity in time | one phase, minutes to hours (`L=900s` default in `reconcile`) | one fire, including the prologue that runs *before* any phase is chosen |
| Failure mode | lost race → `False` → back off and re-pick | busy → **skip the fire, exit success** |
| Fencing | none — it is advisory and durable, not a mutex | `flock` around every record mutation |

**Neither subsumes the other, for two independent reasons.**

1. **The claim cannot protect the prologue.** Autodev steps 1 through 4b run *before* step 5's claim, touch no issue, and mutate shared files. There is no issue to compare-and-swap on, because which phase this fire will work on has not been decided yet. A finer-grained lock cannot protect work that happens before the grain exists.

2. **The lease cannot replace the claim.** `owner.lock` is a local file. Design line 200 refuses cross-machine automatic takeover outright, and line 452 says the GitHub marker *"is durable audit and crash-recovery evidence, not a distributed mutex."* Two workstations would each hold their own uncontended `owner.lock` and both proceed to claim the same phase. Only GitHub can arbitrate that, and `claim.py` already does.

**Precedence when they disagree.** The lease is *admission control*; the claim is *work assignment*.

- A fire **must hold the run lease before it may claim a phase.** Ownership is a precondition of assignment, never the reverse.
- A worker that holds a claim but has lost or never held the lease **must stop, and must not release the claim.** Releasing is a ledger mutation, and a worker that has lost admission is not the authority on that phase. It stops at the next checkpoint boundary and lets the phase lease lapse; `reconcile`'s existing `stale-lease-reclaim` (`ledger/reconcile.py:43-49`) is the reclaim path, and it already does not count against the retry cap. **Plan 02 adds no second reclaim path.**
- A live run lease **never** makes a phase claimable. `owner.json` is not consulted for phase eligibility, ever.

**Two anti-requirements, checked in the definition of done:**

```bash
grep -rn 'conductor\.core\.ownership\|core import ownership' ledger/    # must return nothing
grep -rn 'ledger\.' conductor/core/ownership.py conductor/core/rebind.py   # must return nothing
```

If `claim.py` ever imports `ownership`, the GitHub ledger stops being the single source of truth for phase assignment. If `ownership.py` ever imports `ledger`, a local mutex acquires an opinion about a distributed one. Both directions are wrong and both are cheap to detect.

**One deliberate consequence.** The two lease durations differ by an order of magnitude (120s versus 900s) and that is not an inconsistency to reconcile. The run lease is a locally renewed admission token whose holder's liveness is directly observable. The phase lease is a durable cross-fire abandonment timer whose holder may be on a machine this process cannot see. They measure different things.

---

## The gap this plan closes — concrete, current, and narrower than it looks

Autodev's prologue — steps 1, 1b, 2, 3, 4, 4b — runs before the claim and writes shared state:

- **step 1b** runs `git fetch "$R" "$D" && git merge "$R/$D"` on the run branch **every fire**, taking `index.lock` in the run worktree;
- **steps 2 and 3** run `conductor assert run --level spec`, which writes `assertions/<slug>/run/results.json` through `assertions/run.py:169-172` — a plain `open(..., "w")` + `json.dump`, **not** an atomic replace — and both `ledger reconcile --from-gate` and the done-gate read that file.

Two concurrent fires can interleave those writes. What actually guards against that today, verified at `conductor/resume_script.py:208-219`:

```sh
# (c) one headless fire at a time — hold the lock in the main checkout for the whole fire.
exec 9>"$PROJECT/.conductor/resume.lock"
flock -n 9 || exit 0

# (a) never double-drive: exit if a claude process already holds the project OR worktree cwd
for pid in $(pgrep -f 'claude' 2>/dev/null); do
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    case "$cwd" in "$PROJECT"|"$PROJECT"/*|"$WORKTREE"|"$WORKTREE"/*) exit 0 ;; esac
done
```

**Be precise about what that does and does not cover** — the roadmap's framing overstates the hole, and an implementer who believes the prologue is unguarded will write the wrong fix.

| Pair of fires | Guarded today? |
| --- | --- |
| OS-cron × OS-cron, same project | **Yes** — guard (c), the `flock`, held for the whole fire |
| OS-cron × in-session REPL | **Partly** — guard (a), but it is a TOCTOU snapshot taken by only one of the two participants, and `pgrep -f 'claude'` matches one host by name |
| in-session × in-session | **No** — the in-session tier takes nothing at all |
| OS-cron × OS-cron, *different runs*, same project | Yes, and **wrongly so**: `resume.lock` is project-scoped, so Plan 01's whole multi-run premise serializes two independent runs against each other |

So the real defects are: guard (a) is an unsynchronized check made by one side only and blind to a Codex worker; the in-session tier takes no lock; and the lock that does exist is scoped to the wrong object.

**Plan 02's deliverable is that both tiers consult one per-run record under one mutex, so the check-and-claim is atomic instead of a snapshot.** Task 10 wires it. The pgrep guard stays — `processes_under` is Plan 04's and the driver rewrite is Plan 05's — so this plan strictly adds a guard rather than replacing one.

**Honest limit, stated up front.** A *legacy flat* run has no run key and no `run.json`, so it has nothing to key a per-run record on. `own acquire` on an unresolvable run prints a notice and exits 0 without tracking ownership, leaving today's guards exactly as they are. The gap closes for per-run state; it closes for the currently-live legacy run only when Plan 03 migrates it. Do not paper this over by inventing a synthetic run key for legacy state — that is Plan 03's job and it has to preserve branch names.

---

## Scope boundary — what this plan does not touch

| Out of scope | Owner |
| --- | --- |
| `heartbeat.sh`, `heartbeat.json`, schedule installation and removal, `conductor resume`/`finish`, the checkpoint sequence, `compaction.marker` | **Plan 05** |
| Legacy flat-state migration; giving a legacy run a run key | **Plan 03** |
| `process_alive`, `processes_under`, `process_identity`, `permission_profile`, `validate_permissions` — their *implementations* | **Plan 04** |
| Verdict schema, review freshness, review debt, discharge | **Plan 07** |
| Rewriting `resume_script.py`'s driver, narrowing `resume.lock` to per-run, retiring the pgrep guard | **Plan 05** |
| Phase eligibility, the retry cap, `stale-lease-reclaim` | **already shipped in `ledger/`; unchanged** |

---

## Global Constraints

- **Host floor:** Claude Code `2.1.224`, Codex CLI `0.147.0`.
- **Canonical editable checkout:** `~/programming/conductor`, established by a fresh `git clone`; the old `~/.claude/conductor` is retired in place. *(Superseded mechanism — see Plan 04 correction 7. Plan 02 never reads, writes, or names the checkout root.)*
- **Plugin identity:** one public name, `conductor`.
- **Run key format:** `<spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]`. Generation 1 omits `-g<N>`.
- **Run integration branch:** `conductor/run-<run-key>`. **Phase branch:** `conductor/<run-key>/phase-<phase-id>`.
- **Run status vocabulary (exactly these six):** `active`, `checkpointed`, `blocked`, `awaiting-team-merge`, `terminal`, `failed`.
- **Review policy vocabulary (exactly these three):** `opposite-required`, `same-host-fallback-allowed`, `blocked-pending-opposite-host`.
- **Global lock order:** `migration.lock` (when applicable), then `project.lock` (when applicable), then `owner.lock`, then `state.lock`. Multi-run project operations acquire run locks in **sorted run-key order**. **Plan 02 does not change this order** — it replaces Plan 01's flat busy-lock refusal with liveness interpretation at the same acquisition points.
- **Lease defaults:** 120 second lease, renewed at least every 30 seconds. A repository may lengthen these but may **not** configure a renewal interval greater than one quarter of the lease duration.
- **Review freshness:** default maximum review age 24 hours. **Per-fire context budget:** default `0.60`. *(Plans 07 and 05.)*
- **Conductor never merges to the repository default branch.**
- **Every state write** uses a sibling temporary file, flush, fsync, and atomic replace. `run.json` writes additionally require `state.lock` and the current revision; `project.json` mutations require `project.lock` and its current revision. **`owner.lock` is the documented exception and Task 1 explains why** — you cannot atomically replace a file you hold an `flock` on.
- **Every actionable failure reports:** run key and current state, the failed invariant or operation, the affected branch/worktree/pull request/state path, whether any write occurred, and the exact inspect/retry/takeover/migrate/recovery command.
- **Journal discipline (Plan 01 residual, non-negotiable):** every transaction entry that writes a `run.json` **must** carry a `{"lock": {"path": ..., "run_key": ...}}` hint. An entry without one replays with no serialization against that run's writers. Recovery holds those locks in sorted run-key order and refuses to move a revision backwards; **never assume an unapplied journal will win.**
- **Tooling gates:** `ruff check . && ruff format --check .`, `pyright .`, `pytest -q`. Python 3.12.

---

## Working agreements for this plan

- **Do not reformat files this plan does not touch.** `ruff format --check .` fails on 11 pre-existing files. Run `ruff format` on **only** the files you create or modify.
- **The done-gate is frozen.** `assertions/manifest.yaml` and `assertions/self_enforcement/` hold A1–A16 under `conductor/freeze.py`. **A12 (`test_a12_skills_call_resolvers.py`) observes `skills/autodev/SKILL.md`, which Task 10 edits.** Re-run `./bin/conductor gate verify` after Task 10 and treat a failure as a real regression, never a baseline to refresh.
- **Task 10 is a live behaviour change to a shipped skill.** Per the standing rule, that means a feature branch, a PR, a codex review, and a plugin version bump — not a docs-direct commit. It is deliberately the last task so the branch's review has everything else already settled.
- **Linux only.** Liveness reads `/proc` through Plan 04's `conductor.hosts.proc`. Same limitation as the shipped `pgrep` guard; nothing regresses, but nothing covers macOS either.
- **Commit granularity.** One commit per task; message style is files modified with line numbers plus one or two bullets on what was done.

---

## File Structure

**New:**

| File | Responsibility |
| --- | --- |
| `conductor/core/ownership.py` | the ownership record, its file placement, the two acquisition tiers, `prove_exited`, `takeover`, `prune` |
| `conductor/core/rebind.py` | workstation rebind: the project transaction over every run's ownership record |
| `conductor/own_cmd.py` | the `conductor own` CLI verb group |

**Modified:**

| File | Change |
| --- | --- |
| `conductor/core/locks.py` | seed `_held` from `CONDUCTOR_HELD_LOCKS` and export it to children, so the order invariant survives a subprocess boundary (Task 3). `LOCK_ORDER` is untouched. |
| `conductor/core/registry.py:113-116` | the exhausted-attempts `RevisionConflict` stops hardcoding "no write occurred" (Task 8) |
| `conductor/core/repoint.py:208-220` | the flat busy-`owner.lock` refusal becomes liveness interpretation, at the same acquisition point (Task 9) |
| `conductor/resume_script.py` | the generated driver acquires and releases the ownership record around its fire (Task 10) |
| `skills/autodev/SKILL.md` | a step 0 that acquires the record before the prologue, and release on every exit path (Task 10) |
| `bin/conductor` | add the `own` verb; extend the usage text |

**Tests:**

| File | Covers |
| --- | --- |
| `tests/conductor/core/test_ownership_record.py` | Task 1 |
| `tests/conductor/core/test_ownership_acquire.py` | Task 2 |
| `tests/conductor/core/test_lock_propagation.py` | Task 3 |
| `tests/conductor/core/test_ownership_liveness.py` | Task 4 |
| `tests/conductor/core/test_takeover.py` | Tasks 5, 6 |
| `tests/conductor/core/test_prune.py` | Task 7 |
| `tests/conductor/core/test_rebind.py` | Task 8 |
| `tests/conductor/core/test_repoint.py` | Task 9 (extend the existing file) |
| `tests/conductor/test_own_cmd.py` | Task 10 |

---

## How to read the test steps

Every test step carries a **Falsifier** line: the exact edit you would make to the implementation to prove the test can fail. Plan 01's residuals record **four tests that could not fail**, and a recent branch shipped three more; none was found by the suite passing or failing, every one by someone asking *would this fail if the code under test were reverted?* **Before marking any task done, apply its falsifier, watch the named test fail, and revert.**

---

## Shared test fixtures

Every task below uses these. Add them to `tests/conductor/core/conftest.py` as part of Task 1.

```python
"""Fixtures shared by the Plan 02 ownership tests."""

from __future__ import annotations

import pytest

from conductor.core import registry, runkey, runstate, schema, workstation

NOW = "2026-08-17T12:00:00+00:00"
SPEC = "docs/specs/alpha.md"


@pytest.fixture
def state_root(tmp_path):
    """A `.conductor` state root with one registered, active run."""
    root = tmp_path / "repo" / ".conductor"
    root.mkdir(parents=True)
    key = runkey.run_key(SPEC)
    ws = workstation.workstation_id()
    registry.init(str(root), workstation_id=ws, repo_identity={"root_commit": "abc", "origin_url": None})
    registry.update(str(root), lambda doc: registry.register(doc, spec=SPEC, run_key=key, generation=1))
    runstate.create(
        str(root),
        key,
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path=SPEC,
            workstation_id=ws,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest="a" * 64,
            now=NOW,
        ),
    )
    return str(root)


@pytest.fixture
def run_key_value():
    return runkey.run_key(SPEC)


class FakeAdapter:
    """Stands in for a Plan 04 HostAdapter. Liveness is a set the test controls."""

    def __init__(self, host_id: str, alive: set[str] | None = None, posture: str = "supervised"):
        self.id = host_id
        self._alive = set(alive or ())
        self._posture = posture
        self.terminated: list[str] = []

    def process_alive(self, identity: str) -> bool:
        if not identity.startswith(f"{self.id}:"):
            raise ValueError(f"{identity!r} is not a {self.id} identity")
        return identity in self._alive

    def process_identity(self, pid: int) -> str:
        return f"{self.id}:{pid}:1000"

    def permission_profile(self, posture: str = "supervised") -> dict:
        if posture not in ("supervised", "scoped", "full-bypass"):
            raise ValueError(f"unknown posture {posture!r}")
        return {"host": self.id, "posture": posture, "argv": [f"--{self.id}-{posture}"]}

    def validate_permissions(self, profile: dict) -> None:
        if profile.get("host") != self.id:
            raise ValueError(f"profile minted by {profile.get('host')!r} cannot authorise {self.id}")

    def kill(self, identity: str) -> None:
        self.terminated.append(identity)
        self._alive.discard(identity)


@pytest.fixture
def probes():
    """`{host_id: FakeAdapter}` plus a `load`-shaped callable over it."""
    table = {"claude": FakeAdapter("claude"), "codex": FakeAdapter("codex")}
    table["load"] = lambda host_id: table[host_id]  # type: ignore[assignment]
    return table
```

---

### Task 1: The ownership record — placement, shape, and the `flock`/rename hazard

**Files:**
- Create: `conductor/core/ownership.py`
- Create: `tests/conductor/core/conftest.py` (the fixtures above)
- Test: `tests/conductor/core/test_ownership_record.py`

**Interfaces:**
- Consumes: `conductor.core.atomic.write_json_atomic`/`read_json`, `conductor.core.locks.hold`, `conductor.core.runstate.owner_lock_path`, `conductor.core.runkey.is_safe_run_key`.
- Produces:
  - `RECORD_SCHEMA_VERSION: int` (1)
  - `TIERS: tuple[str, ...]` — `("wrapper", "in-session")`
  - `class OwnerBusy(RuntimeError)`, `class OwnerAmbiguous(RuntimeError)`, `class OwnerOrphaned(RuntimeError)`, `class OwnerMissing(RuntimeError)`
  - `class OwnerRecord(NamedTuple)` — `run_key, host, workstation_id, wrapper_identity, host_identity, tier, posture, renewed_at, lease_expires_at, heartbeat_id`
  - `record_path(state_root: str, run_key: str) -> str`
  - `read(state_root: str, run_key: str) -> OwnerRecord | None`
  - `_write(state_root: str, run_key: str, record: OwnerRecord | None) -> None` (module-private; every caller holds `owner.lock`)

**Design note — why the record is not inside `owner.lock`.** Design line 184 says *"owner.lock is the authoritative execution-ownership lock and records the run key, host, wrapper process identity, launched host process identity, lease, and heartbeat identity."* Read literally, that puts the fields inside the lock file. **It is not implementable together with the global atomic-write constraint.** Plan 01's `atomic.write_atomic` finishes with `os.replace(tmp, path)`, which installs a *new inode* at the path. A process holding `flock` on the old inode keeps holding it — on a file nothing will ever open again — while every subsequent acquirer locks the new inode uncontended. The mutex would silently stop excluding anything, and nothing would fail.

So: **`owner.lock` stays a zero-byte pure mutex; the fields live in a sibling `owner.json` that the mutex guards.** The design's intent — one authority, not two — is preserved exactly, because `owner.json` is only ever read or written while `owner.lock` is held. This is recorded as correction 1 in §"Where this plan corrects the roadmap and the design".

**Design note — why `OwnerRecord` has ten fields, not the roadmap's six.** Additions, each with the invariant it serves:

| Field | Why the roadmap's six are insufficient |
| --- | --- |
| `workstation_id` | design line 200 refuses automatic takeover when the registry names a different workstation. Reading it from `project.json` at takeover time would hang a refusal off a document Plan 01 documents as a mirror (`registry.py:14-17`) |
| `tier` | the two execution tiers have genuinely different liveness stories (Task 2). A reader that cannot tell them apart cannot decide whether an unheld `flock` is evidence of anything |
| `posture` | design line 200's bypass non-transfer needs the *prior* posture recorded to prove the new host did not inherit it (Task 6) |
| `renewed_at` | a wrapper that is alive but has stopped renewing is hung, not healthy. Without a renewal timestamp that state is indistinguishable from a healthy one |

`heartbeat_id` is typed `str | None`, not the roadmap's `str`: Plan 05 owns schedules and there is no schedule id to record until it lands.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_ownership_record.py`:

```python
"""The ownership record: one authority, guarded by a mutex that must not be replaced.

`owner.lock` is a zero-byte flock. The fields live in `owner.json` beside it, because an
atomic-replace write of the lock file itself would install a new inode and silently orphan
every existing holder's lock — a mutex that stops excluding anything, with nothing failing.
"""

from __future__ import annotations

import os

import pytest

from conductor.core import locks, ownership, runstate


def test_the_lock_and_the_record_are_different_files(state_root, run_key_value):
    lock = runstate.owner_lock_path(state_root, run_key_value)
    record = ownership.record_path(state_root, run_key_value)
    assert lock != record
    assert os.path.dirname(lock) == os.path.dirname(record)
    assert record.endswith("owner.json")


def test_writing_the_record_leaves_the_lock_inode_untouched(state_root, run_key_value):
    """The regression that makes the whole design collapse silently."""
    lock = runstate.owner_lock_path(state_root, run_key_value)
    with locks.hold(lock, kind="owner", run_key=run_key_value):
        before = os.stat(lock).st_ino
        ownership._write(state_root, run_key_value, _record(run_key_value))
        after = os.stat(lock).st_ino
    assert before == after


def test_read_returns_none_when_no_record_exists(state_root, run_key_value):
    assert ownership.read(state_root, run_key_value) is None


def test_a_record_round_trips_through_every_field(state_root, run_key_value):
    original = _record(run_key_value)
    with locks.hold(runstate.owner_lock_path(state_root, run_key_value), kind="owner", run_key=run_key_value):
        ownership._write(state_root, run_key_value, original)
    assert ownership.read(state_root, run_key_value) == original


def test_writing_none_clears_the_record(state_root, run_key_value):
    lock = runstate.owner_lock_path(state_root, run_key_value)
    with locks.hold(lock, kind="owner", run_key=run_key_value):
        ownership._write(state_root, run_key_value, _record(run_key_value))
        ownership._write(state_root, run_key_value, None)
    assert ownership.read(state_root, run_key_value) is None
    assert not os.path.exists(ownership.record_path(state_root, run_key_value))


def test_a_record_naming_another_run_is_refused_rather_than_returned(state_root, run_key_value):
    """A hand-edited or half-restored record must not be believed about a run it does not name."""
    path = ownership.record_path(state_root, run_key_value)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write('{"schema_version": 1, "run_key": "someone-else-11111111", "host": "claude",'
                     ' "workstation_id": "w", "wrapper_identity": "claude:1:1", "host_identity": null,'
                     ' "tier": "wrapper", "posture": "supervised", "renewed_at": "2026-08-17T12:00:00+00:00",'
                     ' "lease_expires_at": "2026-08-17T12:02:00+00:00", "heartbeat_id": null}')
    with pytest.raises(ownership.OwnerAmbiguous) as excinfo:
        ownership.read(state_root, run_key_value)
    assert run_key_value in str(excinfo.value)
    assert "someone-else-11111111" in str(excinfo.value)


def test_an_unknown_tier_is_refused(state_root, run_key_value):
    with pytest.raises(ownership.OwnerAmbiguous):
        ownership.OwnerRecord(
            run_key=run_key_value, host="claude", workstation_id="w",
            wrapper_identity="claude:1:1", host_identity=None, tier="daemon",
            posture="supervised", renewed_at="2026-08-17T12:00:00+00:00",
            lease_expires_at="2026-08-17T12:02:00+00:00", heartbeat_id=None,
        ).validated()


def _record(key):
    return ownership.OwnerRecord(
        run_key=key, host="claude", workstation_id="w0",
        wrapper_identity="claude:4242:99001", host_identity="claude:4243:99002",
        tier="wrapper", posture="supervised",
        renewed_at="2026-08-17T12:00:00+00:00",
        lease_expires_at="2026-08-17T12:02:00+00:00",
        heartbeat_id=None,
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_ownership_record.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.ownership'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/ownership.py`:

```python
"""Per-run execution ownership: the record, its lease, and the operations over it.

`owner.lock` is the mutex; `owner.json` beside it is the record the mutex guards. They are two
files on purpose. Design line 184 reads as though the fields live inside the lock, but Plan 01's
durable-write contract finishes every write with `os.replace`, which installs a NEW INODE at the
path. A process holding `flock` on the old inode keeps holding it, on a file nothing will ever
open again, while every later acquirer locks the new inode uncontended. The mutex would stop
excluding anything and nothing would fail. So the lock file is never written; only the record is.

AUTHORITY: this record is the sole execution-ownership authority within one workstation's
canonical state root. `run.json`'s `lease` and `heartbeat` blocks are diagnostic mirrors refreshed
on renewal (design line 184). The GitHub issue/PR ownership marker is durable audit evidence, not
a distributed mutex (design line 452). NOTHING here is consulted to decide phase eligibility —
that is `ledger/claim.py`'s, mediated by GitHub, and the two layers do not import each other.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from conductor.core import atomic, runstate

RECORD_SCHEMA_VERSION = 1

# "wrapper": a shell/heartbeat process holds owner.lock for its launched host's whole lifetime
#            (design line 190). "in-session": a `/conductor:autodev` fire inside a live host REPL,
#            where no process outlives a single tool call, so the record carries admission and the
#            mutex is taken only around record mutations. See Task 2.
TIERS = ("wrapper", "in-session")

_FIELDS = (
    "run_key", "host", "workstation_id", "wrapper_identity", "host_identity",
    "tier", "posture", "renewed_at", "lease_expires_at", "heartbeat_id",
)


class OwnerBusy(RuntimeError):
    """A live owner holds this run; the caller must not proceed."""


class OwnerAmbiguous(RuntimeError):
    """Ownership state cannot be interpreted safely. Always fail-closed on this."""


class OwnerOrphaned(RuntimeError):
    """The wrapper exited but its launched host is still live (design line 198)."""


class OwnerMissing(RuntimeError):
    """An operation that requires a recorded owner found none."""


class OwnerRecord(NamedTuple):
    run_key: str
    host: str
    workstation_id: str
    wrapper_identity: str
    host_identity: str | None
    tier: str
    posture: str
    renewed_at: str
    lease_expires_at: str
    heartbeat_id: str | None

    def validated(self) -> OwnerRecord:
        if self.tier not in TIERS:
            raise OwnerAmbiguous(f"ownership record tier {self.tier!r}; expected one of {TIERS}")
        if not self.wrapper_identity:
            raise OwnerAmbiguous(f"ownership record for {self.run_key!r} has no wrapper identity")
        return self

    def as_doc(self) -> dict:
        doc = {field: getattr(self, field) for field in _FIELDS}
        doc["schema_version"] = RECORD_SCHEMA_VERSION
        return doc


def record_path(state_root: str, run_key: str) -> str:
    """`owner.json`, beside `owner.lock` in the run directory."""
    return os.path.join(runstate.run_dir(state_root, run_key), "owner.json")


def read(state_root: str, run_key: str) -> OwnerRecord | None:
    """The recorded owner, or `None`. Raises `OwnerAmbiguous` on a record that does not describe
    this run — a hand-edited or partially restored file must not be believed."""
    doc = atomic.read_json(record_path(state_root, run_key))
    if doc is None:
        return None
    missing = [field for field in _FIELDS if field not in doc]
    if missing:
        raise OwnerAmbiguous(
            f"ownership record at {record_path(state_root, run_key)} is missing "
            f"{', '.join(missing)}; no write occurred. Inspect it, then clear it with: "
            f"conductor own prune --run {run_key}"
        )
    if doc["run_key"] != run_key:
        raise OwnerAmbiguous(
            f"ownership record at {record_path(state_root, run_key)} names run "
            f"{doc['run_key']!r}, not {run_key!r}; no write occurred. Inspect both with: "
            f"conductor run show --run {run_key}"
        )
    return OwnerRecord(**{field: doc[field] for field in _FIELDS}).validated()


def _write(state_root: str, run_key: str, record: OwnerRecord | None) -> None:
    """Replace or remove the record. EVERY caller holds `owner.lock` for this run — the lock file
    itself is never touched, so the atomic replace here cannot orphan anyone's lock."""
    path = record_path(state_root, run_key)
    if record is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    atomic.write_json_atomic(path, record.validated().as_doc())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_ownership_record.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Falsify**

**Falsifier:** in `_write`, replace the `atomic.write_json_atomic(path, ...)` line with a write to `runstate.owner_lock_path(state_root, run_key)` via `atomic.write_json_atomic`. Run `pytest tests/conductor/core/test_ownership_record.py::test_writing_the_record_leaves_the_lock_inode_untouched -q`.
Expected: FAIL — the inode changes. Revert.

Second falsifier, for the ambiguity guard: delete the `if doc["run_key"] != run_key:` block in `read`. `test_a_record_naming_another_run_is_refused_rather_than_returned` must fail. Revert.

- [ ] **Step 6: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_ownership_record.py tests/conductor/core/conftest.py && pyright conductor/core`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add conductor/core/ownership.py tests/conductor/core/conftest.py tests/conductor/core/test_ownership_record.py
git commit -m "conductor/core/ownership.py:1-125 — the ownership record and its placement

- owner.lock stays a zero-byte mutex; the fields live in a sibling owner.json, because an
  atomic replace of the lock file installs a new inode and silently orphans every holder
- read() refuses a record naming another run rather than returning it"
```

---

### Task 2: `acquire`, `renew`, `release` — two tiers, one record

**Files:**
- Modify: `conductor/core/ownership.py`
- Test: `tests/conductor/core/test_ownership_acquire.py`

**Interfaces:**
- Consumes: Task 1's record; `conductor.core.locks.hold`, `conductor.core.runstate.update`.
- Produces:
  - `DEFAULT_LEASE_SECONDS: int` (120), `MAX_RENEWAL_FRACTION: float` (0.25)
  - `acquire(state_root, run_key, *, host, workstation_id, wrapper_identity, tier="wrapper", posture="supervised", host_identity=None, heartbeat_id=None, lease_seconds=DEFAULT_LEASE_SECONDS, now=None) -> OwnerRecord`
  - `renew(state_root, run_key, *, wrapper_identity, lease_seconds=DEFAULT_LEASE_SECONDS, host_identity=None, now=None) -> OwnerRecord`
  - `release(state_root, run_key, *, wrapper_identity) -> None`
  - `lease_expired(record: OwnerRecord, *, now=None) -> bool`
  - `assert_renewal_interval(lease_seconds: int, renewal_seconds: int) -> None`

**Design note — the two tiers, and why exclusion is decided by the record.** Design line 190 has the wrapper *"acquire owner.lock exclusively and remain its sole holder through takeover and the launched host's lifetime."* That is exact and implementable for the wrapper tier: a shell script can hold a file descriptor for the whole fire.

It is **not** implementable for the in-session tier. `/conductor:autodev` fired inside a live Claude or Codex REPL is a sequence of tool calls; a `conductor own …` subprocess exits, and its `flock` dies with it. There is no process between the REPL and the work that could hold a descriptor.

So the mutex protects the *transition*, and the *record* carries admission:

- **`acquire`** takes `owner.lock`, reads the record, refuses if one is live, writes its own, releases. Milliseconds.
- **Admission** is then held by the record's identity + lease for the fire's duration.
- **A wrapper-tier caller may additionally hold `owner.lock` for its whole fire**, which is strictly stronger and remains design-conformant. It is not required for exclusion, because both tiers refuse on a live record.

This is why `wrapper_identity` matters more than the lock: it is checkable by anyone, at any later time, without having been present when the lock was taken. A crashed REPL is *provable* through `process_alive`, not merely *expired*. Design line 186's "expiry is necessary but never sufficient" is exactly this, and Task 4 implements the sufficiency half.

**Precedence rule to encode:** a lapsed lease whose `wrapper_identity` is still live is **not** takeover-eligible and **is** a skip. An automatic fire that meets it exits success (design/Plan 05: a skipped fire caused by a live lock is a success). Only an explicit `conductor own takeover` may proceed, and only through Task 5's proofs.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_ownership_acquire.py`:

```python
"""Acquisition, renewal, release — and the fact that exclusion is decided by the RECORD.

The wrapper tier can hold owner.lock for a whole fire; the in-session tier cannot, because a
`conductor own` subprocess exits and takes its flock with it. Both tiers therefore refuse on a
live record, which is what makes them exclude EACH OTHER rather than only themselves.
"""

from __future__ import annotations

import pytest

from conductor.core import locks, ownership, runstate

T0 = "2026-08-17T12:00:00+00:00"
T_LATER = "2026-08-17T12:05:00+00:00"


def _acq(state_root, key, *, tier="wrapper", identity="claude:100:1", now=T0, **kw):
    return ownership.acquire(
        state_root, key, host="claude", workstation_id="w0",
        wrapper_identity=identity, tier=tier, now=now, **kw,
    )


def test_acquire_writes_a_record_and_mirrors_it_into_run_json(state_root, run_key_value):
    record = _acq(state_root, run_key_value)
    assert ownership.read(state_root, run_key_value) == record
    run = runstate.load(state_root, run_key_value)
    assert run["lease"]["owner"] == "claude:100:1"
    assert run["lease"]["expires_at"] == record.lease_expires_at
    assert run["heartbeat"]["process_identity"] == "claude:100:1"


def test_a_second_acquire_against_a_live_record_is_refused_naming_the_holder(state_root, run_key_value):
    _acq(state_root, run_key_value, identity="claude:100:1")
    with pytest.raises(ownership.OwnerBusy) as excinfo:
        _acq(state_root, run_key_value, identity="claude:200:2")
    assert "claude:100:1" in str(excinfo.value)
    assert run_key_value in str(excinfo.value)


def test_the_two_tiers_exclude_each_other(state_root, run_key_value):
    """The gap this plan closes: today the in-session tier takes nothing at all."""
    _acq(state_root, run_key_value, tier="in-session", identity="claude:100:1")
    with pytest.raises(ownership.OwnerBusy):
        _acq(state_root, run_key_value, tier="wrapper", identity="claude:200:2")


def test_reacquiring_with_the_same_identity_is_idempotent_not_busy(state_root, run_key_value):
    """A retried fire, or a driver restarted inside its own lease, must not lock itself out."""
    first = _acq(state_root, run_key_value, identity="claude:100:1")
    second = _acq(state_root, run_key_value, identity="claude:100:1", now=T_LATER)
    assert second.wrapper_identity == first.wrapper_identity
    assert second.renewed_at == T_LATER


def test_an_expired_lease_does_not_by_itself_admit_a_new_owner(state_root, run_key_value):
    """Design line 186: expiry is necessary but NEVER sufficient. Task 4 supplies sufficiency;
    `acquire` alone must not grant on staleness."""
    _acq(state_root, run_key_value, identity="claude:100:1", lease_seconds=1)
    with pytest.raises(ownership.OwnerBusy) as excinfo:
        _acq(state_root, run_key_value, identity="claude:200:2", now=T_LATER)
    assert "conductor own takeover" in str(excinfo.value)


def test_renew_extends_the_lease_and_refuses_a_different_identity(state_root, run_key_value):
    original = _acq(state_root, run_key_value, identity="claude:100:1")
    renewed = ownership.renew(state_root, run_key_value, wrapper_identity="claude:100:1", now=T_LATER)
    assert renewed.lease_expires_at > original.lease_expires_at
    assert renewed.renewed_at == T_LATER
    with pytest.raises(ownership.OwnerBusy):
        ownership.renew(state_root, run_key_value, wrapper_identity="claude:200:2", now=T_LATER)


def test_renew_without_a_record_is_missing_not_a_silent_reacquire(state_root, run_key_value):
    with pytest.raises(ownership.OwnerMissing):
        ownership.renew(state_root, run_key_value, wrapper_identity="claude:100:1")


def test_release_clears_only_your_own_record(state_root, run_key_value):
    _acq(state_root, run_key_value, identity="claude:100:1")
    with pytest.raises(ownership.OwnerBusy):
        ownership.release(state_root, run_key_value, wrapper_identity="claude:200:2")
    assert ownership.read(state_root, run_key_value) is not None
    ownership.release(state_root, run_key_value, wrapper_identity="claude:100:1")
    assert ownership.read(state_root, run_key_value) is None
    assert runstate.load(state_root, run_key_value)["lease"]["owner"] is None


def test_release_of_an_absent_record_is_a_no_op(state_root, run_key_value):
    ownership.release(state_root, run_key_value, wrapper_identity="claude:100:1")


def test_acquire_takes_owner_then_state_and_never_project(state_root, run_key_value):
    """Observe the locks ACQUIRE itself takes. A test that only exercised `locks.hold` directly
    would pass with `acquire` deleted — the failure mode Plan 01's residuals record four times."""
    seen = []
    original = locks.hold

    def spy(path, **kw):
        seen.append(kw["kind"])
        return original(path, **kw)

    import conductor.core.ownership as module
    module.locks.hold = spy
    try:
        _acq(state_root, run_key_value)
    finally:
        module.locks.hold = original
    assert "project" not in seen
    assert seen.index("owner") < seen.index("state")


def test_acquire_cannot_be_called_while_holding_project_lock_out_of_order(state_root, run_key_value):
    """The order is migration -> project -> owner -> state, so holding project.lock ACROSS an
    acquire is legal. The illegal direction is the one a fire could reach by shelling out."""
    with locks.hold(f"{state_root}/project.lock", kind="project"):
        _acq(state_root, run_key_value)          # legal: project is ranked above owner
    with locks.hold(runstate.owner_lock_path(state_root, run_key_value), kind="owner", run_key=run_key_value):
        with pytest.raises(locks.LockOrderError):
            with locks.hold(f"{state_root}/project.lock", kind="project"):
                pass


def test_a_renewal_interval_over_a_quarter_of_the_lease_is_refused():
    ownership.assert_renewal_interval(120, 30)
    ownership.assert_renewal_interval(600, 60)
    with pytest.raises(ValueError) as excinfo:
        ownership.assert_renewal_interval(120, 31)
    assert "one quarter" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_ownership_acquire.py -q`
Expected: FAIL — `AttributeError: module 'conductor.core.ownership' has no attribute 'acquire'`

- [ ] **Step 3: Write the implementation**

Append to `conductor/core/ownership.py`:

```python
DEFAULT_LEASE_SECONDS = 120
MAX_RENEWAL_FRACTION = 0.25


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plus(stamp: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def assert_renewal_interval(lease_seconds: int, renewal_seconds: int) -> None:
    """Design line 186: a repository may lengthen the lease but may not renew less often than
    once per quarter-lease. A renewal interval that approaches the lease means a single slow
    fire looks dead to everyone else."""
    if renewal_seconds > lease_seconds * MAX_RENEWAL_FRACTION:
        raise ValueError(
            f"renewal interval {renewal_seconds}s exceeds one quarter of the {lease_seconds}s "
            f"lease (max {lease_seconds * MAX_RENEWAL_FRACTION:.0f}s); no write occurred"
        )


def lease_expired(record: OwnerRecord, *, now: str | None = None) -> bool:
    """Whether the recorded lease has lapsed. NECESSARY BUT NEVER SUFFICIENT for takeover —
    `prove_exited` (Task 4) supplies the other half."""
    return (now or _now()) >= record.lease_expires_at


def _mirror(state_root: str, run_key: str, record: OwnerRecord | None, *, now: str) -> None:
    """Refresh `run.json`'s diagnostic mirror. Design line 184: these fields are a mirror
    refreshed on renewal, never a second ownership authority."""

    def mutate(doc: dict) -> dict:
        doc["lease"] = {
            "owner": record.wrapper_identity if record else None,
            "expires_at": record.lease_expires_at if record else None,
            "renewed_at": record.renewed_at if record else None,
        }
        doc["heartbeat"] = {
            "schedule_id": record.heartbeat_id if record else None,
            "process_identity": record.wrapper_identity if record else None,
        }
        doc["worker_host"] = record.host if record else doc.get("worker_host")
        doc["updated_at"] = now
        return doc

    runstate.update(state_root, run_key, mutate)


def acquire(
    state_root: str,
    run_key: str,
    *,
    host: str,
    workstation_id: str,
    wrapper_identity: str,
    tier: str = "wrapper",
    posture: str = "supervised",
    host_identity: str | None = None,
    heartbeat_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
) -> OwnerRecord:
    """Become this run's owner, or refuse.

    Refusal on ANY recorded owner other than yourself is deliberate. A lapsed lease is not
    grounds to take the run — a lapsed lease over a live process is a slow fire, and stealing it
    produces two workers on one run branch, which is the failure this whole plan exists to
    prevent. `conductor own takeover` is the supervised path and it proves exit first."""
    stamp = now or _now()
    with locks.hold(runstate.owner_lock_path(state_root, run_key), kind="owner", run_key=run_key):
        existing = read(state_root, run_key)
        if existing is not None and existing.wrapper_identity != wrapper_identity:
            raise OwnerBusy(
                f"run {run_key!r} is owned by {existing.wrapper_identity} on host "
                f"{existing.host} (tier {existing.tier}, lease until "
                f"{existing.lease_expires_at}); no write occurred. Inspect it with: "
                f"conductor own status --run {run_key} — or, if that process is gone: "
                f"conductor own takeover --run {run_key} --host {host}"
            )
        record = OwnerRecord(
            run_key=run_key,
            host=host,
            workstation_id=workstation_id,
            wrapper_identity=wrapper_identity,
            host_identity=host_identity if host_identity is not None
            else (existing.host_identity if existing else None),
            tier=tier,
            posture=posture,
            renewed_at=stamp,
            lease_expires_at=_plus(stamp, lease_seconds),
            heartbeat_id=heartbeat_id if heartbeat_id is not None
            else (existing.heartbeat_id if existing else None),
        ).validated()
        _write(state_root, run_key, record)
        _mirror(state_root, run_key, record, now=stamp)
        return record


def renew(
    state_root: str,
    run_key: str,
    *,
    wrapper_identity: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    host_identity: str | None = None,
    now: str | None = None,
) -> OwnerRecord:
    """Extend your own lease. Never creates a record: a renewal that silently re-acquires would
    let a worker whose ownership was legitimately taken away quietly take it back."""
    stamp = now or _now()
    with locks.hold(runstate.owner_lock_path(state_root, run_key), kind="owner", run_key=run_key):
        existing = read(state_root, run_key)
        if existing is None:
            raise OwnerMissing(
                f"run {run_key!r} has no ownership record to renew; no write occurred. "
                f"Acquire one with: conductor own acquire --run {run_key}"
            )
        if existing.wrapper_identity != wrapper_identity:
            raise OwnerBusy(
                f"run {run_key!r} is owned by {existing.wrapper_identity}, not {wrapper_identity}; "
                f"no write occurred. Inspect it with: conductor own status --run {run_key}"
            )
        record = existing._replace(
            renewed_at=stamp,
            lease_expires_at=_plus(stamp, lease_seconds),
            host_identity=host_identity if host_identity is not None else existing.host_identity,
        )
        _write(state_root, run_key, record)
        _mirror(state_root, run_key, record, now=stamp)
        return record


def release(state_root: str, run_key: str, *, wrapper_identity: str) -> None:
    """Give up your own ownership. Releasing an absent record is a no-op so an exit trap is safe
    to run twice; releasing SOMEONE ELSE'S is refused, because that is a takeover in disguise."""
    with locks.hold(runstate.owner_lock_path(state_root, run_key), kind="owner", run_key=run_key):
        existing = read(state_root, run_key)
        if existing is None:
            return
        if existing.wrapper_identity != wrapper_identity:
            raise OwnerBusy(
                f"run {run_key!r} is owned by {existing.wrapper_identity}, not {wrapper_identity}; "
                f"no write occurred. Clearing another owner is: "
                f"conductor own prune --run {run_key}"
            )
        _write(state_root, run_key, None)
        _mirror(state_root, run_key, None, now=_now())
```

Add `from conductor.core import atomic, locks, runstate` at the top (extending Task 1's import).

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_ownership_acquire.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Falsify**

**Falsifier:** in `acquire`, change the refusal condition to also grant when the lease has lapsed:

```python
if existing is not None and existing.wrapper_identity != wrapper_identity and not lease_expired(existing, now=stamp):
```

Run `pytest tests/conductor/core/test_ownership_acquire.py::test_an_expired_lease_does_not_by_itself_admit_a_new_owner -q`.
Expected: FAIL. Revert. This falsifier is the single most important one in the plan — it is the exact shortcut that reintroduces two workers on one run branch.

Second falsifier: delete the `_mirror(...)` call in `release`. `test_release_clears_only_your_own_record` must fail on the `run.json` assertion. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_ownership_acquire.py && pyright conductor/core
git add conductor/core/ownership.py tests/conductor/core/test_ownership_acquire.py
git commit -m "conductor/core/ownership.py:126-300 — acquire/renew/release over the record

- both execution tiers refuse on a live record, so the in-session tier and the OS-cron driver
  exclude each other instead of only themselves
- an expired lease alone never admits a new owner; run.json's lease block stays a mirror"
```

---

### Task 3: Carrying the lock order across a subprocess boundary

**Files:**
- Modify: `conductor/core/locks.py`
- Test: `tests/conductor/core/test_lock_propagation.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `HELD_ENV: str` (`"CONDUCTOR_HELD_LOCKS"`), `child_env(base: dict[str, str] | None = None) -> dict[str, str]`. `LOCK_ORDER` is unchanged.

**Why this task exists.** `locks._held` is a `contextvars.ContextVar` — per process. Plan 01 never needed more, because every Plan 01 operation began and ended inside one process. Plan 02 creates the first hold that spans a process boundary: a wrapper-tier driver holds `owner.lock` in the shell and then invokes `conductor` subprocesses inside that hold. In the child, `_held` is empty, so a child that reaches for `project.lock` — which `conductor run new` does, and which `registry.commit` does on every path — passes `_check_order` cleanly and inverts the global order against `rebind`, which holds `project.lock` and then wants every `owner.lock`. That is a genuine two-process deadlock the invariant was written to prevent and currently cannot see.

**This does not change the lock order.** It makes the existing order observable where it was previously invisible. Plan 01's residual note that re-entrancy is keyed by the resolved lock **file** is what makes the encoding safe: the environment carries resolved absolute paths, so a child under a different state root is not falsely refused.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_lock_propagation.py`:

```python
"""The lock order is enforced by a per-process contextvar. Plan 02 is the first thing to hold a
lock ACROSS a process boundary, so the order has to travel with the child or it stops being an
invariant exactly where the deadlock lives."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conductor.core import locks

_CHILD = """
import json, os, sys
from conductor.core import locks
try:
    with locks.hold(sys.argv[1], kind=sys.argv[2]):
        print(json.dumps({"ok": True}))
except locks.LockOrderError as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
"""


def _child(env, path, kind, repo_root):
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, path, kind],
        capture_output=True, text=True, check=True,
        env={**env, "PYTHONPATH": repo_root},
    )
    return json.loads(out.stdout)


def test_child_env_encodes_the_resolved_lock_files_currently_held(tmp_path):
    assert locks.child_env({})[locks.HELD_ENV] == ""
    with locks.hold(str(tmp_path / "owner.lock"), kind="owner", run_key="alpha-1111"):
        encoded = locks.child_env({})[locks.HELD_ENV]
    assert "owner" in encoded
    assert str((tmp_path / "owner.lock").resolve()) in encoded


def test_a_child_taking_a_higher_lock_under_an_inherited_hold_is_refused(tmp_path, repo_root):
    with locks.hold(str(tmp_path / "owner.lock"), kind="owner", run_key="alpha-1111"):
        env = locks.child_env(dict(os.environ))
    result = _child(env, str(tmp_path / "project.lock"), "project", repo_root)
    assert result["ok"] is False
    assert "project" in result["error"] and "owner" in result["error"]


def test_a_child_taking_a_lower_lock_under_an_inherited_hold_is_allowed(tmp_path, repo_root):
    with locks.hold(str(tmp_path / "owner.lock"), kind="owner", run_key="alpha-1111"):
        env = locks.child_env(dict(os.environ))
    assert _child(env, str(tmp_path / "state.lock"), "state", repo_root)["ok"] is True


def test_a_child_with_no_inherited_hold_is_unconstrained(tmp_path, repo_root):
    env = {k: v for k, v in os.environ.items() if k != locks.HELD_ENV}
    assert _child(env, str(tmp_path / "project.lock"), "project", repo_root)["ok"] is True


def test_a_malformed_inherited_value_is_refused_rather_than_ignored(tmp_path):
    """Silently ignoring it would turn a corrupted environment into an unenforced invariant."""
    os.environ[locks.HELD_ENV] = "not-a-lock-spec"
    try:
        with pytest.raises(locks.LockOrderError):
            with locks.hold(str(tmp_path / "state.lock"), kind="state"):
                pass
    finally:
        del os.environ[locks.HELD_ENV]


@pytest.fixture
def repo_root():
    import conductor

    return os.path.dirname(os.path.dirname(os.path.abspath(conductor.__file__)))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_lock_propagation.py -q`
Expected: FAIL — `AttributeError: module 'conductor.core.locks' has no attribute 'HELD_ENV'`

- [ ] **Step 3: Write the implementation**

In `conductor/core/locks.py`, add below `LOCK_ORDER`:

```python
# The order invariant is enforced by a per-process contextvar, which is invisible to a child
# process. Plan 02 is the first caller to hold a lock across a subprocess boundary (a wrapper-tier
# driver holding owner.lock while invoking `conductor` verbs), and a child that reaches for
# project.lock inside that hold inverts the global order against `rebind` with nothing to detect
# it. The held set therefore travels in the environment as resolved absolute paths — resolved,
# because Plan 01 established that re-entrancy is keyed by the lock FILE, so two `project.lock`
# files under different state roots must not be conflated.
HELD_ENV = "CONDUCTOR_HELD_LOCKS"
```

Add the encoder and decoder, and make `_check_order` consult both:

```python
def _encode(entries: tuple[tuple[str, int, str | None, str], ...]) -> str:
    return ";".join(f"{kind}|{run or ''}|{path}" for kind, _rank, run, path in entries)


def _decode(value: str) -> tuple[tuple[str, int, str | None, str], ...]:
    decoded: list[tuple[str, int, str | None, str]] = []
    for chunk in filter(None, value.split(";")):
        parts = chunk.split("|", 2)
        if len(parts) != 3 or parts[0] not in LOCK_ORDER or not os.path.isabs(parts[2]):
            raise LockOrderError(
                f"{HELD_ENV} is malformed at {chunk!r}; refusing to take any lock rather than "
                "proceed with an unenforced lock order. Unset it if no parent holds a lock."
            )
        decoded.append((parts[0], LOCK_ORDER.index(parts[0]), parts[1] or None, parts[2]))
    return tuple(decoded)


def inherited() -> tuple[tuple[str, int, str | None, str], ...]:
    """Locks held by an ancestor process, decoded from the environment."""
    return _decode(os.environ.get(HELD_ENV, ""))


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """`base` plus this process's held locks, for passing to a subprocess."""
    env = dict(base if base is not None else os.environ)
    env[HELD_ENV] = _encode(inherited() + _held.get())
    return env
```

Then in `_check_order`, replace `for held_kind, held_rank, held_run, held_path in _held.get():` with:

```python
    for held_kind, held_rank, held_run, held_path in inherited() + _held.get():
```

(The existing `_held` tuple already carries the resolved path as its fourth element — `locks.py:48` takes `path` as a parameter for exactly this reason.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_lock_propagation.py tests/conductor/core/test_locks.py -q`
Expected: PASS, and Plan 01's existing `test_locks.py` still green.

- [ ] **Step 5: Falsify**

**Falsifier:** revert `_check_order` to `for ... in _held.get():`. `test_a_child_taking_a_higher_lock_under_an_inherited_hold_is_refused` must fail. Revert.

Second falsifier: in `_decode`, replace the `raise LockOrderError(...)` with `continue`. `test_a_malformed_inherited_value_is_refused_rather_than_ignored` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/locks.py tests/conductor/core/test_lock_propagation.py && pyright conductor/core && pytest tests/conductor/core -q
git add conductor/core/locks.py tests/conductor/core/test_lock_propagation.py
git commit -m "conductor/core/locks.py:24-40,48-75 — carry the held-lock set into child processes

- CONDUCTOR_HELD_LOCKS encodes resolved lock files so a subprocess cannot invert the order
- a malformed value refuses every acquisition rather than silently disabling the invariant"
```

---

### Task 4: `prove_exited` — the orphan distinction

> **Blocked until Plan 04 merges.** `conductor/hosts/` does not exist on `main`. Everything from here on imports it.

**Files:**
- Modify: `conductor/core/ownership.py`
- Test: `tests/conductor/core/test_ownership_liveness.py`

**Interfaces:**
- Consumes: `conductor.hosts.base.load(host_id) -> HostAdapter`, and from that adapter `process_alive(identity: str) -> bool`. **Do not mint an identity string here.** `wrapper_identity` and `host_identity` are always `"<host>:<pid>:<start-ticks>"` produced by Plan 04's `process_identity`.
- Produces:
  - `class Liveness(NamedTuple)` — `wrapper_alive: bool`, `host_alive: bool`, `expired: bool`
  - `inspect(record: OwnerRecord, *, load=None, now=None) -> Liveness`
  - `prove_exited(record: OwnerRecord, *, load=None, now=None) -> bool`

**The four states, and why only one of them is takeover-eligible.** Design line 198: *"A dead wrapper with a live launched host is an orphaned state, not a takeover-eligible stale owner."*

| wrapper | launched host | verdict |
| --- | --- | --- |
| alive | any | **live owner.** Not takeover-eligible at any lease age. An automatic fire skips and exits success |
| dead | alive | **orphaned.** `OwnerOrphaned`, with the exact `conductor own prune --run <key> --terminate-orphan <identity>` command. Never taken over |
| dead | dead | exited. Takeover-eligible **and only then if the lease has also lapsed** |
| unknown either side | — | **ambiguous.** `OwnerAmbiguous`. Fail closed |

"Unknown" is a real state, not a theoretical one: `process_alive` raises when handed an identity minted by the other host, and a record whose `host` names an adapter that cannot be loaded — a Codex record on a machine with no Codex — cannot answer the question at all. Returning `False` there would be a fail-open: the caller would conclude the worker had exited and take over a live run.

**`prove_exited` returns a bool but is not a predicate you may ignore.** It returns `True` only for `(dead, dead)` *and* an expired lease. Every other state raises. That asymmetry is deliberate — a caller that writes `if prove_exited(rec):` and falls through on `False` cannot accidentally treat an orphan as a stale owner, because it never sees `False` for one.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_ownership_liveness.py`:

```python
"""Liveness interpretation (design line 198). Expiry is necessary but never sufficient, and a
dead wrapper with a live launched host is an ORPHAN — a state that blocks takeover rather than
authorising it. Every ambiguity fails closed."""

from __future__ import annotations

import pytest

from conductor.core import ownership

T0 = "2026-08-17T12:00:00+00:00"
T_LATE = "2026-08-17T13:00:00+00:00"


def _rec(**kw):
    base = dict(
        run_key="alpha-11111111", host="claude", workstation_id="w0",
        wrapper_identity="claude:100:1", host_identity="claude:101:2",
        tier="wrapper", posture="supervised", renewed_at=T0,
        lease_expires_at="2026-08-17T12:02:00+00:00", heartbeat_id=None,
    )
    base.update(kw)
    return ownership.OwnerRecord(**base)


def test_a_live_wrapper_is_never_takeover_eligible_however_stale_the_lease(probes):
    probes["claude"]._alive = {"claude:100:1"}
    with pytest.raises(ownership.OwnerBusy) as excinfo:
        ownership.prove_exited(_rec(), load=probes["load"], now=T_LATE)
    assert "claude:100:1" in str(excinfo.value)


def test_a_dead_wrapper_with_a_live_launched_host_is_orphaned_not_stale(probes):
    probes["claude"]._alive = {"claude:101:2"}
    with pytest.raises(ownership.OwnerOrphaned) as excinfo:
        ownership.prove_exited(_rec(), load=probes["load"], now=T_LATE)
    message = str(excinfo.value)
    assert "claude:101:2" in message
    assert "--terminate-orphan claude:101:2" in message


def test_both_dead_and_the_lease_lapsed_proves_exit(probes):
    probes["claude"]._alive = set()
    assert ownership.prove_exited(_rec(), load=probes["load"], now=T_LATE) is True


def test_both_dead_but_the_lease_still_current_does_not_prove_exit(probes):
    """A record written milliseconds ago by a process that has not appeared in the table yet."""
    probes["claude"]._alive = set()
    assert ownership.prove_exited(_rec(), load=probes["load"], now=T0) is False


def test_a_record_with_no_launched_host_is_judged_on_the_wrapper_alone(probes):
    probes["claude"]._alive = set()
    assert ownership.prove_exited(_rec(host_identity=None), load=probes["load"], now=T_LATE) is True


def test_an_unloadable_host_adapter_is_ambiguous_not_dead(probes):
    def load(host_id):
        raise LookupError(f"no adapter for {host_id!r}")

    with pytest.raises(ownership.OwnerAmbiguous) as excinfo:
        ownership.prove_exited(_rec(host="codex"), load=load, now=T_LATE)
    assert "codex" in str(excinfo.value)


def test_an_identity_the_adapter_refuses_is_ambiguous_not_dead(probes):
    """`process_alive` raises on a foreign identity. Reading that as `False` would take over a
    live run — the exact fail-open this guard exists to prevent."""
    with pytest.raises(ownership.OwnerAmbiguous):
        ownership.prove_exited(
            _rec(host="codex", wrapper_identity="claude:100:1"),
            load=probes["load"], now=T_LATE,
        )


def test_inspect_reports_all_three_axes_without_raising(probes):
    probes["claude"]._alive = {"claude:101:2"}
    state = ownership.inspect(_rec(), load=probes["load"], now=T_LATE)
    assert state == ownership.Liveness(wrapper_alive=False, host_alive=True, expired=True)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_ownership_liveness.py -q`
Expected: FAIL — `AttributeError: module 'conductor.core.ownership' has no attribute 'prove_exited'`

- [ ] **Step 3: Write the implementation**

Append to `conductor/core/ownership.py`:

```python
class Liveness(NamedTuple):
    wrapper_alive: bool
    host_alive: bool
    expired: bool


def _adapter(record: OwnerRecord, load):
    if load is None:
        from conductor.hosts import base  # imported lazily: Plan 04 lands after this module

        load = base.load
    try:
        return load(record.host)
    except Exception as exc:
        raise OwnerAmbiguous(
            f"run {record.run_key!r} records an owner on host {record.host!r}, and that host's "
            f"adapter cannot be loaded here ({exc}); no write occurred. Liveness is unknowable "
            f"from this machine, so ownership is left alone. Inspect it with: "
            f"conductor own status --run {record.run_key}"
        ) from exc


def inspect(record: OwnerRecord, *, load=None, now: str | None = None) -> Liveness:
    """The three axes takeover and prune decide on. Raises only on genuine ambiguity."""
    adapter = _adapter(record, load)
    try:
        wrapper_alive = adapter.process_alive(record.wrapper_identity)
        host_alive = (
            adapter.process_alive(record.host_identity)
            if record.host_identity is not None
            else False
        )
    except Exception as exc:
        raise OwnerAmbiguous(
            f"run {record.run_key!r}: the {record.host} adapter refused an identity from its own "
            f"ownership record ({exc}); no write occurred. A refused identity is UNKNOWN, never "
            f"dead — reading it as dead would take over a live run. Inspect it with: "
            f"conductor own status --run {record.run_key}"
        ) from exc
    return Liveness(wrapper_alive, host_alive, lease_expired(record, now=now))


def prove_exited(record: OwnerRecord, *, load=None, now: str | None = None) -> bool:
    """Whether both the wrapper and its launched host have provably exited AND the lease lapsed.

    Returns `False` only for the one benign case — everything is dead but the lease is still
    current, i.e. a record written moments ago by a process not yet visible. Every DANGEROUS state
    raises instead of returning `False`, so a caller that falls through on a falsy result can
    never treat an orphan as a stale owner."""
    state = inspect(record, load=load, now=now)
    if state.wrapper_alive:
        raise OwnerBusy(
            f"run {record.run_key!r} has a LIVE owner: wrapper {record.wrapper_identity} on host "
            f"{record.host}; no write occurred. Expiry alone never authorises takeover. Wait for "
            f"it to exit, or stop it, then retry: conductor own status --run {record.run_key}"
        )
    if state.host_alive:
        raise OwnerOrphaned(
            f"run {record.run_key!r} is ORPHANED: wrapper {record.wrapper_identity} exited but "
            f"its launched host {record.host_identity} is still running; no write occurred. "
            f"A non-destructive prune refuses this. Terminate the identity-matched orphan with: "
            f"conductor own prune --run {record.run_key} "
            f"--terminate-orphan {record.host_identity}"
        )
    return state.expired
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_ownership_liveness.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Falsify**

**Falsifier:** in `inspect`, change the `except Exception` that wraps the two `process_alive` calls to `return Liveness(False, False, lease_expired(record, now=now))`. `test_an_identity_the_adapter_refuses_is_ambiguous_not_dead` must fail. Revert.

Second falsifier: delete the `if state.host_alive:` block. `test_a_dead_wrapper_with_a_live_launched_host_is_orphaned_not_stale` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_ownership_liveness.py && pyright conductor/core
git add conductor/core/ownership.py tests/conductor/core/test_ownership_liveness.py
git commit -m "conductor/core/ownership.py:301-380 — liveness interpretation over Plan 04's process_alive

- a dead wrapper with a live launched host raises OwnerOrphaned; only (dead, dead, expired) proves exit
- an unloadable adapter or a refused identity is ambiguous, never dead"
```

---

### Task 5: `takeover` — the seven-step compare-and-swap

**Files:**
- Modify: `conductor/core/ownership.py`
- Test: `tests/conductor/core/test_takeover.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4; `conductor.core.registry.load`, `conductor.core.runstate.commit`.
- Produces:
  - `takeover(state_root, run_key, *, new_host, workstation_id, wrapper_identity, launch, posture="supervised", reconcile=None, load=None, lease_seconds=DEFAULT_LEASE_SECONDS, now=None) -> dict` — returns the updated run document.
  - `launch: Callable[[OwnerRecord], str | None]` — the caller's launcher. Returns the launched host's identity, or `None` for a record-only takeover.

**Mapping to design lines 188–196.** Each numbered step there becomes a named block:

1. `owner.lock` is acquired first and held for the whole operation — there is no release-and-relaunch window.
2. `state.lock` next (rank 3, after rank 2 — the order is unchanged), then the caller's `reconcile` hook runs. **Plan 02 does not implement reconciliation**; Git, GitHub, worktree, and test reconciliation are Plans 05 and 06. The hook is called and its failure aborts, so the ordering is established and testable now.
3. `prove_exited` under both locks. Any doubt aborts before mutation.
4. `worker_host` and `reviewer_host` are updated **together**, and reviews on unmerged phases whose reviewer host equals the new worker host are invalidated.
5. `runstate.commit(..., expect_revision=...)` — the compare-and-swap, then `state.lock` releases.
6. Heartbeat entry replacement is **Plan 05's**; Task 5 records the intent by clearing `heartbeat.schedule_id` and naming Plan 05 in the docstring. Do not install a schedule here.
7. `launch` runs while `owner.lock` is still held, and its identity is recorded. On launch failure the failure is recorded under `state.lock`, the prior non-running record is restored, and only then is `owner.lock` released.

**Cross-machine refusal (design line 200).** `project.json`'s `workstation_id` is compared against the caller's. A mismatch refuses. This reads registry data, not the status mirror — `registry.py:14-17` forbids hanging a decision off the mirror, and `workstation_id` is not part of it.

**The `phase_reviews` contract Task 5 depends on, and hands forward to Plan 07.** Plan 01 creates `phase_reviews: []` with no element shape. Takeover needs two fields. The minimal contract, which **Plan 07 must honour**: each entry is a mapping with at least `phase_id: str`, `reviewer_host: str`, and `merged: bool`. **An entry missing either of the latter two is invalidated** — fail-closed, because an entry Conductor cannot classify is not evidence that a review is valid.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_takeover.py`:

```python
"""Takeover: one compare-and-swap, with proofs before any mutation (design lines 188-196).

There is no owner.lock release window anywhere in this operation. If the new host fails to
launch, ownership goes back to the prior NON-RUNNING record — not to nobody, and not to a
record that claims a process which does not exist.
"""

from __future__ import annotations

import pytest

from conductor.core import locks, ownership, registry, runstate

T0 = "2026-08-17T12:00:00+00:00"
T_LATE = "2026-08-17T13:00:00+00:00"


def _own(state_root, key, *, identity="claude:100:1", host="claude", posture="supervised", ws=None):
    return ownership.acquire(
        state_root, key, host=host,
        workstation_id=ws or registry.load(state_root)["workstation_id"],
        wrapper_identity=identity, host_identity=f"{host}:101:2",
        posture=posture, now=T0, lease_seconds=1,
    )


def _takeover(state_root, key, probes, **kw):
    kw.setdefault("new_host", "codex")
    kw.setdefault("workstation_id", registry.load(state_root)["workstation_id"])
    kw.setdefault("wrapper_identity", "codex:900:9")
    kw.setdefault("launch", lambda record: "codex:901:10")
    return ownership.takeover(state_root, key, load=probes["load"], now=T_LATE, **kw)


def test_takeover_swaps_the_owner_and_inverts_worker_and_reviewer_together(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    run = _takeover(state_root, run_key_value, probes)
    assert run["worker_host"] == "codex"
    assert run["reviewer_host"] == "claude"
    record = ownership.read(state_root, run_key_value)
    assert record.host == "codex"
    assert record.wrapper_identity == "codex:900:9"
    assert record.host_identity == "codex:901:10"


def test_takeover_invalidates_an_unmerged_review_authored_by_the_new_worker_host(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    runstate.update(state_root, run_key_value, lambda doc: doc.update(phase_reviews=[
        {"phase_id": "1", "reviewer_host": "codex", "merged": False},
        {"phase_id": "2", "reviewer_host": "codex", "merged": True},
        {"phase_id": "3", "reviewer_host": "claude", "merged": False},
    ]) or doc)
    reviews = _takeover(state_root, run_key_value, probes)["phase_reviews"]
    by_phase = {r["phase_id"]: r for r in reviews}
    assert by_phase["1"].get("invalidated_by") == "takeover"      # unmerged, new worker authored
    assert "invalidated_by" not in by_phase["2"]                  # merged: retained (design line 193)
    assert "invalidated_by" not in by_phase["3"]                  # opposite host: still valid


def test_a_review_entry_missing_its_fields_is_invalidated_not_trusted(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    runstate.update(state_root, run_key_value, lambda doc: doc.update(
        phase_reviews=[{"phase_id": "1"}]) or doc)
    reviews = _takeover(state_root, run_key_value, probes)["phase_reviews"]
    assert reviews[0]["invalidated_by"] == "takeover"


def test_a_live_owner_blocks_takeover_before_any_mutation(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = {"claude:100:1"}
    before = runstate.load(state_root, run_key_value)
    with pytest.raises(ownership.OwnerBusy):
        _takeover(state_root, run_key_value, probes)
    assert runstate.load(state_root, run_key_value) == before
    assert ownership.read(state_root, run_key_value).host == "claude"


def test_an_orphan_blocks_takeover_and_names_the_prune_command(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = {"claude:101:2"}
    with pytest.raises(ownership.OwnerOrphaned) as excinfo:
        _takeover(state_root, run_key_value, probes)
    assert "--terminate-orphan claude:101:2" in str(excinfo.value)


def test_a_different_workstation_refuses_automatic_takeover(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    with pytest.raises(ownership.OwnerAmbiguous) as excinfo:
        _takeover(state_root, run_key_value, probes, workstation_id="some-other-workstation")
    assert "conductor project rebind" in str(excinfo.value)


def test_a_failed_reconcile_aborts_before_the_owner_record_changes(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()

    def boom(_run):
        raise RuntimeError("git reconcile failed")

    with pytest.raises(RuntimeError):
        _takeover(state_root, run_key_value, probes, reconcile=boom)
    assert ownership.read(state_root, run_key_value).host == "claude"


def test_a_failed_launch_restores_the_prior_non_running_record(state_root, run_key_value, probes):
    """Design line 196. Ownership must not be left claiming a process that does not exist, and
    must not be left absent either — an unowned run with a swapped worker_host is worse."""
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()

    def fails(_record):
        raise OSError("spawn failed")

    with pytest.raises(OSError):
        _takeover(state_root, run_key_value, probes, launch=fails)
    record = ownership.read(state_root, run_key_value)
    assert record is not None
    assert record.host == "claude"
    assert record.host_identity is None       # not running
    assert runstate.load(state_root, run_key_value)["worker_host"] == "claude"


def test_takeover_holds_owner_then_state_and_never_reaches_for_project(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    seen = []
    original = locks.hold

    def spy(path, **kw):
        seen.append(kw["kind"])
        return original(path, **kw)

    import conductor.core.ownership as module
    module.locks.hold = spy
    try:
        _takeover(state_root, run_key_value, probes)
    finally:
        module.locks.hold = original
    assert "project" not in seen
    assert seen.index("owner") < seen.index("state")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_takeover.py -q`
Expected: FAIL — `AttributeError: module 'conductor.core.ownership' has no attribute 'takeover'`

- [ ] **Step 3: Write the implementation**

Append to `conductor/core/ownership.py`:

```python
def _invalidate_reviews(doc: dict, new_worker_host: str, *, now: str) -> dict:
    """Design line 193: a posted review on an UNMERGED phase whose reviewer host equals the new
    worker host is invalidated — that phase now needs a fresh opposite-host review. Merged phases
    retain their records and any stored debt-reviewer requirement unchanged.

    An entry missing `reviewer_host` or `merged` is invalidated rather than kept. Plan 07 owns the
    verdict schema; until it lands, an entry this code cannot classify is not evidence of a valid
    review, and treating it as one would let a takeover silently inherit its own review."""
    reviews = []
    for entry in doc.get("phase_reviews", []):
        classifiable = isinstance(entry, dict) and "reviewer_host" in entry and "merged" in entry
        authored_by_new_worker = classifiable and entry["reviewer_host"] == new_worker_host
        unmerged = classifiable and not entry["merged"]
        if not classifiable or (authored_by_new_worker and unmerged):
            entry = {**entry, "invalidated_by": "takeover", "invalidated_at": now}
        reviews.append(entry)
    doc["phase_reviews"] = reviews
    return doc


def takeover(
    state_root: str,
    run_key: str,
    *,
    new_host: str,
    workstation_id: str,
    wrapper_identity: str,
    launch,
    posture: str = "supervised",
    reconcile=None,
    load=None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
) -> dict:
    """Move a run to `new_host` under one compare-and-swap (design lines 188-196).

    `owner.lock` is held for the WHOLE operation, including the launch — there is no release or
    transfer window at any point. `posture` is taken from the CALLER, never inherited from the
    prior record: design line 200 forbids translating a bypass or unsafe permission profile from
    one host into the other, and inheriting it is exactly that translation (see Task 6).

    Step 6 of the design (replace this run's heartbeat entry) is PLAN 05's — schedules do not
    exist yet. This clears `heartbeat.schedule_id` so a stale entry cannot be believed, and
    installs nothing."""
    stamp = now or _now()
    lock = runstate.owner_lock_path(state_root, run_key)
    with locks.hold(lock, kind="owner", run_key=run_key):                       # step 1
        prior = read(state_root, run_key)
        if prior is None:
            raise OwnerMissing(
                f"run {run_key!r} has no ownership record to take over; no write occurred. "
                f"Start it on this host with: conductor own acquire --run {run_key}"
            )
        project = registry.load(state_root)
        if project is not None and project.get("workstation_id") != workstation_id:
            raise OwnerAmbiguous(
                f"run {run_key!r} belongs to workstation {project.get('workstation_id')!r}, not "
                f"{workstation_id!r}; no write occurred. Conductor refuses automatic cross-machine "
                f"takeover. Transfer the project first with: conductor project rebind "
                f"--confirm-prior-workstation-quiesced {project.get('workstation_id')}"
            )
        with locks.hold(                                                        # step 2
            runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key
        ):
            doc = runstate.load(state_root, run_key)
            if doc is None:
                raise OwnerMissing(
                    f"run {run_key!r} has an ownership record but no run record at "
                    f"{runstate.run_path(state_root, run_key)}; no write occurred. "
                    f"Inspect it with: conductor run show --run {run_key}"
                )
            if reconcile is not None:
                reconcile(doc)
            prove_exited(prior, load=load, now=stamp)                           # step 3
            expect = doc["revision"]
            doc["worker_host"] = new_host                                       # step 4
            doc["reviewer_host"] = opposite_of(new_host)
            doc["last_worker_host"] = prior.host
            doc["last_reviewer_host"] = doc.get("reviewer_host")
            _invalidate_reviews(doc, new_host, now=stamp)
            doc["heartbeat"] = {"schedule_id": None, "process_identity": wrapper_identity}
            doc["lease"] = {
                "owner": wrapper_identity,
                "expires_at": _plus(stamp, lease_seconds),
                "renewed_at": stamp,
            }
            doc["updated_at"] = stamp
            doc = runstate.commit(state_root, run_key, doc, expect_revision=expect)   # step 5
        pending = OwnerRecord(
            run_key=run_key, host=new_host, workstation_id=workstation_id,
            wrapper_identity=wrapper_identity, host_identity=None, tier="wrapper",
            posture=posture, renewed_at=stamp, lease_expires_at=_plus(stamp, lease_seconds),
            heartbeat_id=None,
        ).validated()
        _write(state_root, run_key, pending)
        try:
            host_identity = launch(pending)                                     # step 7
        except BaseException:
            with locks.hold(
                runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key
            ):
                reverted = runstate.load(state_root, run_key) or {}
                expect = reverted["revision"]
                reverted["worker_host"] = prior.host
                reverted["reviewer_host"] = opposite_of(prior.host)
                reverted["lease"] = {"owner": None, "expires_at": None, "renewed_at": stamp}
                reverted["heartbeat"] = {"schedule_id": None, "process_identity": None}
                reverted["updated_at"] = stamp
                runstate.commit(state_root, run_key, reverted, expect_revision=expect)
            _write(state_root, run_key, prior._replace(host_identity=None, renewed_at=stamp))
            raise
        _write(state_root, run_key, pending._replace(host_identity=host_identity))
        return doc


def opposite_of(host_id: str) -> str:
    """The reviewer host for a given worker host. Delegates to Plan 04 rather than restating the
    pairing, so there is one table."""
    from conductor.hosts import base

    return base.opposite(host_id)
```

Extend the module import to `from conductor.core import atomic, locks, registry, runstate`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_takeover.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Falsify**

**Falsifier:** in `takeover`, move `prove_exited(prior, ...)` to *after* `runstate.commit`. `test_a_live_owner_blocks_takeover_before_any_mutation` must fail on its `before ==` assertion. Revert.

Second falsifier: in the launch-failure handler, replace `_write(state_root, run_key, prior._replace(...))` with `_write(state_root, run_key, None)`. `test_a_failed_launch_restores_the_prior_non_running_record` must fail. Revert.

Third falsifier: in `_invalidate_reviews`, change `if not classifiable or (...)` to `if authored_by_new_worker and unmerged`. `test_a_review_entry_missing_its_fields_is_invalidated_not_trusted` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_takeover.py && pyright conductor/core
git add conductor/core/ownership.py tests/conductor/core/test_takeover.py
git commit -m "conductor/core/ownership.py:381-500 — the seven-step takeover compare-and-swap

- owner.lock is held across the launch; a failed launch restores the prior non-running record
- unmerged reviews authored by the incoming worker host are invalidated, merged ones retained"
```

---

### Task 6: Permission non-transfer across a takeover

**Files:**
- Modify: `conductor/core/ownership.py`
- Test: `tests/conductor/core/test_takeover.py` (extend)

**Interfaces:**
- Consumes: Plan 04's `permission_profile(posture) -> dict` and `validate_permissions(profile) -> None`.
- Produces: `takeover(...)` gains `require_explicit_bypass: bool = True` behaviour; no new public name.

**Why this contract test belongs to Plan 02 and not Plan 04.** Design line 200: *"No takeover translates a bypass or unsafe permission profile from one host into the other."* Plan 04's Task 6 can test that a Claude profile fails `CodexAdapter.validate_permissions` — that is a property of two adapters and it is tested there. **It cannot test the rule the design actually states**, because the rule is about *takeover*, and takeover does not exist in Plan 04. Until there is an operation that carries ownership from one host to another, there is nothing that could translate a profile, so there is nothing to prove does not.

Plan 04's residuals list says as much from the other side: *"lease and takeover semantics over `process_alive`"* are Plan 02's. **This is the task that discharges design line 499's "permission profiles and bypass non-transfer" bullet for the takeover half.**

**The rule, made mechanical.** Three things must hold, and each is a separate assertion:

1. The new owner's posture comes from the **caller's argument**, never from `prior.posture`. `takeover` must not read that field at all when choosing.
2. A prior `full-bypass` posture does **not** produce a `full-bypass` successor by default. Default is `supervised`.
3. The prior host's minted profile must fail the new host's `validate_permissions`, and `takeover` must never hand it there — it mints a fresh profile from the new adapter and validates that.

- [ ] **Step 1: Write the failing test**

Append to `tests/conductor/core/test_takeover.py`:

```python
# --- permission non-transfer (design line 200; test bullet at design line 499) ----------------
#
# This contract cannot be tested in Plan 04. Bypass non-transfer is a property of TAKEOVER, and
# takeover does not exist until this plan. Plan 04 can only prove that one host's profile fails
# the other's validator; it cannot prove that no operation ever carries one across.


def test_a_full_bypass_predecessor_does_not_confer_full_bypass_on_the_successor(
    state_root, run_key_value, probes
):
    _own(state_root, run_key_value, posture="full-bypass")
    probes["claude"]._alive = set()
    _takeover(state_root, run_key_value, probes)
    assert ownership.read(state_root, run_key_value).posture == "supervised"


def test_the_successors_posture_comes_from_the_caller_not_the_record(
    state_root, run_key_value, probes
):
    _own(state_root, run_key_value, posture="full-bypass")
    probes["claude"]._alive = set()
    _takeover(state_root, run_key_value, probes, posture="scoped")
    assert ownership.read(state_root, run_key_value).posture == "scoped"


def test_takeover_validates_a_freshly_minted_profile_on_the_NEW_host(
    state_root, run_key_value, probes
):
    """The prior host's profile must never reach the new host's validator, and the new host's
    own profile must pass it. A takeover that skipped this would report a posture that was
    never actually applied."""
    _own(state_root, run_key_value, posture="full-bypass")
    probes["claude"]._alive = set()
    validated = []
    probes["codex"].validate_permissions = lambda profile: validated.append(profile)
    _takeover(state_root, run_key_value, probes, posture="scoped")
    assert [p["host"] for p in validated] == ["codex"]
    assert validated[0]["posture"] == "scoped"


def test_a_profile_minted_by_the_prior_host_is_rejected_by_the_new_hosts_validator(probes):
    """The property Plan 04 owns, asserted here too because this plan is what would violate it."""
    stale = probes["claude"].permission_profile("full-bypass")
    with pytest.raises(ValueError):
        probes["codex"].validate_permissions(stale)


def test_an_unsafe_posture_the_new_host_refuses_aborts_before_the_swap(
    state_root, run_key_value, probes
):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()

    def refuse(_profile):
        raise ValueError("codex refuses full-bypass under this configuration")

    probes["codex"].validate_permissions = refuse
    with pytest.raises(ValueError):
        _takeover(state_root, run_key_value, probes, posture="full-bypass")
    assert ownership.read(state_root, run_key_value).host == "claude"
    assert runstate.load(state_root, run_key_value)["worker_host"] == "claude"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/conductor/core/test_takeover.py -q -k "posture or profile or bypass"`
Expected: FAIL — the successor inherits `full-bypass`, and nothing validates a profile.

- [ ] **Step 3: Write the implementation**

In `takeover`, immediately after the cross-machine check and **before** `state.lock` is taken, insert:

```python
        # Design line 200: no takeover translates a bypass or unsafe permission profile from one
        # host into the other. `posture` is the CALLER's argument; `prior.posture` is deliberately
        # never read here. Inheriting it would be the translation the rule forbids, and it would
        # report a posture that was never applied on the new host — the flags do not even mean
        # the same thing (Claude: mode + settings file; Codex: a graded sandbox axis).
        adapter = _adapter_for_host(new_host, load)
        adapter.validate_permissions(adapter.permission_profile(posture))
```

and add the helper beside `_adapter`:

```python
def _adapter_for_host(host_id: str, load):
    """The adapter for a host named by the CALLER rather than by a record."""
    if load is None:
        from conductor.hosts import base

        load = base.load
    return load(host_id)
```

`posture` already defaults to `"supervised"` in the signature; no other change is needed for assertion 2, and assertion 1 holds because `prior.posture` appears nowhere in `takeover`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/conductor/core/test_takeover.py -q`
Expected: PASS (14 passed).

- [ ] **Step 5: Falsify**

**Falsifier:** in `takeover`, change the `pending` record's `posture=posture` to `posture=prior.posture`. `test_a_full_bypass_predecessor_does_not_confer_full_bypass_on_the_successor` and `test_the_successors_posture_comes_from_the_caller_not_the_record` must both fail. Revert.

Second falsifier: delete the two `adapter…validate_permissions(...)` lines. `test_takeover_validates_a_freshly_minted_profile_on_the_NEW_host` and `test_an_unsafe_posture_the_new_host_refuses_aborts_before_the_swap` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_takeover.py && pyright conductor/core
git add conductor/core/ownership.py tests/conductor/core/test_takeover.py
git commit -m "conductor/core/ownership.py:400-412 — bypass non-transfer at takeover

- the successor's posture comes from the caller; prior.posture is never read
- a freshly minted profile is validated on the NEW host before the swap, and a refusal aborts"
```

---

### Task 7: `prune` — orphan refusal and identity-matched termination

**Files:**
- Modify: `conductor/core/ownership.py`
- Test: `tests/conductor/core/test_prune.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4.
- Produces: `prune(state_root, run_key, *, terminate_orphan: str | None = None, load=None, kill=None, now=None) -> dict` — a report `{"cleared": bool, "terminated": str | None, "prior": dict | None}`.

**Design line 198, sentence by sentence.** *"Prune acquires owner.lock then state.lock for inspection and clearing"* — that order, no `project.lock`. *"If the identity-matched host is still live, the non-destructive prune refuses and prints an explicit `conductor heartbeat prune --run <run-key> --terminate-orphan <identity-token>` recovery command"* — refusal is the default, and the command names an identity token, not a PID. *"that command terminates only the identity-matched orphan and proves exit before clearing the record"* — three separate obligations: match the identity, terminate only that, and re-prove exit after.

**The verb name.** The design writes `conductor heartbeat prune`. `heartbeat` is Plan 05's verb group and does not exist. This plan ships it as `conductor own prune` and Plan 05 may alias it. Recorded as correction 3.

**Why `--terminate-orphan` takes the identity and not a PID.** The identity carries start-ticks (Plan 04 Task 7). Between the prune that printed the command and the prune that runs it, the kernel can reassign the PID. Terminating on a bare PID would kill an unrelated process, and there would be no signal that it had. The implementation re-checks `process_alive(identity)` immediately before signalling and refuses if the identity no longer matches.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_prune.py`:

```python
"""Prune: clear a dead run's ownership record, and refuse to do it destructively by accident.

The dangerous case is the orphan — a wrapper that died leaving its launched host running. The
non-destructive prune REFUSES that and prints the identity-scoped recovery command; only the
explicit --terminate-orphan form may act, and only on the identity it was given.
"""

from __future__ import annotations

import pytest

from conductor.core import locks, ownership, registry, runstate

T0 = "2026-08-17T12:00:00+00:00"
T_LATE = "2026-08-17T13:00:00+00:00"


def _own(state_root, key, probes, *, identity="claude:100:1", host_identity="claude:101:2"):
    return ownership.acquire(
        state_root, key, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity=identity, host_identity=host_identity,
        now=T0, lease_seconds=1,
    )


def test_prune_clears_a_fully_exited_owner_and_its_run_json_mirror(state_root, run_key_value, probes):
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = set()
    report = ownership.prune(state_root, run_key_value, load=probes["load"], now=T_LATE)
    assert report["cleared"] is True
    assert report["terminated"] is None
    assert report["prior"]["wrapper_identity"] == "claude:100:1"
    assert ownership.read(state_root, run_key_value) is None
    assert runstate.load(state_root, run_key_value)["lease"]["owner"] is None


def test_prune_refuses_a_live_owner(state_root, run_key_value, probes):
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = {"claude:100:1"}
    with pytest.raises(ownership.OwnerBusy):
        ownership.prune(state_root, run_key_value, load=probes["load"], now=T_LATE)
    assert ownership.read(state_root, run_key_value) is not None


def test_a_non_destructive_prune_refuses_an_orphan_and_prints_the_exact_command(
    state_root, run_key_value, probes
):
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = {"claude:101:2"}
    with pytest.raises(ownership.OwnerOrphaned) as excinfo:
        ownership.prune(state_root, run_key_value, load=probes["load"], now=T_LATE)
    assert f"conductor own prune --run {run_key_value} --terminate-orphan claude:101:2" in str(excinfo.value)
    assert ownership.read(state_root, run_key_value) is not None


def test_terminate_orphan_kills_only_the_identity_it_was_given_then_clears(
    state_root, run_key_value, probes
):
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = {"claude:101:2", "claude:777:7"}
    report = ownership.prune(
        state_root, run_key_value, terminate_orphan="claude:101:2",
        load=probes["load"], kill=probes["claude"].kill, now=T_LATE,
    )
    assert probes["claude"].terminated == ["claude:101:2"]
    assert "claude:777:7" in probes["claude"]._alive
    assert report["terminated"] == "claude:101:2"
    assert ownership.read(state_root, run_key_value) is None


def test_terminate_orphan_refuses_an_identity_the_record_does_not_name(
    state_root, run_key_value, probes
):
    """PID reuse: the identity printed a minute ago may name a different process now, and an
    identity that is not this record's launched host is never ours to kill."""
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = {"claude:101:2"}
    with pytest.raises(ownership.OwnerAmbiguous) as excinfo:
        ownership.prune(
            state_root, run_key_value, terminate_orphan="claude:999:9",
            load=probes["load"], kill=probes["claude"].kill, now=T_LATE,
        )
    assert "claude:999:9" in str(excinfo.value)
    assert probes["claude"].terminated == []


def test_terminate_orphan_proves_exit_after_signalling_before_clearing(
    state_root, run_key_value, probes
):
    """Design line 198: 'proves exit before clearing the record'. A kill that did not take must
    not leave an unowned run with a live host still writing to it."""
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = {"claude:101:2"}
    with pytest.raises(ownership.OwnerOrphaned):
        ownership.prune(
            state_root, run_key_value, terminate_orphan="claude:101:2",
            load=probes["load"], kill=lambda _identity: None, now=T_LATE,   # signal does nothing
        )
    assert ownership.read(state_root, run_key_value) is not None


def test_prune_of_an_absent_record_reports_nothing_cleared(state_root, run_key_value, probes):
    report = ownership.prune(state_root, run_key_value, load=probes["load"], now=T_LATE)
    assert report == {"cleared": False, "terminated": None, "prior": None}


def test_prune_takes_owner_then_state_and_never_project(state_root, run_key_value, probes):
    _own(state_root, run_key_value, probes)
    probes["claude"]._alive = set()
    seen = []
    original = locks.hold

    def spy(path, **kw):
        seen.append(kw["kind"])
        return original(path, **kw)

    import conductor.core.ownership as module
    module.locks.hold = spy
    try:
        ownership.prune(state_root, run_key_value, load=probes["load"], now=T_LATE)
    finally:
        module.locks.hold = original
    assert "project" not in seen
    assert seen.index("owner") < seen.index("state")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_prune.py -q`
Expected: FAIL — `AttributeError: module 'conductor.core.ownership' has no attribute 'prune'`

- [ ] **Step 3: Write the implementation**

Append to `conductor/core/ownership.py`:

```python
def _default_kill(identity: str) -> None:
    """Signal the process named by an identity. The identity's start-ticks were already matched
    against the record and re-checked for liveness by the caller, so the PID cannot have been
    reused between the check and this call within one prune."""
    import os
    import signal

    os.kill(int(identity.split(":")[1]), signal.SIGTERM)


def prune(
    state_root: str,
    run_key: str,
    *,
    terminate_orphan: str | None = None,
    load=None,
    kill=None,
    now: str | None = None,
) -> dict:
    """Clear a dead run's ownership record (design line 198).

    Non-destructive by default: a live owner refuses, and an ORPHAN — a dead wrapper with a live
    launched host — refuses too, printing the identity-scoped recovery command. `terminate_orphan`
    is the explicit destructive form; it terminates ONLY the identity the record names, and proves
    exit again before clearing."""
    stamp = now or _now()
    with locks.hold(runstate.owner_lock_path(state_root, run_key), kind="owner", run_key=run_key):
        record = read(state_root, run_key)
        if record is None:
            return {"cleared": False, "terminated": None, "prior": None}
        if terminate_orphan is not None:
            if terminate_orphan != record.host_identity:
                raise OwnerAmbiguous(
                    f"run {run_key!r} records launched host {record.host_identity!r}, not "
                    f"{terminate_orphan!r}; no write occurred and nothing was signalled. An "
                    f"identity that this record does not name is never ours to terminate — the "
                    f"kernel may have reassigned that PID since the command was printed. "
                    f"Re-read the current state with: conductor own status --run {run_key}"
                )
            state = inspect(record, load=load, now=stamp)
            if state.wrapper_alive:
                raise OwnerBusy(
                    f"run {run_key!r} still has a LIVE wrapper {record.wrapper_identity}; no "
                    f"write occurred and nothing was signalled. This is not an orphan. Stop the "
                    f"wrapper first, then retry: conductor own status --run {run_key}"
                )
            if state.host_alive:
                (kill or _default_kill)(terminate_orphan)
        prove_exited(record, load=load, now=stamp)
        with locks.hold(
            runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key
        ):
            _write(state_root, run_key, None)
            _mirror_locked(state_root, run_key, None, now=stamp)
        return {
            "cleared": True,
            "terminated": terminate_orphan,
            "prior": record.as_doc(),
        }
```

`prune` holds `state.lock` explicitly (design line 198 says so), so it cannot call `_mirror`, which takes the lock itself via `runstate.update` — that would be a re-entrant acquisition and `locks._check_order` refuses it by design. Split `_mirror` into a locked and an unlocked half:

```python
def _mirror_locked(state_root: str, run_key: str, record: OwnerRecord | None, *, now: str) -> dict:
    """The mirror refresh, for a caller ALREADY holding this run's `state.lock`."""
    doc = runstate.load(state_root, run_key)
    if doc is None:
        return {}
    expect = doc["revision"]
    doc["lease"] = {
        "owner": record.wrapper_identity if record else None,
        "expires_at": record.lease_expires_at if record else None,
        "renewed_at": record.renewed_at if record else None,
    }
    doc["heartbeat"] = {
        "schedule_id": record.heartbeat_id if record else None,
        "process_identity": record.wrapper_identity if record else None,
    }
    if record is not None:
        doc["worker_host"] = record.host
    doc["updated_at"] = now
    return runstate.commit(state_root, run_key, doc, expect_revision=expect)
```

and rewrite Task 2's `_mirror` to take the lock and delegate:

```python
def _mirror(state_root: str, run_key: str, record: OwnerRecord | None, *, now: str) -> None:
    with locks.hold(runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key):
        _mirror_locked(state_root, run_key, record, now=now)
```

Re-run Task 2's tests after this refactor: `pytest tests/conductor/core/test_ownership_acquire.py -q` must still be green.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/conductor/core/test_prune.py tests/conductor/core/test_ownership_acquire.py -q`
Expected: PASS (8 + 12 passed)

- [ ] **Step 5: Falsify**

**Falsifier:** in `prune`, move `prove_exited(record, ...)` to before the `if terminate_orphan is not None:` block. `test_terminate_orphan_kills_only_the_identity_it_was_given_then_clears` must fail — the orphan raises before anything can be terminated. Revert.

Second falsifier: change the identity check to `if terminate_orphan is None:`. `test_terminate_orphan_refuses_an_identity_the_record_does_not_name` must fail. Revert.

Third falsifier: move the final `prove_exited` call to before `(kill or _default_kill)(...)` and delete the post-kill proof. `test_terminate_orphan_proves_exit_after_signalling_before_clearing` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/ownership.py tests/conductor/core/test_prune.py && pyright conductor/core
git add conductor/core/ownership.py tests/conductor/core/test_prune.py
git commit -m "conductor/core/ownership.py:501-590 — prune, with orphan refusal by default

- owner.lock then state.lock; a live wrapper or a live launched host refuses non-destructively
- --terminate-orphan signals only the identity the record names and re-proves exit before clearing"
```

---

### Task 8: `rebind` — the project transaction over every run's ownership

**Files:**
- Create: `conductor/core/rebind.py`
- Modify: `conductor/core/registry.py:113-116`
- Test: `tests/conductor/core/test_rebind.py`

**Interfaces:**
- Consumes: `registry.load/commit`, `runstate.owner_lock_path/state_lock_path/run_path`, `transaction.prepare/commit/apply/recover/write_status`, `locks.hold`, Task 1's record.
- Produces:
  - `class RebindRefused(RuntimeError)`
  - `rebind(state_root, *, workstation_id, confirm_prior_workstation_quiesced: str, schedules_present=None, load=None, now=None) -> dict` — the updated project document.

**Design line 202, made mechanical.** Rebind *"acquires project.lock, every run's owner.lock in sorted run-key order, and then every run's state.lock in the same order. It refuses while any identity-matched local owner or local schedule remains, clears stale ownership records through a project transaction, and retains the prior workstation ID in project.json history."* Also: *"Rebind installs no schedules."*

**The lock sequence is exactly `repoint`'s** (`conductor/core/repoint.py:178-228`), which is the only shape `locks._check_order` permits: project → all owner (sorted) → all state (sorted). Copy that structure; do not invent a second one. Where `repoint` scopes to one spec path's generations, rebind scopes to **every** run key in the registry (`registry.run_keys(doc)`, which already returns them sorted).

**The journal discipline this task exists to exercise.** Rebind writes `project.json` and N `run.json` files, so it is a project transaction. Per Plan 01's residuals, **every entry that writes a `run.json` must carry `{"lock": {"path": state_lock_path, "run_key": key}}`**. `repoint.py:324-338` is the reference. An entry without one replays with no serialisation against that run's writers, and `transaction.recover` — which locks per run in sorted key order and refuses to move a revision backwards — cannot derive the lock path from an opaque target.

**Two corrections to what the residuals expected of this plan.**

1. **Plan 02 is not `registry.update`'s first production caller, and cannot be.** `registry.update` calls `registry.commit`, which takes `project.lock` itself (`registry.py:68`). Rebind must hold `project.lock` across the owner and state locks, so calling `update` inside that hold is a re-entrant acquisition and `locks._check_order` refuses it — correctly. Rebind therefore commits through `transaction`, exactly as `repoint` does. The residual's expectation was reasonable and is simply wrong about the mechanism. **What this task does discharge** is the misreported phrase at `registry.py:113-116`: the exhausted-attempts `RevisionConflict` hardcodes `"no write occurred"`, while every `commit` attempt it made ran `transaction.recover`, which writes. One line, using the `transaction.write_status` helper that already exists for exactly this.

2. **The status-mirror residual cannot be discharged here either.** `registry.mirror_status` converging only when `cmd_new` runs is real, and resume/finish should write the mirror in the same transaction as the record. **Plan 02 mutates no run status** — takeover swaps hosts, prune clears ownership, rebind clears ownership; none of them changes `status`. There is nothing to prove a `commit_run_and_mirror` helper on, and shipping an unused one is the padding this plan is meant to avoid. What Task 8 leaves Plan 05 is the *shape*: one journal, lock hints on every run entry, sorted order, `project.json` in the same transaction. Plan 05's `resume` and `finish` copy it.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_rebind.py`:

```python
"""Workstation rebind (design line 202): sequential transfer of a whole project's runs.

Conductor cannot verify processes or schedules on the prior workstation. The attestation flag is
the operator's statement that they are stopped, and without it rebind fails closed. It installs
no schedules — each run needs an explicit `conductor resume` on the new machine.
"""

from __future__ import annotations

import os

import pytest

from conductor.core import locks, ownership, rebind, registry, runstate, transaction

T0 = "2026-08-17T12:00:00+00:00"
T_LATE = "2026-08-17T13:00:00+00:00"


def _own(state_root, key, *, identity="claude:100:1"):
    return ownership.acquire(
        state_root, key, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity=identity, host_identity=None, now=T0, lease_seconds=1,
    )


def _rebind(state_root, probes, **kw):
    prior = kw.pop("prior", None) or registry.load(state_root)["workstation_id"]
    kw.setdefault("workstation_id", "new-workstation-id")
    kw.setdefault("load", probes["load"])
    kw.setdefault("now", T_LATE)
    return rebind.rebind(state_root, confirm_prior_workstation_quiesced=prior, **kw)


def test_rebind_without_the_matching_attestation_fails_closed(state_root, probes):
    with pytest.raises(rebind.RebindRefused) as excinfo:
        _rebind(state_root, probes, prior="not-the-recorded-id")
    assert "--confirm-prior-workstation-quiesced" in str(excinfo.value)
    assert registry.load(state_root)["workstation_id"] != "new-workstation-id"


def test_rebind_moves_the_workstation_id_and_retains_the_prior_in_history(state_root, probes):
    prior = registry.load(state_root)["workstation_id"]
    doc = _rebind(state_root, probes)
    assert doc["workstation_id"] == "new-workstation-id"
    assert prior in doc["workstation_history"]


def test_rebind_clears_every_runs_stale_ownership_record(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    _rebind(state_root, probes)
    assert ownership.read(state_root, run_key_value) is None
    assert runstate.load(state_root, run_key_value)["lease"]["owner"] is None
    assert runstate.load(state_root, run_key_value)["workstation_id"] == "new-workstation-id"


def test_a_live_identity_matched_local_owner_refuses_the_whole_rebind(state_root, run_key_value, probes):
    _own(state_root, run_key_value)
    probes["claude"]._alive = {"claude:100:1"}
    with pytest.raises(rebind.RebindRefused) as excinfo:
        _rebind(state_root, probes)
    assert run_key_value in str(excinfo.value)
    assert registry.load(state_root)["workstation_id"] != "new-workstation-id"


def test_a_remaining_local_schedule_refuses_the_rebind(state_root, probes):
    with pytest.raises(rebind.RebindRefused) as excinfo:
        _rebind(state_root, probes, schedules_present=lambda: ["conductor-autodev /home/x/repo"])
    assert "conductor-autodev /home/x/repo" in str(excinfo.value)


def test_rebind_installs_no_schedules_and_says_so(state_root, run_key_value, probes):
    """Design line 202. Each transferred nonterminal run needs an explicit resume."""
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    installed = []
    doc = _rebind(state_root, probes, schedules_present=lambda: installed)
    assert installed == []
    assert doc["specs"]  # runs survived the transfer
    assert runstate.load(state_root, run_key_value)["heartbeat"]["schedule_id"] is None


def test_every_run_json_entry_in_the_journal_carries_a_lock_hint(state_root, run_key_value, probes, monkeypatch):
    """Plan 01 residual, non-negotiable: an entry that writes a run.json without a lock hint
    replays with no serialisation against that run's writers."""
    _own(state_root, run_key_value)
    probes["claude"]._alive = set()
    captured = {}
    original = transaction.prepare

    def spy(root, txn_id, entries):
        captured["entries"] = entries
        return original(root, txn_id, entries)

    monkeypatch.setattr(rebind.transaction, "prepare", spy)
    _rebind(state_root, probes)
    run_entries = [e for e in captured["entries"] if e["path"].endswith("run.json")]
    assert run_entries
    for entry in run_entries:
        assert os.path.isabs(entry["lock"]["path"])
        assert entry["lock"]["path"].endswith("state.lock")
        assert entry["lock"]["run_key"]


def test_the_lock_sequence_is_project_then_owners_then_states_all_sorted(state_root, probes):
    seen = []
    original = locks.hold

    def spy(path, **kw):
        seen.append((kw["kind"], kw.get("run_key")))
        return original(path, **kw)

    import conductor.core.rebind as module
    module.locks.hold = spy
    try:
        _rebind(state_root, probes)
    finally:
        module.locks.hold = original
    kinds = [kind for kind, _ in seen]
    assert kinds[0] == "project"
    owners = [run for kind, run in seen if kind == "owner"]
    states = [run for kind, run in seen if kind == "state"]
    assert owners == sorted(owners)
    assert states == sorted(states)
    assert kinds.index("owner") < kinds.index("state")


def test_registry_update_exhaustion_no_longer_hardcodes_no_write_occurred(state_root, monkeypatch):
    """registry.py:113-116 — every attempt ran transaction.recover, which WRITES."""
    monkeypatch.setattr(
        registry, "commit",
        lambda *a, **k: (_ for _ in ()).throw(registry.RevisionConflict("moved")),
    )
    monkeypatch.setattr(transaction, "recover", lambda root: ["txn-a"])
    with pytest.raises(registry.RevisionConflict) as excinfo:
        registry.update(state_root, lambda doc: doc, attempts=2)
    assert "no write occurred" not in str(excinfo.value)
    assert "txn-a" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_rebind.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.rebind'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/rebind.py`:

```python
"""Sequential workstation transfer (design line 202).

Conductor cannot see the prior workstation's processes or schedules. The attestation flag is the
operator's explicit statement that all of them are stopped; without it rebind fails closed. What
Conductor CAN verify is local: no identity-matched live owner, no local schedule. Both are
checked, and either one refuses the whole operation rather than transferring some runs.

The lock sequence is `repoint`'s, which is the only one `locks._check_order` permits:
project.lock, then every run's owner.lock in sorted run-key order, then every run's state.lock in
the same order.

REBIND INSTALLS NO SCHEDULES. Each transferred nonterminal run requires an explicit
`conductor resume --run <key>` on the new workstation (Plan 05), which reconciles first and only
then installs a schedule if the run is work-capable. `awaiting-team-merge` stays schedule-less.
"""

from __future__ import annotations

import contextlib

from conductor.core import atomic, locks, ownership, registry, runstate, schema, transaction


class RebindRefused(RuntimeError):
    """A precondition for transferring this project's runs is not met. Nothing was written."""


def rebind(
    state_root: str,
    *,
    workstation_id: str,
    confirm_prior_workstation_quiesced: str,
    schedules_present=None,
    load=None,
    now: str | None = None,
) -> dict:
    """Move this project's state root to `workstation_id`. Returns the updated project document."""
    stamp = now or ownership._now()
    with locks.hold(registry.lock_path(state_root), kind="project"):
        status = transaction.write_status(transaction.recover(state_root))
        project = registry.load(state_root)
        if project is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)}; {status}. "
                "Create one with: conductor run new <spec.md>"
            )
        prior = project["workstation_id"]
        if confirm_prior_workstation_quiesced != prior:
            raise RebindRefused(
                f"this project's recorded workstation is {prior!r}, and the attestation named "
                f"{confirm_prior_workstation_quiesced!r}; {status}. Conductor cannot verify the "
                f"prior workstation's processes or schedules, so the flag is your attestation "
                f"that they are stopped. Retry with: conductor project rebind "
                f"--confirm-prior-workstation-quiesced {prior}"
            )
        remaining = list(schedules_present() if schedules_present else ())
        if remaining:
            raise RebindRefused(
                f"local schedule(s) still installed for this project: {', '.join(remaining)}; "
                f"{status}. Remove them before rebinding, then retry: conductor project rebind "
                f"--confirm-prior-workstation-quiesced {prior}"
            )
        keys = registry.run_keys(project)
        with contextlib.ExitStack() as stack:
            for key in keys:
                stack.enter_context(
                    locks.hold(
                        runstate.owner_lock_path(state_root, key),
                        kind="owner", run_key=key, timeout=5.0,
                    )
                )
            for key in keys:
                stack.enter_context(
                    locks.hold(
                        runstate.state_lock_path(state_root, key),
                        kind="state", run_key=key,
                    )
                )
            for key in keys:
                record = ownership.read(state_root, key)
                if record is None:
                    continue
                state = ownership.inspect(record, load=load, now=stamp)
                if state.wrapper_alive or state.host_alive:
                    raise RebindRefused(
                        f"run {key!r} still has a live local owner "
                        f"(wrapper {record.wrapper_identity}, launched host "
                        f"{record.host_identity}); {status}. Rebind transfers the whole project "
                        f"or none of it. Stop it, or clear it with: conductor own prune "
                        f"--run {key}, then retry: conductor project rebind "
                        f"--confirm-prior-workstation-quiesced {prior}"
                    )
            entries: list[dict] = []
            for key in keys:
                doc = runstate.load(state_root, key)
                if doc is None:
                    continue
                after = schema.clone(doc)
                after["workstation_id"] = workstation_id
                after["lease"] = {"owner": None, "expires_at": None, "renewed_at": stamp}
                after["heartbeat"] = {"schedule_id": None, "process_identity": None}
                after["updated_at"] = stamp
                after["revision"] = doc["revision"] + 1
                schema.validate_run(after)
                entries.append(
                    {
                        "path": runstate.run_path(state_root, key),
                        "before": doc,
                        "after": after,
                        # Recovery runs in a later process holding only project.lock, with no way
                        # to derive which lock guards this file. Name it in the journal.
                        "lock": {
                            "path": runstate.state_lock_path(state_root, key),
                            "run_key": key,
                        },
                    }
                )
            after_project = schema.clone(project)
            after_project["workstation_id"] = workstation_id
            after_project["workstation_history"] = [
                *after_project.get("workstation_history", []),
                prior,
            ]
            after_project["revision"] = project["revision"] + 1
            schema.validate_project(after_project)
            entries.append(
                {
                    "path": registry.registry_path(state_root),
                    "before": project,
                    "after": after_project,
                }
            )
            txn_id = f"rebind-{workstation_id}"
            transaction.prepare(state_root, txn_id, entries)
            transaction.commit(state_root, txn_id)
            transaction.apply(state_root, txn_id)
            for key in keys:
                ownership._write(state_root, key, None)
        return after_project
```

Then fix `conductor/core/registry.py:113-116`:

```python
    raise RevisionConflict(
        f"project.json at {registry_path(state_root)} changed under {attempts} attempts; "
        f"{transaction.write_status(transaction.pending(state_root))}. "
        f"Last conflict: {last}"
    )
```

`write_status` and `pending` are already exported (`transaction.py:209,219`); this is the fifth recover-then-refuse site the Plan 01 fix wave missed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/conductor/core/test_rebind.py tests/conductor/core/test_registry.py -q`
Expected: PASS, with Plan 01's `test_registry.py` still green.

- [ ] **Step 5: Falsify**

**Falsifier:** in the `entries.append({...})` for run documents, delete the `"lock": {...}` key. `test_every_run_json_entry_in_the_journal_carries_a_lock_hint` must fail. Revert. (`transaction._check_entries` accepts an absent lock — it validates the hint's shape when present, not its presence — so this falsifier genuinely tests the plan's discipline rather than the library's.)

Second falsifier: change the attestation comparison to `if False:`. `test_rebind_without_the_matching_attestation_fails_closed` must fail. Revert.

Third falsifier: revert `registry.py`'s message to the hardcoded `"no write occurred"`. `test_registry_update_exhaustion_no_longer_hardcodes_no_write_occurred` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/rebind.py conductor/core/registry.py tests/conductor/core/test_rebind.py && pyright conductor/core
git add conductor/core/rebind.py conductor/core/registry.py tests/conductor/core/test_rebind.py
git commit -m "conductor/core/rebind.py:1-165, conductor/core/registry.py:113-117 — workstation rebind

- project.lock, then every owner.lock and state.lock in sorted run-key order; one journal with a
  lock hint on every run.json entry; refuses on a live local owner, a schedule, or a bad attestation
- registry.update's exhausted-attempts refusal reports the real write status instead of a constant"
```

---

### Task 9: `repoint`'s flat refusal becomes liveness interpretation

**Files:**
- Modify: `conductor/core/repoint.py:208-220`
- Test: `tests/conductor/core/test_repoint.py` (extend the existing file)

**Interfaces:**
- Consumes: `ownership.read`, `ownership.inspect`, `ownership.prove_exited`.
- Produces: no new public name. `repoint`'s existing signature is unchanged.

**The residual this discharges, verbatim.** Plan 01's residuals: *"Cross-machine and lease semantics are absent by design. Plan 01 treats a busy `owner.lock` as a flat refusal. The acquisition order (`project` → `owner` → `state`, run locks in sorted run-key order) is established and tested; **Plan 02 replaces the refusal with liveness interpretation without changing that order**."* The code even names this plan at `repoint.py:209-210`.

**What changes and what does not.** The `locks.hold(..., kind="owner", ..., timeout=owner_timeout)` call stays exactly where it is, with the same kind, the same run key, and the same position in the sequence. **Only the `except locks.LockTimeout` handler changes.** Today it always refuses. After this task it distinguishes:

- lock busy → a wrapper-tier owner really is holding it → refuse, as today, but name the owner from the record rather than only the lock path;
- lock free but a live record → an in-session owner → refuse, which today's code cannot detect at all because the in-session tier holds no lock;
- lock free, record present, provably exited → the record is stale; refuse *and* print the prune command, so the operator has a next step instead of a dead end;
- lock free, no record → proceed, as today.

The third case is the real improvement: `repoint` currently gives an operator whose worker crashed no way forward except deleting a lock file by hand.

**A note on the timeout residual.** Plan 01 also records that `repoint` holds `project.lock` plus N owner plus N state locks across up to two git subprocesses at `_GIT_TIMEOUT = 30.0` each — a worst case near 60 seconds — and that the timeout on that path should be a few seconds. **That is not this task.** It is a separate one-line change with its own risk (a slow network `git` call), and folding it in would make this task's review about two things. Leave it in the residuals.

- [ ] **Step 1: Write the failing test**

Append to `tests/conductor/core/test_repoint.py`:

```python
# --- Plan 02: the flat busy-owner refusal becomes liveness interpretation --------------------
#
# repoint.py:209-210 names this plan in a comment. The acquisition ORDER is unchanged; only the
# LockTimeout handler and the pre-check learn to read the record.

from conductor.core import ownership as _ownership


def test_repoint_refuses_a_live_in_session_owner_that_holds_no_lock(git_repo, probes):
    """The case Plan 01 could not see: the in-session tier records ownership without holding
    owner.lock for the fire, so a lock-only check reads it as free."""
    root, state_root, key = _one_run(git_repo)
    _ownership.acquire(
        state_root, key, host="claude", workstation_id="w0",
        wrapper_identity="claude:100:1", tier="in-session",
    )
    probes["claude"]._alive = {"claude:100:1"}
    _write_spec(root, "docs/specs/moved.md")
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=key,
                        new_spec_path="docs/specs/moved.md", load=probes["load"])
    assert "claude:100:1" in str(excinfo.value)
    assert "no write occurred" in str(excinfo.value)


def test_repoint_refuses_a_stale_record_but_now_names_the_prune_command(git_repo, probes):
    root, state_root, key = _one_run(git_repo)
    _ownership.acquire(
        state_root, key, host="claude", workstation_id="w0",
        wrapper_identity="claude:100:1", lease_seconds=1,
    )
    probes["claude"]._alive = set()
    _write_spec(root, "docs/specs/moved.md")
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(state_root, repo_root=root, run_key=key,
                        new_spec_path="docs/specs/moved.md", load=probes["load"],
                        now="2026-08-17T13:00:00+00:00")
    assert f"conductor own prune --run {key}" in str(excinfo.value)


def test_repoint_proceeds_when_no_ownership_record_exists(git_repo, probes):
    root, state_root, key = _one_run(git_repo)
    _write_spec(root, "docs/specs/moved.md")
    doc = repoint.repoint(state_root, repo_root=root, run_key=key,
                          new_spec_path="docs/specs/moved.md", load=probes["load"])
    assert doc["spec_path"] == "docs/specs/moved.md"


def test_the_owner_lock_acquisition_order_is_unchanged(git_repo, probes):
    """The whole point of the residual's wording: replace the refusal, not the order."""
    root, state_root, key = _one_run(git_repo)
    _write_spec(root, "docs/specs/moved.md")
    seen = []
    original = locks.hold

    def spy(path, **kw):
        seen.append(kw["kind"])
        return original(path, **kw)

    repoint.locks.hold = spy
    try:
        repoint.repoint(state_root, repo_root=root, run_key=key,
                        new_spec_path="docs/specs/moved.md", load=probes["load"])
    finally:
        repoint.locks.hold = original
    assert seen[0] == "project"
    assert seen.index("owner") < seen.index("state")
```

Add the two helpers this file needs at its top if they are not already present (Plan 01's `test_repoint.py` has equivalents under its own names — reuse them rather than duplicating; `_one_run(git_repo) -> (repo_root, state_root, run_key)` registers one active run, `_write_spec(root, rel)` creates a spec file and commits it).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/conductor/core/test_repoint.py -q -k "in_session or prune_command or no_ownership_record"`
Expected: FAIL — `repoint.repoint() got an unexpected keyword argument 'load'`

- [ ] **Step 3: Write the implementation**

In `conductor/core/repoint.py`, add `load=None` and `now: str | None = None` to `repoint`'s keyword-only parameters, import `ownership`, and replace the handler at lines 208–220 plus add a record check after the lock succeeds:

```python
            for key in keys:
                shares = (
                    ""
                    if key == run_key
                    else f" — it shares {old_rel} with run {run_key!r} and moves with it"
                )
                try:
                    stack.enter_context(
                        locks.hold(
                            runstate.owner_lock_path(state_root, key),
                            kind="owner",
                            run_key=key,
                            timeout=owner_timeout,
                        )
                    )
                except locks.LockTimeout as exc:
                    # Plan 02: a busy lock is a WRAPPER-TIER owner holding it for its fire. Name
                    # the owner from the record where one exists; the lock path alone told an
                    # operator nothing about who to stop.
                    held = _describe_owner(state_root, key)
                    raise RepointRefused(
                        f"run {key!r} has a live owner {held} holding "
                        f"{runstate.owner_lock_path(state_root, key)}{shares}; {status}. "
                        f"Stop the worker or wait for it to exit, then retry: {retry}"
                    ) from exc
                # Plan 02: the lock being free is NOT evidence the run is unowned. The in-session
                # tier records ownership without holding owner.lock across its fire, so Plan 01's
                # lock-only check read a live in-session worker as absent.
                record = ownership.read(state_root, key)
                if record is not None:
                    try:
                        stale = ownership.prove_exited(record, load=load, now=now)
                    except (ownership.OwnerBusy, ownership.OwnerOrphaned) as exc:
                        raise RepointRefused(
                            f"run {key!r} has a live owner {record.wrapper_identity} on host "
                            f"{record.host} (tier {record.tier}){shares}; {status} — "
                            f"rewriting its spec path underneath a running worker would move its "
                            f"gate and branch mid-fire. {exc} Then retry: {retry}"
                        ) from exc
                    if stale:
                        raise RepointRefused(
                            f"run {key!r} has a STALE ownership record for "
                            f"{record.wrapper_identity}, which has exited{shares}; {status}. "
                            f"Clear it with: conductor own prune --run {key} — then retry: {retry}"
                        )
                    raise RepointRefused(
                        f"run {key!r} has a current ownership record for "
                        f"{record.wrapper_identity} whose lease has not lapsed{shares}; "
                        f"{status}. Wait for the lease to expire or stop the worker, then "
                        f"retry: {retry}"
                    )
```

and the small describer:

```python
def _describe_owner(state_root: str, run_key: str) -> str:
    """The recorded owner's identity for a message, or a neutral phrase. Never raises — this
    runs inside a refusal path and must not replace the operator's real error with its own."""
    try:
        record = ownership.read(state_root, run_key)
    except Exception:
        return "(record unreadable)"
    return f"{record.wrapper_identity} on host {record.host}" if record else "(unrecorded)"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/conductor/core/test_repoint.py -q`
Expected: PASS, with every pre-existing `test_repoint.py` test still green. **Plan 01's residuals warn about exactly this file:** two of its tests build a deliberately corrupt state, and when a check closes a hole the cheap move is to weaken the check. If a pre-existing test now fails, read it before touching either side.

- [ ] **Step 5: Falsify**

**Falsifier:** delete the `record = ownership.read(...)` block (everything after the `except locks.LockTimeout` handler). `test_repoint_refuses_a_live_in_session_owner_that_holds_no_lock` must fail. Revert.

Second falsifier: in the `if stale:` branch, change the message to drop `conductor own prune`. `test_repoint_refuses_a_stale_record_but_now_names_the_prune_command` must fail. Revert.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/core tests/conductor/core && ruff format --check conductor/core/repoint.py tests/conductor/core/test_repoint.py && pyright conductor/core && pytest tests/conductor/core -q
git add conductor/core/repoint.py tests/conductor/core/test_repoint.py
git commit -m "conductor/core/repoint.py:196-245 — liveness interpretation replaces the flat owner refusal

- a free owner.lock is no longer read as unowned; the in-session tier's record is consulted
- a stale record refuses with the prune command instead of a dead end; lock order unchanged"
```

---

### Task 10: `conductor own` and wiring both execution tiers

**Files:**
- Create: `conductor/own_cmd.py`
- Modify: `bin/conductor`
- Modify: `conductor/resume_script.py`
- Modify: `skills/autodev/SKILL.md`
- Test: `tests/conductor/test_own_cmd.py`

**Interfaces:**
- Consumes: everything above, plus `conductor.core.resolve.resolve`, `conductor.core.workstation.workstation_id`, `conductor.hosts.base.load(...).process_identity`.
- Produces: `conductor own {acquire|renew|release|status|prune|takeover}` and `conductor project rebind`.

**This is the task that closes the prologue gap.** Everything before it built a mechanism nothing calls.

**Exit codes**, following `run_cmd.py:41-45`: `0` ok, `1` refused, `2` ambiguous, `3` no run resolved, `64` usage. **`own acquire` on a busy run exits `0`** — a skipped fire caused by a live owner is a success, not an error, and a driver that treated it as a failure would fill the log with false alarms. It prints `skip: <reason>` on stdout and the caller checks that, which is why `--if-free` exists as the driver's form.

**The legacy exception, stated in the code.** `own acquire` on a state root with no registry, or a repo with no resolvable run, prints `legacy: no per-run state; ownership not tracked` and exits `0` without writing. Today's live run is a legacy flat run — it has no run key to key a record on, and inventing one would collide with Plan 03's obligation to preserve branch names. **The gap therefore closes for per-run state and stays open for a legacy run until Plan 03 migrates it.** Do not treat this as a bug to fix here.

**The two tier wirings.**

*Wrapper tier* (`conductor/resume_script.py`): the generated driver keeps its existing `flock` on `$PROJECT/.conductor/resume.lock` — narrowing that to per-run is Plan 05's, and changing it here would mean rewriting a script many live fires have proven. It gains a record acquisition after the existing guards, and a release trap:

```sh
# (d) per-run ownership — the guard the in-session tier can also see. Skips are SUCCESS.
OWN="$("$CONDUCTOR" own acquire --if-free --tier wrapper --project "$PROJECT" 2>>"$LOG")" || {
    printf '%s own-acquire-failed\n' "$(ts)" >> "$LOG"; exit 6; }
case "$OWN" in
    skip:*)   printf '%s own-skip %s\n' "$(ts)" "$OWN" >> "$LOG"; exit 0 ;;
    legacy:*) : ;;   # pre-Plan-03 flat state: today's guards (a)-(c) are the whole story
    *)        trap '"$CONDUCTOR" own release --project "$PROJECT" >/dev/null 2>&1' EXIT ;;
esac
```

*In-session tier* (`skills/autodev/SKILL.md`): a new **step 0** before step 1, and a release on the two exit paths (step 3b's terminal path and the handoff at the end of a fire). This is a behaviour change to a shipped skill: feature branch, PR, codex review, plugin version bump.

**A12 observes `skills/autodev/SKILL.md`.** Run `./bin/conductor gate verify` after this task and treat a failure as a real regression.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/test_own_cmd.py`:

```python
"""The `conductor own` verb group, and the contract the two execution tiers rely on.

The most load-bearing behaviour here is an exit code: a fire skipped because another fire owns
the run is a SUCCESS. A driver that read it as a failure would log an alarm every seven minutes
for the whole life of a run.
"""

from __future__ import annotations

import pytest

from conductor.core import ownership, registry
from conductor import own_cmd


def _run(argv, capsys):
    code = own_cmd.main(argv)
    return code, capsys.readouterr()


def test_acquire_then_status_then_release_round_trip(state_root, run_key_value, capsys, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    code, out = _run(["acquire", "--run", run_key_value], capsys)
    assert code == 0
    assert "owned" in out.out
    code, out = _run(["status", "--run", run_key_value], capsys)
    assert code == 0 and run_key_value in out.out
    code, _ = _run(["release", "--run", run_key_value], capsys)
    assert code == 0
    assert ownership.read(state_root, run_key_value) is None


def test_if_free_against_a_live_owner_exits_zero_and_prints_skip(
    state_root, run_key_value, capsys, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    ownership.acquire(
        state_root, run_key_value, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity="claude:100:1",
    )
    code, out = _run(["acquire", "--if-free", "--run", run_key_value], capsys)
    assert code == 0
    assert out.out.startswith("skip:")


def test_acquire_without_if_free_against_a_live_owner_exits_one(
    state_root, run_key_value, capsys, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    ownership.acquire(
        state_root, run_key_value, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity="claude:100:1",
    )
    code, out = _run(["acquire", "--run", run_key_value], capsys)
    assert code == 1
    assert "conductor own takeover" in out.err


def test_a_repo_with_no_registry_reports_legacy_and_exits_zero(tmp_path, capsys, monkeypatch):
    """Today's live run is a legacy flat run with no run key. It must keep working untouched
    until Plan 03 migrates it."""
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", str(tmp_path))
    code, out = _run(["acquire", "--if-free"], capsys)
    assert code == 0
    assert out.out.startswith("legacy:")


def test_two_active_runs_without_run_refuse_rather_than_guess(
    state_root, capsys, monkeypatch, second_run
):
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    code, out = _run(["acquire", "--if-free"], capsys)
    assert code == 2
    assert "--run" in out.err


def test_prune_reports_what_it_cleared(state_root, run_key_value, capsys, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    monkeypatch.setattr(own_cmd.ownership, "prove_exited", lambda *a, **k: True)
    ownership.acquire(
        state_root, run_key_value, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity="claude:100:1",
    )
    code, out = _run(["prune", "--run", run_key_value], capsys)
    assert code == 0
    assert "claude:100:1" in out.out


def test_every_refusal_names_the_run_the_write_status_and_a_next_command(
    state_root, run_key_value, capsys, monkeypatch
):
    """The design's actionable-failure contract (line 454), asserted once over the whole group."""
    monkeypatch.setenv("CONDUCTOR_STATE_ROOT", state_root)
    ownership.acquire(
        state_root, run_key_value, host="claude",
        workstation_id=registry.load(state_root)["workstation_id"],
        wrapper_identity="claude:100:1",
    )
    for argv in (["acquire", "--run", run_key_value], ["renew", "--run", run_key_value,
                 "--identity", "claude:999:9"]):
        _code, out = _run(argv, capsys)
        assert run_key_value in out.err
        assert "no write occurred" in out.err
        assert "conductor " in out.err
```

Add a `second_run` fixture to `tests/conductor/core/conftest.py` that registers a second active spec in the same registry.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/test_own_cmd.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.own_cmd'`

- [ ] **Step 3: Write the CLI**

Create `conductor/own_cmd.py`, following `run_cmd.py`'s conventions exactly — `argparse` subparsers, `default=argparse.SUPPRESS` on `--project` (`run_cmd.py:399` records why: a subparser default must not overwrite the group-level value), and the same exit codes:

```python
"""`conductor own` — inspect and change a run's execution ownership.

`own acquire --if-free` is the form both execution tiers call at the start of a fire. It exits 0
when another fire owns the run, because a skipped fire is a SUCCESS: an unattended driver that
logged an error every time a longer fire was still running would bury real failures.
"""

from __future__ import annotations

import argparse
import os
import sys

from conductor.core import ownership, rebind, resolve, workstation

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 2
EXIT_NO_RUN = 3
EXIT_USAGE = 64


def _identity(host: str, explicit: str | None) -> str:
    """The wrapper identity to record. `os.getppid()` is deliberate: the thing that owns the run
    is the driver or REPL that invoked this CLI, not this short-lived process, which exits
    immediately and would leave an identity that is dead the moment it is written."""
    if explicit:
        return explicit
    from conductor.hosts import base

    return base.load(host).process_identity(os.getppid())


def _resolution(args) -> tuple[str, str] | None:
    """`(state_root, run_key)`, or `None` for pre-Plan-03 legacy flat state.

    `CONDUCTOR_STATE_ROOT` is the test and driver override; otherwise the run is resolved the
    same way every other verb resolves it. An ambiguous resolution PROPAGATES — guessing which
    of two active runs a fire owns is how two fires end up owning one run each other's."""
    override = os.environ.get("CONDUCTOR_STATE_ROOT")
    run = getattr(args, "run", None)
    if override is not None:
        if run:
            return (override, run)
        active = resolve.active_run_keys(override)
        if not active:
            return None
        if len(active) > 1:
            raise resolve.RunAmbiguous(
                f"{len(active)} active runs under {override} ({', '.join(active)}); "
                "no write occurred. Name one with --run <run-key>."
            )
        return (override, active[0])
    try:
        res = resolve.resolve(run_key=run, start=getattr(args, "project", None))
    except resolve.RunNotFound:
        return None
    return (res.state_root, res.run_key)


def cmd_acquire(args) -> int:
    where = _resolution(args)
    if where is None:
        print("legacy: no per-run state; ownership not tracked")
        return EXIT_OK
    state_root, run_key = where
    host = args.host or os.environ.get("CONDUCTOR_HOST", "claude")
    try:
        record = ownership.acquire(
            state_root, run_key, host=host,
            workstation_id=workstation.workstation_id(),
            wrapper_identity=_identity(host, args.identity),
            tier=args.tier, posture=args.posture,
        )
    except ownership.OwnerBusy as exc:
        if args.if_free:
            print(f"skip: {exc}")
            return EXIT_OK
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    print(f"owned {record.run_key} by {record.wrapper_identity} until {record.lease_expires_at}")
    return EXIT_OK
```

`renew`, `release`, `status`, `prune`, `takeover`, and `rebind` follow the same shape: call `_resolution`, call the corresponding `ownership` or `rebind` function, and map exceptions in one shared `main()` handler — `OwnerBusy`/`OwnerMissing`/`RebindRefused` → `EXIT_FAIL`, `OwnerAmbiguous`/`OwnerOrphaned`/`resolve.RunAmbiguous` → `EXIT_AMBIGUOUS`, `resolve.RunNotFound` → `EXIT_NO_RUN` — printing the exception text to stderr **unchanged**.

**Do not re-word the messages in the CLI.** Every refusal above was written to satisfy design line 454 (run key, failed invariant, affected path, whether a write occurred, the exact recovery command) at the point where the refusal is decided. A second phrasing in the CLI is a second thing to keep true.

`prune` and `takeover` additionally take `--terminate-orphan <identity>` and `--host <host>` respectively; `takeover`'s `launch` argument is supplied by the CLI as a function that spawns the new host through `conductor.hosts.base.load(host).worker_argv(...)` and returns its `process_identity`.

- [ ] **Step 4: Wire `bin/conductor`**

Add beside the existing `run)` case at `bin/conductor:42`:

```sh
  own) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.own_cmd "$@" ;;
  project) shift; [ "${1:-}" = "rebind" ] || { echo "usage: conductor project rebind --confirm-prior-workstation-quiesced <id>" >&2; exit 64; }; shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.own_cmd rebind "$@" ;;
```

and extend the usage text with:

```
  conductor own {acquire [--if-free] [--tier wrapper|in-session]|renew|release|status|prune [--terminate-orphan <identity>]|takeover --host <host>} [--run <run-key>]
  conductor project rebind --confirm-prior-workstation-quiesced <workstation-id>
```

- [ ] **Step 5: Wire the wrapper tier**

In `conductor/resume_script.py`, insert the guard-(d) block shown above immediately after the existing guard (b) — the done-gate green check at line 221 — so a finished run still exits before acquiring anything. Update the module docstring at line 13, which enumerates the guards, to read `(no-double-drive, done-gate-green exit, flock, per-run ownership)`.

Run the existing driver tests: `pytest tests/conductor/test_resume_script.py -q`. If a test asserts the generated script byte-for-byte, update the expected text — that is a legitimate change to a generated artifact, not a weakened test.

- [ ] **Step 6: Wire the in-session tier**

In `skills/autodev/SKILL.md`, add before the numbered step 1:

```markdown
0. **TAKE RUN OWNERSHIP — before anything reads or writes shared state.**
   `conductor own acquire --if-free --tier in-session`. Output beginning `skip:` → **another fire
   owns this run; write nothing, exit clean.** That is a SUCCESS, not a failure: the other fire is
   making the progress. Output beginning `legacy:` → this run predates per-run state (Plan 03
   migrates it); proceed, the Tier-B driver's own guards are the whole story there. Otherwise you
   own the run — and steps 1b and 2-3 below, which merge the run branch and rewrite
   `assertions/<slug>/run/results.json`, are now yours alone. **Release on EVERY exit path:**
   `conductor own release` before the final handoff in step 3b and before the handoff that ends an
   ordinary fire. Releasing twice is a no-op, so an early exit that releases is always safe.
   This is ownership of the RUN, not of a phase. Step 5's `ledger.claim` still decides which phase
   you may work on, and the two never override each other: ownership is what admits you to the
   fire, the claim is what assigns you the work.
```

and add `conductor own release` to step 3b's terminal sequence and to the ordinary end-of-fire handoff.

- [ ] **Step 7: Run everything**

```bash
pytest -q
./bin/conductor gate verify
./bin/conductor own status --run <a-real-run-key>   # from a checkout with per-run state
ruff check . && ruff format --check conductor/own_cmd.py conductor/resume_script.py tests/conductor/test_own_cmd.py && pyright .
```
Expected: suite green with the new tests counted; `gate verify` clean (A12 observes the edited `SKILL.md`).

- [ ] **Step 8: Falsify**

**Falsifier:** in `cmd_acquire`, change the `if args.if_free:` branch to `return EXIT_FAIL`. `test_if_free_against_a_live_owner_exits_zero_and_prints_skip` must fail. Revert. This is the falsifier that matters operationally — the wrong exit code here turns every overlapping fire into a logged failure and trains the operator to ignore the log.

Second falsifier: delete the `legacy:` early return. `test_a_repo_with_no_registry_reports_legacy_and_exits_zero` must fail. Revert.

Third falsifier: in `_resolution`, make the multi-run case return the first key instead of raising. `test_two_active_runs_without_run_refuse_rather_than_guess` must fail. Revert.

- [ ] **Step 9: Bump the plugin version and commit**

Editing a shipped `SKILL.md` is a behaviour change, and `claude plugin update` no-ops unless `.claude-plugin/plugin.json`'s version moves.

```bash
git add conductor/own_cmd.py bin/conductor conductor/resume_script.py skills/autodev/SKILL.md tests/conductor/test_own_cmd.py .claude-plugin/plugin.json
git commit -m "conductor/own_cmd.py:1-210, skills/autodev/SKILL.md:26-38, conductor/resume_script.py:13,221-231 — wire per-run ownership into both tiers

- autodev takes the run lease before its prologue; the generated driver acquires and releases it too
- a fire skipped because another fire owns the run exits 0; legacy flat state is untracked until Plan 03"
```

---

## Where this plan corrects the roadmap and the design

Recorded rather than papered over, so a later reader comparing the plan to its sources does not assume drift.

**1. `owner.lock` cannot hold the ownership fields.** Design line 184 reads as though the run key, host, identities, lease, and heartbeat id live inside `owner.lock`. Plan 01's durable-write contract finishes every write with `os.replace`, which installs a new inode; a process holding `flock` on the old one keeps holding a file nothing will reopen, while every later acquirer locks the new inode uncontended. The mutex would stop excluding anything with nothing failing. The lock is a zero-byte mutex; the fields live in a sibling `owner.json` it guards. One authority, two files.

**2. `owner.lock` cannot be held for the whole fire on both tiers, and exclusion therefore rests on the record.** Design line 190 has a wrapper hold the lock across its launched host's lifetime. That is exact for the OS-cron tier. The in-session tier — `/conductor:autodev` inside a live REPL — has no process between the host and the work that outlives a tool call, so a `conductor own` subprocess takes its `flock` to the grave. Both tiers therefore refuse on a *live record*, and the wrapper tier may additionally hold the lock, which is strictly stronger. Design line 186's "expiry is necessary but never sufficient" is what makes this safe: a record outliving its holder is settled by `prove_exited`, not by a timer.

**3. The prune verb is `conductor own prune`, not `conductor heartbeat prune`.** Design line 198 names a verb group Plan 05 owns and that does not exist. Plan 05 may alias it.

**4. `OwnerRecord` has ten fields, not the roadmap's six**, and `heartbeat_id` is `str | None`. Each addition is justified in Task 1 against a specific invariant: `workstation_id` (cross-machine refusal without consulting the mirror), `tier` (the two tiers have different liveness stories), `posture` (bypass non-transfer needs the prior value recorded), `renewed_at` (a hung wrapper is otherwise indistinguishable from a healthy one).

**5. The roadmap's dependency table is wrong: Plan 02 depends on 01 *and* 04.** `prove_exited` calls Plan 04's `process_alive`, and Plan 04's front matter says so from the other side. Tasks 4–10 cannot go green until `conductor/hosts/` exists.

**6. Plan 02 is not `registry.update`'s first production caller, and cannot be.** The residuals expected rebind to be it. `registry.update` → `registry.commit` takes `project.lock` internally (`registry.py:68`), and rebind must hold `project.lock` across every owner and state lock, so calling it there is a re-entrant acquisition `locks._check_order` correctly refuses. Rebind commits through `transaction`, as `repoint` does. The residual's *other* half — the misreported "no write occurred" at `registry.py:113-116` — is fixed in Task 8 regardless.

**7. The mirror-convergence residual cannot be discharged by this plan.** `registry.mirror_status` converging only on `cmd_new` is real, but Plan 02 mutates no run *status*: takeover swaps hosts, prune and rebind clear ownership. There is nothing here to prove a `commit_run_and_mirror` helper on, and shipping an unused one is padding. Task 8 establishes the shape — one journal, a lock hint on every run entry, sorted order, `project.json` in the same transaction — and Plan 05's `resume`/`finish` copy it.

**8. The roadmap overstates the prologue gap.** It reads as though two concurrent fires can freely interleave `index.lock` and `results.json` writes. `conductor/resume_script.py:209-210` already holds a whole-fire `flock`, so OS-cron × OS-cron is guarded today. The real defects are narrower and are stated in §"The gap this plan closes": the in-session tier takes nothing, the cross-tier guard is a one-sided TOCTOU snapshot matching one host by process name, and the lock that exists is project-scoped rather than run-scoped — which defeats Plan 01's multi-run premise from the other direction.

**9. The design does not settle run-lease versus phase-claim at all.** §"Ownership, locking, and takeover" never mentions `ledger/claim.py`, which has shipped and works. §"The central question" above states the layering, the precedence, and two grep-checkable anti-requirements, and flags this as a design gap rather than an inference.

---

## Residuals this plan knowingly leaves

- **The prologue gap stays open for the currently-live legacy run.** A flat `.conductor/{goal.md,run_branch}` run has no run key to key a record on. `own acquire` reports `legacy:` and does nothing. **Plan 03 closes it**, and inventing a synthetic key here would collide with Plan 03's obligation to preserve exact branch names.
- **`resume.lock` is still project-scoped.** Two independent runs in one repository still serialise their OS-cron fires against each other. Narrowing it is Plan 05's, together with the driver rewrite.
- **The `pgrep -f 'claude'` double-drive guard stays.** It is one-sided, TOCTOU, and blind to a Codex worker. Plan 04's `processes_under` is the replacement and Plan 05 wires it. Plan 02 strictly adds a guard rather than replacing one.
- **In-session leases are not renewed on a timer.** A model loop cannot promise a 30-second cadence. The lease lapses during a long phase, and the holder is then judged by `process_alive(wrapper_identity)` against the REPL's own long-lived process. Correct, but it means an in-session lease's *expiry* carries almost no information; only its identity does.
- **`_default_kill` sends `SIGTERM` and does not escalate.** A launched host ignoring `SIGTERM` leaves the orphan in place and `prove_exited` refuses to clear. That is the fail-closed direction, but there is no `SIGKILL` path and no operator affordance beyond doing it by hand.
- **`repoint` still holds `project.lock` plus N owner plus N state locks across up to two 30-second git subprocesses.** Named in Plan 01's residuals; Task 9 deliberately did not fold the timeout change in.
- **`CONDUCTOR_HELD_LOCKS` is inherited, not verified.** A child trusts the environment. A stale value from a parent that already released would refuse a legal acquisition — fail-closed, but confusing. There is no liveness check on the encoded entries.
- **`assertions/run.py:169-172` still writes `results.json` non-atomically.** The ownership lease now prevents two *fires* from interleaving those writes, which is the failure that was actually reachable. The write is still a plain truncate-and-rewrite, so a crash mid-write leaves a torn file. Converting it to `atomic.write_json_atomic` is a one-line change nobody has scheduled.
- **`takeover`'s `reconcile` hook is called but nothing implements it.** Git, GitHub, worktree, and test reconciliation are Plans 05 and 06. The ordering is established and tested; the content is not there.
- **No test races a real takeover against a real firing heartbeat.** Design line 518 requires that integration test. It needs Plan 05's heartbeat to exist.

---

## Definition of done for this plan

- [ ] `pytest -q` green with the new tests counted. Record before/after counts in the PR, as Plan 01 did.
- [ ] `./bin/conductor gate verify` clean — A1–A16 unchanged and unweakened, including A12 over the edited `skills/autodev/SKILL.md`.
- [ ] `ruff check .` clean; `ruff format --check` clean on **every file this plan created or modified** (not repo-wide — 11 pre-existing files fail).
- [ ] `pyright .` reports no new errors.
- [ ] **The two layers stayed separate:**
      `grep -rn 'conductor\.core\.ownership\|core import ownership' ledger/` returns nothing, and
      `grep -rn 'ledger\.' conductor/core/ownership.py conductor/core/rebind.py` returns nothing.
- [ ] **The lock order is unchanged:** `git diff -- conductor/core/locks.py | grep -n 'LOCK_ORDER'` shows no change to the tuple.
- [ ] **No host string escaped into core:** `grep -rn 'dangerously-skip-permissions\|dangerously-bypass-approvals\|CLAUDE_PLUGIN_ROOT\|codex exec' conductor/core/` returns nothing.
- [ ] **Every falsifier was run.** For each task, the named edit was applied, the named test observed failing, and the edit reverted. **State this explicitly in the PR.** Plan 01 found four tests that could not fail; a recent branch shipped three more.
- [ ] `./bin/conductor own status --run <key>`, `own acquire --if-free`, and `own prune --run <key>` all work from a clean checkout with per-run state, and `own acquire --if-free` prints `legacy:` and exits 0 in a repo with no registry.
- [ ] `.claude-plugin/plugin.json` version bumped (Task 10 changes a shipped skill).
- [ ] The roadmap's dependency table (line 73) is corrected to `01, 04`.

---

## Self-review

**Spec coverage.** Design §"Ownership, locking, and takeover": one worker owner per run → Task 2; `owner.lock` records identity/lease/heartbeat → Task 1 (with correction 1); `run.json` fields as diagnostic mirror → Tasks 2, 7; lease defaults and the quarter-lease renewal rule → Task 2; expiry necessary-not-sufficient → Tasks 2, 4; the seven-step takeover → Task 5, with step 6 explicitly deferred to Plan 05; orphan refusal and `--terminate-orphan` → Tasks 4, 7; prune lock ordering → Task 7; cross-machine refusal → Task 5; rebind → Task 8. §"Failure handling" lease/marker paragraphs → Tasks 2, 4, and the anti-requirements in §"The central question". §"Unit and contract tests": lock/lease/PID-reuse (line 492) → Tasks 2, 4; orphan refusal, prune ordering, identity-matched termination, cross-machine refusal (line 493) → Tasks 4, 5, 7; rebind (line 494) → Task 8; takeover invalidating a review authored by the new worker host (line 498) → Task 5; permission profiles and bypass non-transfer (line 499) → Task 6.

**Deliberately deferred, with the owning plan named inline:** reconciliation content and heartbeat entry replacement (Plan 05), migration of legacy flat state (Plan 03), `process_alive`/`permission_profile` implementations (Plan 04), verdict schema and debt (Plan 07), narrowing `resume.lock` and retiring the pgrep guard (Plan 05).

**Placeholder scan.** No step says "TBD", "add appropriate error handling", "handle edge cases", or "write tests for the above". Every code step carries code. Three deliberate abbreviations, each naming exactly what to copy: Task 10's `renew`/`release`/`status`/`prune`/`takeover` subcommands follow `cmd_acquire`'s written shape with the stated exception mapping; Task 9's two test helpers reuse `test_repoint.py`'s existing equivalents rather than duplicating them; Task 8's `second_run` fixture mirrors the written `state_root` fixture with a second spec path. None hides a decision.

**Type consistency.** `identity` is always `"<host>:<pid>:<start-ticks>"` and is minted only by Plan 04's `process_identity` — Task 4 states this and no task mints one. `wrapper_identity` and `host_identity` are the two identity fields everywhere; neither is ever a bare PID. `state_root` is the `.conductor` directory, never the repository root — Plan 01's convention. `posture` is one of Plan 04's `POSTURES` and is never a host flag. `tier` is one of `ownership.TIERS` and never a host id. `load` is the same injected `HostAdapter` factory in `inspect`, `prove_exited`, `takeover`, `prune`, `rebind`, and `repoint`. `OwnerRecord` is constructed in exactly three places (`acquire`, `takeover`, `read`) and mutated only through `._replace`. `_mirror` takes `state.lock`; `_mirror_locked` assumes the caller holds it — Task 7 introduces the split and re-runs Task 2's tests to prove nothing regressed.

**One thing a reviewer should push on.** Task 3 modifies `conductor/core/locks.py`, which Plan 01 owns and this plan otherwise leaves alone. The justification is that Plan 02 creates the *first* cross-process lock hold, so the invariant becomes decorative exactly where Plan 02 puts weight on it. If a reviewer judges that out of scope, the alternative is to forbid the wrapper tier from invoking `conductor` verbs inside its hold — which is not enforceable and would be violated by the first person who did not read this paragraph. The change does not touch `LOCK_ORDER`, and the definition of done greps for that.

**A second thing worth pushing on.** Tasks 4–10 cannot be executed until Plan 04 merges. If Plan 04 slips, the tempting move is to write a small `/proc` reader inside `conductor/core` "just for now". Do not. A second identity format reintroduces PID reuse, and the two would disagree the first time a host's start-tick parsing differed. Ship Tasks 1–3 and wait.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-10-plan-02-ownership-takeover.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration. Carry each task's falsifier into the reviewer's dispatch and have the reviewer build its own revert-proof rather than trusting the implementer's report; Plan 01's residuals record three implementer reports on one branch that claimed coverage which did not exist, all three caught this way. Carry findings from one task into the next task's dispatch and ask whether they have analogues — that practice found the defects the suite did not.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
