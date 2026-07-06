# Spec: Conductor self-enforcement hardening (v0.6.x)

**Normative spec:** this file. Source review: `docs/reviews/2026-07-05-conductor-self-enforcement-review.md`.

## Problem

A 4-agent review of conductor 0.5.2 found the tool is mechanically strong at the leaves (tested
Python gates) but prose-fragile at the joints: several load-bearing rules are skill prose an agent
can skip or under-specify, and the least-privilege permission path exists only as documentation, so
the dangerous `--dangerously-skip-permissions` one-liner wins by inertia. This spec hardens the
highest-value gaps by converting prose into tested CLI surface and making least-privilege the easy
default.

## Scope & preconditions

**Phase 0 (done by hand, PR #37, conductor 0.6.0 — NOT part of this run):** `conductor merge <pr>`
(fuses the merge gate with the merge, refuses `base=default`), reconcile no-progress escalation,
and the three CLI one-liners. This run assumes conductor >= 0.6.0 is installed.

Every phase adds NEW CLI surface or docs. No phase rewires the live `.conductor/` run scratch, the
running Tier-B driver, or the crontab of the run executing this spec — changes go live only when
the owner updates the plugin after the run. Each phase is one PR into the run branch, gated by its
assertions.

## Phase 1 — `conductor resume-script grant`: make least-privilege a paste (review A-1, A-5, A-8)

**Spec:** Add a `grant` subcommand to `conductor/resume_script.py` (wired in `bin/conductor`) so an
owner authorizes an unattended run without hand-authoring anything.

- `conductor resume-script grant --scoped --project <p> --worktree <w>` writes a starter scoped
  allowlist file and the exact `CONDUCTOR_RESUME_CLAUDE_FLAGS` line into `<p>/.conductor/resume-env.sh`
  that loads it, covering git/gh/pytest/ruff/pyright/conductor/docker. It is the default and the
  recommended path. The generated allowlist MUST reject blanket wildcards (`Bash(*)`, `Bash(*:*)`).
- `conductor resume-script grant --full` writes `CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"`
  but ONLY when an explicit `--i-understand-standing-full-access` token is also passed; without the
  token it refuses (non-zero) and prints why.
- Any `resume-env.sh` the command creates is `chmod 0600`. The generated driver refuses to source a
  `resume-env.sh` that is group- or world-writable (fail loud, like `driver-unresolved`).

- [ ] `grant --scoped` writes both artifacts and names the loader flag
- [ ] `grant --full` without the token refuses; with the token writes the bypass line
- [ ] generated allowlist rejects blanket wildcards
- [ ] created `resume-env.sh` is mode 0600; driver refuses a world-writable one

## Phase 2 — README "Unattended authority" + canonical bypass spelling (review A-2, A-9)

**Spec:** Put the permission decision on the documented path. Add an "Unattended authority"
subsection to README §3 stating the decision plainly, showing both options (scoped default, full
bypass) and pointing at `grant`. Normalize the two bypass spellings across docs to one canonical
form.

- [ ] README contains an "Unattended authority" section naming both `grant --scoped` and `grant --full`
- [ ] a single canonical bypass spelling is used across README and recovery.md

## Phase 3 — Posture visibility in the generated driver (review A-4, A-6)

**Spec:** Make the permission posture observable. The generated Tier-B driver logs a posture label
at `fire-start` (`posture=full-bypass|scoped|supervised`, no secrets). The `resume-script write`
nudge is split into two concrete branches (scoped vs full) and gated on "permission posture
undecided" rather than "resume-env.sh absent".

- [ ] the generated driver logs a `posture=` label at fire-start
- [ ] the write nudge fires when no posture is set even if resume-env.sh exists, and names both paths

## Phase 4 — `conductor gate lint`: frozen-gate quality + integrity (review B-4)

**Spec:** Add `conductor gate lint` (run at `/conductor:start` before `gate freeze`) that fails
closed on a manifest command that could load an unfrozen conftest (requires the pinned
`--noconftest` / `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` form) and flags an assertion test file with no
negative ("must not contain") clause. Add a `gate freeze` contract-test needle so the freeze step
cannot silently rot out of the skill. Extend `gate freeze`/`verify` to also hash `<spec>.assertions.md`.

- [ ] `gate lint` rejects an unpinned manifest command; accepts the pinned form
- [ ] `gate lint` flags an assertion test with no negative clause
- [ ] a `gate freeze` needle exists in the start contract test
- [ ] `gate freeze`/`verify` covers `<spec>.assertions.md`

## Phase 5 — Single-sourced identifiers: `run-branch name` + `default-branch` (review B-5)

**Spec:** Mirror the `conductor remote` precedent so cross-skill string contracts have one
implementation. `conductor run-branch name <spec>` emits the canonical `conductor/run-<slug>` for a
spec path; `conductor default-branch` resolves the repo default (via `gh repo view` /
`symbolic-ref`, fail-open to `main`). The autodev/start prose calls them instead of deriving in
prose. `merge-gate` emits a distinct `topology-off:no-run_branch` line when the run_branch file is
absent (instead of silently disabling the base leg).

- [ ] `run-branch name <spec>` emits a deterministic canonical name; start and autodev call it
- [ ] `default-branch` resolves via gh and falls open to `main`
- [ ] `merge-gate` emits `topology-off:no-run_branch` when the file is missing

## Phase 6 — `conductor driver install|status`: unconditional Tier-B + cron ownership (review B-3, B-6)

**Spec:** Own the crontab wiring in tested code. `conductor resume-script install-cron` /
`uninstall-cron` compute the marker (`# conductor-autodev <main-root>` from `--git-common-dir`) once
so start (install) and autodev step 3b (removal) share one implementation and cannot drift.
`conductor driver install` always writes the script + cron for an unattended run (fail-closed
default, not "if you judge CronCreate non-durable"). `conductor driver status` exits non-zero unless
a durable driver (crontab marker or scheduled_tasks.json) exists and tails the resume log for recent
`driver-unresolved` / non-zero fires.

- [ ] `install-cron`/`uninstall-cron` derive an identical marker; removal matches install
- [ ] `driver status` exits non-zero when no durable driver exists; zero when one does
- [ ] `driver status` reports recent driver-unresolved / non-zero fires

## Out of scope (follow-up)

B-7 (run-infra digest guard), A-3 (a permission dry-run tick), and A-7 (disarm the bypass on run
completion) are deferred to a later pass; noted so their omission is deliberate.
