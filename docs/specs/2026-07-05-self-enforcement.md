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

## Phase 1 — Session-mode-aware unattended authority (review A-1, A-3, A-5, A-8)

**Owner decision (2026-07-06): inherit Claude Code's existing permission model — do NOT invent
conductor-specific permission flags or tokens.** `/conductor:start` reads the permission posture of
the session it was launched in and acts on it. Two cases:

- **(A) The launching session is already in bypass / skip-permissions mode.** The unattended run
  inherits full autonomy (the Tier-B driver fires with the same posture). Before proceeding, `start`
  prints a **big, explicit warning** — a standing full-access agent will fire every heartbeat with
  the owner's credentials (gh merge, push, docker, broad edits, subagents), surviving reboots, until
  the gate is green — and requires the owner to **acknowledge to continue**. The acknowledgment is
  the gate; there is no extra conductor flag.

- **(B) The launching session is in a less-privileged mode** (default ask-per-tool, or a scoped
  allowlist). `start` runs a **dry-run** that enumerates, from the plan's recipe, the concrete
  privileged operations each phase will perform (create branch, `git push`, `gh pr create`,
  `conductor merge`, docker via `CONDUCTOR_MERGE_VERIFY`, file writes, subagents) and reports **which
  of those the current mode would prompt for** — i.e. exactly which unattended steps would stall
  without the owner present. It then offers a concrete choice with those examples in front of the
  owner: (i) elevate to bypass (with the (A) warning), (ii) widen the session's own scoped allowlist
  to cover the listed operations, or (iii) proceed knowing precisely which steps will require them.

**Deliverable (the assertable core):** a `conductor authority preview` (a.k.a. `--dry-run`) that
maps the recipe's privileged operations for a plan and prints the concrete per-phase intervention
list. Detection of the launching session's posture and the interactive warning/choice are
agent-executed (they run in the owner's live `start` session, which CAN prompt — the headless
`/conductor:autodev` fires never prompt), so they are prose, not frozen assertions.

Safety carried over regardless of posture: any `resume-env.sh` the tool writes (to carry the
inherited flags / `CONDUCTOR_MERGE_VERIFY`) is `chmod 0600`, and the generated driver refuses to
source a group- or world-writable `resume-env.sh` (fail loud, like `driver-unresolved`).

**Fail-closed posture resolution (a frozen invariant, A2).** Whatever the detection mechanism, the
function that maps a (possibly-unknown) detected mode to the run's posture MUST resolve an
**unknown, unreadable, or ambiguous** mode to the **least-privileged** posture (supervised) — NEVER
to bypass. A misread can only ever under-grant, never over-grant.

> **Open implementation question (flagged, not resolved here):** can `/conductor:start` read its own
> Claude Code session permission mode programmatically? If the harness exposes it, detection is
> automatic; if not, `start` asks the owner once ("what posture should the unattended run use?") —
> still on the documented path, still Claude-native. The dry-run (B) works either way, since it
> enumerates from the recipe, not from the detected mode. Either way the fail-closed rule above holds.

- [ ] `authority preview` enumerates the recipe's privileged operations for a plan (branch, push, gh, merge, docker, subagents, writes)
- [ ] a bypass-mode launching session triggers the warning + acknowledgment before the run proceeds unattended
- [ ] a less-privileged session gets the concrete per-phase "these steps will be manual" report + the elevate / widen-allowlist / proceed choice
- [ ] any `resume-env.sh` the tool writes is mode 0600; the driver refuses a world-writable one

## Phase 2 — README "Unattended authority" + canonical bypass spelling (review A-2, A-9)

**Spec:** Put the permission decision on the documented path. Add an "Unattended authority"
subsection to README §3 stating the model plainly: an unattended run **inherits the permission mode
of the session you launch `/conductor:start` in** — launch in bypass mode and you are warned and
asked to acknowledge; launch in a less-privileged mode and `start` shows you which steps would need
you. No conductor-specific permission command. Normalize the two bypass spellings
(`--dangerously-skip-permissions` vs `--permission-mode bypassPermissions`) across docs to one
canonical form. No doc may reference the removed `grant` command.

