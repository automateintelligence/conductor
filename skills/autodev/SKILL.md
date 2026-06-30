---
name: autodev
description: The conductor worker. One fire = one phase of progress toward the spec's done-gate. Re-loads the goal, reconciles from durable state, runs the machine done-gate, claims and executes the next phase in a fresh subagent via the recipe, merges through the safety gate, writes a handoff, and exits. Driven by cron /loop; never ends the run itself.
---

# /conductor:autodev — one phase per fire (§8)

Autonomous. **Ask no questions.** Do exactly one coherent phase, then exit. The cron `/loop`
re-fires you; only a green done-gate (or an explicit escalation-halt) stops the run.

**The done-gate is frozen (§5).** Never edit an existing assertion in `assertions/manifest.yaml`
or a test file it references — the runner fail-closes (`exit 6`) on any change to a frozen check.
Make a red assertion green by implementing the **product**, never by weakening the check. Closing
a real coverage gap ADDS new assertions via `/conductor:assertions-to-tests`; it never edits or
deletes existing ones.

> **Conductor CLI path:** invoke it as `"$CLAUDE_PLUGIN_ROOT/bin/conductor"` (written `conductor`
> below); installed plugins are not on `PATH`.

1. **RE-LOAD GOAL (fresh context).** Done only when `conductor assert run --level spec` exits 0.
   Re-read goal + paths from the durable handoff/ledger; trust git/issues, not memory.
2. **RECONCILE (precedence git/tests > PR > label).** `ledger.reconcile(phase, ..., now_ts, L)`.
   The per-phase retry count is **durable** (issue body) and maintained by reconcile itself: a
   still-red live-owned phase is counted, and at the cap `retry-cap-exceeded` → `status:blocked`
   (escalates — a genuinely failing phase stops instead of looping every fire); `stale-lease-reclaim`
   resets it. PROGRESS SELF-CHECK.
3. **SPEC-DONE GATE.** `conductor assert run --level spec` (fail-closed; unrunnable = NOT done).
   **All green AND no plans left** → mark done, use **`CronList`** to find the driver cron, then
   **`CronDelete`** it, final handoff, STOP.
4. **PICK the next eligible phase** (unassigned & not blocked/done; climb the ladder):
   - phase available → SPLIT-CHECK (§6.1); else run the recipe.
   - plan done → `/superpowers:writing-plans` next plan → `ledger.generate` (or `ledger.convert`).
   - no plans left but assertions red → `/superpowers:writing-plans` to close the gap → generate.
5. **CLAIM.** `ledger.claim(phase, worker, now_ts, ttl)`. If False, back off and re-pick.
6. **EXECUTE the phase in a FRESH SUBAGENT** via the recipe (one PR per phase). Conducted skills:
   `/superpowers:*` are plugin skills; `/code-review`, `/codex`, `/document-release` are
   **environment-provided** commands (verified by `/conductor:start` preflight):
   1. `/superpowers:subagent-driven-development` to implement the phase's tasks.
   2. `/code-review` (self-review) per task. 3. **commit after every task.**
   4. **one PR per phase** (`Closes #<phase-issue>`).
   5. `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<n>`.
   6. `/superpowers:receiving-code-review` — apply Codex fixes, commit, comment on the PR.
   7. **merge ONLY if `conductor merge-gate <pr>` returns ok** (§6.2). Then `gh pr merge --merge`
      (no squash), or `--merge --auto` if a merge queue is configured. Gate blocks → resolve
      (e.g. rebase on `merge-state:BEHIND`) or escalate; **never force-merge**.
   8. `/document-release`.
   Capture `baseline_revision..final_revision` (equal = did nothing). Respect the per-fire budget
   (checkpoint+handoff if exceeded).
7. **ESCALATION (§9):** patch-later → `escalate.file_followup(debt|feature)`+link; continue.
   build-now → bounded deepen-in-place: `/superpowers:writing-plans` scoped → generate sub-plan;
   `escalate.block_on_subplan(phase)`; on completion `escalate.write_adr`. build-now AND needs
   human judgment → **halt** with handoff+issue (only branch that pages the user). Process failure
   → exit; next fire reconciles (§10).
8. **RECORD.** label/progress; commit; update `plan.md` index; renew or `ledger.release` the lease.
9. **WRITE HANDOFF (§4)** (`conductor.handoff.write`) to `.conductor/` (gitignored — local resume
   scratch only); then commit + **push** the code changes and ledger state. EXIT.
