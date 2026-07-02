---
name: issue-sync
description: Automate GitHub issue hierarchy management — generate a milestone/phase/task sub-issue hierarchy from a plan dict, convert a plan.md file, or reconcile an issue against §7 rules — fully headless, never prompt the user.
---

# /conductor:issue-sync

Fully automated. The skill **never prompts** the user or pauses for confirmation; all three
operations run headlessly via `conductor ledger` sub-commands backed by `python3 -m ledger`.

> **Conductor CLI path:** invoke it as `"$CLAUDE_PLUGIN_ROOT/bin/conductor"` (written `conductor`
> below); installed plugins are not on `PATH`.

## Operations

### generate

`conductor ledger generate <plan.json>`

Reads a JSON plan dict `{title, phases: [{title, status, tasks: [...]}]}` and creates:

1. One GitHub milestone for the plan title.
2. One phase issue per phase, labelled `status:<phase.status>`, attached to the milestone.
3. One task issue per task string, attached to the same milestone and linked to its parent
   as a **sub-issue** via `POST /repos/{repo}/issues/{parent}/sub_issues`.

If the sub-issue API is unavailable, `generate` falls back to writing a checklist
(`- [ ] #N`) in the phase-issue body and sets `fallback: true` in the returned dict.

Returns `{milestone, phases: [{number, sub_issues: [int, ...], fallback}]}`.

**Idempotent:** `generate` reuses an existing milestone/issues (matched by exact title within
the milestone) instead of creating duplicates, so re-invoking `/conductor:start` — or retrying
after a `gh` call fails partway — never duplicates the hierarchy or doubles the work.

### convert

`conductor ledger convert <plan.md>`

Parses a Markdown plan and delegates to `generate`. Returns the same shape. Two heading
dialects are accepted — an H2 is a **phase** when it has a trailing `[status]` (conductor
dialect) OR starts with `Phase` (the dialect real plan-writing skills emit; status defaults
to `ready`):

```
## Phase 1 — Relationship-quality scoring (A3, A4, A5)
## Backend [draft]
```

A phase's `- [ ] task` lines run to the next H2 of ANY kind, so a non-phase section
(`## Global Constraints`, `## CI notes`) can never leak tasks into the preceding phase.
**Assertion binding:** ids in the heading's trailing parens (`(A3, A4, A5)` or
`(A8/A16/A19)`) are extracted and written into the phase-issue body as a machine-readable
marker:

```
<!-- conductor-assertions: A3,A4,A5 -->
```

`generate` also backfills/updates this marker on reused phase issues (idempotent — an
unchanged marker is never rewritten). The marker is what lets `reconcile --from-gate` and
`phase-done` DERIVE test state from the done-gate instead of trusting caller flags.

### reconcile

`conductor ledger reconcile <issue#> [--tests-red | --from-gate [--results PATH]] [--pr-merged] [--commits N] [-R N] [--now-ts N] [-L N]`

**Prefer `--from-gate`** (mutually exclusive with `--tests-red`): it derives the phase's
test state from the runner's `results.json` (default `<project>/assertions/run/results.json`;
run `conductor assert run --level spec` first) via the issue's `conductor-assertions` marker —
ground truth instead of a worker-reported flag. Marker tokens resolve to manifest ids
exactly, case-insensitively, or by letters+number prefix (`A3` → `a03-…`, never `a30-…`).
Fail-closed: a missing marker, missing results file, or unresolved token exits with a
distinct error — it can never silently read as green.

Applies §7 reconcile rules to a single issue. Returns `{action, new_status}`.

**Precedence**: `git/tests > PR > label` — test-suite state overrides merge state, which
overrides the current status label. Rule evaluation order:

1. **Stale-lease reclaim** (runs first, before retry cap): if the issue is
   `status:in-progress` with an assignee but the `conductor-lease` timestamp in the body is
   more than `L` seconds older than `now_ts`, reconcile unassigns all workers and resets to
   `status:ready`. This ensures a **stale** lease is reclaimed before the retry counter can
   fire. reconcile **resets the durable retry counter itself** on a `stale-lease-reclaim`.
2. **Retry cap**: a live-owned `status:in-progress` phase that is still `tests_red` has its
   **durable** attempt count (a body marker) bumped; at `>= R` → `status:blocked` (escalates,
   so a genuinely failing phase stops looping forever instead of retrying every fire).
3. **Done + tests red**: reopen and set `status:in-progress` (invalid combination repair).
4. **Abandoned** (`status:in-progress`, no assignee): reset to `status:ready`.
5. **Closed, PR not merged**: reopen and set `status:in-progress`.
6. Otherwise: `action: none`.

### phase-done

`conductor ledger phase-done <issue#> [--plan <plan.md>] [--results PATH] [--no-gate-check]`

**Atomic end-of-phase bookkeeping** — one command replacing the clerical steps workers
reliably drop (dogfood evidence: 0/27 plan checkboxes, labels never maintained). Fail-closed:
it first verifies every id in the issue's `conductor-assertions` marker is GREEN in
`results.json` (same resolution rules as `--from-gate`); a red/unresolved/missing anything
returns an error and touches **nothing**. Only `--no-gate-check` (explicit) skips that.
On success, in order:

1. Label `status:done` (other `status:*` removed).
2. Close every task sub-issue (checklist-fallback bodies get `- [ ] #N` → `- [x] #N`).
3. Unassign all workers; strip the `conductor-lease` + `conductor-attempts` markers
   (the `conductor-assertions` marker is preserved).
4. Close the phase issue.
5. With `--plan`: tick every `- [ ]` in the plan section whose phase heading equals the
   issue title (best-effort: a missing section is reported in the result, never fatal).

Returns `{ok, issue, sub_issues_closed, checklist_ticked, plan?}`; exits 1 when not ok.

## Sub-Issue Hierarchy

`generate` and `convert` build a three-tier hierarchy on GitHub:

- **Milestone** (plan title) → groups all phase and task issues
  - **Phase issue** (`status:<x>` label) → parent node in the GitHub sub-issue API
    - **Task sub-issue** (one per task string) → child linked via the sub-issue API

`reconcile` operates on a single phase issue. It never touches the milestone or task
sub-issues directly — those are managed by `generate`/`convert`.

## Stale Lease Handling

A lease is written as an HTML comment in the issue body during `claim`:

```
<!-- conductor-lease worker=<login> ts=<unix-ts> -->
```

The durable per-phase retry count is a sibling marker in the same body, maintained entirely by
`reconcile` (the worker never passes or resets it):

```
<!-- conductor-attempts n=<count> -->
```

`reconcile` reads the lease marker when checking for a **stale** lease. Reclaim runs before the
retry cap so that a worker that crashed does not consume a retry slot — reconcile resets the
durable `conductor-attempts` counter on a `stale-lease-reclaim` action.

## Design Constraints

- **Never prompt**: the skill is fully automated (design §5). No interactive confirmations.
- **Precedence enforced**: `git/tests > PR > label` is the fixed evaluation order in
  `reconcile`; higher-priority signals always override lower ones.
- **Sub-issue fallback**: if the GitHub sub-issue API fails, a checklist body is written
  instead, keeping `generate` safe on older API plans.
- **Stale reclaim before retry cap**: a **stale** worker lease is always reclaimed first so
  that retry exhaustion only penalises a genuinely live, repeatedly-failing owner.
