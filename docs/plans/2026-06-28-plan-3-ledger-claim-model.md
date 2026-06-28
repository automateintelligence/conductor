# Conductor MVP — Plan 3: Ledger + Claim Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build conductor's **ledger** — `/conductor:issue-sync` (generate / convert /
reconcile a GitHub-issue hierarchy from a plan) plus the **claim model** (assignee + lease +
labels, §7) — so work state lives server-side, conflict-free, parallel-correct, with the
N=1 single loop as the degenerate case.

**Architecture:** A Python package `ledger/` wrapping `gh api` (labels, milestones, issues,
**sub-issues**, assignees, lease comments) + the §7 reconcile/claim logic, exposed via a
`/conductor:issue-sync` skill and `conductor ledger …` CLI. Promotes the Stage-0 **E3**
prototype (`experiments/E3-gh-ledger/reconcile.py` + its validated `gh api` patterns). The
load-bearing §7 rules (precedence, invalid-combo repair, lease) are pure functions, unit-tested
with the gh layer mocked; one integration test hits an ephemeral milestone (cleaned up).

**Tech Stack:** Python 3 (stdlib + pytest), `gh` v2.4.0 via `gh api` (REST), conductor plugin.

## Global Constraints

- **GitHub is canonical for work state; git is ground truth** (§7). Sub-issues are the Tasks
  representation (E3 proved they work via `gh api`); checklist is the documented fallback.
- **gh portability (amendment D):** drive labels + sub-issues through `gh api`, never
  `gh label`/`gh issue` sub-issue subcommands (absent in gh 2.4.0). Sub-issue add:
  `gh api --method POST repos/<o>/<r>/issues/<parent>/sub_issues -F sub_issue_id=<child DB id>`
  (typed int; DB id from `…/issues/<n> --jq .id`, not the display number).
- **Ground-truth precedence (§7):** `git commits + tests > PR state > issue status-label`. On
  conflict the higher source wins and the lower is repaired.
- **Parallel-correct from day one (§7):** claim = **assignee** (not the status label); lease =
  heartbeat timestamp + TTL `L`. N=1 single loop needs no claiming, but the model is built in.
- **Namespacing (locked):** `/conductor:issue-sync`; no bare names (amendment F).
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Conventions (LOCKED)
Status labels = lifecycle (NOT a lock): `draft, ready, in-progress, in-review, done, blocked`.
Assignee+lease = the claim. Hierarchy: Plan→Milestone, Phase→Issue, Tasks→sub-issues, PR per
phase `Closes #<phase>`, `plan.md` = thin index.

---

## File Structure

| Path | Responsibility |
|---|---|
| `ledger/__init__.py` | Package marker. |
| `ledger/gh.py` | Thin `gh api` wrappers: `ensure_label`, `create_milestone`, `create_issue`, `add_sub_issue`, `set_labels`, `assign`, `close_issue`, `reopen_issue`, `issue_state`, `write_lease`, `read_lease`. One `_gh_api(method, path, **fields)` seam (mockable). |
| `ledger/model.py` | Dataclasses: `PhaseState(status, assignee, lease, commits_since_baseline, pr_state, tests_red)`; enums for status/precedence. |
| `ledger/sync.py` | `generate(plan)` and `convert(plan_md_path)` → milestone/issues/sub-issues/labels. |
| `ledger/claim.py` | `eligible(phase)`, `claim(issue, worker, ttl)`, `renew_lease`, `release`, `stale(lease, L)`. |
| `ledger/reconcile.py` | §7 precedence + invalid-combo repairs + retry cap `R` + stale-lease reclaim. Promotes E3. |
| `skills/issue-sync/SKILL.md` | `/conductor:issue-sync` — generate / convert / reconcile entry point. |
| `bin/conductor` | add `ledger {generate|convert|reconcile|claim}` subcommands. |
| `tests/ledger/test_*.py` | unit (gh mocked) + one integration (ephemeral milestone). |

---

## Task 1: `gh api` wrapper layer (`ledger/gh.py`)

Promote E3's validated `gh api` calls into typed wrappers behind one mockable seam.

