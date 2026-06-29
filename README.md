# conductor

**Autonomous spec-completion loop for Claude Code.** State a goal, walk away, come
back to either finished work that passes a machine-checked definition of done, or a
clear, recoverable note about why it stopped.

Conductor is a Claude Code plugin. You point it at a spec whose definition of done is
explicit and machine-checkable (built with [spec-craft](https://github.com/automateintelligence/spec-craft)),
and it drives the work to completion: it plans, tracks every phase as a GitHub issue,
executes one phase at a time in a fresh subagent, merges each phase only through a
safety gate, and stops the moment the done-gate goes green. An external cron `/loop`
is the clock, so the agent never decides on its own that the work is "done enough" —
only green executable assertions stop the run.

---

## Why

The hard part of an autonomous coding agent is not writing code. It is drift: the agent
declares victory early, wanders off the goal, loses its place after a crash, or merges
something half-finished. Conductor is built to remove each of those failure modes.

- **Done is a machine check, not a vibe.** The run ends only when `conductor assert run
  --level spec` exits 0 — real tests derived from your spec's assertions. The gate is
  fail-closed: a missing or unrunnable gate counts as NOT done, never as done-by-default.
- **It cannot quit early or wander.** The clock is external (a cron `/loop`), the goal is
  re-loaded from durable state on every fire, and the worker never ends the run itself. It
  works one phase, writes a handoff, and exits. The next tick picks up from ground truth.
- **Durable, crash-proof state.** Git history and GitHub issues are the ledger. Every
  iteration commits, pushes, and updates issues, so a fresh process (or a fresh machine)
  resumes with zero local context. No `/clear`, no `/compact`, no context bloat.
- **Every merge passes a gate.** A phase merges only if `conductor merge-gate <pr>` is
  clean: not a draft, mergeable, clean merge state, no changes-requested, no unresolved
  review threads, and the gate command re-verified against the actual merge ref. Never a
  force-merge.
- **It pages you only when a human is actually needed.** Patch-later work becomes a
  follow-up issue; build-now work spawns a bounded sub-plan and an ADR. The single branch
  that stops to ask you is a real "needs human judgment" call.

If you have ever told an agent "finish this" and come back to a confident, broken,
half-merged mess, that is the gap conductor closes.

---

## How it works

Two phases: a one-time setup you run, then an autonomous loop the cron drives.

```
SETUP  (you run this once)
──────────────────────────
  spec.md
    │
    ├─ /spec-craft:expectations ........... writes "## Expectations" (success · failure · must-nots)
    │
    ├─ /spec-craft:executable-assertions .. 4-part assertion specs (claim · setup · observation · kind)
    │
    └─ /conductor:start <spec>   ── the supervisor; idempotent, reconcile-first ───┐
         0. conductor preflight ........... does every conducted skill resolve? (fail-closed)
         1. /conductor:assertions-to-tests  → assertions/manifest.yaml + RED tests  = the DONE-GATE
         2. /superpowers:writing-plans .... plan.md (phases → tasks)        [only if no plan yet]
         3. /conductor:issue-sync ......... GitHub milestone + phase issues + task sub-issues + labels
         4. conductor goal set ............ the durable target
         5. CronCreate .................... registers "/loop /conductor:autodev"  = the clock
                                                                                  │
THE LOOP  (the cron drives it; you walk away)                                     │
─────────────────────────────────────────────  ◄─────────────────────────────────┘
  every interval ─► /conductor:autodev      (one fire = one phase, in fresh context)
    1. RE-LOAD GOAL ...... from the handoff + ledger  (trust git/issues, not memory)
    2. RECONCILE ......... ledger.reconcile      (precedence: git/tests > PR > label)
    3. DONE-GATE ......... conductor assert run --level spec
         ├─ exit 0  AND no plans left ─► CronList → CronDelete → final handoff → STOP ✓
         └─ not green ─► keep going
    4. PICK .............. next eligible phase (unassigned, unblocked; climb the ladder)
    5. CLAIM ............. ledger.claim          (GitHub assignee + lease marker)
    6. EXECUTE ........... in a FRESH SUBAGENT, via the recipe, one PR per phase:
          subagent-driven-development → /code-review each task → commit each task
            → open PR (Closes #phase) → /codex review → receiving-code-review
            → conductor merge-gate <pr>  ──ok?──►  gh pr merge --merge   (never force)
            → /document-release
    7. ESCALATE (§9) ..... patch-later: follow-up issue · build-now: sub-plan + ADR
                            · needs human judgment: HALT  (the only branch that pages you)
    8. RECORD ............ labels/progress; renew or release the lease
    9. HANDOFF ........... write .conductor/ handoff; commit + push; EXIT
                            (cron re-fires next interval ─► back to step 1)
```

Roles compose, they do not nest: `/conductor:start` is the supervisor you invoke once,
`/conductor:autodev` is the worker the clock fires, the goal is the target, and the
schedule is the clock. The full design lives in
[`docs/specs/2026-06-28-autodev-design.md`](docs/specs/2026-06-28-autodev-design.md).

---

## Install

Conductor is a Claude Code plugin that declares `dependencies: ["spec-craft"]`, so a
marketplace install pulls in spec-craft automatically.

### Prerequisites

- **Claude Code** with the conducted skill stack available — superpowers, spec-kit,
  `/codex`, `/code-review`, `/document-release`. `conductor preflight` checks for every
  one and fail-closes if any is missing.
- **`gh` CLI**, authenticated (`gh auth status`). GitHub issues are the ledger.
- **Python 3.12** on PATH (the runner, ledger, and gate modules are Python).

### Install as a plugin (shared)

Once the plugins are published to a marketplace (a `.claude-plugin/marketplace.json`
listing both `conductor` and `spec-craft`):

```
/plugin marketplace add automateintelligence/conductor
/plugin install conductor@<marketplace-name>     # auto-installs the spec-craft dependency
```

CLI equivalents:

```bash
claude plugin marketplace add automateintelligence/conductor
claude plugin install conductor@<marketplace-name>
```

> **Note:** these repos do not ship a `marketplace.json` yet, so the one-command install
> above is not wired up today. Until it is, use the local method below.

### Install locally (works today)

Clone both repos side by side and load them as plugin directories:

```bash
git clone https://github.com/automateintelligence/spec-craft
git clone https://github.com/automateintelligence/conductor
claude --plugin-dir ./spec-craft --plugin-dir ./conductor
```

### Verify

```bash
conductor preflight          # prints MISSING: <cmd> and exits 1 if any conducted skill is absent
claude plugin list           # conductor + spec-craft should appear
```

After install the skills are available as `/conductor:start`, `/conductor:autodev`,
`/conductor:assertions-to-tests`, `/conductor:issue-sync`, and (from the dependency)
`/spec-craft:expectations`, `/spec-craft:executable-assertions`.

---

## Use

### 1. Make "done" explicit (with spec-craft)

Start from a spec file. Give it a checkable definition of done:

```
/spec-craft:expectations path/to/spec.md            # adds an ## Expectations section
/spec-craft:executable-assertions path/to/spec.md   # derives 4-part assertion specs
```

See the [spec-craft README](https://github.com/automateintelligence/spec-craft) for what
these produce. Conductor needs the assertions; `/conductor:start` stops and points you
here if they are missing.

### 2. Start the run

```
/conductor:start path/to/spec.md
```

This is the supervisor, and it is idempotent — every step probes durable state first and
skips what is already done, so you can re-run it any time to resume. In order it runs
preflight, turns the assertions into the runnable done-gate
(`/conductor:assertions-to-tests`), writes the first plan if there is none, syncs the
GitHub issue hierarchy (`/conductor:issue-sync`), records the goal, and registers the cron
driver. Pass `--auto-assert` to let it run the spec-craft skills for you when assertions
are absent.

### 3. Walk away

The cron fires `/conductor:autodev` on its interval. Each fire re-loads the goal,
reconciles state, checks the done-gate, claims and builds the next phase in a fresh
subagent, merges it through `conductor merge-gate`, writes a handoff, and exits. When the
gate is green and no plans remain, the worker deletes its own cron and stops.

### 4. Check in, resume, or stop

- **Where is it?** Read the latest handoff in `.conductor/` (local resume scratch), or the
  GitHub issues / milestone (the durable ledger).
- **Resume after a crash or restart:** re-run `/conductor:start path/to/spec.md`. It
  reconciles and continues from the first incomplete step.
- **Stop it early:** `CronList` then `CronDelete` the driver cron (the worker does this
  itself on completion).

---

## CLI reference

The `conductor` command (`bin/conductor`) fronts the Python modules.

| Command | What it does |
|---|---|
| `conductor assert run [--level spec\|phase\|task]` | Run the done-gate. Exit `0` all green, `1` ≥1 red, `2` manifest missing, `3` manifest unparseable, `4` overall timeout, `5` no matching assertions / bad args. Fail-closed by design. |
| `conductor ledger generate <plan.json>` | Create the GitHub milestone + phase issues + task sub-issues + labels from a plan dict. |
| `conductor ledger convert <plan.md>` | Parse a Markdown plan (`# Title` / `## Phase [status]` / `- [ ] task`), then generate. |
| `conductor ledger reconcile <issue#> [--tests-red] [--pr-merged] [--commits N] [--retries N] [-R N] [--now-ts N] [-L N]` | Apply the §7 reconcile rules (precedence git/tests > PR > label); returns `{action, new_status}`. |
| `conductor goal set <text...>` / `conductor goal get` | Record / read the durable goal (`.conductor/goal.md`). |
| `conductor preflight` | Static availability gate: every conducted skill resolves, else exit 1. |
| `conductor merge-gate <pr>` | Autonomous merge safety gate (see below); exit 0 ok, 1 blocked. |

`conductor merge-gate` blocks a merge on any of: draft PR, merge state not `CLEAN`,
mergeable not `MERGEABLE`, review decision `CHANGES_REQUESTED`, unresolved review threads,
or the verify command (default `pytest -q`) failing when re-run against the real
`refs/pull/<pr>/merge` ref. Env: `CONDUCTOR_REPO`, `CONDUCTOR_MERGE_VERIFY`. The runner
honors `CONDUCTOR_MANIFEST`, `CONDUCTOR_OVERALL_TIMEOUT`, `CONDUCTOR_ISOLATE`.

The lease operations (`claim`, `eligible`, `release`, `read_lease`, `renew_lease`,
`lease_is_stale`) are a Python API in `ledger/claim.py`, used by the skills. They are not
CLI subcommands.

---

## Skills

| Skill | Who calls it | What it does |
|---|---|---|
| `/conductor:start` | you, once | Supervisor. Preflight, build the done-gate, plan, sync issues, set the goal, start the cron. Idempotent / reconcile-first. |
| `/conductor:autodev` | the cron `/loop` | Worker. One fire = one phase: reconcile, gate, claim, execute in a fresh subagent, merge through the gate, handoff, exit. Self-stops when done. |
| `/conductor:assertions-to-tests` | `/conductor:start` (and the plan flow) | Turns each 4-part assertion spec into one runnable test wired into `assertions/manifest.yaml` by id. Builds the done-gate; does not implement product behavior. |
| `/conductor:issue-sync` | `/conductor:start` and `/conductor:autodev` | Headless GitHub issue hierarchy: generate / convert / reconcile. Never prompts. |

---

## Components

| Path | Responsibility |
|---|---|
| `assertions/run.py` | The done-gate runner. Per-assertion + overall timeouts, isolation, fail-closed. |
| `assertions/manifest.yaml` | The assertions (id, command, level, kind, timeouts). |
| `bin/conductor` | CLI dispatcher over the Python modules. |
| `ledger/` | GitHub-issue ledger: `gh` wrappers, claim/lease, §7 reconcile, plan→issues sync. |
| `conductor/preflight.py` | Conducted-stack availability gate. |
| `conductor/merge_gate.py` | Autonomous merge safety gate (§6.2). |
| `conductor/handoff.py` | Durable handoff writer (§4). |
| `conductor/escalate.py` | Escalation: follow-up issues, sub-plan blocks, ADRs (§9). |
| `conductor/start_probe.py` | Idempotency probe for `/conductor:start`. |
| `docs/specs/2026-06-28-autodev-design.md` | The full design. |
| `docs/plans/` | The build plans (1: spec-craft, 2: done-gate, 3: ledger, 4: autodev+start). |

---

## Status

The MVP is built and merged: the four `/conductor:*` skills, the `conductor` and `ledger`
Python packages, and the done-gate runner. The end-to-end loop has reached green
unattended and self-stopped in the recorded smoke test
(`experiments/E5-end-to-end/promote_check.sh`, gated behind `RUN_CONDUCTOR_E2E=1`).
