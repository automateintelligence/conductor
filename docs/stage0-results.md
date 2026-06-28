# Stage 0 — framework-validation results

**Gate (design §11):** `E0–E5` must pass and be recorded before any `/writing-plans` or
MVP build. **E5 locks the composition** (Option 1 in-session `/loop` vs Option 2 cloud
`/schedule` watchdog).

**Run context:** repo `automateintelligence/conductor` (private); `gh` v2.4.0 (sub-issues
via `gh api`); launcher `claude` v2.1.195 (`-p` headless verified).

| # | Experiment | Pass criteria (§12) | Status |
|---|---|---|---|
| E0 | loop fires stub, self-stops on machine gate | fires on interval; self-stops when green; never asks | ✅ pass |
| E1 | survives fresh context (no `/clear`/`/compact`) | fires continue across fresh context; state from disk; counter correct | ✅ pass |
| E2 | subagent dispatch; `baseline..final` bracket; thin session | commit captured by SHA bracket; main context small | ✅ pass |
| E3 | gh ledger ops + state model + reconcile repair | hierarchy + label/assignee transitions correct; reconcile repairs invalid combo | ✅ pass |
| E4 | assertion spec → runnable test + runner (the crux) | red→green; exit codes correct; **unrunnable = NOT done** | ✅ pass |
| E5 | end-to-end micro-spec, BOTH orderings | ≥1 reaches green with zero intervention → **lock composition** | ⏳ pending |

**Legend:** ⏳ pending · 🔄 in progress · ✅ pass · ❌ fail · ⚠️ partial/conditional

---

## E0 — loop fires & self-stops on a machine gate — ✅ PASS

**Setup:** `experiments/E0-loop-selfstop/step.sh` — each fire increments `run/counter.txt`;
the "spec" flips the gate (`run/gate.txt` → `DONE`) after `THRESHOLD=3` fires; a one-line
assertion `grep -q DONE` sets the exit code (`0`=green/done, `1`=red), matching the §5.2
done-gate semantics.

**Live run:** harness cron `ac6397dc` (`* * * * *`, session-only) fired the stub on the
minute; each fire was a separate idle re-invocation that read the counter from disk:
- fire 1 → `gate=RED` (exit 1) — continue
- fire 2 → `gate=RED` (exit 1) — continue
- fire 3 → `gate=GREEN` (exit 0) — gate satisfied → **autonomous self-stop**:
  `CronDelete(ac6397dc)` + wrote `run/STOPPED` (fire=3). `CronList` then returned
  **"No scheduled jobs."**

**Never asked** a question on any fire. Also pre-validated deterministically
(fires 1–4 → RED/RED/GREEN/GREEN; green is idempotent).

**Verdict:** fires on interval ✓ · self-stops when the machine gate goes green ✓ · zero
intervention ✓.

## E1 — fresh-context survival — ✅ PASS

Two complementary forms of evidence that the loop survives a wiped context, continuing
purely from disk:

**(a) Cron re-invocations (from E0).** Each of the 3 cron fires was a separate idle
re-invocation; the counter persisted on disk and advanced 1→2→3 with no reliance on one
continuous context.

**(b) Fresh `claude -p` processes (the real relaunch mechanism — amendment A).** Disk
state seeded mid-run (`counter=1`, no session aware of it). Two brand-new headless
sessions (`--no-session-persistence`, zero shared memory) each ran the stub:
- fresh #1 → read `counter=1` from disk → `fire=3 gate=GREEN`, disk `counter=3`
- fresh #2 → `fire=4 gate=GREEN`, disk `counter=4`; gate `DONE`

A new process is a *stronger* reset than `/clear` (a whole new context window, not just a
transcript wipe) and is automatable (unlike a user `/clear`). Literal `/clear` survival of
cron `/loop` was prior-verified 2026-06-27 (design §4).

**Caveat:** fresh #1 ran the one-line stub twice (counter 1→3) — the nested agent re-ran
the command; harmless for an idempotent counter stub, but a real `/autodev` must run its
phase exactly once per fire (already the §6 contract).

**Verdict:** fires continue across fresh contexts ✓ · state from disk ✓ · counter correct ✓.
Resolves the `/clear` question: **we relaunch, we don't clear.**