**Files:** Create `ledger/__init__.py`, `ledger/gh.py`, `tests/ledger/__init__.py`, `tests/ledger/test_gh.py`

**Interfaces:**
- Produces: `_gh_api(method: str, path: str, fields: dict|None=None, jq: str|None=None) -> Any`
  and wrappers `ensure_label(repo,name,color)`, `create_milestone(repo,title)->int`,
  `create_issue(repo,title,body,milestone=None,labels=())->dict` (returns `{number, id}`),
  `add_sub_issue(repo,parent_number,child_db_id)`, `set_labels(repo,n,add=(),remove=())`,
  `assign(repo,n,login)`, `close_issue(repo,n)`, `reopen_issue(repo,n)`,
  `issue_state(repo,n)->dict` (`{state, labels, assignees, id}`).

- [ ] **Step 1: Write failing unit tests (gh mocked)**

```python
# tests/ledger/test_gh.py
from unittest.mock import patch
from ledger import gh

def test_add_sub_issue_uses_typed_int_db_id():
    calls = []
    with patch.object(gh, "_gh_api", lambda m, p, fields=None, jq=None: calls.append((m, p, fields)) or {}):
        gh.add_sub_issue("o/r", 1, 4761391764)
    m, p, fields = calls[-1]
    assert m == "POST" and p == "repos/o/r/issues/1/sub_issues"
    assert fields == {"sub_issue_id": 4761391764}        # int, not str (E3 deviation #2)

def test_create_issue_returns_number_and_db_id():
    with patch.object(gh, "_gh_api", lambda *a, **k: {"number": 7, "id": 999}):
        out = gh.create_issue("o/r", "Phase A", "body")
    assert out == {"number": 7, "id": 999}
```

- [ ] **Step 2: Run → FAIL** (`ledger` absent).

- [ ] **Step 3: Implement `ledger/gh.py`**

```python
import json, subprocess

def _gh_api(method, path, fields=None, jq=None):
    cmd = ["gh", "api", "--method", method, path]
    for k, v in (fields or {}).items():
        # -F sends typed (int/bool); -f sends string. Sub-issue ids MUST be typed ints.
        cmd += (["-F", f"{k}={v}"] if isinstance(v, (int, bool)) else ["-f", f"{k}={v}"])
    if jq:
        cmd += ["--jq", jq]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() and not jq else out.stdout.strip()

def ensure_label(repo, name, color="ededed"):
    try:
        _gh_api("POST", f"repos/{repo}/labels", {"name": name, "color": color})
    except RuntimeError as e:
        if "already_exists" not in str(e):                    # idempotent
            raise

def create_milestone(repo, title):
    return _gh_api("POST", f"repos/{repo}/milestones", {"title": title}, jq=None)["number"]

def create_issue(repo, title, body, milestone=None, labels=()):
    f = {"title": title, "body": body}
    if milestone: f["milestone"] = int(milestone)
    data = _gh_api("POST", f"repos/{repo}/issues", f)
    if labels: set_labels(repo, data["number"], add=labels)
    return {"number": data["number"], "id": data["id"]}

def add_sub_issue(repo, parent_number, child_db_id):
    return _gh_api("POST", f"repos/{repo}/issues/{parent_number}/sub_issues",
                   {"sub_issue_id": int(child_db_id)})       # typed int (E3)

def set_labels(repo, n, add=(), remove=()):
    cur = set(issue_state(repo, n)["labels"])
    new = sorted((cur | set(add)) - set(remove))
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", {"labels": json.dumps(new)})

def assign(repo, n, login):
    _gh_api("POST", f"repos/{repo}/issues/{n}/assignees", {"assignees": json.dumps([login])})

def close_issue(repo, n):  _gh_api("PATCH", f"repos/{repo}/issues/{n}", {"state": "closed"})
def reopen_issue(repo, n): _gh_api("PATCH", f"repos/{repo}/issues/{n}", {"state": "open"})

def issue_state(repo, n):
    d = _gh_api("GET", f"repos/{repo}/issues/{n}")
    return {"state": d["state"], "id": d["id"],
            "labels": [l["name"] for l in d.get("labels", [])],
            "assignees": [a["login"] for a in d.get("assignees", [])]}
```
(`labels`/`assignees` as `-f key=<json>` strings is the simplest gh-portable form; `_gh_api`
sends non-int values with `-f`.)

