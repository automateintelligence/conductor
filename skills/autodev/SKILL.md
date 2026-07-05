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

**Run infrastructure is OWNER-owned (same class as the frozen gate).** The Tier-B resume script,
its crontab lines, merge-gate env (`CONDUCTOR_MERGE_VERIFY`, `CONDUCTOR_MIN_REVIEWS`, …), the
driver-cron cadence, and anything under `~/.claude/scripts/` are guardrails around you — a worker
must NEVER modify them mid-run, however good the improvement looks (live finding 2026-07-02: a
worker rewrote its own watchdog unreviewed). Found a real defect in them? Escalate it —
`escalate.file_followup(debt)` with the proposed patch — and keep working. The only exception is
step 3b's terminal crontab removal.

> **Conductor CLI path:** invoke it as `"$CLAUDE_PLUGIN_ROOT/bin/conductor"` (written `conductor`
> below); installed plugins are not on `PATH`.

1. **RE-LOAD GOAL (fresh context).** Done only when `conductor assert run --level spec` exits 0.
   Re-read goal + paths from the durable handoff/ledger; trust git/issues, not memory. Read the
   run branch from `<project>/.conductor/run_branch`; file missing → recompute the EXACT name
   `conductor/run-<spec-slug>` from the goal's spec path and check `git ls-remote origin
   refs/heads/<that name>` — present → rewrite the file (fresh-clone reconcile); absent while
   the goal says topology is configured → HALT and escalate (never fall back to a wildcard
   scan or to direct default-branch merges).
1b. **KEEP THE RUN BRANCH CURRENT (every fire, before anything else builds).** On the run
   branch: `git fetch origin <default> && git merge origin/<default>` (MERGE, never rebase — a
   shared integration branch's history is load-bearing; phase branches may rebase, the run
   branch never does). Conflicts get resolved NOW, by you, in this small increment — or
   escalated — never left to accumulate for the owner's final review. If the merge brought
   changes, re-run `conductor assert run --level spec` before proceeding: gate-green must mean
   green against CURRENT reality, not day-1 reality.
2. **RECONCILE (precedence git/tests > PR > label).** `conductor ledger reconcile <n> --from-gate`
   — test state is **derived** from `assertions/run/results.json` via the issue's
   `conductor-assertions` marker; never hand-report `--tests-red` (worker-reported truth decays).
   Run `conductor assert run --level spec` first so results.json is fresh. A closed
   `status:done` phase whose gate is green is **terminal** — reconcile leaves it closed even
   without PR state (git/tests > PR); pass `--pr-merged` only when you have verified it.
   The per-phase retry count is **durable** (issue body) and maintained by reconcile itself: a
   still-red live-owned phase is counted, and at the cap `retry-cap-exceeded` → `status:blocked`
   (escalates — a genuinely failing phase stops instead of looping every fire); `stale-lease-reclaim`
   resets it. PROGRESS SELF-CHECK.
3. **SPEC-DONE GATE.** `conductor assert run --level spec` (fail-closed; unrunnable = NOT done).
   **All green AND no plans left** → the run is complete:
   **3a. OPEN THE FINAL OWNER PR (run topology only — skip when no run branch is configured).**
   Verify the run branch is not behind the default branch (step 1b just merged; re-check).
   Generate the review packet — `conductor run-packet <run-branch> > /tmp/packet.md` — then
   `gh pr create --base <default> --head <run-branch> --body-file /tmp/packet.md`, title
   "Conductor run complete: <spec-slug> — owner review", and assign the owner. **NEVER merge
   this PR — not with merge-gate ok, not with --admin, not at all.** It is the owner's single
   review point for the whole run; conductor's authority ends at opening it. (Where the repo
   has branch protection, the server enforces this; where it doesn't — free-plan private
   repos — this rule and the base leg are the enforcement.)
   **3b.** Mark done, use **`CronList`** to find the driver cron, then
   **CronDelete** it, AND remove any Tier-B OS fallback — the crontab lines carrying the
   literal marker plus their resume script:
   `crontab -l | grep -F -v -- "# conductor-autodev $(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")" | crontab -`
   (`grep -F` = fixed string; the MAIN-checkout-root path is identical from the run worktree and
   the owner checkout — `--show-toplevel` is not — so install and removal always agree) — else the heartbeat keeps firing no-ops forever.
   This removal is the ONLY sanctioned mutation of run infrastructure. The final handoff names the leftover run worktree and `.conductor/run_branch` — they stay
   until the owner resolves the final PR; the next `/conductor:start` reconcile removes them
   once the run branch is gone from the remote. Final handoff, STOP.
4. **PICK the next eligible phase** (unassigned & not blocked/done; climb the ladder):
   - phase available → SPLIT-CHECK (§6.1); else run the recipe.
   - plan done → `/superpowers:writing-plans` next plan → `ledger.generate` (or `ledger.convert`).
   - no plans left but assertions red → `/superpowers:writing-plans` to close the gap → generate.
