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
   The plan MUST carry (`conductor plan-lint` enforces every item):
   - a `**Normative spec:** <path>` header line (plus the assertions path) directly after the H1,
     stating the spec is normative over the plan on any conflict and that workers read the phase's
     `Spec:` sections **before** implementing;
   - phases as `## Phase N — Title (A-ids)`: scope each phase by SPEC sections/capabilities, then
     attach the assertion ids it must turn green in the trailing parens (issue-sync turns those
     into the ledger's machine-readable gate mapping);
   - per phase: a `**Spec:** §N <section name>; …` pointer line and `- [ ]` task lines;
   - the per-phase recipe verbatim: subagent implement → `/code-review` per task (against the
     phase's Spec sections, not just the diff) → commit per task → one PR per phase
     (`Closes #<phase-issue>`) → codex review ×2 posted as "Codex review" PR comments →
     `conductor merge-gate` → merge → `/document-release` → `conductor ledger phase-done`.
   SKIP if a plan/milestone exists.
4b. **LINT + CODEX-REVIEW THE PLAN** — it dictates every phase and must not stay the
   least-reviewed setup artifact. `conductor plan-lint <plan.md> --spec <spec.md>` must exit 0:
   fix the plan, never bypass the lint. Then codex-review the plan **against the spec** (does
   every spec section land in a phase? is intent preserved, not just assertion coverage?) and
   apply the fixes. SKIP only if both were already done for this plan.
5. **issue-sync** — `ledger.generate` (or `convert <plan.md>`; the parser reads the real
   `## Phase N — Title (ids)` dialect directly and writes each phase's `conductor-assertions`
   marker). SKIP if the hierarchy exists; else reconcile.
6. **Record `/goal`** (`conductor goal set`) and **start the driver:** register a harness cron via
   **`CronCreate`** — `prompt: "/conductor:autodev"`, `cron: "*/7 * * * *"` (≈ every 7 min),
   `durable: true`. Record its id. SKIP if already registered. The interval is only a
   **heartbeat**: `CronCreate` fires **only while the REPL is idle**, so a tick never overlaps a
   running fire — it no-ops until the current phase finishes, so the interval need not match phase
   duration. `durable: true` lets the cron survive a Claude restart.
   **Tell the user two limits:** (a) recurring crons **auto-expire after 7 days** — re-invoke
   `/conductor:start` to keep a longer run going; (b) the in-session cron **dies when the terminal
   closes** — for true cross-session survival set up the **Tier-B OS autostart**
   (`@reboot … claude -p "/conductor:start <spec>"` — reconcile-first, so it resumes; see
   `experiments/E5-end-to-end/recovery.md`).
7. **(Phase 2 only)** start the dispatcher loop — the supervisor that caps concurrency and assigns
   eligible phases to parallel workers. Single-loop needs no cap (`CronCreate` can't overlap fires);
   controlled parallelism is the dispatcher's job, not the cron cadence.

A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
