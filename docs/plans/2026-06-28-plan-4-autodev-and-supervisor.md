# Conductor MVP — Plan 4: `/conductor:autodev` + `/conductor:start` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the **worker** `/conductor:autodev` (one fire = one phase: reload goal →
reconcile → spec-done gate → pick → claim → execute the recipe in a fresh subagent → merge
through the safety gate → escalate or record → handoff → exit) and the **supervisor**
`/conductor:start` (reconcile-first, idempotent setup that launches the loop) — the in-session,
single-loop MVP runtime.

**Architecture:** Two skills + three tested Python modules (`merge_gate`, `handoff`,
`escalate`). The skills orchestrate the Plans 1–3 foundation (`/spec-craft:*`, `conductor assert
run`, `ledger.*`) and external conducted skills. The driver is a cron `/loop /conductor:autodev`.
Promotes Stage-0 **E0/E1/E2/E5**.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`, `gh`,
cron `/loop`.

## Global Constraints

- **One phase per fire** (§6.1) with bounds: pre-execution **split-check**; per-phase retry cap
  `R`; per-fire runtime/token budget → checkpoint(commit)+handoff if exceeded.
- **Anti-laziness (§3):** external driver; goal re-loaded every fire; done is a machine check
  (`conductor assert run --level spec` exit 0). The agent cannot end the run.
- **Merge safety gate (§6.2), fail-closed:** autonomous `--merge` proceeds only if ALL hold:
  required checks green, branch current with base, no changes-requested, **no unresolved review
  threads**, no conflicts, **local re-verify green on the merge ref**, branch-protection/merge-queue
  satisfied. Any unknown/missing state blocks → resolve or escalate; never force-merge.
- **Reconcile-first & idempotent (amendment B):** `/conductor:start` AND `/conductor:autodev`
  read durable ground truth first, are re-runnable, resume from the first incomplete step. Heavy
  work in fresh subagents (thin session, amendment A).
- **Durable every iteration (§4):** commit+push git AND write issues AND write a handoff.
- **Requires the conducted stack (amendment E):** `/spec-craft:*` (via `dependencies:
  ["spec-craft"]`), `/superpowers:*`, and the environment-provided `/code-review`, `/codex`,
  `/document-release`. `/conductor:start` PREFLIGHTS their availability and fails closed if any
  is missing (Codex #4).
- **Namespacing (locked, amendment F):** supervisor `/conductor:start`, worker
  `/conductor:autodev`; conducted skills keep their namespace. No bare names / aliases.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `conductor/merge_gate.py` | §6.2 full precondition check → `{ok, blockers}` (CLEAN + threads + merge-ref verify; fail-closed). |
| `conductor/handoff.py` | §4 handoff payload builder/writer (validates required fields). |
| `conductor/escalate.py` | §9 helpers: file `debt`/`feature`, block-on-subplan, write ADR. |
| `conductor/preflight.py` | verify the conducted skill/plugin stack is available (amendment E / Codex #4). |
| `skills/autodev/SKILL.md` | `/conductor:autodev` — §8 per-fire algorithm + §6 recipe. |
| `skills/start/SKILL.md` | `/conductor:start` — preflight + §3 reconcile-first setup + launch. |
| `bin/conductor` | add `goal {set,get}`, `merge-gate <pr>`, `preflight`, `handoff`. |
| `tests/conductor/test_*.py` | unit (mocked) for the modules; skill structural; gated deterministic E2E. |

---

## Task 1: Merge safety gate (`conductor/merge_gate.py`, §6.2) — full gate (Codex #1)

**Files:** Create `conductor/merge_gate.py`, `tests/conductor/__init__.py`, `tests/conductor/test_merge_gate.py`

**Interfaces:**
- `check(repo, pr, *, local_verify, gh_json=_gh_json, threads=_unresolved_threads,
  merge_ref_verify=_merge_ref_verify) -> {"ok": bool, "blockers": [str]}`. Seams are injectable
  for tests. Fail-closed: any non-green signal blocks.

- [ ] **Step 1: Write failing tests**

```python
# tests/conductor/test_merge_gate.py
from conductor import merge_gate

