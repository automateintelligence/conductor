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

## C. Recovery — two tiers for two different situations (not interchangeable)

`/schedule` runs **cloud** routines; it has **no handle on the local machine**, so it
cannot launch or command a *local* session. Recovery splits by *what failed*, and the two
tiers are **complementary, not two ways to build the same thing**. Both restart paths
re-invoke the **reconcile-first `/conductor`** over the durable substrate (pushed git +
issues + handoff) and resume from the **last pushed point**.

| Tier | Situation | Mechanism | Status |
|---|---|---|---|
| **B (local)** | machine **available** — reboot, Claude crashed, terminal closed | OS autostart (`@reboot` cron / systemd / login agent) → `claude -p "/conductor resume" </dev/null` | **tested** (fresh `claude -p` reconcile-resume skipped all done steps; OS trigger = snippet, not reboot-tested) |
| **A (cloud)** | machine **off / unreachable**, work must continue | cloud `/schedule` → fresh container runs `/conductor`+in-cloud `/loop` (Option 1 in the cloud), clones from GitHub | **design only — untested (E7)**; needs cloud repo+`gh` access |

If progress is only needed while the machine is on, **Tier B alone is complete** (no cloud)
— matching the "`/loop` primary, `/schedule` only for extraordinary multi-day" preference.

**Local⇄cloud overlap (both briefly running):** correctness is handled by the shared
done-gate + ledger **lease/claim** (§7 — whoever holds the fresh lease owns the unit; the
other backs off; this is **E8, untested**); *cost* is handled by local resume standing the
cloud watchdog down (delete the `/schedule` routine — a one-line prompt confirm suffices).

Note: harness `CronCreate(durable:true)` survives a *Claude* restart but still needs Claude
relaunched and won't survive a machine reboot by itself — so Tier B must be an OS-level
autostart, not a harness cron.

> Amends §10: split "loop/conductor dies" into **Tier B (local OS-autostart, tested)** vs
> **Tier A (cloud `/schedule`, design-only — validate in E7; lease handling E8)**; both
> re-invoke a reconcile-first `/conductor`.

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

---

## E. Conductor needs its full skill/plugin stack wherever it runs — Anthropic cloud lacks it

Conductor is an **orchestrator of other skills** (superpowers `/writing-plans`,
`/subagent-driven-development`, `/code-review`, `/receiving-code-review`,
`/document-release`; spec-kit `/plan`+`/tasks`; `/codex`). It only runs where that stack
exists:

- **Locally: fine.** The user's machine has the skills (this build used them). Tier B
  (local autostart) is unaffected.
- **Anthropic cloud `/schedule` (Tier A): blocked.** Confirmed — cloud instances do **not**
  have superpowers / spec-kit, and `/codex` needs a CLI binary that isn't there. A cloud
  `/conductor` would fail at the first `/writing-plans` / `/subagent-driven-development`
  call. Vendoring the markdown skills into the repo might cover some, but not the plugin
  machinery or the codex binary; whether the stack is installable in Anthropic cloud at all
  is **unverified** (a feasibility spike, not an assumption).
- **Per-host preconditions (any host):** **(a)** the skill/plugin stack, **(b)** `gh`
  credentials for the repo, **(c)** model access. None is automatic; each host is
  provisioned.

**Durable "walk away for days" tier = an always-on host the user controls** (home server,
own cloud VM, or the workstation/WSL left on), provisioned with the same skill stack and
driven by Option 1 + Tier B autostart — the *same code path as local*. Anthropic-cloud
`/schedule` is an optional enhancement, gated on proving skills-in-cloud.

> Amends §2/§4: the cross-session "walk away for days" tier should be a **user-controlled
> always-on host with the skill stack**, NOT assumed to be Anthropic-cloud `/schedule`
> (which lacks the stack). Tier A is feasibility-gated — E7 must first verify the
> skill/plugin/codex stack is even installable in cloud.

---

## F. Plugin invocation convention — `/conductor:<skill>` (namespaced), no bare names, no aliases

Confirmed (claude-code-guide + on-disk plugin layout + Codex review of Plan 1): a Claude Code
**plugin** skill is invoked **`/<plugin>:<skill>`**. The SKILL.md `name:` is display metadata,
not the command. Consequences for conductor (a plugin, §2.1/§11):

- The design doc's bare `/conductor`, `/autodev`, `/expectations`, `/executable-assertions`
  are **shorthand**. The real invocations are `/conductor:conductor` (supervisor — there is
  **no** bare `/conductor` for a plugin), `/conductor:autodev`, `/conductor:expectations`,
  `/conductor:executable-assertions`, `/conductor:assertions-to-tests`, `/conductor:issue-sync`.
- **No command aliases** exist; a skill can only invoke another skill internally.
- Conducted external skills keep their own namespace: `/superpowers:test-driven-development`,
  `/superpowers:subagent-driven-development`, etc.
- Layout: manifest at `.claude-plugin/plugin.json` (`name` required; rest optional);
  `skills/` and `commands/` auto-discovered. Validate with `claude plugin validate ./ [--strict]`.

> Amends §3/§6/§11 naming: read every bare `/x` for a conductor-owned skill as
> `/conductor:x`. If bare names are ever required, conductor must ship as **standalone
> skills** (`.claude/skills/`), not a plugin — but that loses single-plugin install (§2.1).
