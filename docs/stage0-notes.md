# Stage 0 — design amendments

Findings surfaced while running the Stage 0 experiments that should be folded into the
design (`docs/specs/2026-06-28-autodev-design.md`) before / during `/writing-plans`.

---

## A. Fresh context per iteration without `/clear` or `/compact`

**Problem (raised in review):** the design's in-session durability (§4) leaned on cron
`/loop` "surviving `/clear`/compaction", but `/clear` cannot be agent-issued and
`/compact` degrades a long run. How does the loop get *fresh* context automatically?

**Resolution — we never clear; we relaunch.** Three automatable mechanisms, none needs
a user `/clear`:

1. **Fresh `claude -p` process per iteration (local).**
   `claude -p "/autodev" < /dev/null --no-session-persistence` spawns a brand-new
   session with a fresh context window, runs the phase, exits. A new process *is* a new
   session — strictly better than `/clear`, which only wipes the visible transcript but
   keeps the same long-lived session. **Verified** (E1): nested `claude -p` returned the
   requested token, exit 0.
2. **Fresh subagent per iteration (in-session — design §4 "thin-session").**
   Heavy work goes to a dispatched subagent whose context dies on return; the main
   `/loop` session only accumulates a tiny distilled summary. Bounds context without
   clearing. (E2.)
3. **Cloud `/schedule` (cross-session).** Each cloud fire = fresh container = fresh
   session; survives session/container death. The multi-day recovery tier. (E5/E7.)

**Architecture:** `/loop` (or OS cron) is the *clock*; each fire starts a fresh
process/subagent for the heavy work; `/schedule` is the cross-session recovery wrapper.
`--permission-mode` is required to let an unattended `claude -p` worker run its tools.

> Amends §4 (durability) and §3 (composition): the freshness mechanism is explicit and
> automated, not "remember to `/clear`."

---

## B. `/conductor` is reconcile-first and idempotent (not just `/autodev`)

**Problem (raised in review):** `/conductor` (the once-at-launch supervisor) is itself
heavy — TDD-ify the assertion specs, author plan 1 via `/writing-plans`, issue-sync —
and can blow context or be killed mid-setup. §10 only said "watchdog restarts it",
which is insufficient if a re-run redoes or corrupts partial setup.

**Resolution:** `/conductor` must be **reconcile-first and idempotent**, the same
contract as `/autodev` (§8). Re-invoking `/conductor` after a restart/context-loss must
detect existing state and resume from the first incomplete step:

| Setup step (§3) | Idempotency probe (resume if present) |
|---|---|
| repo / branch | exists? |
| assertions implemented as tests + manifest | `assertions/manifest.yaml` present + runner executes? |
| plan authored | plan index / milestone exists? |
| issue-sync | milestone+issues present and reconciled? |
| `/goal` recorded | goal artifact present? |
| driver started | cron/schedule already registered (don't double-register)? |

Heavy setup steps (TDD assertions, author plan) should run in **fresh subagents** so
`/conductor`'s own context stays thin. Restart = re-invoke `/conductor` → it reconciles.

> Amends §3 (conductor setup), §8 (add a `/conductor` reconcile-first contract), and
> §10 (loop/conductor death → re-invoke `/conductor`, which reconciles).
> Validated in E5 (re-run `/conductor` mid-setup → resumes, no double work).

---

## C. Recovery after a *local* restart — two restart tiers

`/schedule` runs **cloud** routines; it has **no handle on the local machine**, so it
cannot launch or command a *local* session. Recovery therefore has two parallel tiers,
both resuming from the same durable substrate (pushed git + issues + handoff) via a
reconcile-first `/conductor`:

| Restart | Mechanism | Work resumes |
|---|---|---|
| reboot / session death, resume **locally** | OS autostart — `@reboot` cron / systemd user service / login agent → `claude -p "/conductor resume" < /dev/null --permission-mode …` | local machine |
| local death, resume **anywhere** | cloud `/schedule` → `/conductor` in a fresh cloud container (clones from GitHub) | cloud |

Harness `CronCreate(durable:true)` persists jobs to `.claude/scheduled_tasks.json` and
survives a *Claude* restart, but still requires Claude itself to be relaunched — it does
not relaunch Claude or survive a machine reboot by itself. So the **local** watchdog must
be an OS-level autostart, not a harness cron. `/schedule` is the **cloud** watchdog
(design Option 2, §3/§10); the OS-autostart is its **local** counterpart.

> Amends §10: split "loop/conductor dies" into local-autostart vs cloud-`/schedule`
> recovery; both re-invoke a reconcile-first `/conductor`. Local-autostart demonstrated
> in E5.

---

## D. issue-sync must use `gh api`, not `gh issue` / `gh label` subcommands

The environment pins **gh 2.4.0**, which has **no `gh label` command** and **no `gh issue`
sub-issue subcommands**. E3 confirmed the GitHub REST API works fine through `gh api`
regardless of CLI age (gh just proxies HTTP):

- **labels:** `gh api -X POST repos/<o>/<r>/labels -f name=… -f color=…` (idempotent — PATCH
  the label on a 422 "already exists").
- **sub-issues:** `gh api --method POST repos/<o>/<r>/issues/<parent>/sub_issues -F sub_issue_id=<child DB id>`
  — note `-F` (typed **integer**) and the child's **database id** (`.id`), NOT its display
  number. Verify with `…/issues/<parent>/sub_issues` and `.sub_issues_summary.total`.

Sub-issues are the design's preferred Tasks representation (§7) and they **work** here, so
issue-sync should use real sub-issues (checklist remains the documented fallback).

> Amends §7 issue-sync: pin the `gh api` surface so issue-sync is portable across gh
> versions; do not depend on `gh issue` / `gh label` subcommand availability.
