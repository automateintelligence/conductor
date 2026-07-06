# conductor

**Autonomous spec-completion loop for Claude Code.** State a goal, walk away, come
back to either finished work that passes a machine-checked definition of done, or a
clear, recoverable note about why it stopped.

**State grounded in GitHub to survive sessions and restarts.**
Conductor is a Claude Code plugin. You point it at a spec whose definition of done is
explicit and machine-checkable (built with [spec-craft](https://github.com/automateintelligence/spec-craft)),
and it drives the work to completion: it plans, tracks every phase as a GitHub issue,
executes one phase at a time in a fresh subagent, merges each phase only through a
safety gate, and stops the moment the done-gate goes green. An external cron `/loop`
is the clock, so the agent never decides on its own that the work is "done enough" —
only green executable assertions stop the run.

Conductor works best on specs authored with
[SuperPowers](https://github.com/obra/superpowers) or
[GitHub's spec-kit](https://github.com/github/spec-kit): both produce specs with explicit
requirements and phased tasks, which is exactly the shape the loop plans and executes
against.

---

## Why

The hard part of an autonomous coding agent is not writing code. It is drift: the agent
declares victory early, wanders off the goal, loses its place after a crash, or merges
something half-finished. Conductor is built to remove each of those failure modes.

- **Done is a machine check, not a vibe.** The run ends only when `conductor assert run
  --level spec` exits 0 — real tests derived from your spec's assertions. The gate is
  fail-closed (a missing or unrunnable gate counts as NOT done), and it is *frozen* at setup
  so the worker can't weaken a check to fake done ([why](#why-the-worker-cant-cheat-the-gate)).
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
    ├─ /spec-craft:executable-assertions .. 4-part specs → <spec>.assertions.md (claim · setup · observation · kind)
    │
    └─ /conductor:start <spec>   ── the supervisor; idempotent, reconcile-first ───┐
         0. conductor preflight ........... does every conducted skill resolve? (fail-closed)
         1. /conductor:assertions-to-tests  reads <spec>.assertions.md → assertions/manifest.yaml + RED tests = DONE-GATE
            conductor gate lint ............ fail-closed lint of the gate's own quality (unpinned/weak tests)
            conductor gate freeze .......... snapshot + commit the gate (FROZEN; worker can't weaken it)
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
         ├─ exit 0  AND no plans left ─► open FINAL OWNER PR (run branch → default,
         │      body = conductor run-packet; conductor NEVER merges it)
         │      → CronList → CronDelete → remove Tier-B crontab lines → final handoff → STOP ✓
         └─ not green ─► keep going
    4. PICK .............. next eligible phase (unassigned, unblocked; climb the ladder)
    5. CLAIM ............. ledger.claim          (GitHub assignee + lease marker)
    6. EXECUTE ........... in a FRESH SUBAGENT, via the recipe, one PR per phase:
          subagent-driven-development → /code-review each task → commit each task
            → open PR, base = the RUN branch (Closes #phase) → /codex review ×2
              (Codex hit its 5h/weekly limit? → /code-review fallback + tell the owner, never stall)
            → receiving-code-review
            → conductor merge-gate <pr>  ──ok?──►  gh pr merge --merge   (never force;
              lands on conductor/run-<slug>, never the default branch)
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

### Why the clock is external

An in-process loop would let the agent decide when it is done, let context bloat across
iterations, and die when the process dies. An external cron `/loop` inverts all three: the
worker cannot end the run (only a green gate or a halt does), every tick starts from durable
ground truth in a fresh context, and a crash costs exactly one tick — the next fire
reconciles from git and the issue ledger and continues.

---

## Why the worker can't cheat the gate

"Done is a machine check" only holds if the check itself can't be quietly weakened. The
worker writes product code *and* could, in principle, edit the done-gate tests in the same
loop — turning a red assertion green by gutting the test instead of satisfying it. Conductor
stops that mechanically:

- **The gate is frozen at setup.** After `/conductor:start` builds the done-gate from your
  human-confirmed assertions, `conductor gate freeze` records a digest baseline
  (`assertions/.frozen`, committed) of every assertion's manifest entry and the test files
  its command references.
- **The runner fail-closes on tamper.** Before running, `conductor assert run` verifies the
  baseline; if a frozen assertion's entry or test file changed, or an assertion was removed,
  it exits `6` (done-gate tampered = NOT done). The worker can't reach green by weakening a
  check — it has to implement the product.
- **Adding is allowed, weakening is not.** Closing a genuine coverage gap *adds* new
  assertions via `/conductor:assertions-to-tests`; frozen ones can't be edited or deleted.
  Product code that a test merely imports is not frozen, so the worker still writes it.
- **The baseline is git-tracked.** Editing `.frozen` itself to launder a weakened check shows
  up in the PR diff, where `/codex` review and `conductor merge-gate`'s unresolved-thread
  block can catch it. That last hop is review, not a mechanical proof — it is the one place
  the gate's integrity still leans on a reviewer, and it is named here on purpose.

This is distinct from the **merge-gate**. `conductor merge-gate <pr>` is per-PR merge
*safety* (not a draft, mergeable, clean state, no changes-requested, no unresolved threads,
and a verify command re-run on the real merge ref); its default verify is `pytest -q`, the
project's test suite, **not** `conductor assert run --level spec`. The merge-gate guards each
merge; the done-gate defines whole-spec completion. They are different checks.

---

## Install

Conductor is a Claude Code plugin that declares `dependencies: ["spec-craft"]`, so a
marketplace install pulls in spec-craft automatically.

### Prerequisites

- **Claude Code** with the conducted skill stack available —
  [superpowers](https://github.com/obra/superpowers),
  [spec-kit](https://github.com/github/spec-kit), `/codex`, `/code-review`,
  `/document-release`. `conductor preflight` checks for every one and fail-closes if any
  is missing.
- **`gh` CLI**, authenticated (`gh auth status`). GitHub issues are the ledger.
- **Python 3.12** on PATH (the runner, ledger, and gate modules are Python).

### Install as a plugin (recommended)

Add the `automateintelligence` marketplace once, then install conductor — its `spec-craft`
dependency is pulled automatically:

```
/plugin marketplace add automateintelligence/marketplace
/plugin install conductor@automateintelligence     # auto-installs the spec-craft dependency
```

CLI equivalents:

```bash
claude plugin marketplace add automateintelligence/marketplace
claude plugin install conductor@automateintelligence
```

> The catalog lives in [automateintelligence/marketplace](https://github.com/automateintelligence/marketplace)
> and lists both plugins, so `claude plugin install spec-craft@automateintelligence` installs
> spec-craft on its own.

### Install locally (dev / `--plugin-dir`)

Clone both repos side by side and load them as plugin directories:

```bash
git clone https://github.com/automateintelligence/spec-craft
git clone https://github.com/automateintelligence/conductor
claude --plugin-dir ./spec-craft --plugin-dir ./conductor
```

In dev mode the plugins aren't in the marketplace cache, so point preflight at spec-craft
(conductor's own root is found automatically):

```bash
export CONDUCTOR_PLUGIN_DIRS="$PWD/spec-craft"
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

### 1. Write the spec, then make "done" explicit

Conductor works best when the spec itself comes out of a structured workflow:
[SuperPowers](https://github.com/obra/superpowers) (`/superpowers:brainstorming` →
`/superpowers:writing-plans`) or [GitHub's spec-kit](https://github.com/github/spec-kit)
(`/speckit:specify` → `/speckit:plan` → `/speckit:tasks`). Both produce specs with
explicit requirements and phased tasks — the exact shape the loop plans and executes
against. A loose prose spec still runs, but the worker has to infer structure those
workflows make explicit.

Then give the spec a checkable definition of done (with spec-craft):

```
/spec-craft:expectations path/to/spec.md            # adds an ## Expectations section
/spec-craft:executable-assertions path/to/spec.md   # derives 4-part assertion specs
```

See the [spec-craft README](https://github.com/automateintelligence/spec-craft) for what
these produce. Conductor needs the assertions; `/conductor:start` stops and points you
here if they are missing. spec-craft stays runner-agnostic and emits an assertion `kind`;
conductor is the runner that maps that `kind` onto the gate's `level` — the `level` column
in `assertions/manifest.yaml` is that handoff boundary working as designed, not a leak.

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

The cron fires `/conductor:autodev` on its interval. Each fire re-loads the goal, keeps the
run branch current with the default branch, reconciles state, checks the done-gate, claims and
builds the next phase in a fresh subagent, and merges it through `conductor merge-gate` — into
the run's integration branch `conductor/run-<slug>`, never your default branch. When the gate
is green and no plans remain, the worker opens ONE final owner-reviewed PR (run branch →
default, body generated by `conductor run-packet`), which only YOU merge, then deletes its own
cron and stops.

The interval is just a **heartbeat**: `CronCreate` fires only while the session is idle, so a tick
never overlaps a running fire (it no-ops until the current phase finishes), and the interval need
not match how long a phase takes. Two limits to know: the recurring cron **auto-expires after 7
days** (re-run `/conductor:start` to continue), and an in-session cron **dies when the terminal
closes**. For a run that survives reboots and closed terminals, install the **Tier-B OS
watchdog**: a flock-guarded resume script firing `claude -p "/conductor:autodev"` (session-detect
no-op while a terminal is live; exits once the gate is green) plus `@reboot` + heartbeat crontab
lines tagged `# conductor-autodev <main-root>` (the main-checkout root, identical from the
run worktree and the owner checkout) — spec in
[`experiments/E5-end-to-end/recovery.md`](experiments/E5-end-to-end/recovery.md).

#### Unattended authority

There is no conductor-specific permission command: an unattended run **inherits the
permission mode of the session you launch `/conductor:start` in**. The permission decision
is made once, at launch, on the same path you already use for every Claude session.

- **Launch in bypass mode** (`claude --dangerously-skip-permissions`) and `start` warns you
  about the standing blast radius — full access on every heartbeat, for the life of the run —
  and requires you to acknowledge before it continues.
- **Launch in a less-privileged mode** and `start` shows a dry-run (`conductor authority
  preview`) naming the concrete privileged operations each phase performs (branch, push,
  `gh pr`, merge, docker via `CONDUCTOR_MERGE_VERIFY`, subagents, file writes) and which of
  them would need you. It then offers three ways forward: relaunch elevated, widen the
  session allowlist, or proceed as-is knowing exactly which steps will wait for you.

Safety floor either way: any `resume-env.sh` conductor writes (the Tier-B watchdog's env
file) is mode `0600`, and the generated driver refuses to source one that is group- or
world-writable.

### 4. Check in, resume, or stop

- **Where is it?** Read the latest handoff in `.conductor/` (local resume scratch), or the
  GitHub issues / milestone (the durable ledger).
- **Resume after a crash or restart:** re-run `/conductor:start path/to/spec.md`. It
  reconciles and continues from the first incomplete step.
- **Stop it early:** `CronList` then `CronDelete` the driver cron (the worker does this
  itself on completion).

When the worker halts on `needs human judgment`, the handoff *is* the recoverable note —
what it did, why it stopped, and the exact command to resume:

```
# Conductor handoff

**Goal / done:** URL shortener passes its spec  (done = `conductor assert run --level spec` exits 0)

**Reference docs:** spec=specs/shortener.md; expectations=specs/shortener.md#expectations;
assertions=assertions/manifest.yaml; plan-index=plan.md; ADRs=docs/ADR/

**Active:** plan=plan.md; milestone=#12; phase issue #18 (status:blocked)

**Last unit:** a1b2c3d..e4f5a6b — implemented the redirect handler; expiry rule still undecided
**Next unit:** HALTED — needs human judgment: the spec doesn't say whether an expired link 404s or 410s

**Open:** debt=#21 feature=#22 blocked=#18
**Branch/worktree:** phase-18-redirects

**Resume:** `claude -p '/conductor:start specs/shortener.md'`
```

---

## CLI reference

The `conductor` command (`bin/conductor`) fronts the Python modules.

| Command | What it does |
|---|---|
| `conductor assert run [--level spec\|phase\|task]` | Run the done-gate. Exit `0` all green, `1` ≥1 red, `2` manifest missing, `3` manifest unparseable, `4` overall timeout, `5` no matching assertions / bad args, `6` done-gate tampered. Fail-closed by design. |
| `conductor ledger generate <plan.json>` | Create the GitHub milestone + phase issues + task sub-issues + labels from a plan dict. Idempotent: a re-run reuses existing milestone/issues (matched by title), never duplicating the hierarchy. |
| `conductor ledger convert <plan.md>` | Parse a Markdown plan (`# Title` / `## Phase [status]` / `- [ ] task`), then generate. |
| `conductor ledger reconcile <issue#> [--tests-red] [--pr-merged] [--commits N] [-R N] [--now-ts N] [-L N]` | Apply the §7 reconcile rules (precedence git/tests > PR > label); the durable per-phase retry count is maintained by reconcile itself and escalates to `status:blocked` at the cap `R`; returns `{action, new_status}`. |
| `conductor goal set <text...>` / `conductor goal get` | Record / read the durable goal (`.conductor/goal.md`). |
| `conductor preflight` | Static availability gate: every conducted skill resolves, else exit 1. |
| `conductor authority preview <plan.md>` | Dry-run of unattended authority: prints, per phase of the plan, every privileged operation an unattended fire performs (branch, push, gh pr, merge, docker via `CONDUCTOR_MERGE_VERIFY`, subagents, file writes), each marked owner-required unless the session pre-authorizes it. |
| `conductor merge-gate <pr>` | Autonomous merge safety gate (see below); exit 0 ok, 1 blocked. |
| `conductor run-branch name <spec.md>` | Emit the canonical run branch for a spec — one line, `conductor/run-<slug>`, deterministic (same spec → byte-identical name; different specs → different names). The single source `start` and `autodev` call instead of deriving the slug in prose. |
| `conductor default-branch` | Emit the repo's default branch — one non-empty line, resolved via `gh repo view` then the `origin/HEAD` symbolic ref; any failure falls open to `main` (exit 0, never empty). |
| `conductor gate {lint\|freeze\|verify}` | Lint the gate for mechanically-detectable weak-test patterns (unpinned commands, no negative clause, trivially-true asserts; fail-closed), then freeze it at setup / verify it is unchanged. Freeze also digests the `<spec>.assertions.md` source. The runner enforces this — see [Why the worker can't cheat the gate](#why-the-worker-cant-cheat-the-gate). |

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
| `conductor/freeze.py` | Done-gate freeze guard: snapshot + verify gate integrity (§5). |
| `docs/specs/2026-06-28-autodev-design.md` | The full design. |
| `docs/plans/` | The build plans (1: spec-craft, 2: done-gate, 3: ledger, 4: autodev+start). |

---

## Cost and footprint

Each tick is one phase executed in a fresh subagent, so cost scales with the number of
phases, not with how long you leave it running. A tick with nothing to do is cheap: it
re-loads the goal, reconciles, runs the done-gate, and exits without spawning the
implementation subagent. The expensive ticks are the ones that actually build a phase
(subagent-driven-development + reviews + merge). A stalled run (gate already green, or
waiting on a halt) costs about a reconcile and a gate run per tick. Pick the cron interval
to trade how fast phases get attempted against how much idle polling you want to pay for — a
short interval is safe, since the cron only fires when idle and can never overlap a running fire.

---

## Status

The MVP is built and merged: the four `/conductor:*` skills, the `conductor` and `ledger`
Python packages, the done-gate runner, and the gate-freeze guard.

Covered by deterministic tests: the assertion runner and its fail-closed exit codes
(including freeze-tamper → exit 6), the ledger (claim/lease, §7 reconcile, issue sync), the
merge-gate, escalation, the handoff writer, `/conductor:start`'s reconcile/idempotency probe,
and stale-lease reclaim (the logic underneath crash-resume).

Not yet in the deterministic suite: the full unattended cron loop end to end, and a
real-process crash-and-resume. Those were exercised once by the gated recorded smoke
(`experiments/E5-end-to-end/promote_check.sh`, behind `RUN_CONDUCTOR_E2E=1`), which reached
green unattended and self-stopped. Read "runs overnight unattended" as validated by that one
smoke run plus the unit-tested reconcile logic, not by a broad soak.
