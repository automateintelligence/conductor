# Conductor MVP — Plan 4: `/conductor:autodev` + `/conductor:start` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the **worker** `/conductor:autodev` (one fire = one phase: reload goal →
reconcile → spec-done gate → pick → claim → execute the recipe in a fresh subagent → merge
through the safety gate → escalate or record → handoff → exit) and the **supervisor**
`/conductor:start` (reconcile-first, idempotent setup that launches the loop) — the in-session,
single-loop MVP runtime.

**Architecture:** Two skills + three small tested Python modules (`merge_gate`, `handoff`,
`escalate`). The skills are instruction sets that orchestrate the Plans 1–3 foundation
(`/spec-craft:*`, `conductor assert run`, `ledger.*`) and external conducted skills
(`/superpowers:*`, `/code-review`, `/codex`, `/document-release`). The driver is a cron
`/loop /conductor:autodev`. Promotes Stage-0 **E0/E1/E2/E5** (validated: loop fires + self-stops;
fresh-context relaunch; `baseline..final` subagent bracket; end-to-end conductor→loop→green +
reconcile-first idempotency).

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`, `gh`,
cron `/loop`.

## Global Constraints

- **One phase per fire** (§6.1) with bounds: pre-execution **split-check**; per-phase retry cap
  `R`; per-fire runtime/token budget → checkpoint(commit)+handoff if exceeded.
- **Anti-laziness (§3):** the driver is external (cron); the goal is re-loaded every fire; done
  is a machine check (`conductor assert run --level spec` exit 0) — the agent cannot end the run.
- **Merge safety gate (§6.2):** autonomous `--merge` proceeds only if ALL hold; else block →
  resolve/escalate, never force-merge. Fail-closed (any unknown state blocks).
- **Reconcile-first & idempotent (amendment B):** `/conductor:start` AND `/conductor:autodev`
  read durable ground truth first, are safely re-runnable, and resume from the first incomplete
  step. Heavy work runs in fresh subagents (thin session, §4 / amendment A).
- **Durable every iteration (§4):** commit + push git AND write GitHub issues AND write a
  handoff, so a fresh process/container resumes with zero local context.
- **Requires the conducted stack (amendment E):** `/spec-craft:*`, `/superpowers:*`, `/codex`,
  `/code-review`, `/document-release` must be installed wherever conductor runs (conductor's
  manifest declares `dependencies: ["spec-craft"]`; the rest are environment preconditions).
- **Namespacing (locked, amendment F):** supervisor `/conductor:start`, worker
  `/conductor:autodev`; conducted skills keep their namespace. No bare names / aliases.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `conductor/merge_gate.py` | §6.2 autonomous-merge precondition check → `{ok, blockers}` (fail-closed). |
| `conductor/handoff.py` | §4 handoff payload builder/writer (validates required fields). |
| `conductor/escalate.py` | §9 helpers: file `debt`/`feature` follow-up, block-on-subplan, write ADR. |
| `skills/autodev/SKILL.md` | `/conductor:autodev` — §8 per-fire algorithm + §6 recipe. |
| `skills/start/SKILL.md` | `/conductor:start` — §3 reconcile-first setup + launch the loop. |
| `bin/conductor` | add `goal {set,get}`, `merge-gate <pr>`, `handoff` subcommands. |
| `tests/conductor/test_*.py` | unit (mocked) for merge_gate/handoff/escalate; skill structural; gated E2E. |

---

## Task 1: Merge safety gate (`conductor/merge_gate.py`, §6.2)

**Files:** Create `conductor/merge_gate.py`, `tests/conductor/__init__.py`, `tests/conductor/test_merge_gate.py`

**Interfaces:**
- `check(repo, pr, *, local_verify, pr_view=_pr_view, run=subprocess.run) -> {"ok": bool, "blockers": [str]}`.
  Fail-closed: a non-`CLEAN` merge state, non-`MERGEABLE`, changes-requested, or a failing local
  re-verify each blocks.

- [ ] **Step 1: Write failing tests**

```python
# tests/conductor/test_merge_gate.py
from conductor import merge_gate

