# Plan 01 residuals — run identity and project registry

**Branch:** `worktree-plan-01-run-identity` (47 commits, `57ebe13..db51b0c`)
**Plan:** `docs/superpowers/plans/2026-08-10-plan-01-run-identity-registry.md`
**Suite at merge:** 835 passed, 1 skipped (baseline 565/1). `./bin/conductor gate verify` intact.

Every task had a scoped review; a whole-branch review then ran 105 mutations (86 killed) and a
final fix wave closed its six Important findings. This file records what was deliberately left,
so Plans 02–07 inherit the knowledge rather than rediscovering it.

## Constraints Plan 02 must honour

**`transaction.recover` now locks and refuses to regress — both halves, closed.** This entry
previously recorded the hazard as open and deferred it to Plan 02. A codex review escalated it
(revision *reuse* makes it an ABA, not merely a lost update), and it was fixed here instead.

`recover` takes the per-run `state.lock` for every run its journal touches, in sorted run-key
order, after the caller's `project.lock`. It cannot derive a lock path from an opaque absolute
target path, so each entry carries one: `{"lock": {"path": ..., "run_key": ...}}`, written at
`prepare` time by `run_cmd.cmd_new` and `repoint.repoint`. **An entry that writes a `run.json`
without a lock hint is a bug** — it replays with no serialisation against that run's writers.

Locking alone was never sufficient, which is the half worth carrying forward. Verbatim replay is
what makes recovery idempotent and is also what made it dangerous, so `_write_image` now converges
on the file rather than the journal: an image is applied only when it moves the revision forward
(`_regresses`). A second replay sees its own result; a file that is genuinely ahead is left alone
and that transaction is dropped as superseded. **Plan 02's heartbeat and lease writers inherit
both rules: prepare with a lock hint, and never assume an unapplied journal will win.**

`prepare` additionally refuses while another journal is pending. Journal ids are caller-supplied
strings replayed in sorted order — lexicographic, not causal — so two overlapping journals would
have let the id that happens to sort last decide the final image.

(The `transaction.py` header used to argue a dependency inversion. The import graph shows no cycle
would result — `registry`/`repoint`/`resolve` sit above `runstate`, not below. The real cost was
layering, and the lock-hint-in-the-journal shape is what preserved it: the module stays generic
over opaque absolute paths and learns the locks as data.)

**The registry status mirror converges only when `cmd_new` runs.** `registry.mirror_status` has
one production caller, `run_cmd._reconcile_mirror`. A project whose runs all ended and never
starts another keeps a permanently wrong `project.json`. Plan 05's `resume`/`finish` should write
the mirror in the same transaction as the record. `registry.py:14-17` states the doctrine: the
mirror never carries a decision; `run.json` is authoritative.

**`locks._check_order` keyed/unkeyed mix — closed.** Re-entrancy is now decided by the resolved
lock FILE, not by `(kind, run_key)`. The old key was wrong in both directions: two `project.lock`
files under different state roots were falsely refused as re-entrant, and one file taken under two
different kinds was allowed straight into a self-deadlock.

**Cross-machine and lease semantics are absent by design.** Plan 01 treats a busy `owner.lock`
as a flat refusal. The acquisition order (`project` → `owner` → `state`, run locks in sorted
run-key order) is established and tested; Plan 02 replaces the refusal with liveness
interpretation without changing that order.

## Codex review (2026-08-11) — what it changed

A four-slice codex review of the PR found eleven [P1]s. Ten were real and are fixed in
`9d82e75`; the entries above and below are updated accordingly. Two things are worth keeping:

- **One finding was wrong, and the tests are what proved it.** Codex asked for
  `run_key == derive_run_key(spec_path, generation)`. That is false by design — `repoint`
  deliberately keeps the key when a spec is renamed so a live run's gate and branch do not move
  under it. Twelve `test_repoint.py` failures said so immediately. The invariant that IS true is
  membership: the key must derive from some path the document declares, current or in
  `path_history`, which still blocks pairing an arbitrary key with an arbitrary spec.
- **Two fixes made a corrupt state unrepresentable, which broke tests that needed to build it.**
  `test_repoint.py` constructed a divergent `run.json` through `runstate.update`; validation now
  refuses that. The corruption is still reachable in the wild (hand-edited, partially restored),
  so `_diverge_spec_path` writes the bytes directly. **When a validator closes a hole, check what
  the negative tests were using to open it** — the cheap move is to weaken the validator.

Fixed here and no longer open: the recovery lock/ABA pair, the lock re-entrancy key, the gate
refusal resolving onto another run's real gate, `runstate.commit`'s missing key/document
cross-check, unnormalized spec paths and `specs` keys, `runkey` containment on the resolved path,
`atomic`'s unfsynced parent levels and post-fsync `chmod`, `workstation`'s empty-then-filled
publish, and digest authorization spanning a whole mapping.

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
  making it a wider gate than the staged path. (Digest *authorization* was since narrowed: every
  generation in a mapping must consent, because the whole mapping moves. Refreshing is separate
  and still open.)
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