def _clean():
    return {"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED", "isDraft": False}

def _call(pr_json, *, threads=False, merge_ref_ok=True):
    return merge_gate.check("o/r", 1, local_verify="true",
                            gh_json=lambda r, p, f: pr_json,
                            threads=lambda r, p: (["t1"] if threads else []),
                            merge_ref_verify=lambda r, p, lv: merge_ref_ok)

def test_all_green_passes():
    assert _call(_clean()) == {"ok": True, "blockers": []}

def test_behind_or_blocked_merge_state_blocks():
    assert "merge-state:BEHIND" in _call({**_clean(), "mergeStateStatus": "BEHIND"})["blockers"]
    assert "merge-state:BLOCKED" in _call({**_clean(), "mergeStateStatus": "BLOCKED"})["blockers"]

def test_conflicts_block():
    assert any("mergeable" in b for b in _call({**_clean(), "mergeable": "CONFLICTING"})["blockers"])

def test_changes_requested_blocks():
    assert "changes-requested" in _call({**_clean(), "reviewDecision": "CHANGES_REQUESTED"})["blockers"]

def test_unresolved_review_threads_block():
    assert "unresolved-review-threads" in _call(_clean(), threads=True)["blockers"]

def test_merge_ref_verify_failure_blocks():
    assert "merge-ref-verify-failed" in _call(_clean(), merge_ref_ok=False)["blockers"]

def test_draft_blocks():
    assert "draft" in _call({**_clean(), "isDraft": True})["blockers"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import json, os, shutil, subprocess, tempfile

def _gh_json(repo, pr, fields):
    out = subprocess.run(["gh", "pr", "view", str(pr), "-R", repo, "--json", fields],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)

def _unresolved_threads(repo, pr):
    """gh 2.4.0 has no reviewThreads json field -> use GraphQL. Returns the list of unresolved."""
    owner, name = repo.split("/")
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
         "pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}")
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"r={name}",
         "-F", f"n={pr}",
         "--jq", ".data.repository.pullRequest.reviewThreads.nodes[]|select(.isResolved==false)"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [line for line in out.stdout.splitlines() if line.strip()]

def _merge_ref_verify(repo, pr, local_verify, run=subprocess.run):
    """§6.2: re-verify on the ACTUAL merge ref (base+PR merged), not the current workspace."""
    wt = tempfile.mkdtemp(prefix=f"mergeref-{pr}-")
    try:
        if run(f"git fetch origin refs/pull/{pr}/merge && git worktree add --detach {wt} FETCH_HEAD",
               shell=True).returncode != 0:
            return False                                  # no merge ref (conflicts) -> fail-closed
        return run(local_verify, shell=True, cwd=wt).returncode == 0
    finally:
        run(f"git worktree remove --force {wt}", shell=True)
        shutil.rmtree(wt, ignore_errors=True)