- [ ] **Step 4: Run unit tests → PASS.** Lint+typecheck.

- [ ] **Step 5: Commit** (`Plan3 T1: ledger/gh.py gh api wrappers (promote E3)` …+ trailer).

---

## Task 2: Lease helpers + claim model (`ledger/claim.py`)

Lease = a heartbeat written as a hidden marker in the issue body (or a `lease:<ts>` comment);
TTL `L`. Claim = assignee. Eligibility = unassigned AND not blocked/done/dep-blocked.

**Files:** Create `ledger/claim.py`, `ledger/model.py`, `tests/ledger/test_claim.py`

**Interfaces:**
- Produces: `eligible(state: dict) -> bool`; `claim(repo, n, worker, now_ts, ttl) -> bool`
  (assign + write lease, re-read to confirm sole assignee, return False if lost the race);
  `renew_lease(repo, n, now_ts)`; `release(repo, n)`; `lease_is_stale(lease_ts, now_ts, L) -> bool`.

- [ ] **Step 1: Write failing tests**

```python
# tests/ledger/test_claim.py
from ledger import claim

def test_eligible_only_when_unassigned_and_open_state():
    assert claim.eligible({"assignees": [], "labels": ["status:ready"], "state": "open"})
    assert not claim.eligible({"assignees": ["x"], "labels": ["status:ready"], "state": "open"})
    assert not claim.eligible({"assignees": [], "labels": ["status:blocked"], "state": "open"})
    assert not claim.eligible({"assignees": [], "labels": ["status:done"], "state": "closed"})

def test_lease_staleness():
    assert claim.lease_is_stale(100, now_ts=100 + 901, L=900)
    assert not claim.lease_is_stale(100, now_ts=100 + 10, L=900)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/claim.py`:

```python
BLOCKING = {"status:blocked", "status:done"}

def eligible(state):
    labels = set(state.get("labels", []))
    return (not state.get("assignees")
            and state.get("state") != "closed"
            and not (labels & BLOCKING)
            and "dep-blocked" not in labels)

def lease_is_stale(lease_ts, now_ts, L):
    return lease_ts is None or (now_ts - lease_ts) > L

def claim(repo, n, worker, now_ts, ttl, gh):
    if not eligible(gh.issue_state(repo, n)):
        return False
    gh.assign(repo, n, worker)
    gh.set_labels(repo, n, add=["status:in-progress"], remove=["status:ready"])
    renew_lease(repo, n, now_ts, gh)
    confirm = gh.issue_state(repo, n)["assignees"]          # re-read: sole assignee?
    return confirm == [worker]

def renew_lease(repo, n, now_ts, gh):
    gh.set_labels(repo, n, add=[f"lease:{now_ts}"],
                  remove=[l for l in gh.issue_state(repo, n)["labels"] if l.startswith("lease:")])

def release(repo, n, gh):
    gh.set_labels(repo, n, remove=[l for l in gh.issue_state(repo, n)["labels"] if l.startswith("lease:")])
```
(Lease as a `lease:<ts>` label keeps it server-side and visible; a single loop never calls
`claim`, but the path exists for multi-loop, §7.)

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit.**

---

## Task 3: Reconcile (§7 precedence + repairs) — promote E3

Promote `experiments/E3-gh-ledger/reconcile.py` (validated: detected `done`+red → reopen →
in-progress; permitted `done`+green) into `ledger/reconcile.py`, generalized over the §7 rules.

**Files:** Create `ledger/reconcile.py`, `tests/ledger/test_reconcile.py`

**Interfaces:**
- Produces: `reconcile(repo, n, tests_red: bool, pr_merged: bool, commits_since_baseline: int,
  retries: int, R: int, gh) -> dict` applying precedence `git/tests > PR > label`, returning
  `{action, new_status}` and performing the repair. Encodes the §7 invalid-combo table.

- [ ] **Step 1: Write failing tests (the §7 table)**

