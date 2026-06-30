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

0. **PREFLIGHT (`conductor preflight`).** Confirm every conducted command resolves (Codex #1):
   `/spec-craft:*`, `/superpowers:*`, and environment-provided `/code-review`, `/codex`,
   `/document-release`. Any **missing → STOP** and tell the user to install it (fail-closed,
   amendment E). Do not launch a loop that dies at the first conducted call.
1. **Detect spec source**; load spec + Expectations + Executable Assertions.
2. **PRECONDITION — assertions present?** No → STOP and point the user at `/spec-craft:expectations`
   then `/spec-craft:executable-assertions` (or, with `--auto-assert`, launch them).
3. **Implement assertions as runnable tests** via `/conductor:assertions-to-tests`. **SKIP only if
   `start_probe.assertions_ready(expected_ids, "assertions/manifest.yaml", <assert-run --level spec
   exit>)` is True** — i.e. the manifest has one entry per `/spec-craft:executable-assertions` id
   AND the runner exit ∈ {0,1} (Codex #3). Otherwise (re)build it.
   **Then FREEZE the gate (§5):** `conductor gate freeze` records `assertions/.frozen` (commit it)
   so the worker cannot later weaken a check; the runner fail-closes (exit 6) if a frozen
   assertion or its test file changes. SKIP if `.frozen` exists and `conductor gate verify` is clean.
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit), fresh subagent. SKIP if a plan/milestone exists.
5. **issue-sync** — `ledger.generate` (or `convert`). SKIP if the hierarchy exists; else reconcile.
6. **Record `/goal`** (`conductor goal set`) and **start the driver:** register a harness cron via
   **`CronCreate`** — `prompt: "/conductor:autodev"`, `cron: "*/7 * * * *"` (≈7 min; an off-:00/:30
   minute), `durable: true`. Record its id. SKIP if already registered. The interval is only a
   **heartbeat**: `CronCreate` fires **only while the REPL is idle**, so a tick never overlaps a
   running fire — it no-ops until the current phase finishes, so the interval need not match phase
   duration. `durable: true` lets the cron survive a Claude restart.
   **Tell the user two limits:** (a) recurring crons **auto-expire after 7 days** — re-invoke
   `/conductor:start` to keep a longer run going; (b) the in-session cron **dies when the terminal
   closes** — for true cross-session survival set up the **Tier-B OS autostart**
   (`@reboot … claude -p "/conductor resume <spec>"`; see `experiments/E5-end-to-end/recovery.md`).
7. **(Phase 2 only)** start the dispatcher loop — the supervisor that caps concurrency and assigns
   eligible phases to parallel workers. Single-loop needs no cap (`CronCreate` can't overlap fires);
   controlled parallelism is the dispatcher's job, not the cron cadence.

A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
