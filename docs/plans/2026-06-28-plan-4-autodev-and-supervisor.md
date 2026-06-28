# Conductor MVP — Plan 4: `/conductor:autodev` + `/conductor:start` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the **worker** `/conductor:autodev` (one fire = one phase) and the **supervisor**
`/conductor:start` (reconcile-first, idempotent setup that launches the loop) — the in-session,
single-loop MVP runtime.

**Architecture:** Two skills + tested Python modules (`merge_gate`, `handoff`, `escalate`,
`preflight`, `start_probe`). The skills orchestrate the Plans 1–3 foundation (`/spec-craft:*`,
`conductor assert run`, `ledger.*`) and conducted skills. The driver is a cron
`/loop /conductor:autodev`. Promotes Stage-0 **E0/E1/E2/E5**.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`, `gh`,
cron `/loop`.

## Global Constraints

- **One phase per fire** (§6.1) with bounds: split-check; per-phase retry cap `R`; per-fire budget
  → checkpoint(commit)+handoff if exceeded.
- **Anti-laziness (§3):** external driver; goal re-loaded every fire; done is a machine check
  (`conductor assert run --level spec` exit 0). The agent cannot end the run.
- **Merge safety gate (§6.2), fail-closed:** required checks green, branch current, no
  changes-requested, no unresolved review threads, no conflicts, local re-verify on the merge ref,
  branch-protection/merge-queue satisfied. Any unknown/missing state blocks → resolve/escalate.
- **Reconcile-first & idempotent (amendment B);** heavy work in fresh subagents (amendment A).
- **Durable every iteration (§4):** commit+push git AND write issues AND write a handoff.
- **Requires the conducted stack (amendment E):** `/spec-craft:*` (via `dependencies:
  ["spec-craft"]`), `/superpowers:*`, and environment-provided `/code-review`, `/codex`,
  `/document-release`. `/conductor:start` PREFLIGHTS exact command availability, fail-closed.
- **Namespacing (locked, amendment F):** `/conductor:start`, `/conductor:autodev`; conducted
  skills keep their namespace. No bare names / aliases.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `conductor/merge_gate.py` | §6.2 full precondition check (CLEAN + threads(paginated) + merge-ref verify). |
| `conductor/handoff.py` | §4 handoff payload builder/writer. |
| `conductor/escalate.py` | §9 helpers: file `debt`/`feature`, block-on-subplan, write ADR. |
| `conductor/preflight.py` | verify exact conducted **command** availability (Codex #1). |
| `conductor/start_probe.py` | §3 step-3 assertion-coverage probe (Codex #3). |
| `skills/autodev/SKILL.md` | `/conductor:autodev` — §8 algorithm + §6 recipe. |
| `skills/start/SKILL.md` | `/conductor:start` — preflight + §3 reconcile-first setup + launch. |
| `bin/conductor` | add `goal {set,get}`, `merge-gate <pr>`, `preflight`, `handoff`. |
| `tests/conductor/test_*.py` | unit (mocked/seamed); skill structural; isolated deterministic E2E. |

---

## Task 1: Merge safety gate (`conductor/merge_gate.py`, §6.2)

**Files:** Create `conductor/merge_gate.py`, `tests/conductor/__init__.py`, `tests/conductor/test_merge_gate.py`

**Interfaces:** `check(repo, pr, *, local_verify, gh_json=_gh_json, threads=_unresolved_threads,
merge_ref_verify=_merge_ref_verify) -> {"ok": bool, "blockers": [str]}` (injectable seams; fail-closed).

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/test_merge_gate.py
from conductor import merge_gate

def _clean():
    return {"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED", "isDraft": False}

def _call(pr_json, *, threads=(), merge_ref_ok=True):
    return merge_gate.check("o/r", 1, local_verify="true",
                            gh_json=lambda r, p, f: pr_json,
                            threads=lambda r, p: list(threads),
                            merge_ref_verify=lambda r, p, lv: merge_ref_ok)

def test_all_green_passes(): assert _call(_clean()) == {"ok": True, "blockers": []}
def test_behind_or_blocked_blocks():
    assert "merge-state:BEHIND" in _call({**_clean(), "mergeStateStatus": "BEHIND"})["blockers"]
    assert "merge-state:BLOCKED" in _call({**_clean(), "mergeStateStatus": "BLOCKED"})["blockers"]
def test_conflicts_block():
    assert any("mergeable" in b for b in _call({**_clean(), "mergeable": "CONFLICTING"})["blockers"])
def test_changes_requested_blocks():
    assert "changes-requested" in _call({**_clean(), "reviewDecision": "CHANGES_REQUESTED"})["blockers"]
def test_unresolved_threads_block():
    assert "unresolved" in _call(_clean(), threads=["unresolved"])["blockers"]
def test_unpaginated_threads_fail_closed():               # Codex minor
    assert "threads-unpaginated" in _call(_clean(), threads=["threads-unpaginated"])["blockers"]
def test_merge_ref_verify_failure_blocks():
    assert "merge-ref-verify-failed" in _call(_clean(), merge_ref_ok=False)["blockers"]
def test_draft_blocks():
    assert "draft" in _call({**_clean(), "isDraft": True})["blockers"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import json, shutil, subprocess, tempfile

def _gh_json(repo, pr, fields):
    out = subprocess.run(["gh", "pr", "view", str(pr), "-R", repo, "--json", fields],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)

def _unresolved_threads(repo, pr):
    """gh 2.4.0 has no reviewThreads json field -> GraphQL. MVP scans the first 100 threads; if
    there are MORE (hasNextPage) it fails closed ('threads-unpaginated') rather than risk missing
    an unresolved thread past page 1 (Codex minor)."""
    owner, name = repo.split("/")
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
         "pullRequest(number:$n){reviewThreads(first:100){"
         "pageInfo{hasNextPage} nodes{isResolved}}}}}")
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"r={name}",
         "-F", f"n={pr}", "--jq",
         "(.data.repository.pullRequest.reviewThreads|"
         "[(.nodes[]|select(.isResolved==false)|\"unresolved\"),"
         "(if .pageInfo.hasNextPage then \"threads-unpaginated\" else empty end)])[]"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [ln for ln in out.stdout.splitlines() if ln.strip()]

def _merge_ref_verify(repo, pr, local_verify, run=subprocess.run):
    """§6.2: re-verify on the ACTUAL merge ref (base+PR merged), not the current workspace."""
    wt = tempfile.mkdtemp(prefix=f"mergeref-{pr}-")
    try:
        if run(f"git fetch origin refs/pull/{pr}/merge && git worktree add --detach {wt} FETCH_HEAD",
               shell=True).returncode != 0:
            return False
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
    if d.get("mergeStateStatus") != "CLEAN":          # CLEAN = checks green + reviews ok + current + queue ok
        blockers.append(f"merge-state:{d.get('mergeStateStatus')}")
    if d.get("mergeable") != "MERGEABLE":
        blockers.append(f"mergeable:{d.get('mergeable')}")
    if d.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("changes-requested")
    blockers += threads(repo, pr)                     # 'unresolved' and/or 'threads-unpaginated'
    if not merge_ref_verify(repo, pr, local_verify):
        blockers.append("merge-ref-verify-failed")
    return {"ok": not blockers, "blockers": blockers}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T1: merge_gate full §6.2 (threads paginated+fail-closed, merge-ref verify)`).