def check(repo, pr, *, local_verify, gh_json=_gh_json, threads=_unresolved_threads,
          merge_ref_verify=_merge_ref_verify):
    d = gh_json(repo, pr, "mergeStateStatus,mergeable,reviewDecision,isDraft")
    blockers = []
    if d.get("isDraft"):
        blockers.append("draft")
    # CLEAN = required checks green + reviews satisfied + branch current + queue satisfied
    # (excludes BEHIND/BLOCKED/DIRTY/UNSTABLE). The worker's merge step uses `gh pr merge --auto`
    # when a merge queue is configured; a not-CLEAN state here blocks the direct path.
    if d.get("mergeStateStatus") != "CLEAN":
        blockers.append(f"merge-state:{d.get('mergeStateStatus')}")
    if d.get("mergeable") != "MERGEABLE":
        blockers.append(f"mergeable:{d.get('mergeable')}")
    if d.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("changes-requested")
    if threads(repo, pr):
        blockers.append("unresolved-review-threads")
    if not merge_ref_verify(repo, pr, local_verify):
        blockers.append("merge-ref-verify-failed")
    return {"ok": not blockers, "blockers": blockers}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T1: merge_gate full §6.2 gate (threads + merge-ref verify, Codex #1)`).

---

## Task 2: Handoff writer (`conductor/handoff.py`, §4)

**Files:** Create `conductor/handoff.py`, `tests/conductor/test_handoff.py`

**Interfaces:** `build(ctx)->str`; `write(ctx, path)->str` (raises on a missing required key).
Required: `goal, paths{spec,expectations,assertions,plan_index,adr_dir}, active_plan, milestone,
phase_issue, phase_status, baseline, final, last_unit_summary, next_unit, open_issues{debt,
feature,blocked}, branch, resume_cmd`.

- [ ] **Step 1: Failing tests**

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

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T2: handoff writer (§4)`).

---

## Task 3: Escalation helpers (`conductor/escalate.py`, §9)

**Files:** Create `conductor/escalate.py`, `tests/conductor/test_escalate.py`

**Interfaces:** `file_followup(repo, kind, title, body, link_issue=None, gh=ledger.gh)->int`;
`block_on_subplan(repo, phase_issue, gh=ledger.gh)`; `write_adr(adr_dir, slug, body)->str`.

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/test_escalate.py
import os
from unittest.mock import MagicMock
from conductor import escalate

def test_file_followup_labels_and_links():
    gh = MagicMock(); gh.create_issue.return_value = {"number": 42, "id": 1}
    assert escalate.file_followup("o/r", "debt", "hard bit", "what's hard", link_issue=7, gh=gh) == 42
    gh.ensure_label.assert_called_with("o/r", "debt")
    gh._gh_api.assert_called()                       # back-link comment on #7

def test_block_on_subplan_sets_labels():
    gh = MagicMock(); escalate.block_on_subplan("o/r", 7, gh=gh)
    _, kwargs = gh.set_labels.call_args
    assert "status:blocked" in kwargs["add"] and "blocked-on-subplan" in kwargs["add"]

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

## Task 4: Preflight (`conductor/preflight.py`) — conducted-stack availability (Codex #4)

`/conductor:start` must confirm the conducted skills exist before launching, failing closed if
any is missing (amendment E). The external commands (`/code-review`, `/codex`,
`/document-release`) are **environment-provided** (gstack/codex in this setup); their exact
command names depend on the install, so verify by **required plugin/command presence**, not by
hardcoding.

**Files:** Create `conductor/preflight.py`, `tests/conductor/test_preflight.py`

**Interfaces:** `check(plugins_installed: set[str], required=REQUIRED_PLUGINS) -> {"ok": bool,
"missing": [str]}`; `installed_plugins(run=subprocess.run) -> set[str]` (parses `claude plugin list`).

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/test_preflight.py
from conductor import preflight

def test_missing_plugin_fails_closed():
    out = preflight.check({"spec-craft", "superpowers"})       # missing the codex/review stack
    assert not out["ok"] and out["missing"]

def test_all_present_ok():
    out = preflight.check({"spec-craft", "superpowers", "gstack", "codex"})
    assert out["ok"] and out["missing"] == []
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import subprocess

# Plugins/marketplaces that provide the conducted skills (amendment E). Adjust to the install:
# - spec-craft  -> /spec-craft:expectations, /spec-craft:executable-assertions (dependency)
# - superpowers -> /superpowers:subagent-driven-development, requesting/receiving-code-review, writing-plans
# - gstack/codex -> /code-review, /codex, /document-release (environment-provided)
REQUIRED_PLUGINS = {"spec-craft", "superpowers", "gstack", "codex"}

def installed_plugins(run=subprocess.run):
    out = run(["claude", "plugin", "list"], capture_output=True, text=True)
    names = set()
    for line in out.stdout.splitlines():
        tok = line.strip().split()
        if tok:
            names.add(tok[0].lstrip("@").split("@")[0])
    return names

def check(plugins_installed, required=REQUIRED_PLUGINS):
    missing = sorted(required - set(plugins_installed))
    return {"ok": not missing, "missing": missing}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T4: preflight conducted-stack check (Codex #4)`).