5. **CLAIM.** `ledger.claim(phase, worker, now_ts, ttl)`. If False, back off and re-pick.
6. **EXECUTE the phase in a FRESH SUBAGENT** via the recipe (one PR per phase). **Build to the
   SPEC:** hand the subagent the plan's `Normative spec:` path plus this phase's `**Spec:**`
   sections and require reading them BEFORE implementing. The plan is a summary and the assertions
   are only the mechanical done-floor — the spec's spirit and intent is the work, so gate-green is
   necessary, never sufficient. Conducted skills: `/superpowers:*` are plugin skills;
   `/code-review`, `/codex`, `/document-release` are **environment-provided** commands (verified
   by `/conductor:start` preflight):
   0. **Reconcile-within-phase (restart safety):** diff the phase's `- [ ]` tasks against
      `git log` on the phase branch (per-task commits are the breadcrumbs) and the gate's
      per-assertion state; skip tasks already done. A dirty tree left by a dead worker: commit it
      to the phase branch as `wip: reclaimed partial work` — never discard it, never build over
      it blind.
   1. `/superpowers:subagent-driven-development` to implement the phase's tasks — on a phase
      branch forked from the RUN branch (never from the default branch when a run branch is
      configured).
   2. `/code-review` (self-review) per task — review against the phase's Spec sections, not just
      the diff. 3. **commit after every task.**
   4. **one PR per phase, base = the RUN branch** (`Closes #<phase-issue>` for traceability —
      merge-gate blocks without it, and its base leg blocks any other base with
      `base-mismatch`; run-branch merges don't auto-close issues — `phase-done` does that).
   5. `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<n>
      against the phase's Spec sections` — post the result as a PR comment starting with the gate's
      review marker (**`CONDUCTOR_REVIEW_MARKER`, default "Codex review"**).
      **Codex usage-limit fallback — continue uninterrupted, never stall.** If `/codex` reports its
      5-hour OR weekly usage limit is exhausted (its stderr/stdout names a usage/rate/quota limit,
      or `/status` shows the window spent — distinct from a transient timeout, which you retry ONCE
      first), do NOT halt and do NOT park the phase until the window resets: a spent WEEKLY quota
      would freeze the whole run for days, breaking the "walk away and it keeps making progress"
      contract. Fall back to `/code-review` for the independent pre-merge review. The gate is
      OWNER-CONFIGURED and you must NOT change its env, so honor two constraints as they are set:
        - **Marker (`CONDUCTOR_REVIEW_MARKER`, default `Codex review`):** post `/code-review`'s
          findings as the PR comment with that exact marker at the START of the body, labeled
          honestly — e.g. **"Codex review — UNAVAILABLE (usage limit); Claude /code-review
          fallback"** — so `conductor merge-gate` still counts it AND the degradation stays visible.
          Read the configured marker; do not assume it is the default.
        - **Provenance (`CONDUCTOR_REVIEW_AUTHOR`):** if it is pinned to a non-worker account, a
          worker-posted fallback can NEVER be counted — do NOT post unusable reviews in a loop;
          escalate needs-human instead (that config is incompatible with conductor's local-posting
          Codex flow anyway). Check this BEFORE falling back.
      Keep posting eligible final-state fallback reviews until `conductor merge-gate <pr>` passes —
      it needs `CONDUCTOR_MIN_REVIEWS` marker comments with the newest postdating the newest commit;
      do NOT assume that count is 2. Then **let the owner know** (the §9 *patch-later* branch, not a
      halt):
      `escalate.file_followup(repo, "debt", "Codex-fallback review: phase #<n>", body, link_issue=<phase#>)`
      with `body` naming the phase, the PR, which limit tripped (5-hour vs weekly), and that this
      phase traded Codex's independence for Claude's — flag it for optional independent re-review.
      The open `debt` issue rides the handoff's `Open:` line to the final owner PR, where YOU decide;
      it SURFACES the degradation, it does not silently repair it. Keep working.
   6. `/superpowers:receiving-code-review` — apply fixes, commit, then **codex re-reviews the
      FINAL state** (posted as another "Codex review" comment; if Codex is still usage-limited the
      step-5 fallback applies again — `/code-review` the final state under the same configured
      marker); repeat until the last review postdates the last commit and raises nothing blocking.
      merge-gate enforces both: `CONDUCTOR_MIN_REVIEWS` marker comments and review-of-final-state —
      the fallback satisfies them by running the SAME independent review rounds on the final diff,
      on `/code-review` instead of Codex.
   7. **merge INTO THE RUN BRANCH, ONLY if `conductor merge-gate <pr>` returns ok** (§6.2).
      Then `gh pr merge --merge` (no squash), or `--merge --auto` if a merge queue is
      configured. Gate blocks → resolve (e.g. rebase the PHASE branch on `merge-state:BEHIND`)
      or escalate; **never force-merge**. Then `git checkout <run-branch> && git pull`.
   8. `/document-release`.
   Capture `baseline_revision..final_revision` (equal = did nothing). Respect the per-fire budget
   (checkpoint+handoff if exceeded).
7. **ESCALATION (§9):** patch-later → `escalate.file_followup(debt|feature)`+link; continue.
   build-now → bounded deepen-in-place: `/superpowers:writing-plans` scoped → generate sub-plan;
   `escalate.block_on_subplan(phase)`; on completion `escalate.write_adr`. build-now AND needs
   human judgment → **halt** with handoff+issue (only branch that pages the user). Process failure
   → exit; next fire reconciles (§10).
8. **RECORD — MECHANICAL, one command.** Phase complete = `conductor ledger phase-done
   <phase-issue#> --plan <plan.md>`: it verifies the phase's gate assertions are GREEN
   (fail-closed), then labels `status:done`, closes task sub-issues, strips the lease, closes the
   issue, and ticks the plan's checkboxes. NEVER do these by hand — hand bookkeeping decays
   (dogfood: 0/27 checkboxes, labels never maintained). Phase incomplete this fire: renew the
   lease and commit progress.
9. **WRITE HANDOFF (§4)** (`conductor.handoff.write`) to `.conductor/` (gitignored — local resume
   scratch only); then commit + **push** the code changes and ledger state. EXIT.