---

## Task 2: Handoff writer (`conductor/handoff.py`, §4)

**Files:** Create `conductor/handoff.py`, `tests/conductor/test_handoff.py`

**Interfaces:** `build(ctx)->str`; `write(ctx, path)->str` (raises on missing required key).
Required: `goal, paths{spec,expectations,assertions,plan_index,adr_dir}, active_plan, milestone,
phase_issue, phase_status, baseline, final, last_unit_summary, next_unit, open_issues, branch, resume_cmd`.

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
from unittest.mock import MagicMock
from conductor import escalate

def test_file_followup_labels_and_links():
    gh = MagicMock(); gh.create_issue.return_value = {"number": 42, "id": 1}
    assert escalate.file_followup("o/r", "debt", "hard bit", "what's hard", link_issue=7, gh=gh) == 42
    gh.ensure_label.assert_called_with("o/r", "debt"); gh._gh_api.assert_called()

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

## Task 4: Preflight — exact conducted-COMMAND availability (`conductor/preflight.py`, Codex #1)

Verify the exact slash **commands** the recipe/setup invoke exist on disk (user skills → bare;
plugin skills/commands → `plugin:name`), not just plugin names. Runtime invocability is confirmed
by the recorded smoke (T7).

**Files:** Create `conductor/preflight.py`, `tests/conductor/test_preflight.py`