def _ok_pr():
    return {"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "reviewDecision": "APPROVED"}

def test_all_green_passes():
    out = merge_gate.check("o/r", 1, local_verify="true",
                           pr_view=lambda r, p: _ok_pr(), run=lambda c, shell: type("R", (), {"returncode": 0})())
    assert out == {"ok": True, "blockers": []}

def test_behind_branch_blocks():
    pr = {**_ok_pr(), "mergeStateStatus": "BEHIND"}
    out = merge_gate.check("o/r", 1, local_verify="true",
                           pr_view=lambda r, p: pr, run=lambda c, shell: type("R", (), {"returncode": 0})())
    assert not out["ok"] and any("merge-state" in b for b in out["blockers"])

def test_changes_requested_blocks():
    pr = {**_ok_pr(), "reviewDecision": "CHANGES_REQUESTED"}
    out = merge_gate.check("o/r", 1, local_verify="true",
                           pr_view=lambda r, p: pr, run=lambda c, shell: type("R", (), {"returncode": 0})())
    assert "changes-requested" in out["blockers"]

def test_local_verify_failure_blocks():
    out = merge_gate.check("o/r", 1, local_verify="false",
                           pr_view=lambda r, p: _ok_pr(), run=lambda c, shell: type("R", (), {"returncode": 1})())
    assert "local-verify-failed" in out["blockers"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import json, subprocess

def _pr_view(repo, pr):
    out = subprocess.run(
        ["gh", "pr", "view", str(pr), "-R", repo, "--json",
         "mergeStateStatus,mergeable,reviewDecision"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)

def check(repo, pr, *, local_verify, pr_view=_pr_view, run=subprocess.run):
    """§6.2 autonomous-merge preconditions, fail-closed. `local_verify` is a shell command
    re-run on the merge ref (do not trust CI alone)."""
    d = pr_view(repo, pr)
    blockers = []
    if d.get("mergeStateStatus") != "CLEAN":            # CLEAN = checks green + reviews ok + current
        blockers.append(f"merge-state:{d.get('mergeStateStatus')}")
    if d.get("mergeable") != "MERGEABLE":               # CONFLICTING/UNKNOWN -> fail-closed
        blockers.append(f"mergeable:{d.get('mergeable')}")
    if d.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("changes-requested")
    if run(local_verify, shell=True).returncode != 0:   # re-verify locally on the merge ref
        blockers.append("local-verify-failed")
    return {"ok": not blockers, "blockers": blockers}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T1: merge_gate (§6.2 fail-closed)`).

---

## Task 2: Handoff writer (`conductor/handoff.py`, §4)

**Files:** Create `conductor/handoff.py`, `tests/conductor/test_handoff.py`

**Interfaces:**
- `build(ctx: dict) -> str`; `write(ctx: dict, path: str) -> str`. Required ctx keys: `goal`,
  `paths` (spec/expectations/assertions/plan_index/adr_dir), `active_plan`, `milestone`,
  `phase_issue`, `phase_status`, `baseline`, `final`, `last_unit_summary`, `next_unit`,
  `open_issues` (debt/feature/blocked), `branch`, `resume_cmd`. `write` raises on a missing field.

- [ ] **Step 1: Write failing tests**

```python
# tests/conductor/test_handoff.py
import pytest
from conductor import handoff

def _ctx(**over):
    ctx = {"goal": "ship X", "paths": {"spec": "s", "expectations": "e", "assertions": "a",
           "plan_index": "p", "adr_dir": "d"}, "active_plan": "plan-1", "milestone": 3,
           "phase_issue": 7, "phase_status": "status:in-progress", "baseline": "aaa",
           "final": "bbb", "last_unit_summary": "did thing", "next_unit": "phase 2",
           "open_issues": {"debt": [1], "feature": [], "blocked": []}, "branch": "feat/x",
           "resume_cmd": "claude -p '/conductor:start <spec>'"}
    ctx.update(over); return ctx

def test_build_includes_required_payload():
    md = handoff.build(_ctx())
    for needle in ["ship X", "assert run --level spec", "aaa..bbb", "#7", "claude -p",
                   "status:in-progress", "feat/x"]:
        assert needle in md, needle

def test_write_rejects_missing_field(tmp_path):
    with pytest.raises(ValueError):
        handoff.write({"goal": "x"}, str(tmp_path / "h.md"))
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
REQUIRED = ["goal", "paths", "active_plan", "milestone", "phase_issue", "phase_status",
            "baseline", "final", "last_unit_summary", "next_unit", "open_issues", "branch",
            "resume_cmd"]

def build(ctx):
    p = ctx["paths"]; oi = ctx["open_issues"]
    return f"""# Conductor handoff

**Goal / done:** {ctx['goal']}  (done = `conductor assert run --level spec` exits 0)

**Reference docs:** spec={p['spec']}; expectations={p['expectations']}; assertions={p['assertions']};
plan-index={p['plan_index']}; ADRs={p['adr_dir']}

**Active:** plan={ctx['active_plan']}; milestone=#{ctx['milestone']}; phase issue #{ctx['phase_issue']} ({ctx['phase_status']})

**Last unit:** {ctx['baseline']}..{ctx['final']} — {ctx['last_unit_summary']}
**Next unit:** {ctx['next_unit']}

**Open:** debt={oi['debt']} feature={oi['feature']} blocked={oi['blocked']}
**Branch/worktree:** {ctx['branch']}

**Resume:** `{ctx['resume_cmd']}`
"""

def write(ctx, path):
    missing = [k for k in REQUIRED if k not in ctx]
    if missing:
        raise ValueError(f"handoff missing required fields: {missing}")
    with open(path, "w") as f:
        f.write(build(ctx))
    return path
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T2: handoff writer (§4 payload)`).

---

## Task 3: Escalation helpers (`conductor/escalate.py`, §9)

**Files:** Create `conductor/escalate.py`, `tests/conductor/test_escalate.py`

**Interfaces:**
- `file_followup(repo, kind, title, body, link_issue=None, gh=ledger.gh) -> int` (kind ∈
  {debt, feature}; labels + optional back-link comment). `block_on_subplan(repo, phase_issue, gh)`
  (set `status:blocked` + `blocked-on-subplan`, remove in-progress). `write_adr(adr_dir, slug, body) -> str`.

- [ ] **Step 1: Write failing tests**

```python
# tests/conductor/test_escalate.py
from unittest.mock import MagicMock
from conductor import escalate

def test_file_followup_labels_and_links():
    gh = MagicMock(); gh.create_issue.return_value = {"number": 42, "id": 1}
    n = escalate.file_followup("o/r", "debt", "hard bit", "what's hard", link_issue=7, gh=gh)
    assert n == 42
    gh.ensure_label.assert_called_with("o/r", "debt")
    gh._gh_api.assert_called()                       # back-link comment on #7

def test_block_on_subplan_sets_labels():
    gh = MagicMock()
    escalate.block_on_subplan("o/r", 7, gh=gh)
    args, kwargs = gh.set_labels.call_args
    assert "status:blocked" in kwargs.get("add", []) and "blocked-on-subplan" in kwargs.get("add", [])

def test_write_adr(tmp_path):
    p = escalate.write_adr(str(tmp_path), "deepen-phase-2", "## Decision\nDid X.")
    assert p.endswith("deepen-phase-2.md")
    content = open(p).read()
    assert "deepen-phase-2" in content and "Did X." in content
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import os
from ledger import gh as _gh

def file_followup(repo, kind, title, body, link_issue=None, gh=_gh):
    assert kind in ("debt", "feature")
    gh.ensure_label(repo, kind)
    issue = gh.create_issue(repo, title, body, labels=[kind])
    if link_issue:
        gh._gh_api("POST", f"repos/{repo}/issues/{link_issue}/comments",
                   body={"body": f"Excavated {kind}: #{issue['number']}"})
    return issue["number"]

def block_on_subplan(repo, phase_issue, gh=_gh):
    gh.set_labels(repo, phase_issue, add=["status:blocked", "blocked-on-subplan"],
                  remove=["status:in-progress"])

def write_adr(adr_dir, slug, body):
    os.makedirs(adr_dir, exist_ok=True)
    path = os.path.join(adr_dir, f"{slug}.md")
    with open(path, "w") as f:
        f.write(f"# ADR: {slug}\n\n{body}\n")
    return path
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T3: escalate helpers (§9)`).

---

## Task 4: `/conductor:autodev` skill (§8 algorithm + §6 recipe)

The worker. One fire = one phase. Promotes E5's `autodev_e5` with the real ledger + recipe.

**Files:** Create `skills/autodev/SKILL.md`; test `tests/conductor/test_skill_outputs.py`

- [ ] **Step 1: Structural test**

```python
# tests/conductor/test_skill_outputs.py
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_autodev_skill_contract():
    body = open(os.path.join(ROOT, "skills/autodev/SKILL.md")).read().lower()
    for needle in ["re-load goal", "reconcile", "assert run --level spec", "fresh subagent",
                   "merge_gate", "handoff", "one phase", "crondelete", "no questions"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write the skill** (`skills/autodev/SKILL.md`, `name: autodev`):

```markdown
---
name: autodev
description: The conductor worker. One fire = one phase of progress toward the spec's done-gate. Re-loads the goal, reconciles from durable state, runs the machine done-gate, claims and executes the next phase in a fresh subagent via the recipe, merges through the safety gate, writes a handoff, and exits. Driven by cron /loop; never ends the run itself.
---

# /conductor:autodev — one phase per fire (§8)

Autonomous. **Ask no questions.** Do exactly one coherent phase, then exit. The cron `/loop`
re-fires you; you cannot end the run — only a green done-gate (or an explicit escalation-halt)
stops it.

1. **RE-LOAD GOAL (fresh context).** The spec is done only when `conductor assert run --level
   spec` exits 0. Re-read goal + paths from the durable handoff/ledger; trust git/issues, not memory.
2. **RECONCILE (precedence git/tests > PR > label).** For the active phase issue call
   `ledger.reconcile(..., now_ts=<now>, L=<lease ttl>)`. If it returns `stale-lease-reclaim`,
   **reset that phase's retry counter**. PROGRESS SELF-CHECK: did the last unit advance the
   spec? looping? ballooning past the plan?
3. **SPEC-DONE GATE.** Run `conductor assert run --level spec` (fail-closed; an unrunnable gate
   is NOT done). If **all green AND no plans left** → mark the spec done, `CronDelete` the loop,
   write a final handoff, STOP.
4. **PICK the next eligible phase** (unassigned & not blocked/done; climb the ladder):
   - phase available → **SPLIT-CHECK (§6.1):** estimate size (task count / diff / token budget);
     if over threshold, deepen-in-place split first (§9) instead of an oversized fire. Else run
     the recipe below.
   - plan done (phases all green) → `/superpowers:writing-plans` next plan → `ledger.generate`.
   - no plans left but assertions red → `/superpowers:writing-plans` to close the gap → generate.
5. **CLAIM.** `ledger.claim(phase, worker, now_ts, ttl)` (assign self + lease; sets
   `status:in-progress`). If claim returns False, back off and re-pick.
6. **EXECUTE the phase in a FRESH SUBAGENT** via the recipe (one PR per phase):
   1. `/superpowers:subagent-driven-development` to implement the phase's tasks.
   2. `/code-review` (self-review) of each task completion.
   3. **commit after every task** (`<files-changed> — <description>`).
   4. **one PR per phase** (`Closes #<phase-issue>`).
   5. `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<n>`.
   6. `/superpowers:receiving-code-review` — apply Codex fixes, commit, comment on the PR.
   7. **merge `--merge` (no squash) ONLY if `conductor merge-gate <pr>` is ok** (§6.2). Else
      block → resolve or escalate; never force-merge.
   8. `/document-release`.
   Capture `baseline_revision..final_revision` (git HEAD before/after) — equal means it did nothing.
   Respect the **per-fire budget**: if exceeded mid-phase, checkpoint (commit) + handoff and let
   the next fire resume the owned unit.
7. **ESCALATION (§9) at design friction.** Ask: patchable later, or a structural must-build-now?
   - patch-later → `conductor/escalate.py file_followup(debt|feature)` + link comment; continue.
   - build-now → one bounded deepen-in-place: `/superpowers:writing-plans` scoped to this
     phase → generate sub-plan; `escalate.block_on_subplan(phase)`; the same loop works the
     sub-plan; on completion write an ADR (`escalate.write_adr`). The parent unblocks when the
     sub-plan goes green.
   - build-now AND needs human judgment (ambiguous spec / architecture / secrets) → **halt** with
     a handoff + issue. The ONLY branch that pages the user.
   On process failure (crash/limit) → just exit; the next fire reconciles (§10).
8. **RECORD.** Update label/progress; commit; update the `plan.md` index; renew or release the
   lease (`ledger.release` on unit completion).
9. **WRITE HANDOFF (§4)** (`conductor/handoff.py`) + commit + **push**. EXIT.
```

- [ ] **Step 4: Run → PASS.** **Commit** (`Plan4 T4: /conductor:autodev skill (§8 + §6 recipe)`).

---

## Task 5: `/conductor:start` skill (§3 reconcile-first setup)

The supervisor you invoke once. Reconcile-first + idempotent (amendment B): re-invoking detects
existing state and resumes. Promotes E5's `conductor_e5` idempotency.

**Files:** Create `skills/start/SKILL.md`; modify `bin/conductor` (add `goal {set,get}`); test `tests/conductor/test_skill_outputs.py`

- [ ] **Step 1: Structural test**

```python
# add to tests/conductor/test_skill_outputs.py
def test_start_skill_contract():
    body = open(os.path.join(ROOT, "skills/start/SKILL.md")).read().lower()
    for needle in ["reconcile-first", "idempotent", "spec-craft:executable-assertions",
                   "conductor:assertions-to-tests", "issue-sync", "/loop /conductor:autodev",
                   "already done", "resume"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write the skill** (`skills/start/SKILL.md`, `name: start`):

```markdown
---
name: start
description: Start (or resume) an autonomous conductor run for a spec. Reconcile-first and idempotent — re-invoking detects existing setup and resumes from the first incomplete step. Validates the done-gate precondition, turns the spec's assertions into tests, syncs the GitHub issue ledger, records the goal, and starts the cron loop. Invoke once; after that the loop drives /conductor:autodev.
---

# /conductor:start — set up + launch the loop (§3, reconcile-first)

**Idempotent (amendment B): each step probes durable state first and SKIPS if already done.**
Heavy steps run in fresh subagents (thin session). Never prompt unless a precondition genuinely
needs human judgment.

1. **Detect spec source** (superpowers or spec-kit); load spec + Expectations + Executable Assertions.
2. **PRECONDITION — assertions present?** No → STOP and point the user at
   `/spec-craft:expectations` then `/spec-craft:executable-assertions` (or, with `--auto-assert`,
   launch them yourself). The done-gate cannot exist without assertion specs (§5).
3. **Implement assertion specs as runnable tests** via `/conductor:assertions-to-tests` →
   `assertions/manifest.yaml` + the runner (§5.1–5.2). SKIP if the manifest exists and
   `conductor assert run` executes (reconcile).
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit `/plan`+`/tasks`) to author
   plan 1, in a fresh subagent. SKIP if a plan index / milestone exists.
5. **issue-sync** — `ledger.generate` (or `convert` an existing `plan.md`/`tasks.md`). SKIP if
   the hierarchy exists; otherwise reconcile it.
6. **Record the `/goal`** (`conductor goal set`) and **start the driver:** register the cron
   `/loop /conductor:autodev` (one fire = one phase). SKIP if the loop is already registered
   (don't double-register).
7. **(Phase 2, multi-loop only)** start the dispatcher loop (§3/§7).

After setup, single-loop `/conductor:start` is idle; the cron drives `/conductor:autodev` to a
green done-gate. A restart = re-invoke `/conductor:start` → it reconciles and resumes (recovery
Tier B/A, amendment C).
```

- [ ] **Step 4: `bin/conductor goal {set,get}`** — store the goal/done-condition in
  `.conductor/goal.md` (set) / print it (get).

- [ ] **Step 5: Run → PASS.** **Commit** (`Plan4 T5: /conductor:start skill (§3 reconcile-first) + conductor goal CLI`).

---

## Task 6: Driver + end-to-end integration (promote E5)

Validate the full single-loop runtime unattended, idempotency, and re-fire recovery.

**Files:** Create `tests/conductor/test_e2e.py`

- [ ] **Step 1: Write the E2E (gated; promotes E5)**

```python
# tests/conductor/test_e2e.py — @pytest.mark.integration, skipped unless RUN_CONDUCTOR_E2E=1
import os, subprocess, sys, textwrap, pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_CONDUCTOR_E2E") != "1",
                                reason="set RUN_CONDUCTOR_E2E=1 to run the unattended loop E2E")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_unattended_micro_spec_reaches_green(tmp_path):
    # A trivial spec with ONE spec-level assertion that starts RED; the loop must drive it green
    # and self-stop with zero intervention (mirror E5: conductor_e5 + autodev_e5 step).
    # Uses the real `conductor assert run --level spec` gate + a stubbed one-phase recipe that
    # implements the behavior. Asserts: final `conductor assert run --level spec` exit 0;
    # the cron self-deleted; a handoff was written.
    ...  # harness mirrors experiments/E5-end-to-end/{conductor_e5.sh,autodev_e5_step.sh}
        # promoted onto /conductor:start + /conductor:autodev + ledger + runner.

def test_start_is_idempotent(tmp_path):
    # Run the /conductor:start setup twice; second run reports every step "already done"
    # (no double-register of the cron, no duplicate milestone). Mirrors E5 idempotency proof.
    ...

def test_refire_reconciles_after_killed_phase(tmp_path):
    # Kill a phase mid-fire (no/partial commits, label still in-progress); the next fire
    # reconciles (commit-if-coherent or reset), bounded retries, no double-work (§10).
    ...
```

> The `...` bodies are filled by promoting the validated Stage-0 harness
> (`experiments/E5-end-to-end/`): `conductor_e5.sh` → the `/conductor:start` idempotent setup,
> `autodev_e5_step.sh` → one `/conductor:autodev` fire driving the real `conductor assert run
> --level spec` gate. The gate, cron self-stop, fresh-context relaunch, and reconcile-first
> idempotency are already E0/E1/E5-validated; this test wires them onto the real skills.

- [ ] **Step 2: Run gated E2E locally** (`RUN_CONDUCTOR_E2E=1 pytest tests/conductor/test_e2e.py -v`):
  the loop reaches `conductor assert run --level spec` exit 0 unattended and self-stops; idempotent
  re-run skips; killed-phase re-fire reconciles. Record output.

- [ ] **Step 3: Plugin discovery + full quality gate**

```bash
claude plugin validate . --strict
test -f skills/autodev/SKILL.md && test -f skills/start/SKILL.md && echo "WORKER+SUPERVISOR PRESENT"
ruff check . && ruff format --check . && pyright . && pytest -q
```

- [ ] **Step 4: Commit** (`Plan4 T6: driver + unattended E2E + idempotency + recovery (promote E5)`).

---

## Self-Review

**Coverage (§11 comps 6–7; §3/§4/§6/§8/§9/§10):**
- §8 per-fire algorithm → T4 (reload goal → reconcile → done-gate → pick/split-check → claim →
  execute-in-subagent → merge-gate → escalate → record → handoff → exit).
- §6 recipe + §6.2 merge gate → T4 recipe + T1 `merge_gate.check`.
- §9 escalation (patch-later / build-now deepen-in-place + ADR / page-user) → T4 + T3.
- §3 reconcile-first setup → T5 (idempotent step-skips) + `conductor goal`.
- §4 handoff → T2 + T4 step 9.
- §10 recovery (re-fire reconciles) → T6 `test_refire_reconciles_after_killed_phase`.
- Anti-laziness (external driver, re-loaded goal, machine done) → T4 + T6.

**Placeholder scan:** the tested modules (merge_gate, handoff, escalate) are shown in full. The
T6 E2E bodies are intentionally "promote the validated E5 harness" (the gate/cron/idempotency
are already Stage-0-proven) — this is a wiring task over existing validated code, not a
placeholder for novel logic; the assertions to check are spelled out.

**Type/name consistency:** `merge_gate.check(...)→{ok,blockers}`, `handoff.build/write`,
`escalate.file_followup/block_on_subplan/write_adr`, `ledger.reconcile/claim/release/generate`,
`conductor assert run --level spec` used consistently; `/conductor:start` + `/conductor:autodev`
namespacing uniform; cron drives `/loop /conductor:autodev`.

**Cross-plan:** consumes Plan 1 (`/spec-craft:*`), Plan 2 (`conductor assert run`,
`/conductor:assertions-to-tests`), Plan 3 (`ledger.*`). Completes the MVP (components 1–7).

---

## MVP plan set complete
Plan 1 (spec-craft) · Plan 2 (done-gate) · Plan 3 (ledger) · Plan 4 (autodev + start). Phase 2
(cloud `/schedule` + watchdog + 5hr-resume; multi-loop + dispatcher; optional hook backstop)
remains separate later plans (§11 components 8–10).
