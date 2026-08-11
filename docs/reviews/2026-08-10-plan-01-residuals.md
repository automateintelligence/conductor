# Plan 01 residuals — run identity and project registry

**Branch:** `worktree-plan-01-run-identity` (47 commits, `57ebe13..db51b0c`)
**Plan:** `docs/superpowers/plans/2026-08-10-plan-01-run-identity-registry.md`
**Suite at merge:** 835 passed, 1 skipped (baseline 565/1). `./bin/conductor gate verify` intact.

Every task had a scoped review; a whole-branch review then ran 105 mutations (86 killed) and a
final fix wave closed its six Important findings. This file records what was deliberately left,
so Plans 02–07 inherit the knowledge rather than rediscovering it.

## Constraints Plan 02 must honour

**`transaction.recover` writes `run.json` holding only `project.lock`.** All five call sites
(`registry.py:52,72`, `resolve.py:127`, `run_cmd.py:158`, `repoint.py:182`) hold `project.lock`
alone; `_write_image` rewrites run records with neither `state.lock` nor a revision check. This
is documented at `conductor/core/transaction.py:19-32` and is currently latent — nothing in
production calls `runstate.commit/update/set_status`. **Plan 02's heartbeat and lease writers are
exactly those callers.**

Taking `state.lock` inside `recover` is necessary but *not sufficient*: the journal's after-image
still overwrites a concurrent writer's committed revision, and that writer's own compare-and-swap
will have succeeded, so the update is lost silently. A revision check cannot live in `recover`
either — verbatim after-image restore is what makes recovery idempotent. **Plan 02's writers must
be designed against recovery, not merely locked against it.**

(The `transaction.py` header also argues a dependency inversion. The final re-review checked the
import graph and found no cycle would result — `registry`/`repoint`/`resolve` sit above
`runstate`, not below. The real cost is layering: `transaction` is deliberately generic over
opaque absolute paths. Correct conclusion, overstated reason; do not quote it as impossibility.)

**The registry status mirror converges only when `cmd_new` runs.** `registry.mirror_status` has
one production caller, `run_cmd._reconcile_mirror`. A project whose runs all ended and never
starts another keeps a permanently wrong `project.json`. Plan 05's `resume`/`finish` should write
the mirror in the same transaction as the record. `registry.py:14-17` states the doctrine: the
mirror never carries a decision; `run.json` is authoritative.

**`locks._check_order` permits a keyed/unkeyed mix of the same kind.** No Plan 01 caller mixes
them; Plan 02 is the first that could.

**Cross-machine and lease semantics are absent by design.** Plan 01 treats a busy `owner.lock`
as a flat refusal. The acquisition order (`project` → `owner` → `state`, run locks in sorted
run-key order) is established and tested; Plan 02 replaces the refusal with liveness
interpretation without changing that order.

## Known-latent behaviour

- **`registry.update`'s exhausted-attempts `RevisionConflict` still hardcodes "no write
  occurred"** (`registry.py:113-116`) — the fifth recover-then-refuse site, missed by the fix
  wave. Requires genuine contention across all five attempts, and `registry.update` has zero
  production callers today.
- **`_write_failure`'s fallback can make a false claim** (`run_cmd.py:463-465`): it swallows the
  `ValueError` from a corrupt journal and reports "no transaction is pending", which is false
  when the journal exists but is unreadable. Needs a third phrase.
- **`except OSError` reports "failed while writing state" on read-only verbs** — an unreadable
  `run.json` on `conductor run show` claims a write. Scope it by verb.
- **Run-key gate mode has no gate-*enforcing* consumer.** `assertions/run.py:52` and
  `conductor/freeze.py:298` still call `resolve_gate(root)` with no key; the only run-key caller
  prints a directory. None of the run-key gate safety is load-bearing until Plan 03 retires the
  legacy path.
- **`paths.run_gate_dir` and `paths._resolve_gate_by_run_key` answer the same question
  differently** for a `legacy-slug-v1` record. `run_gate_dir` has no consumer; delete it or make
  it take the record and delegate.
- **`repoint` holds `project.lock` + N owner + N state locks across up to two git subprocesses**
  (`_GIT_TIMEOUT = 30.0` each). Worst case ~60s of held locks. Correct but the timeout should be
  a few seconds on that path.
- **`resolve.repo_root` costs 9 git subprocesses on `conductor run new`**, 2 each on `list` and
  `resolve`. `repo_identity` is computed even when `registry.init` discards it. Memoise the whole
  resolution before Plan 05's cron callers.
- **`repoint` never refreshes `spec_digest`**, so after a rename-plus-edit only rename detection
  can authorise a later repoint of the same run. `_committed_rename` also has no similarity floor,
  making it a wider gate than the staged path.
- **Two genuinely distinct specs with identical bytes cannot both start a run** — the
  byte-identical guard matches runs of any status. As specified; no override exists.

## Method notes

Nine of thirteen tasks had a genuine defect in the plan's own reference code — including a
data-loss path, an unrecoverable orphan state, a verb that could not work in production, and
**four tests that could not fail**. None was found by the suite passing or failing; every one was
found by an implementer or reviewer asking *would this fail if the code under test were reverted?*

Two practices earned their cost and should carry forward:

1. **Reviewers built their own revert-proofs** from scratch overlays outside the repository
   rather than trusting implementer reports. Three reports on this branch claimed coverage that
   did not exist; all three were caught this way.
2. **Findings from one module were carried into the next task's dispatch.** Task 8 was asked
   whether Task 7's two defects had analogues, and to show its reasoning either way — the
   reviewer then verified the reasoning rather than the conclusion.