**Interfaces:** `available_commands(claude_home=None) -> set[str]`; `check(required=REQUIRED_COMMANDS,
available=None) -> {"ok": bool, "missing": [str]}` (inject `available` in tests).

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/test_preflight.py
from conductor import preflight

_ALL = {"spec-craft:expectations", "spec-craft:executable-assertions",
        "conductor:assertions-to-tests", "superpowers:subagent-driven-development",
        "superpowers:requesting-code-review", "superpowers:receiving-code-review",
        "superpowers:writing-plans", "gstack:code-review", "gstack:codex", "gstack:document-release"}

def test_missing_command_fails_closed():
    out = preflight.check(available={"spec-craft:expectations", "superpowers:writing-plans"})
    assert not out["ok"] and "/codex" in out["missing"]

def test_all_present_ok():
    assert preflight.check(available=_ALL)["ok"]            # bare /code-review matches gstack:code-review
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
import glob, os

# Exact commands the recipe (T5) + setup (T6) invoke. Bare names are environment-provided
# (gstack/codex here) and may be a user skill OR a plugin skill (matched suffix below).
REQUIRED_COMMANDS = [
    "/spec-craft:expectations", "/spec-craft:executable-assertions",
    "/conductor:assertions-to-tests",
    "/superpowers:subagent-driven-development", "/superpowers:requesting-code-review",
    "/superpowers:receiving-code-review", "/superpowers:writing-plans",
    "/code-review", "/codex", "/document-release",
]

def available_commands(claude_home=None):
    """Discover invocable slash-command names from disk. user skills -> bare; plugin
    skills/commands -> '<plugin>:<name>'. (Runtime invocability is confirmed by the T7 smoke;
    this is the static availability gate.)"""
    home = claude_home or os.path.expanduser("~/.claude")
    cmds = set()
    for md in glob.glob(f"{home}/skills/*/SKILL.md"):
        cmds.add(os.path.basename(os.path.dirname(md)))
    for path in glob.glob(f"{home}/plugins/cache/*/*/*/skills/*/SKILL.md"):
        parts = path.split(os.sep); plugin = parts[parts.index("cache") + 2]
        cmds.add(f"{plugin}:{os.path.basename(os.path.dirname(path))}")
    for path in glob.glob(f"{home}/plugins/cache/*/*/*/commands/*.md"):
        parts = path.split(os.sep); plugin = parts[parts.index("cache") + 2]
        cmds.add(f"{plugin}:{os.path.basename(path)[:-3]}")
    return cmds

def _present(cmd, avail):
    c = cmd.lstrip("/")
    if ":" in c:
        return c in avail
    return c in avail or any(a.endswith(f":{c}") for a in avail)

