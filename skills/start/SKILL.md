---
name: start
description: Start (or resume) an autonomous conductor run for a spec. Reconcile-first and idempotent — re-invoking detects existing setup and resumes from the first incomplete step. Preflights the conducted stack, validates the done-gate precondition, turns the spec's assertions into tests, syncs the GitHub issue ledger, records the goal, and starts the cron loop.
---

# /conductor:start — preflight + set up + launch (§3, reconcile-first)

**Idempotent (amendment B): each step probes durable state first and SKIPS if already done.**

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
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit), fresh subagent. SKIP if a plan/milestone exists.
5. **issue-sync** — `ledger.generate` (or `convert`). SKIP if the hierarchy exists; else reconcile.
6. **Record `/goal`** (`conductor goal set`) and **start the driver:** register cron
   `/loop /conductor:autodev`. SKIP if already registered.
7. **(Phase 2 only)** start the dispatcher loop.

A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