```python
# tests/ledger/test_reconcile.py
from ledger import reconcile
from unittest.mock import MagicMock

def _gh(state):
    g = MagicMock(); g.issue_state.return_value = state; return g

def test_done_but_tests_red_reopens_to_in_progress():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=True,
                              commits_since_baseline=3, retries=0, R=3, gh=g)
    assert out["new_status"] == "status:in-progress"
    g.reopen_issue.assert_called_once()

def test_done_and_tests_green_is_permitted():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile("o/r", 1, tests_red=False, pr_merged=True,
                              commits_since_baseline=3, retries=0, R=3, gh=g)
    assert out["action"] == "none"

def test_in_progress_no_assignee_resets_to_ready():
    g = _gh({"state": "open", "labels": ["status:in-progress"], "assignees": [], "id": 1})
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=False,
                              commits_since_baseline=0, retries=0, R=3, gh=g)
    assert out["new_status"] == "status:ready"

def test_retry_cap_exceeded_routes_to_blocked():
    g = _gh({"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1})
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=False,
                              commits_since_baseline=1, retries=3, R=3, gh=g)
    assert out["new_status"] == "status:blocked"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/reconcile.py` encoding the §7 table:

```python
def reconcile(repo, n, tests_red, pr_merged, commits_since_baseline, retries, R, gh):
    st = gh.issue_state(repo, n)
    labels = set(st["labels"]); status = next((l for l in labels if l.startswith("status:")), None)
    closed = st["state"] == "closed"

    def repair(new_status, action, reopen=False, close=False):
        if reopen: gh.reopen_issue(repo, n)
        if close:  gh.close_issue(repo, n)
        if new_status:
            gh.set_labels(repo, n, add=[new_status],
                          remove=[l for l in labels if l.startswith("status:")])
        return {"action": action, "new_status": new_status}

    # Precedence: git/tests > PR > label.
    # 1. retry cap (§6.1): exhausted attempts -> blocked + escalate
    if tests_red and retries >= R:
        return repair("status:blocked", "retry-cap-exceeded")
    # 2. done/closed but tests red -> reopen -> in-progress (§7)
    if (status == "status:done" or closed) and tests_red:
        return repair("status:in-progress", "reopen-tests-red", reopen=True)
    # 3. in-progress but no assignee -> reset -> ready (abandoned)
    if status == "status:in-progress" and not st["assignees"]:
        return repair("status:ready", "reset-abandoned")
    # 4. closed but PR not merged -> reopen
    if closed and not pr_merged:
        return repair("status:in-progress", "reopen-unmerged", reopen=True)
    # 5. consistent (done+green, or in-progress+assignee, etc.) -> no-op
    return {"action": "none", "new_status": status}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan3 T3: ledger/reconcile.py (§7 precedence, promote E3)`).

---

## Task 4: issue-sync generate + convert (`ledger/sync.py`)

**Files:** Create `ledger/sync.py`, `tests/ledger/test_sync.py`

**Interfaces:**
- Produces: `generate(repo, plan: dict, gh) -> dict` (plan = `{title, phases:[{title, status,
  tasks:[str]}]}`) → creates milestone, phase issues (labeled), task sub-issues; returns
  `{milestone, phases:[{number, sub_issues:[number]}]}`. `convert(repo, plan_md_path, gh)` →
  parse a thin `plan.md`/`tasks.md` into the `plan` dict, then `generate`.

- [ ] **Step 1: Write failing tests (gh mocked, assert the call sequence + sub-issue linking)**

```python
# tests/ledger/test_sync.py
from ledger import sync
from unittest.mock import MagicMock

def test_generate_creates_milestone_phases_and_sub_issues():
    gh = MagicMock()
    gh.create_milestone.return_value = 1
    gh.create_issue.side_effect = [{"number": 1, "id": 100}, {"number": 2, "id": 200},
                                   {"number": 3, "id": 300}]  # phaseA, taskA1... (per call)
    plan = {"title": "P", "phases": [{"title": "A", "status": "ready", "tasks": ["t1"]}]}
    out = sync.generate("o/r", plan, gh)
    gh.create_milestone.assert_called_once_with("o/r", "P")
    # phase A (#1) gets task #2 linked as a sub-issue by DB id
    gh.add_sub_issue.assert_called_with("o/r", 1, 200)
    assert out["phases"][0]["number"] == 1 and out["phases"][0]["sub_issues"] == [2]

def test_convert_parses_thin_plan_md(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text("# Plan P\n## Phase A [ready]\n- [ ] t1\n- [ ] t2\n## Phase B [draft]\n")
    plan = sync.parse_plan_md(p.read_text())
    assert plan["title"] == "Plan P"
    assert plan["phases"][0] == {"title": "Phase A", "status": "ready", "tasks": ["t1", "t2"]}
    assert plan["phases"][1]["status"] == "draft"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/sync.py` (`parse_plan_md`, `generate`, `convert`). `generate`:
