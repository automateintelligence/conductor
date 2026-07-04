---
name: prepare
description: Brownfield onboarding — align an existing repo (spec, plan, ledger already present in any state of drift) with conductor's requirements, then hand off to /conductor:start. Plan lint + fix, ledger alignment by assertion-id sets, markers + task sub-issues, statuses derived from the gate, run-branch topology setup. Idempotent and dry-run-first; owner-supervised by design.
---

# /conductor:prepare — brownfield alignment (owner-supervised)

Use when a project already HAS a spec, a plan, or a ledger that predates conductor — or drifted
from it — and the owner wants conductor to drive it. prepare EVALUATES everything against
conductor's requirements, shows the owner what it would change, applies on approval, and ends
with a "ready for `/conductor:start`" report. Re-runnable: every step probes durable state and
skips what is already aligned. (This recipe was executed by hand on the first live project and
verified end-to-end before it became a skill.)

**Owner-supervised by design** — the one conductor skill that is: prepare mutates an EXISTING
repo's artifacts (plan text, issue titles, milestone), so the owner sees every dry-run report
before `--apply`. Contrast issue-sync/autodev, which never prompt.

> **Conductor CLI path:** invoke it as `"$CLAUDE_PLUGIN_ROOT/bin/conductor"` (written `conductor`
> below); installed plugins are not on `PATH`. Run from the project root.

0. **INVENTORY.** Locate: the spec (with `## Expectations`), `<spec>.assertions.md`,
   `assertions/manifest.yaml` + `assertions/.frozen`, the plan(s) under `docs/plans/`, and the
   GitHub milestone/phase issues. Report found/missing.
   - Spec or `<spec>.assertions.md` missing → STOP: point the owner at
     `/spec-craft:expectations` then `/spec-craft:executable-assertions` (prepare aligns
     artifacts; it does not invent the definition of done).
   - Manifest/`.frozen` missing → note it; `/conductor:start` step 3 builds those.
1. **GATE INTEGRITY.** Manifest `id`s must map 1:1 onto the `<spec>.assertions.md` ids —
   extras and gaps are REPORTED, never silently reconciled (adding assertions is
   `/conductor:assertions-to-tests`; removing them is an owner decision). `.frozen` present →
   `conductor gate verify` must be clean; tampered → STOP and show the owner (re-freezing is
   an owner action, never prepare's).
2. **PLAN EVALUATION.** `conductor plan-lint <plan.md> --spec <spec.md>`. On failures, FIX the
   plan to compliance — normative-spec header, per-phase `Spec:` pointers, assertion ids in
   headings (or explicit `gate: none`), `- [ ]` tasks, the per-phase recipe — the spec is
   normative for content; show the owner the diff before committing it. Then codex-review the
   plan **against the spec** (does every spec section land in a phase? intent preserved?) and
   apply fixes. Lint must exit 0 before step 3.
3. **LEDGER ALIGNMENT — dry-run FIRST, always.** `conductor ledger align <plan.md>` and show
   the owner the report: matches (by **assertion-id set** — titles lie, id sets don't),
   renames planned for issues + milestone, unmatched phases/issues, ambiguities. Ambiguities →
   resolve WITH the owner (never guess; align withholds those renames by design). Then
   `conductor ledger align <plan.md> --apply`, then `conductor ledger convert <plan.md>` —
   which now reuses every aligned issue and creates whatever is missing: `conductor-assertions`
   markers and task sub-issues (completed `[x]` tasks never respawn as new sub-issues).
4. **STATUS TRUTH.** `conductor assert run --level spec` (fresh results.json), then for each
   phase issue `conductor ledger reconcile <n> --from-gate` — statuses derive from the gate,
   not from anyone's memory. A phase whose assertions are all green but whose issue is still
   open → `conductor ledger phase-done <n> --plan <plan.md>` (closes sub-issues, ticks the
   plan's boxes). Parked/optional phases → `status:draft` (blocks claiming until the owner
   promotes to `status:ready`).
5. **RUN TOPOLOGY.** Perform `/conductor:start` step 5b's setup now so start sails through:
   run branch `conductor/run-<spec-slug>` (reconcile-first), `<project>/.conductor/run_branch`,
   the run worktree, the default-branch protection probe (honest free-plan messaging). Only
   `CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE=1` (loud, never inferred) skips this.
6. **REPORT — ready for `/conductor:start`.** One summary: gate N/M green; plan lint exit 0;
   ledger aligned (issues renamed, markers written, sub-issues created); statuses truthful;
   run branch + worktree ready; then the exact next command:
   `/conductor:start <spec.md>` from the project root. List everything only the owner can
   decide: unresolved ambiguities, gate integrity problems, protection unavailable
   (free-plan), gateless phases relying on `--no-gate-check`.
