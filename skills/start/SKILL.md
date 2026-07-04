---
name: start
description: Start (or resume) an autonomous conductor run for a spec. Reconcile-first and idempotent — re-invoking detects existing setup and resumes from the first incomplete step. Preflights the conducted stack, validates the done-gate precondition, turns the spec's assertions into tests, syncs the GitHub issue ledger, records the goal, and starts the cron loop.
---

# /conductor:start — preflight + set up + launch (§3, reconcile-first)

**Idempotent (amendment B): each step probes durable state first and SKIPS if already done.**

> **Conductor CLI path:** invoke it as `"$CLAUDE_PLUGIN_ROOT/bin/conductor"` (written `conductor`
> below). Installed plugins are not on `PATH`; if `$CLAUDE_PLUGIN_ROOT` is unset (dev/`--plugin-dir`),
> run the plugin's `bin/conductor` by absolute path and export `CONDUCTOR_PLUGIN_DIRS` with the
> spec-craft dir so preflight can see it.
>
> **Plugin dir vs project (where things live):** the plugin dir is read-only tool code; the **run
> state and the done-gate live in the PROJECT** — the git repo you invoke conductor from. **Run
> `/conductor:start` from the project root** (or set `CONDUCTOR_HOME=<project>`); the CLI resolves
> the project as the git repo of the current directory and writes `.conductor/` (goal, handoff),
> `assertions/manifest.yaml`, `assertions/.frozen`, and the RED tests **there**, git-committed with
> your project — never into the plugin cache. `conductor gate freeze` / `assert run` then operate on
> the project gate automatically; no `CONDUCTOR_MANIFEST` plumbing is needed when you run from the root.

