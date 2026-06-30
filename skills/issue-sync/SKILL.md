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

Parses a Markdown plan (`# Title` / `## Phase [status]` / `- [ ] task` syntax), builds the
plan dict, and delegates to `generate`. Returns the same shape.

### reconcile

`conductor ledger reconcile <issue#> [--tests-red] [--pr-merged] [--commits N] [-R N] [--now-ts N] [-L N]`

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