## E2 — subagent bracket, thin session — ✅ PASS

`baseline_revision = 1011229` captured before dispatch; a fresh general-purpose subagent
did one unit of work (wrote `experiments/E2-subagent-bracket/marker.txt`, staged just that
file, committed). `final_revision = ca4a3d4`.

- `git log 1011229..ca4a3d4` = exactly one commit — the subagent's. The bracket *is* what
  it produced (an equal range would mean it did nothing — §6).
- That commit touches only `marker.txt` (1 file, 1 insertion).
- Main session stayed thin: it received only a short SHA + log summary, not the subagent's
  working context.

**Verdict:** commit captured by `baseline..final` SHA bracket ✓ · main context small ✓.
This is the §6 mechanism that lets a fresh-context fire learn what the previous unit did
without trusting a chat message.

## E3 — gh ledger ops + state model — ✅ PASS

**Hierarchy (issue-sync generate):** milestone `#1 "E3 mini plan"` → phase issues
`#1 Phase A`, `#2 Phase B` → 3 **real GitHub sub-issues** `#3/#4/#5` under Phase A.
Sub-issues API worked: `gh api --method POST repos/.../issues/1/sub_issues -F sub_issue_id=<db_id>`,
verified `sub_issues_summary.total = 3`. Phase A also carries the task checklist (both
representations exist).

**Status-label + assignee transitions (the two §6 axes):** Phase A `status:ready` →
**claim** (assignee + `status:in-progress`) → **merged-PR sim** (`status:done` + closed);
Phase B stays `status:draft`.

**Reconcile repairs invalid combo per §7 precedence (git/tests > PR > issue-label):**
- Round 1 — issue `done`+closed but assertion RED → conflict detected → **repaired**:
  reopen → `status:in-progress` (reconcile exit 10).
- Round 2 — same `done`+closed but assertion GREEN → **permitted** (reconcile exit 0).
- Issue-label held constant across rounds; only the test result varied → precedence shown
  both directions.

**Verdict:** hierarchy + label/assignee transitions correct ✓ · reconcile repairs the
invalid `done`+red combo and permits `done`+green ✓.

**Tooling note (feeds the plan → amendment D):** gh 2.4.0 has **no `gh label` and no
`gh issue` sub-issue subcommands** — issue-sync must drive labels and sub-issues through
`gh api`. The CLI version is irrelevant for `gh api` (it just proxies HTTP).

## E4 — assertion runner (the crux) — ✅ PASS

**Built test-first (TDD):** spec "answer()→42" in `assertions/answer/`.
- RED: `pytest` → `ModuleNotFoundError`, exit 2 (commit `f2c0ae3`).
- GREEN: add `answer.py` (`return 42`) → `1 passed`, exit 0 (commit `c1ea6ab`).

**Runner contract (§5.2):** `bin/conductor assert run` → `assertions/run.py` reads
`assertions/manifest.yaml` (id→command/setup/timeout/level), runs each under a hard
subprocess timeout, prints per-id `[PASS]/[FAIL]` + aggregate, writes
`assertions/run/results.json` (gitignored). Exit map: `0` all green · `1` ≥1 red ·
`2` manifest missing · `3` manifest unparseable · `124` timeout · `127` exec-error
(commit `1011229`). No hard pyyaml dep (guarded import + fallback parser).

**Fail-closed (independently re-verified):**
- happy path → `[PASS] answer-42`, **exit 0**.
- broken dep (answer.py moved) → `[FAIL] answer-42 (rc=2)`, **exit 1** — NOT green.
- missing manifest → `[GATE] FAIL: manifest missing`, **exit 2** — NOT green.

**Verdict:** red→green ✓ · per-id + aggregate exit codes correct ✓ · unrunnable / missing /
crash / timeout = NOT done, never green-by-default ✓. This is the machine-checked
done-gate the loop's terminal condition depends on (§5.1).

## E5 — end-to-end, both orderings → composition lock
_pending_

---

## Stage 0 verdict
_pending — filled after E0–E5._

**Composition lock (from E5):** _pending._

**Design amendments:** see `docs/stage0-notes.md` (A: fresh-context via `claude -p`;
B: `/conductor` reconcile-first idempotency).
