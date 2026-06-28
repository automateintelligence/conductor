# Stage 0 — framework-validation results

**Gate (design §11):** `E0–E5` must pass and be recorded before any `/writing-plans` or
MVP build. **E5 locks the composition** (Option 1 in-session `/loop` vs Option 2 cloud
`/schedule` watchdog).

**Run context:** repo `automateintelligence/conductor` (private); `gh` v2.4.0 (sub-issues
via `gh api`); launcher `claude` v2.1.195 (`-p` headless verified).

| # | Experiment | Pass criteria (§12) | Status |
|---|---|---|---|
| E0 | loop fires stub, self-stops on machine gate | fires on interval; self-stops when green; never asks | ⏳ pending |
| E1 | survives fresh context (no `/clear`/`/compact`) | fires continue across fresh context; state from disk; counter correct | ⏳ pending |
| E2 | subagent dispatch; `baseline..final` bracket; thin session | commit captured by SHA bracket; main context small | ⏳ pending |
| E3 | gh ledger ops + state model + reconcile repair | hierarchy + label/assignee transitions correct; reconcile repairs invalid combo | ⏳ pending |
| E4 | assertion spec → runnable test + runner (the crux) | red→green; exit codes correct; **unrunnable = NOT done** | ⏳ pending |
| E5 | end-to-end micro-spec, BOTH orderings | ≥1 reaches green with zero intervention → **lock composition** | ⏳ pending |

**Legend:** ⏳ pending · 🔄 in progress · ✅ pass · ❌ fail · ⚠️ partial/conditional

---

## E0 — loop fires & self-stops on a machine gate
_pending_

## E1 — fresh-context survival
_pending_

## E2 — subagent bracket, thin session
_pending_

## E3 — gh ledger ops + state model
_pending_

## E4 — assertion runner (the crux)
_pending_

## E5 — end-to-end, both orderings → composition lock
_pending_

---

## Stage 0 verdict
_pending — filled after E0–E5._

**Composition lock (from E5):** _pending._

**Design amendments:** see `docs/stage0-notes.md` (A: fresh-context via `claude -p`;
B: `/conductor` reconcile-first idempotency).