ensure status labels first; create milestone; for each phase create the issue with
`status:<status>`; for each task create an issue and `add_sub_issue(parent_number,
task["id"])`; if `add_sub_issue` raises (feature off), fall back to appending `- [ ] #<task#>`
to the parent body and record `fallback=True`. (Mirrors E3 exactly.)

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit.**

---

## Task 5: `/conductor:issue-sync` skill + `conductor ledger` CLI + integration

**Files:** Create `skills/issue-sync/SKILL.md`; modify `bin/conductor`; create
`tests/ledger/test_integration.py`, `tests/test_skill_outputs.py` (add a check).

- [ ] **Step 1: Skill structural test + write the skill**

`skills/issue-sync/SKILL.md` (frontmatter `name: issue-sync`) documents the three modes —
**generate** (plan → hierarchy), **convert** (existing `plan.md`/`tasks.md` → hierarchy), and
**reconcile** (each iteration: read git/tests/PR/labels, apply `ledger.reconcile` per
precedence) — and states it **never prompts** (§5 "fully automated"). Structural test asserts
the body contains `generate`, `convert`, `reconcile`, `precedence`, `sub-issue`, `never prompt`.

- [ ] **Step 2: CLI** — add to `bin/conductor`:
`conductor ledger generate <plan.json>` / `convert <plan.md>` / `reconcile <issue#> --tests-red …`
dispatching to `python3 -m ledger.<cmd>`.

- [ ] **Step 3: Integration test (ephemeral, cleaned up)** — mirrors E3 against the real repo:
generate a `IT-<runid>` milestone + 2 phases + 3 sub-issues, assert hierarchy via `gh api`,
flip phase A `done`+close, inject red, `reconcile` → asserts reopen→in-progress, then green →
permitted. Teardown: close issues + delete the milestone. Marked `@pytest.mark.integration`
(skipped unless `RUN_GH_INTEGRATION=1`, so CI without gh stays green — and it is NOT the
fail-closed gate).

- [ ] **Step 4: Full quality gate + commit** (`Plan3 T5: /conductor:issue-sync skill + ledger CLI + integration`).

---

## Self-Review

**Coverage (§11 comps 4–5, §7):** component 4 issue-sync (generate/convert/reconcile) → T4+T5;
component 5 ledger+claim (assignee+lease+labels, precedence, invalid-combo repair, retry cap,
stale-lease) → T2+T3. gh portability (amendment D) → T1.

**§7 rule coverage:** precedence git/tests>PR>label (T3); invalid combos done+red→reopen,
in-progress+no-assignee→ready, closed+unmerged→reopen (T3 tests); retry cap→blocked (T3);
lease staleness (T2); eligibility (T2); real sub-issues + checklist fallback (T4, E3-proven).

**Placeholder scan:** load-bearing logic (reconcile, claim, gh wrappers) shown in full; sync
parse/generate specified with the exact E3 fallback behavior.

**Consistency:** `gh` seam `_gh_api` mocked uniformly; status labels `status:<x>` + `lease:<ts>`
consistent T2/T3; `/conductor:issue-sync` namespacing.

**Parallel-correctness:** claim/lease built in (T2) though N=1 single loop never calls `claim`
— enabling multi-loop (Plan: Phase 2 dispatcher) is a config flip, not a rewrite (§7).

---

## Open follow-ups
- Plan 4 (`/conductor:autodev` + `/conductor:conductor`) calls `reconcile` every fire and
  `generate`/`convert` at setup / next-plan / deepen-in-place.