---

## Task 5: `/conductor:autodev` skill (§8 algorithm + §6 recipe)

**Files:** Create `skills/autodev/SKILL.md`; test `tests/conductor/test_skill_outputs.py`

- [ ] **Step 1: Structural test**

```python
# tests/conductor/test_skill_outputs.py
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_autodev_skill_contract():
    body = open(os.path.join(ROOT, "skills/autodev/SKILL.md")).read().lower()
    for needle in ["re-load goal", "reconcile", "assert run --level spec", "fresh subagent",
                   "conductor merge-gate", "handoff", "one phase", "crondelete", "ask no questions",
                   "environment-provided"]:
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
re-fires you; only a green done-gate (or an explicit escalation-halt) stops the run.

1. **RE-LOAD GOAL (fresh context).** Done only when `conductor assert run --level spec` exits 0.
   Re-read goal + paths from the durable handoff/ledger; trust git/issues, not memory.
2. **RECONCILE (precedence git/tests > PR > label).** `ledger.reconcile(phase, ..., now_ts=<now>,
   L=<ttl>)`. If it returns `stale-lease-reclaim`, **reset that phase's retry counter**. PROGRESS
   SELF-CHECK: did the last unit advance the spec? looping? ballooning past the plan?
3. **SPEC-DONE GATE.** `conductor assert run --level spec` (fail-closed; unrunnable = NOT done).
   **All green AND no plans left** → mark spec done, `CronDelete` the loop, final handoff, STOP.
4. **PICK the next eligible phase** (unassigned & not blocked/done; climb the ladder):
   - phase available → **SPLIT-CHECK (§6.1):** estimate size (tasks / diff / token budget); if
     over threshold, deepen-in-place split first (§9). Else run the recipe.
   - plan done → `/superpowers:writing-plans` next plan → `ledger.generate`.
   - no plans left but assertions red → `/superpowers:writing-plans` to close the gap → generate.
5. **CLAIM.** `ledger.claim(phase, worker, now_ts, ttl)`. If False, back off and re-pick.
6. **EXECUTE the phase in a FRESH SUBAGENT** via the recipe (one PR per phase). Conducted skills:
   `/superpowers:*` are plugin skills; `/code-review`, `/codex`, `/document-release` are
   **environment-provided** commands (verified by `/conductor:start` preflight):
   1. `/superpowers:subagent-driven-development` to implement the phase's tasks.
   2. `/code-review` (self-review) of each task completion.
   3. **commit after every task** (`<files-changed> — <description>`).
   4. **one PR per phase** (`Closes #<phase-issue>`).
   5. `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<n>`.
   6. `/superpowers:receiving-code-review` — apply Codex fixes, commit, comment on the PR.
   7. **merge ONLY if `conductor merge-gate <pr>` returns ok** (§6.2 full gate). Then `gh pr merge
      --merge` (no squash), or `--merge --auto` if a merge queue is configured. If the gate
      blocks → resolve (e.g. rebase if `merge-state:BEHIND`) or escalate; **never force-merge**.
   8. `/document-release`.
   Capture `baseline_revision..final_revision` (equal = it did nothing). Respect the **per-fire
   budget**: if exceeded mid-phase, checkpoint(commit)+handoff; the next fire resumes the owned unit.
