# Conductor MVP — Plan 3: Ledger + Claim Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build conductor's **ledger** — `/conductor:issue-sync` (generate / convert /
reconcile a GitHub-issue hierarchy from a plan) plus the **claim model** (assignee + lease +
labels, §7) — so work state lives server-side, conflict-free, parallel-correct, with the
N=1 single loop as the degenerate case.

**Architecture:** A Python package `ledger/` wrapping `gh api` (labels, milestones, issues,
**sub-issues**, assignees, **lease = hidden body marker**) + the §7 reconcile/claim logic,
exposed via a `/conductor:issue-sync` skill and `conductor ledger …` CLI. Promotes the Stage-0
**E3** prototype. Load-bearing §7 rules (precedence, invalid-combo repair, claim, lease,
stale-reclaim) are pure functions, unit-tested with the gh layer mocked; one integration test
hits an ephemeral milestone (cleaned up).

**Tech Stack:** Python 3 (stdlib + pytest), `gh` v2.4.0 via `gh api` (REST), conductor plugin.

## Global Constraints

- **GitHub is canonical for work state; git is ground truth** (§7). Sub-issues are the Tasks
  representation (E3 proved they work via `gh api`); checklist is the documented fallback.
- **gh array fields via `--input` JSON (Codex #1):** mutations send the request body as real
  JSON on stdin (`gh api --method … --input -`), so `labels`/`assignees` are true arrays (and
  setting `[]` clears them). NEVER pass arrays as `-f key='[...]'` (that sends a JSON *string*).
- **gh portability (amendment D):** labels + sub-issues via `gh api`, never `gh label`/`gh
  issue` sub-issue subcommands. Sub-issue add: `POST repos/<o>/<r>/issues/<parent>/sub_issues`
  with body `{"sub_issue_id": <child DB id int>}` (DB id from `…/issues/<n>` `.id`).
- **Ground-truth precedence (§7):** `git commits + tests > PR state > issue status-label`.
- **Claim = assignee; lease = a single hidden body marker** `<!-- conductor-lease worker=<login> ts=<unix> -->`
  with TTL `L`. **Confirm sole ownership BEFORE mutating status/lease** (Codex #2). N=1 single
  loop needs no claiming, but the model is built in.
- **release() unassigns AND clears the lease.** **Stale-lease reclaim precedes retry-cap**
  escalation (Codex #3); a reclaim resets the phase's retry counter.
- **Namespacing (locked):** `/conductor:issue-sync`; no bare names (amendment F).
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Conventions (LOCKED)
Status labels = lifecycle (NOT a lock): `draft, ready, in-progress, in-review, done, blocked`.
Assignee + lease (body marker) = the claim. Hierarchy: Plan→Milestone, Phase→Issue,
Tasks→sub-issues, PR per phase `Closes #<phase>`, `plan.md` = thin index.

---

## File Structure

| Path | Responsibility |
|---|---|
| `ledger/__init__.py` | Package marker. |
| `ledger/gh.py` | `gh api` wrappers (JSON `--input` bodies): `assign`/`unassign`, `set_labels`, `get_body`/`set_body`, sub-issues. One `_gh_api(method, path, body, jq)` seam (mockable). |
| `ledger/claim.py` | `eligible`, `claim` (confirm-then-mutate), `read_lease`/`renew_lease`, `release`, `lease_is_stale`. |
| `ledger/reconcile.py` | §7 precedence + invalid-combo repairs + **stale-reclaim-before-retry-cap**. Promotes E3. |
| `ledger/sync.py` | `generate(plan)` / `convert(plan_md)` → milestone/issues/sub-issues/labels. |
| `skills/issue-sync/SKILL.md` | `/conductor:issue-sync` — generate / convert / reconcile. |
| `bin/conductor` | add `ledger {generate|convert|reconcile}` subcommands. (`claim`/`release` are Python lease ops on `ledger.claim`, not CLI.) |
| `tests/ledger/test_*.py` | unit (gh mocked) + one integration (ephemeral milestone). |

---

## Task 1: `gh api` wrapper layer (`ledger/gh.py`)

Promote E3's `gh api` calls into typed wrappers behind one mockable seam that sends **JSON
request bodies via `--input -`** (Codex #1) — so arrays are real arrays. Includes `unassign`
and body get/set.

**Files:** Create `ledger/__init__.py`, `ledger/gh.py`, `tests/ledger/__init__.py`, `tests/ledger/test_gh.py`

**Interfaces:**
- `_gh_api(method, path, body=None, jq=None)`; `ensure_label`, `create_milestone`,
  `create_issue`→`{number,id}`, `add_sub_issue(repo,parent,child_db_id)`, `set_labels`,
  `assign`, `unassign`, `close_issue`, `reopen_issue`, `issue_state`→`{state,labels,assignees,id}`,
  `get_body`, `set_body`.

- [ ] **Step 1: Write failing unit tests (assert ARRAY bodies, not json-strings — Codex #1)**

```python
# tests/ledger/test_gh.py
from unittest.mock import patch
from ledger import gh

def test_set_labels_sends_real_json_array(monkeypatch):
    captured = {}
    def fake(method, path, body=None, jq=None):
        if method == "GET":
            return {"state": "open", "id": 1, "labels": [{"name": "status:ready"}], "assignees": []}
        captured["body"] = body
        return None
    monkeypatch.setattr(gh, "_gh_api", fake)
    gh.set_labels("o/r", 1, add=["status:in-progress"], remove=["status:ready"])
    assert isinstance(captured["body"]["labels"], list)              # ARRAY, not a JSON string
    assert captured["body"]["labels"] == ["status:in-progress"]

def test_assign_unassign_send_list_bodies(monkeypatch):
    seen = []
    monkeypatch.setattr(gh, "_gh_api", lambda m, p, body=None, jq=None: seen.append((m, body)) or {})
    gh.assign("o/r", 5, "alice"); gh.unassign("o/r", 5, "alice")
    assert seen[0] == ("POST", {"assignees": ["alice"]})
    assert seen[1] == ("DELETE", {"assignees": ["alice"]})

def test_add_sub_issue_typed_int_db_id(monkeypatch):
    seen = []
    monkeypatch.setattr(gh, "_gh_api", lambda m, p, body=None, jq=None: seen.append((m, p, body)) or {})
    gh.add_sub_issue("o/r", 1, 4761391764)
    assert seen[-1] == ("POST", "repos/o/r/issues/1/sub_issues", {"sub_issue_id": 4761391764})
```

- [ ] **Step 2: Run → FAIL** (`ledger` absent).

- [ ] **Step 3: Implement `ledger/gh.py`**

```python
import json, subprocess

def _gh_api(method, path, body=None, jq=None):
    cmd = ["gh", "api", "--method", method, path]
    if jq:
        cmd += ["--jq", jq]
    stdin = None
    if body is not None:
        cmd += ["--input", "-"]                      # JSON request body on stdin (arrays/ints correct)
        stdin = json.dumps(body)
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {out.stderr.strip()}")
    if jq:
        return out.stdout.rstrip("\n")
    return json.loads(out.stdout) if out.stdout.strip() else None

def ensure_label(repo, name, color="ededed"):
    try:
        _gh_api("POST", f"repos/{repo}/labels", body={"name": name, "color": color})
    except RuntimeError as e:
        if "already_exists" not in str(e):
            raise

def create_milestone(repo, title):
    return _gh_api("POST", f"repos/{repo}/milestones", body={"title": title})["number"]

def create_issue(repo, title, body, milestone=None, labels=()):
    payload = {"title": title, "body": body}
    if milestone:
        payload["milestone"] = int(milestone)
    if labels:
        payload["labels"] = list(labels)
    data = _gh_api("POST", f"repos/{repo}/issues", body=payload)
    return {"number": data["number"], "id": data["id"]}

def add_sub_issue(repo, parent, child_db_id):
    return _gh_api("POST", f"repos/{repo}/issues/{parent}/sub_issues",
                   body={"sub_issue_id": int(child_db_id)})

def set_labels(repo, n, add=(), remove=()):
    cur = set(issue_state(repo, n)["labels"])
    new = sorted((cur | set(add)) - set(remove))
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"labels": new})        # real array; [] clears

def assign(repo, n, login):
    _gh_api("POST", f"repos/{repo}/issues/{n}/assignees", body={"assignees": [login]})

def unassign(repo, n, login):
    _gh_api("DELETE", f"repos/{repo}/issues/{n}/assignees", body={"assignees": [login]})

def close_issue(repo, n):  _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"state": "closed"})
def reopen_issue(repo, n): _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"state": "open"})

def issue_state(repo, n):
    d = _gh_api("GET", f"repos/{repo}/issues/{n}")
    return {"state": d["state"], "id": d["id"],
            "labels": [l["name"] for l in d.get("labels", [])],
            "assignees": [a["login"] for a in d.get("assignees", [])]}

def get_body(repo, n):
    return _gh_api("GET", f"repos/{repo}/issues/{n}").get("body") or ""

def set_body(repo, n, body):
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"body": body})
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan3 T1: ledger/gh.py (JSON --input bodies, real arrays — Codex #1)`).

---

## Task 2: Lease (body marker) + claim model (`ledger/claim.py`)

Lease = a single hidden body marker, TTL `L`. **claim confirms sole ownership BEFORE any
status/lease write, and unassigns itself on a lost race** (Codex #2). `release` unassigns +
clears the lease.

**Files:** Create `ledger/claim.py`, `tests/ledger/test_claim.py`

**Interfaces:**
- `eligible(state)->bool`; `read_lease`/`renew_lease`; `claim(repo,n,worker,now_ts,ttl,gh)->bool`;
  `release(repo,n,worker,gh)`; `lease_is_stale(lease_ts,now_ts,L)->bool`.

- [ ] **Step 1: Write failing tests (incl. lost-race no-residue — Codex #2)**

```python
# tests/ledger/test_claim.py
from unittest.mock import MagicMock
from ledger import claim

def test_eligible_only_when_unassigned_and_open():
    assert claim.eligible({"assignees": [], "labels": ["status:ready"], "state": "open"})
    assert not claim.eligible({"assignees": ["x"], "labels": ["status:ready"], "state": "open"})
    assert not claim.eligible({"assignees": [], "labels": ["status:blocked"], "state": "open"})
    assert not claim.eligible({"assignees": [], "labels": ["status:done"], "state": "closed"})

def test_lease_staleness():
    assert claim.lease_is_stale(100, now_ts=100 + 901, L=900)
    assert not claim.lease_is_stale(100, now_ts=100 + 10, L=900)
    assert claim.lease_is_stale(None, now_ts=5, L=900)

def test_lease_body_marker_round_trip():
    body = {"v": "Phase A body."}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    claim.renew_lease("o/r", 1, "alice", 1782600000, gh)
    assert claim.read_lease("o/r", 1, gh) == {"worker": "alice", "ts": 1782600000}
    claim.renew_lease("o/r", 1, "alice", 1782600999, gh)
    assert body["v"].count("conductor-lease") == 1                  # replaced, not stacked
    assert claim.read_lease("o/r", 1, gh)["ts"] == 1782600999

def test_claim_won_mutates_status_and_lease():
    gh = MagicMock()
    gh.issue_state.side_effect = [
        {"assignees": [], "labels": ["status:ready"], "state": "open"},   # eligible
        {"assignees": ["me"], "labels": ["status:ready"], "state": "open"},  # sole owner
    ]
    gh.get_body.return_value = "body"
    assert claim.claim("o/r", 1, "me", now_ts=10, ttl=900, gh=gh) is True
    gh.set_labels.assert_called_once()
    gh.set_body.assert_called_once()

def test_claim_lost_race_backs_off_with_no_residue():               # Codex #2
    gh = MagicMock()
    gh.issue_state.side_effect = [
        {"assignees": [], "labels": ["status:ready"], "state": "open"},      # eligible
        {"assignees": ["other", "me"], "labels": ["status:ready"], "state": "open"},  # lost
    ]
    assert claim.claim("o/r", 1, "me", now_ts=10, ttl=900, gh=gh) is False
    gh.unassign.assert_called_once_with("o/r", 1, "me")             # backed off
    gh.set_labels.assert_not_called(); gh.set_body.assert_not_called()  # NO status/lease residue

def test_release_unassigns_and_clears_lease():
    body = {"v": "x <!-- conductor-lease worker=alice ts=1 -->"}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    claim.release("o/r", 1, "alice", gh)
    gh.unassign.assert_called_once_with("o/r", 1, "alice")
    assert "conductor-lease" not in body["v"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/claim.py`:

```python
import re

BLOCKING = {"status:blocked", "status:done"}
_LEASE = re.compile(r"<!--\s*conductor-lease worker=(\S+) ts=(\d+)\s*-->")

def eligible(state):
    labels = set(state.get("labels", []))
    return (not state.get("assignees")
            and state.get("state") != "closed"
            and not (labels & BLOCKING)
            and "dep-blocked" not in labels)

def lease_is_stale(lease_ts, now_ts, L):
    return lease_ts is None or (now_ts - lease_ts) > L

def read_lease(repo, n, gh):
    m = _LEASE.search(gh.get_body(repo, n) or "")
    return {"worker": m.group(1), "ts": int(m.group(2))} if m else None

def renew_lease(repo, n, worker, now_ts, gh):
    body = _LEASE.sub("", gh.get_body(repo, n) or "").rstrip()
    gh.set_body(repo, n, f"{body}\n\n<!-- conductor-lease worker={worker} ts={now_ts} -->")

def claim(repo, n, worker, now_ts, ttl, gh):
    if not eligible(gh.issue_state(repo, n)):
        return False
    gh.assign(repo, n, worker)
    if gh.issue_state(repo, n)["assignees"] != [worker]:        # lost the race (Codex #2)
        gh.unassign(repo, n, worker)                            # back off; no status/lease touched yet
        return False
    gh.set_labels(repo, n, add=["status:in-progress"], remove=["status:ready"])
    renew_lease(repo, n, worker, now_ts, gh)
    return True
```
(Both-lose is acceptable — they retry next fire; the Phase-2 dispatcher removes the race
entirely, §7. release:)
```python
def release(repo, n, worker, gh):
    gh.unassign(repo, n, worker)
    body = _LEASE.sub("", gh.get_body(repo, n) or "").rstrip()
    gh.set_body(repo, n, body)
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan3 T2: claim confirm-then-mutate + lost-race back-off (Codex #2); lease body marker`).

---

## Task 3: Reconcile (§7 precedence + repairs) — promote E3

Promote `experiments/E3-gh-ledger/reconcile.py`. **Order: stale-lease reclaim BEFORE retry-cap**
(Codex #3) — a dead worker is reclaimed (reset `ready`, unassign), not escalated; a
`stale-lease-reclaim` action signals the caller to reset that phase's retry counter. Retry-cap
then applies only to a LIVE owner's genuine repeated failures.

**Files:** Create `ledger/reconcile.py`, `tests/ledger/test_reconcile.py`

**Interfaces:**
- `reconcile(repo, n, *, tests_red, pr_merged, commits_since_baseline, retries, R, gh,
  now_ts=None, L=900) -> {action, new_status}`.

- [ ] **Step 1: Write failing tests (the §7 table + ordering — Codex #3)**

```python
# tests/ledger/test_reconcile.py
from unittest.mock import MagicMock
from ledger import reconcile

def _gh(state, body=""):
    g = MagicMock(); g.issue_state.return_value = state; g.get_body.return_value = body
    return g

def test_done_but_tests_red_reopens_to_in_progress():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=True,
                              commits_since_baseline=3, retries=0, R=3, gh=g)
    assert out["new_status"] == "status:in-progress"; g.reopen_issue.assert_called_once()

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

def test_stale_lease_reclaims_before_retry_cap(self=None):          # Codex #3
    g = _gh({"state": "open", "labels": ["status:in-progress"], "assignees": ["dead"], "id": 1},
            body="<!-- conductor-lease worker=dead ts=100 -->")
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=False,
                              commits_since_baseline=1, retries=99, R=3, gh=g,  # retries EXHAUSTED
                              now_ts=100 + 5000, L=900)
    assert out["action"] == "stale-lease-reclaim" and out["new_status"] == "status:ready"
    g.unassign.assert_called_once_with("o/r", 1, "dead")            # reclaimed, NOT blocked

def test_live_owner_retry_cap_blocks():
    g = _gh({"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1},
            body="<!-- conductor-lease worker=w ts=100 -->")
    out = reconcile.reconcile("o/r", 1, tests_red=True, pr_merged=False,
                              commits_since_baseline=1, retries=3, R=3, gh=g,
                              now_ts=110, L=900)                     # FRESH lease -> live owner
    assert out["new_status"] == "status:blocked"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/reconcile.py`:

```python
from ledger import claim

def reconcile(repo, n, *, tests_red, pr_merged, commits_since_baseline,
              retries, R, gh, now_ts=None, L=900):
    st = gh.issue_state(repo, n)
    labels = set(st["labels"])
    status = next((l for l in labels if l.startswith("status:")), None)
    closed = st["state"] == "closed"

    def repair(new_status, action, reopen=False):
        if reopen:
            gh.reopen_issue(repo, n)
        if new_status:
            gh.set_labels(repo, n, add=[new_status],
                          remove=[l for l in labels if l.startswith("status:")])
        return {"action": action, "new_status": new_status}

    # Precedence: git/tests > PR > label.
    # 1. STALE-LEASE RECLAIM FIRST (Codex #3): dead owner -> reclaim, do NOT count vs retry cap.
    #    The 'stale-lease-reclaim' action tells the caller to reset this phase's retry counter.
    if status == "status:in-progress" and st["assignees"] and now_ts is not None:
        lease = claim.read_lease(repo, n, gh)
        if claim.lease_is_stale(lease["ts"] if lease else None, now_ts, L):
            for w in st["assignees"]:
                gh.unassign(repo, n, w)
            return repair("status:ready", "stale-lease-reclaim")
    # 2. Retry cap (§6.1) — a LIVE owner's genuine repeated failures -> blocked.
    if tests_red and retries >= R:
        return repair("status:blocked", "retry-cap-exceeded")
    # 3. done/closed but tests red -> reopen -> in-progress.
    if (status == "status:done" or closed) and tests_red:
        return repair("status:in-progress", "reopen-tests-red", reopen=True)
    # 4. in-progress but no assignee -> reset ready (abandoned).
    if status == "status:in-progress" and not st["assignees"]:
        return repair("status:ready", "reset-abandoned")
    # 5. closed but PR not merged -> reopen.
    if closed and not pr_merged:
        return repair("status:in-progress", "reopen-unmerged", reopen=True)
    return {"action": "none", "new_status": status}
```

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit** (`Plan3 T3: reconcile stale-reclaim-before-retry-cap (Codex #3)`).

---

## Task 4: issue-sync generate + convert (`ledger/sync.py`)

**Files:** Create `ledger/sync.py`, `tests/ledger/test_sync.py`

**Interfaces:**
- `generate(repo, plan: dict, gh) -> dict` (plan = `{title, phases:[{title, status, tasks:[str]}]}`)
  → milestone + phase issues (labeled) + task sub-issues; returns `{milestone, phases:[{number,
  sub_issues:[number]}]}`. `convert(repo, plan_md_path, gh)` parses a thin `plan.md` → plan dict
  → `generate`. `parse_plan_md(text)->dict`.

- [ ] **Step 1: Write failing tests** (gh mocked; assert milestone→phases→sub-issue linking by
  DB id; parse `## Phase A [ready]` + `- [ ]` tasks; `add_sub_issue` raises → fallback to
  `- [ ] #<task#>` in the parent body via `set_body`, record `fallback=True` — mirrors E3).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `ledger/sync.py` (`parse_plan_md`, `generate`, `convert`): ensure
  status labels; create milestone; per phase create issue `status:<status>`; per task create
  issue + `add_sub_issue(parent_number, task_db_id)`; on failure append `- [ ] #<task#>` to the
  parent body via `set_body` and set `fallback=True`.

- [ ] **Step 4: Run → PASS.** Lint+typecheck. **Commit.**

---

## Task 5: `/conductor:issue-sync` skill + `conductor ledger` CLI + integration

**Files:** Create `skills/issue-sync/SKILL.md`; modify `bin/conductor`; create
`tests/ledger/test_integration.py`, `tests/test_skill_outputs.py` (add a check).

- [ ] **Step 1: Skill + structural test.** `skills/issue-sync/SKILL.md` (`name: issue-sync`)
  documents **generate / convert / reconcile**, states it **never prompts** (§5), and that
  reconcile follows precedence `git/tests > PR > label` and reclaims stale leases before the
  retry cap. Structural test asserts the body mentions `generate`, `convert`, `reconcile`,
  `precedence`, `sub-issue`, `never prompt`, `stale`.

- [ ] **Step 2: CLI** — `conductor ledger {generate <plan.json>|convert <plan.md>|reconcile <issue#> [flags]}`
  dispatching to `python3 -m ledger.<cmd>`.

- [ ] **Step 3: Integration test (ephemeral, cleaned up)** — mirrors E3 against the real repo:
  generate `IT-<runid>` milestone + 2 phases + 3 sub-issues; assert hierarchy via `gh api`;
  `claim` phase A (sole-owner confirm + lease body marker); flip `done`+close; inject red;
  `reconcile` → reopen→in-progress; set green → permitted; write a stale lease + `reconcile(now_ts
  past L)` → stale-reclaim resets to ready and unassigns. Teardown: close issues + delete the
  milestone. `@pytest.mark.integration`, skipped unless `RUN_GH_INTEGRATION=1` (CI without gh
  stays green; NOT the fail-closed done-gate).

- [ ] **Step 4: Full quality gate + commit** (`Plan3 T5: /conductor:issue-sync skill + ledger CLI + integration`).

---

## Self-Review

**Coverage (§11 comps 4–5, §7):** issue-sync generate/convert/reconcile → T4+T5; ledger+claim
(assignee + lease body marker + labels, precedence, invalid-combo repair, retry cap,
stale-lease reclaim) → T2+T3. gh portability (amendment D) → T1.

**Codex review round 2 (Plan 3) — addressed:**
- **#1 array fields:** `_gh_api` sends JSON bodies via `--input -`; `labels`/`assignees` are real
  arrays (`[]` clears); tested by asserting the body is a list, not a json-string. ✓
- **#2 race-loss residue:** `claim` confirms sole ownership before any status/lease write and
  unassigns itself on loss; `test_claim_lost_race_backs_off_with_no_residue` asserts no
  set_labels/set_body. ✓
- **#3 stale-reclaim ordering:** stale-lease reclaim runs before retry-cap; reclaim resets the
  caller's retry counter; `test_stale_lease_reclaims_before_retry_cap` (retries exhausted →
  reclaim, not blocked) + `test_live_owner_retry_cap_blocks` (fresh lease → blocked). ✓
- Round 1: release unassigns (#3); reconcile takes lease (#4); lease body marker not labels (#5). ✓

**§7 rule coverage:** precedence git/tests>PR>label; invalid combos; retry cap→blocked; stale
reclaim→ready; eligibility; sub-issues + checklist fallback (E3-proven).

**Consistency:** `_gh_api` body seam mocked uniformly; lease marker regex identical in
claim/reconcile; status labels `status:<x>`; `/conductor:issue-sync` namespacing.

---

## Open follow-ups
- Plan 4 (`/conductor:autodev` + `/conductor:start`) calls `reconcile(now_ts=…, L=…)` every
  fire (resetting the retry counter on a `stale-lease-reclaim` action), `generate`/`convert` at
  setup / next-plan / deepen-in-place, and `release` on unit completion.