- [ ] README §3 has an "Unattended authority" section describing the session-inherit model (warning + acknowledgment for bypass; dry-run for less-privileged)
- [ ] no user-facing doc (README, recovery.md, skills/) references `grant --scoped` / `grant --full` (this spec + its assertions, which document the removal, are exempt)
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

B-7 (run-infra digest guard) and A-7 (disarm the bypass on run completion) are deferred to a later
pass; noted so their omission is deliberate. (A-3, the permission dry-run, was pulled INTO Phase 1
by the 2026-07-06 owner decision.)

## Expectations

### Success scenarios

1. The unattended run **inherits the launching session's permission posture** — conductor invents
   no permission flags of its own. An operator already in bypass mode gets full auto; one in a
   less-privileged mode is shown, concretely, which steps would need them.
2. Before an unattended run proceeds with **full** autonomy, `/conductor:start` shows an explicit
   warning of the standing-full-access blast radius and requires the owner to **acknowledge**; it
   never starts unattended full-auto silently.
3. In a **less-privileged** session, `start` shows a **dry-run** that names the concrete privileged
   operations each phase will perform and which ones will require the owner, then offers a real
   choice (elevate / widen allowlist / proceed-with-manual-steps).
4. The **permission posture** of every unattended fire is visible in the resume log as a bare label
   (supervised / scoped / full-bypass).
5. The unattended-authority decision is **discoverable from the README**, not only from
   agent-facing skill files.
6. A done-gate that could be weak or bypassable is caught **before it is frozen**: `gate lint`
   rejects an unpinned manifest command and flags an assertion with no negative clause, and the
   freeze covers the human-authored `<spec>.assertions.md`.
7. The run-branch name and the repo default branch each come from **one command** that both `start`
   and `autodev` call, so the two skills cannot derive them differently.
8. `driver status` tells the operator, on demand, whether the unattended run has a durable driver
   and whether recent fires failed.

### Failure scenarios (confidently wrong)

1. The dry-run **under-reports** a privileged operation the recipe actually performs (e.g. omits the
   docker call in `CONDUCTOR_MERGE_VERIFY`), so the owner elevates too little and the unattended fire
   still stalls — the safe path fails the exact silent-stall way the design exists to prevent.
2. `start` proceeds to unattended **full-auto without the warning + acknowledgment** — a standing
   full-access agent is armed silently.
3. `start` **misreads** the launching session's posture (treats a less-privileged session as bypass,
   inheriting more authority than the owner has) — the run gets more power than the session granted.
4. Posture logging prints the settings-file path or any secret instead of a bare label.
5. `gate lint` passes a manifest command that can load an unfrozen `conftest.py` — the frozen-gate
   bypass it exists to catch.
6. `run-branch name` disagrees with a prose-derived slug, or `default-branch` emits an empty string
   (breaking a `git fetch`) instead of falling open to `main`.
7. `driver status` reports healthy while the durable driver is absent, or while every recent fire
   logged `driver-unresolved` / a non-zero exit.

### Must-nots

1. No phase changes **default behavior**: an operator who invokes none of the new commands still
   gets no permission bypass and the current merge/reconcile/gate semantics. Every addition is
   opt-in.
2. `start` must **never** begin an unattended run with full autonomy without an explicit owner
   acknowledgment, and must **never** grant the unattended run MORE authority than the launching
   session already had.
3. A `resume-env.sh` the tool creates must **never** be group- or world-writable — it can carry the
   bypass flag and the `CONDUCTOR_MERGE_VERIFY` command that runs as shell.
4. `gate lint` must be **fail-closed**: an unparseable or ambiguous manifest command counts as
   reject, never pass.
5. No phase modifies the live running run's `.conductor/` scratch, its Tier-B driver, or its
   crontab — the new behavior takes effect only when the owner updates the plugin after the run.
6. A resolver (`default-branch`, `run-branch name`) must never emit an empty value that would make a
   git command operate on the wrong or a missing ref; it falls open to a safe default.