7. **ESCALATION (§9) at design friction** — patchable later, or structural must-build-now?
   - patch-later → `escalate.file_followup(debt|feature)` + link comment; continue.
   - build-now → one bounded deepen-in-place: `/superpowers:writing-plans` scoped to this phase →
     generate sub-plan; `escalate.block_on_subplan(phase)`; same loop works it; on completion
     `escalate.write_adr(...)`. Parent unblocks when the sub-plan goes green.
   - build-now AND needs human judgment (ambiguous spec / architecture / secrets) → **halt** with
     handoff + issue. The ONLY branch that pages the user.
   On process failure (crash/limit) → just exit; the next fire reconciles (§10).
8. **RECORD.** Update label/progress; commit; update the `plan.md` index; renew or `ledger.release`
   the lease on unit completion.
9. **WRITE HANDOFF (§4)** (`conductor.handoff.write`) + commit + **push**. EXIT.
```

- [ ] **Step 4: Run → PASS.** **Commit** (`Plan4 T5: /conductor:autodev skill (§8 + §6 recipe)`).

---

## Task 6: `/conductor:start` skill (§3 reconcile-first setup + preflight)

**Files:** Create `skills/start/SKILL.md`; modify `bin/conductor` (`goal {set,get}`, `preflight`,
`merge-gate`); test `tests/conductor/test_skill_outputs.py`

- [ ] **Step 1: Structural test**

```python
# add to tests/conductor/test_skill_outputs.py
def test_start_skill_contract():
    body = open(os.path.join(ROOT, "skills/start/SKILL.md")).read().lower()
    for needle in ["preflight", "reconcile-first", "idempotent", "spec-craft:executable-assertions",
                   "conductor:assertions-to-tests", "issue-sync", "/loop /conductor:autodev",
                   "one entry per", "exits 0 or 1", "already done", "resume"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write the skill** (`skills/start/SKILL.md`, `name: start`):

```markdown
---
name: start
description: Start (or resume) an autonomous conductor run for a spec. Reconcile-first and idempotent — re-invoking detects existing setup and resumes from the first incomplete step. Preflights the conducted stack, validates the done-gate precondition, turns the spec's assertions into tests, syncs the GitHub issue ledger, records the goal, and starts the cron loop. Invoke once; after that the loop drives /conductor:autodev.
---

# /conductor:start — preflight + set up + launch (§3, reconcile-first)

**Idempotent (amendment B): each step probes durable state first and SKIPS if already done.**
Heavy steps run in fresh subagents. Never prompt unless a precondition needs human judgment.

0. **PREFLIGHT (`conductor preflight`).** Confirm the conducted stack is installed: `spec-craft`
   (dependency), `superpowers`, and the environment-provided `/code-review`, `/codex`,
   `/document-release` providers. If any is **missing → STOP** and tell the user to install it
   (fail-closed, amendment E). Do not launch a loop that will die at the first conducted call.
1. **Detect spec source** (superpowers or spec-kit); load spec + Expectations + Executable Assertions.
2. **PRECONDITION — assertions present?** No → STOP and point the user at `/spec-craft:expectations`
   then `/spec-craft:executable-assertions` (or, with `--auto-assert`, launch them). The done-gate
   cannot exist without assertion specs (§5).
3. **Implement assertion specs as runnable tests** via `/conductor:assertions-to-tests` →
   `assertions/manifest.yaml` + runner (§5.1–5.2). **SKIP only if the probe holds:** the manifest
   exists, contains **one entry per `/spec-craft:executable-assertions` id** (full coverage), AND
   `conductor assert run --level spec` exits **0 or 1** (a determinate red/green) — NOT 2/3/4/5
   (missing / unparseable / overall-timeout / no-matching-assertions). Otherwise (re)build it
   (Codex #3).
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit) to author plan 1, in a fresh
   subagent. SKIP if a plan index / milestone exists.
5. **issue-sync** — `ledger.generate` (or `convert` an existing `plan.md`/`tasks.md`). SKIP if the
   hierarchy exists; otherwise reconcile it.
6. **Record the `/goal`** (`conductor goal set`) and **start the driver:** register the cron
   `/loop /conductor:autodev`. SKIP if it is already registered (don't double-register).
7. **(Phase 2, multi-loop only)** start the dispatcher loop (§3/§7).

After setup, single-loop `/conductor:start` is idle; the cron drives `/conductor:autodev` to a
green done-gate. A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
```

- [ ] **Step 4: `bin/conductor`** — add `goal {set,get}` (store/print `.conductor/goal.md`),
  `preflight` (exec `python3 -m conductor.preflight` over `installed_plugins()`), `merge-gate <pr>`
  (exec `conductor.merge_gate.check`, exit 0 if ok else 1 printing blockers).

- [ ] **Step 5: Run → PASS.** **Commit** (`Plan4 T6: /conductor:start skill (preflight + §3 reconcile-first) + CLI`).

---

## Task 7: Deterministic E2E + recorded agent smoke (Codex #2)

The agent-driven loop (dispatching subagents, invoking `/code-review` etc.) is validated by a
**recorded manual smoke** (the real Stage-0 E5 run promoted onto the skills). The **deterministic
mechanics** — gate-driven convergence, setup idempotency, and §10 kill/re-fire reconcile — are
covered by concrete pytest here over the real runner + ledger (no agent needed).

**Files:** Create `tests/conductor/test_e2e.py`, `experiments/E5-end-to-end/promote_check.sh`

- [ ] **Step 1: Write concrete deterministic tests**

```python
# tests/conductor/test_e2e.py
import os, subprocess, sys, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]

def test_gate_driven_convergence_red_to_green(tmp_path):
    """The autodev loop's core: gate RED -> implement one unit -> gate GREEN. Deterministic."""
    (tmp_path / "test_feat.py").write_text(textwrap.dedent("""\
        from feat import ship
        def test_ship(): assert ship() == "SHIPPED"
    """))
    (tmp_path / "manifest.yaml").write_text(textwrap.dedent(f"""\
        assertions:
          - id: ship
            claim: "ship() returns SHIPPED"
            command: "python3 -m pytest -q {tmp_path / 'test_feat.py'}"
            level: spec
            kind: example
    """))
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"),
           "PYTHONPATH": str(tmp_path)}
    assert subprocess.run(RUN, env=env, cwd=ROOT).returncode == 1        # RED (no feat.py)
    (tmp_path / "feat.py").write_text('def ship():\\n    return "SHIPPED"\\n')   # one unit of work
    assert subprocess.run(RUN, env=env, cwd=ROOT).returncode == 0        # GREEN -> loop would self-stop

def test_start_setup_is_idempotent(tmp_path):
    """The /conductor:start step-skips: a reconcile-first setup run twice does each step once."""
    script = os.path.join(ROOT, "experiments/E5-end-to-end/conductor_e5.sh")  # promoted setup harness
    first = subprocess.run(["bash", script], cwd=ROOT, capture_output=True, text=True).stdout
    second = subprocess.run(["bash", script], cwd=ROOT, capture_output=True, text=True).stdout
    assert "doing" in first
    assert "doing" not in second and second.count("already done") >= 3      # all steps skipped

def test_refire_reconciles_after_killed_phase():
    """§10: an in-progress phase with a stale lease (dead worker) is reclaimed on the next fire,
    not double-worked. Uses the real ledger.reconcile with a mocked gh seam."""
    from unittest.mock import MagicMock
    from ledger import reconcile
    g = MagicMock()
    g.issue_state.return_value = {"state": "open", "labels": ["status:in-progress"],
                                  "assignees": ["dead"], "id": 1}
    g.get_body.return_value = "<!-- conductor-lease worker=dead ts=100 -->"
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=False, commits_since_baseline=0,
                              retries=0, R=3, gh=g, now_ts=100 + 5000, L=900)
    assert out["action"] == "stale-lease-reclaim" and out["new_status"] == "status:ready"
    g.unassign.assert_called_once_with("o/r", 1, "dead")
```

- [ ] **Step 2: Run → PASS** (`pytest tests/conductor/test_e2e.py -v`).

- [ ] **Step 3: Recorded agent smoke (manual, gated).** `experiments/E5-end-to-end/promote_check.sh`
  drives the REAL skills end-to-end (promotes E5): `/conductor:start <trivial-spec>` (preflight +
  setup + register cron) → the cron fires `/conductor:autodev` → it implements the one phase,
  merges through `conductor merge-gate`, and self-stops when `conductor assert run --level spec`
  exits 0. Run with `RUN_CONDUCTOR_E2E=1`; record: final gate exit 0, cron self-deleted, handoff
  written, no questions asked. (Agent-driven, so recorded evidence — not a pytest unit.)

- [ ] **Step 4: Plugin discovery + full quality gate + commit**

```bash
claude plugin validate . --strict
test -f skills/autodev/SKILL.md && test -f skills/start/SKILL.md && echo "WORKER+SUPERVISOR PRESENT"
ruff check . && ruff format --check . && pyright . && pytest -q
git add tests/conductor/test_e2e.py experiments/E5-end-to-end/promote_check.sh
git commit -m "Plan4 T7: deterministic E2E (gate convergence, idempotency, §10 reclaim) + recorded agent smoke" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Codex review (Plan 4) — addressed:**
- **#1 merge gate:** now a full §6.2 gate — `CLEAN` (covers required checks/currency/reviews/queue
  state) + explicit conflicts + changes-requested + **unresolved review threads** + **local verify
  on the actual `refs/pull/N/merge`** + draft; `--auto` for merge queues. Each blocker tested. ✓
- **#2 E2E placeholders:** replaced `...` with concrete deterministic tests (gate convergence,
  setup idempotency via the promoted E5 harness, §10 stale-reclaim); the agent loop is a clearly
  scoped recorded smoke, not a fake pytest. ✓
- **#3 idempotency probe:** step 3 now requires manifest coverage (one entry per spec-craft id) +
  `assert run --level spec` exit ∈ {0,1}, not 2/3/4/5. ✓
- **#4 conducted-skill names:** `/code-review`/`/codex`/`/document-release` marked
  environment-provided; `conductor preflight` (T4) verifies the stack and `/conductor:start` runs
  it fail-closed before launching. ✓

**Coverage (§11 comps 6–7; §3/§4/§6/§8/§9/§10):** §8 algorithm → T5; §6 recipe + §6.2 gate → T5 +
T1; §9 escalation → T5 + T3; §3 reconcile-first setup + preflight → T6 + T4; §4 handoff → T2 + T5
step 9; §10 recovery → T7. Anti-laziness → T5 + T7.

**Placeholder scan:** the tested modules (merge_gate, handoff, escalate, preflight) and the E2E
tests are shown in full; the only manual step (T7 step 3) is explicitly recorded-smoke, with the
exact pass conditions listed.

**Type/name consistency:** `merge_gate.check→{ok,blockers}`, `handoff.build/write`,
`escalate.*`, `preflight.check/installed_plugins`, `ledger.reconcile/claim/release/generate`,
`conductor assert run --level spec`, `conductor merge-gate <pr>` used consistently;
`/conductor:start` + `/conductor:autodev` + `/loop /conductor:autodev` namespacing uniform.

**Cross-plan:** consumes Plan 1 (`/spec-craft:*`), Plan 2 (`conductor assert run`,
`/conductor:assertions-to-tests`), Plan 3 (`ledger.*`). Completes the MVP (components 1–7).

---

## MVP plan set complete
Plan 1 (spec-craft) · Plan 2 (done-gate) · Plan 3 (ledger) · Plan 4 (autodev + start). Phase 2
(cloud `/schedule` + watchdog + 5hr-resume; multi-loop + dispatcher; optional hook backstop)
remains separate later plans (§11 components 8–10).