0. **PREFLIGHT (`conductor preflight`).** Confirm every conducted command resolves (Codex #1):
   `/spec-craft:*`, `/superpowers:*`, and environment-provided `/code-review`, `/codex`,
   `/document-release`. Any **missing → STOP** and tell the user to install it (fail-closed,
   amendment E). Do not launch a loop that dies at the first conducted call.
1. **Detect spec source**; load spec + Expectations. The **executable-assertion specs** live in
   `<spec>.assertions.md` — the sibling file `/spec-craft:executable-assertions` writes — **not**
   inline in the spec; load them from there if it exists.
2. **PRECONDITION — assertion specs present?** "Present" = **`<spec>.assertions.md`** exists (the
   4-part specs from `/spec-craft:executable-assertions`) — do not look for the specs inline in the
   spec. If it exists the precondition is met: **use it as-is; it may have been hand-edited, so never
   re-run `/spec-craft:executable-assertions` over it** (that clobbers the edits). Absent → STOP and
   point the user at `/spec-craft:expectations` then `/spec-craft:executable-assertions` (or, with
   `--auto-assert`, launch them — which writes `<spec>.assertions.md`).
3. **Implement assertions as runnable tests** via `/conductor:assertions-to-tests`. **SKIP only if
   `start_probe.assertions_ready(expected_ids, "assertions/manifest.yaml", <assert-run --level spec
   exit>)` is True** — i.e. the manifest has one entry per `/spec-craft:executable-assertions` id
   AND the runner exit ∈ {0,1} (Codex #3). Otherwise (re)build it.
   **Then FREEZE the gate (§5):** `conductor gate freeze` records `assertions/.frozen` (commit it)
   so the worker cannot later weaken a check; the runner fail-closes (exit 6) if a frozen
   assertion or its test file changes. SKIP if `.frozen` exists and `conductor gate verify` is clean.
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit) in a fresh subagent — and
   PASS IT the spec, its `## Expectations`, and `<spec>.assertions.md` paths as required inputs.
   **The plan builds to the SPEC.** The executable assertions are only the mechanical done-floor
   that gates objective expectations; the spec's spirit and intent — architecture, behaviors,
   qualities — is the actual work, and there is far more of it than the assertions capture.
   The plan MUST carry every item below. `conductor plan-lint` mechanically enforces their
   **presence** (the floor); the step-4b codex review judges their **substance** — coverage
   and intent (the same division of labor as the done-gate itself):
   - a `**Normative spec:** <path>` header line (plus the assertions path) directly after the H1,
     stating the spec is normative over the plan on any conflict and that workers read the phase's
     `Spec:` sections **before** implementing;
   - phases as `## Phase N — Title (A-ids)`: scope each phase by SPEC sections/capabilities, then
     attach the assertion ids it must turn green in the trailing parens (issue-sync turns those
     into the ledger's machine-readable gate mapping). A deliberately gateless phase (rare —
     `phase-done` cannot gate-verify it) must declare `gate: none` in its section;
   - per phase: a `**Spec:** §N <section name>; …` pointer line and `- [ ]` task lines;
   - the per-phase recipe verbatim: subagent implement on a phase branch forked FROM THE RUN
     BRANCH → `/code-review` per task (against the phase's Spec sections, not just the diff) →
     commit per task → one PR per phase with **base = the run branch** (`Closes #<phase-issue>`)
     → codex review ×2 posted as "Codex review" PR comments → `conductor merge-gate` → merge
     into the run branch → `/document-release` → `conductor ledger phase-done`.
   SKIP if a plan/milestone exists.
4b. **LINT + CODEX-REVIEW THE PLAN** — it dictates every phase and must not stay the
   least-reviewed setup artifact. `conductor plan-lint <plan.md> --spec <spec.md>` must exit 0:
   fix the plan, never bypass the lint. Then codex-review the plan **against the spec** (does
   every spec section land in a phase? is intent preserved, not just assertion coverage?) and
   apply the fixes. SKIP only if both were already done for this plan.
5. **issue-sync** — `ledger.generate` (or `convert <plan.md>`; the parser reads the real
   `## Phase N — Title (ids)` dialect directly and writes each phase's `conductor-assertions`
   marker). SKIP if the hierarchy exists; else reconcile. Phases authored `[draft]` are created
   PARKED — `status:draft` blocks claiming; the owner promotes them to `status:ready` to schedule.
5b. **RUN TOPOLOGY (0.5.0 default): phase PRs merge into a run branch, NEVER directly to the
   default branch.** The default branch belongs to the owner; the run gets an integration branch
   reviewed ONCE, by the owner, at the end.
   - **Reconcile-first, EXACT name:** compute `conductor/run-<spec-slug>` from THIS spec's
     filename, then `git ls-remote origin refs/heads/conductor/run-<spec-slug>` — exists → reuse;
     absent → create off the default branch and push. NEVER bind by wildcard scan
     (`conductor/run-*`): with two active runs a scan grabs the wrong spec's branch.
   - **Stale-run cleanup first:** if `.conductor/run_branch` names a branch that no longer exists
     on the remote (the owner merged the final PR and deleted it — the run is over), remove the
     old run worktree (`git worktree remove <path>`) and the `run_branch` file BEFORE setting up
     this run; otherwise "SKIP if it exists" would resume against a finished run's leftovers.
   - **Write `<project>/.conductor/run_branch`** (single line, the branch name). This is what the
     merge-gate's expected-base leg reads — a phase PR targeting anything else blocks with
     `base-mismatch`. On a fresh clone the file is missing: re-derive it from the ls-remote above
     (this step IS the re-derivation — reconcile-first).
   - **Work in a WORKTREE:** `git worktree add ../<repo>-run-<spec-slug> conductor/run-<spec-slug>`
     — the worker and the Tier-B watchdog operate in the worktree (`CONDUCTOR_HOME` = worktree
     root), so the owner's own checkout is never branch-switched or dirtied by fires. SKIP if it
     exists.
   - **Probe default-branch protection** (`gh api repos/{owner}/{repo}/branches/<default>/protection`):
     404/absent → WARN the owner that nothing server-side stops a merge to the default branch;
     403 ("Upgrade to GitHub Pro…") → tell the owner protection needs a paid plan or a public
     repo, and that until then enforcement is conductor-side only (the base leg + skill rules).
     Best-effort light protection on the run branch (no force-push/delete) if the API allows.
   - **LOUD opt-out:** only `CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE=1` skips this step (no run branch,
     no run_branch file → the base leg stays disabled and phases merge straight to the default
     branch, 0.4.x-style). Print an unmissable warning when set: every phase merge lands on the
     owner's default branch with no final review point. Never default to it; never infer it.
6. **Record `/goal`** (`conductor goal set`) and **start the driver:** register a harness cron via
   **`CronCreate`** — `prompt: "/conductor:autodev"`, `cron: "*/7 * * * *"` (≈ every 7 min),
   `durable: true`. Record its id. SKIP if already registered. The interval is only a
   **heartbeat**: `CronCreate` fires **only while the REPL is idle**, so a tick never overlaps a
   running fire — it no-ops until the current phase finishes, so the interval need not match phase
   duration.
   **VERIFY durability — do not trust the flag.** Current CLI builds silently ignore
   `durable: true`: the response says "Session-only (not written to disk…)" and no
   `scheduled_tasks.json` appears (verified live 2026-07-02). If the response does NOT confirm
   persistence, the loop dies with the terminal — for an unattended run, **install the Tier-B OS
   fallback NOW; do not merely warn the user**:
   - write a resume script that runs `claude -p "/conductor:autodev"` from the RUN WORKTREE
     (step 5b — never the owner's checkout; autodev, not start — a headless one-shot session
     must do a phase, not register a cron that dies with it) and, in order: (a) **exits if any
     claude process is already running with cwd inside the worktree or project** — the live
     terminal's in-session cron is then the sole driver, so the two drivers can never
     double-fire; (b) **exits once `conductor assert run --level spec` is green** — a finished
     run gets no-op fires; (c) holds `flock -n <project>/.conductor/resume.lock` for the whole
     fire — no overlapping headless sessions;
   - add crontab entries carrying the LITERAL marker `# conductor-autodev <main-root>`, where
     `<main-root>` is `$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")` —
     the MAIN checkout root, which is IDENTICAL whether computed from the owner checkout or the
     run worktree (`--show-toplevel` is NOT: it returns the worktree path there, so install and
     removal would disagree). Removal greps for this exact fixed string. One `@reboot` line and
     one periodic heartbeat (e.g. `*/20 * * * *`).
   The marker tag is load-bearing: the autodev STOP branch removes exactly those lines when the
   gate goes green (see `experiments/E5-end-to-end/recovery.md`).
   **Tell the user one limit:** recurring in-session crons **auto-expire after 7 days** —
   re-invoke `/conductor:start` to extend a longer run (the Tier-B heartbeat does this itself).
7. **(Phase 2 only)** start the dispatcher loop — the supervisor that caps concurrency and assigns
   eligible phases to parallel workers. Single-loop needs no cap (`CronCreate` can't overlap fires);
   controlled parallelism is the dispatcher's job, not the cron cadence.

A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