def check(required=REQUIRED_COMMANDS, available=None):
    avail = available if available is not None else available_commands()
    missing = [c for c in required if not _present(c, avail)]
    return {"ok": not missing, "missing": missing}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan4 T4: preflight exact command availability (Codex #1)`).

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
2. **RECONCILE (precedence git/tests > PR > label).** `ledger.reconcile(phase, ..., now_ts, L)`.
   On `stale-lease-reclaim`, **reset that phase's retry counter**. PROGRESS SELF-CHECK.
3. **SPEC-DONE GATE.** `conductor assert run --level spec` (fail-closed; unrunnable = NOT done).
   **All green AND no plans left** → mark done, `CronDelete` the loop, final handoff, STOP.
4. **PICK the next eligible phase** (unassigned & not blocked/done; climb the ladder):
   - phase available → SPLIT-CHECK (§6.1); else run the recipe.
   - plan done → `/superpowers:writing-plans` next plan → `ledger.generate`.
   - no plans left but assertions red → `/superpowers:writing-plans` to close the gap → generate.
5. **CLAIM.** `ledger.claim(phase, worker, now_ts, ttl)`. If False, back off and re-pick.
6. **EXECUTE the phase in a FRESH SUBAGENT** via the recipe (one PR per phase). Conducted skills:
   `/superpowers:*` are plugin skills; `/code-review`, `/codex`, `/document-release` are
   **environment-provided** commands (verified by `/conductor:start` preflight):
   1. `/superpowers:subagent-driven-development` to implement the phase's tasks.
   2. `/code-review` (self-review) per task. 3. **commit after every task.**
   4. **one PR per phase** (`Closes #<phase-issue>`).
   5. `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<n>`.
   6. `/superpowers:receiving-code-review` — apply Codex fixes, commit, comment on the PR.
   7. **merge ONLY if `conductor merge-gate <pr>` returns ok** (§6.2). Then `gh pr merge --merge`
      (no squash), or `--merge --auto` if a merge queue is configured. Gate blocks → resolve
      (e.g. rebase on `merge-state:BEHIND`) or escalate; **never force-merge**.
   8. `/document-release`.
   Capture `baseline_revision..final_revision` (equal = did nothing). Respect the per-fire budget
   (checkpoint+handoff if exceeded).
7. **ESCALATION (§9):** patch-later → `escalate.file_followup(debt|feature)`+link; continue.
   build-now → bounded deepen-in-place: `/superpowers:writing-plans` scoped → generate sub-plan;
   `escalate.block_on_subplan(phase)`; on completion `escalate.write_adr`. build-now AND needs
   human judgment → **halt** with handoff+issue (only branch that pages the user). Process failure
   → exit; next fire reconciles (§10).
8. **RECORD.** label/progress; commit; update `plan.md` index; renew or `ledger.release` the lease.
9. **WRITE HANDOFF (§4)** (`conductor.handoff.write`) + commit + **push**. EXIT.
```

- [ ] **Step 4: Run → PASS.** **Commit** (`Plan4 T5: /conductor:autodev skill (§8 + §6 recipe)`).

---

## Task 6: `start_probe` + `/conductor:start` skill (§3 reconcile-first + preflight)

**Files:** Create `conductor/start_probe.py`, `skills/start/SKILL.md`; modify `bin/conductor`;
test `tests/conductor/test_start_probe.py`, `tests/conductor/test_skill_outputs.py`.

- [ ] **Step 1: Failing test for the assertion-coverage probe (Codex #3)**

```python
# tests/conductor/test_start_probe.py
from conductor import start_probe

def _manifest(tmp_path, ids):
    body = "assertions:\n" + "".join(
        f'  - id: {i}\n    command: "true"\n    level: spec\n' for i in ids)
    p = tmp_path / "manifest.yaml"; p.write_text(body); return str(p)

def test_ready_requires_full_coverage_and_determinate(tmp_path):
    m = _manifest(tmp_path, ["a", "b"])
    assert start_probe.assertions_ready(["a", "b"], m, runner_exit=1) is True   # covered + red ok
    assert start_probe.assertions_ready(["a", "b"], m, runner_exit=0) is True   # covered + green ok
    assert start_probe.assertions_ready(["a", "b", "c"], m, runner_exit=1) is False  # missing c
    assert start_probe.assertions_ready(["a", "b"], m, runner_exit=5) is False  # exit 5 not determinate
    assert start_probe.assertions_ready(["a"], str(tmp_path / "none.yaml"), 1) is False  # no manifest
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `conductor/start_probe.py`

```python
def manifest_ids(manifest_path):
    """Reuse the runner's loader/parser so manifest parsing stays single-sourced."""
    from assertions import run as runner
    return [str(a["id"]) for a in runner.load_assertions(manifest_path)]

def assertions_ready(expected_ids, manifest_path, runner_exit):
    """§3 step-3 idempotency probe (Codex #3): the done-gate is fully built iff the manifest
    covers EVERY expected assertion id AND `conductor assert run --level spec` returned a
    DETERMINATE result (0 or 1) — never 2/3/4/5 (missing/unparseable/timeout/no-match)."""
    try:
        present = set(manifest_ids(manifest_path))
    except Exception:
        return False
    return set(expected_ids) <= present and runner_exit in (0, 1)
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Structural test + write the skill**

```python
# add to tests/conductor/test_skill_outputs.py
def test_start_skill_contract():
    body = open(os.path.join(ROOT, "skills/start/SKILL.md")).read().lower()
    for needle in ["preflight", "reconcile-first", "idempotent", "spec-craft:executable-assertions",
                   "conductor:assertions-to-tests", "issue-sync", "/loop /conductor:autodev",
                   "start_probe.assertions_ready", "already done", "resume"]:
        assert needle in body, needle
```

`skills/start/SKILL.md` (`name: start`):
```markdown
---
name: start
description: Start (or resume) an autonomous conductor run for a spec. Reconcile-first and idempotent — re-invoking detects existing setup and resumes from the first incomplete step. Preflights the conducted stack, validates the done-gate precondition, turns the spec's assertions into tests, syncs the GitHub issue ledger, records the goal, and starts the cron loop.
---

# /conductor:start — preflight + set up + launch (§3, reconcile-first)

**Idempotent (amendment B): each step probes durable state first and SKIPS if already done.**

0. **PREFLIGHT (`conductor preflight`).** Confirm every conducted command resolves (Codex #1):
   `/spec-craft:*`, `/superpowers:*`, and environment-provided `/code-review`, `/codex`,
   `/document-release`. Any **missing → STOP** and tell the user to install it (fail-closed,
   amendment E). Do not launch a loop that dies at the first conducted call.
1. **Detect spec source**; load spec + Expectations + Executable Assertions.
2. **PRECONDITION — assertions present?** No → STOP and point the user at `/spec-craft:expectations`
   then `/spec-craft:executable-assertions` (or, with `--auto-assert`, launch them).
3. **Implement assertions as runnable tests** via `/conductor:assertions-to-tests`. **SKIP only if
   `start_probe.assertions_ready(expected_ids, "assertions/manifest.yaml", <assert-run --level spec
   exit>)` is True** — i.e. the manifest has one entry per `/spec-craft:executable-assertions` id
   AND the runner exit ∈ {0,1} (Codex #3). Otherwise (re)build it.
4. **Plan exists?** No → `/superpowers:writing-plans` (or spec-kit), fresh subagent. SKIP if a plan/milestone exists.
5. **issue-sync** — `ledger.generate` (or `convert`). SKIP if the hierarchy exists; else reconcile.
6. **Record `/goal`** (`conductor goal set`) and **start the driver:** register cron
   `/loop /conductor:autodev`. SKIP if already registered.
7. **(Phase 2 only)** start the dispatcher loop.

A restart = re-invoke `/conductor:start` → it reconciles and resumes (amendment C).
```

- [ ] **Step 6: `bin/conductor`** — add `goal {set,get}` (`.conductor/goal.md`), `preflight`
  (`python3 -m conductor.preflight`), `merge-gate <pr>` (`conductor.merge_gate.check`, exit 0/1).

- [ ] **Step 7: Run → PASS.** **Commit** (`Plan4 T6: start_probe (Codex #3) + /conductor:start skill + CLI`).

---

## Task 7: Isolated deterministic E2E + recorded agent smoke (Codex #2)

**Files:** Create `tests/conductor/test_e2e.py`, `experiments/E5-end-to-end/promote_check.sh`

- [ ] **Step 1: Write isolated deterministic tests (no Stage-0 state, no repo mutation — Codex #2)**

```python
# tests/conductor/test_e2e.py
import os, subprocess, sys, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]

def test_gate_driven_convergence_red_to_green(tmp_path):
    """autodev's core loop: gate RED -> implement one unit -> gate GREEN. Fully isolated."""
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
    assert subprocess.run(RUN, env=env, cwd=ROOT).returncode == 1            # RED
    (tmp_path / "feat.py").write_text('def ship():\\n    return "SHIPPED"\\n')
    assert subprocess.run(RUN, env=env, cwd=ROOT).returncode == 0            # GREEN -> would self-stop

def test_setup_step_is_idempotent(tmp_path):
    """The reconcile-first step-skip pattern: a step does its work once, then probes-and-skips.
    Pure + isolated (temp probe; no Stage-0 script, no repo mutation) — Codex #2."""
    probe = tmp_path / "goal.md"; runs = []
    def setup_step():
        if probe.exists():
            return "already done"
        probe.write_text("goal"); runs.append(1); return "doing"
    assert setup_step() == "doing"
    assert setup_step() == "already done"
    assert len(runs) == 1

def test_refire_reclaims_stale_phase():
    """§10: an in-progress phase with a stale lease (dead worker) is reclaimed, not double-worked."""
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
  drives the REAL skills (promotes E5) in a **temp working copy** (no repo mutation):
  `/conductor:start <trivial-spec>` (preflight + setup + register cron) → cron fires
  `/conductor:autodev` → implements one phase, merges via `conductor merge-gate`, self-stops when
  `conductor assert run --level spec` exits 0; re-run `/conductor:start` → every step "already
  done". Run with `RUN_CONDUCTOR_E2E=1`; record: final gate exit 0, cron self-deleted, handoff
  written, idempotent re-run, no questions asked. (Agent-driven → recorded evidence, not pytest.)

- [ ] **Step 4: Plugin discovery + full quality gate + commit**

```bash
claude plugin validate . --strict
test -f skills/autodev/SKILL.md && test -f skills/start/SKILL.md && echo "WORKER+SUPERVISOR PRESENT"
ruff check . && ruff format --check . && pyright . && pytest -q
git add tests/conductor/test_e2e.py experiments/E5-end-to-end/promote_check.sh
git commit -m "Plan4 T7: isolated deterministic E2E + recorded agent smoke (Codex #2)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Codex review (Plan 4 rev 2) — addressed:**
- **#1 preflight commands:** `preflight` now checks an explicit `REQUIRED_COMMANDS` list against
  on-disk skill/command discovery (user-bare + `plugin:name`), with an injectable `available`
  seam tested for fail-closed; runtime invocability = the T7 recorded smoke. ✓
- **#2 E2E isolation:** the idempotency test is a pure temp-dir step-skip unit (no `conductor_e5.sh`,
  no repo mutation); the agent-loop idempotency is the recorded smoke in a temp working copy. ✓
- **#3 coverage probe:** tested `start_probe.assertions_ready` (manifest covers every expected id
  AND runner exit ∈ {0,1}); `/conductor:start` step 3 calls it. ✓
- **Minor threads pagination:** `_unresolved_threads` queries `hasNextPage` and emits a
  `threads-unpaginated` blocker if >100 (fail-closed); tested. ✓

**Coverage (§11 comps 6–7; §3/§4/§6/§8/§9/§10):** §8 → T5; §6 recipe + §6.2 gate → T5 + T1; §9 →
T5 + T3; §3 setup + preflight + probe → T4 + T6; §4 handoff → T2 + T5; §10 → T7. Anti-laziness → T5 + T7.

**Placeholder scan:** all modules + the E2E tests are shown in full; the single manual step (T7
step 3) is explicitly recorded-smoke with exact pass conditions, in a temp working copy.

**Type/name consistency:** `merge_gate.check`, `handoff.build/write`, `escalate.*`,
`preflight.check/available_commands`, `start_probe.assertions_ready`,
`ledger.reconcile/claim/release/generate`, `conductor assert run --level spec`,
`conductor merge-gate <pr>`; `/conductor:start` + `/conductor:autodev` namespacing uniform.

**Cross-plan:** consumes Plan 1 (`/spec-craft:*`), Plan 2 (`conductor assert run`,
`/conductor:assertions-to-tests`), Plan 3 (`ledger.*`). Completes the MVP (components 1–7).

---

## MVP plan set complete
Plan 1 (spec-craft) · Plan 2 (done-gate) · Plan 3 (ledger) · Plan 4 (autodev + start). Phase 2
(cloud `/schedule` + watchdog + 5hr-resume; multi-loop + dispatcher; optional hook backstop)
remains separate later plans (§11 components 8–10).
