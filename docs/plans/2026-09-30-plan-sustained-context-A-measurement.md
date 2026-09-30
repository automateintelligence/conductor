# Sustained context, Phase A — token accounting — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the token usage of every host dispatch Conductor makes (worker fire, reviewer call) in `run.json` `dispatches`, and report per-phase totals by role from `conductor status`, so the owner can make the §3 go / no-go call on §4 and §5 from measured numbers.

**Architecture:** Each host adapter gains a `usage_from_output` parser for its own machine-readable output (`claude -p --output-format json`, `codex exec --json`). A small `conductor/dispatches.py` validates, appends, and totals dispatch records under the existing `state.lock` + revision discipline. Worker fires are captured by the generated cron driver: the fire command emits JSON into the log, and a post-fire `conductor usage ingest` parses that fire's slice of the log. Reviewer calls are captured by a new `conductor review` verb that launches the opposite host itself through `reviewer_argv` (cold start only; no session resume in this phase) and prints the review text for the worker to post exactly as today.

**Tech Stack:** Python 3.10 stdlib only, pytest, bash (generated driver), `gh`, `git`.

**Spec:** `docs/specs/2026-09-29-sustained-context-design.md` (§3 is this plan; §4 and §5 are gated on this plan's numbers — see "Follow-on plans" at the end).

## Global Constraints

- If a host does not report a field, record it as `null`, never `0` (spec §3).
- Dispatch record fields named by the spec: host, role (`worker` | `reviewer`), phase id, PR head sha, input tokens, cached input tokens, output tokens, wall time (spec §3).
- `conductor status --run <run-key>` prints per-phase totals split by role; `--json` includes them (spec §3).
- Every reviewer invocation stays time-bounded, the same as every other host call (A-DH-4; spec §4 "Launch path").
- Every `run.json` write goes through `conductor.core.runstate.update` (state.lock + revision) (spec §4 "State").
- No gate path reads `dispatches` (spec §8 "Contract"): `conductor/merge_gate.py` and `conductor/merge_cmd.py` must not reference it.
- No test runs a real `claude` or `codex` (repo guard `tests/conftest.py`; spec §8 "Unit").
- No argv is built by shared code: each adapter builds its own in its own module (`conductor/hosts/base.py` docstring; enforced by `tests/conductor/hosts/`). The token `-p` must never appear in `conductor/hosts/codex.py`.
- The worker never resumes a session; Phase A never resumes any session (spec §4, §9).
- Nothing in spec §4 (session resume) or §5 (run digest) is built by this plan (spec §3 "Measure first").
- Test scope per task: the focused test file(s) for that task plus `ruff check` / `ruff format --check` / `pyright` on touched files. The full suite runs once, at the PR.

## Verified host facts this plan relies on

Recorded in Task 1's `docs/reviews/2026-09-30-host-usage-ground-truth.md`, probed on claude 2.1.286 and codex-cli 0.156.1:

1. `claude -p <prompt> --output-format json` prints ONE JSON object with `type: "result"`, `subtype`, `is_error`, `result` (final text), `session_id`, `usage`, `modelUsage`.
2. Claude's top-level `usage` covers only the last main-thread model call. A run that spawned one subagent reported `usage.cache_read_input_tokens = 56653` while `modelUsage` summed to `137139`. **Fire totals must be summed from `modelUsage`** (per model: `inputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `outputTokens`).
3. `codex exec --json` prints JSONL: `thread.started` (`thread_id`), `item.completed` items (`agent_message` text; `error` items are config warnings, not failures), `turn.completed` with `usage: {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`. `input_tokens` already includes `cached_input_tokens`; `output_tokens` already includes reasoning.
4. On resume, BOTH hosts report the session's running total, not the round's own (Claude `modelUsage`, Codex `turn.completed.usage`). Irrelevant to Phase A (no resume); recorded for the §4 plan.
5. Read-only reviewer postures, verified to allow `git diff` and block file writes:
   - Claude: `--permission-mode dontAsk '--allowedTools=Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)'` (the `=` form matters: `--allowedTools` is variadic and swallows a following prompt).
   - Codex: `exec --json --sandbox read-only --cd <dir>` (read-only also blocks network, so the reviewer cannot call `gh`; Conductor supplies PR facts in the prompt).
6. `claude --resume <unknown>` exits 1 with `No conversation found with session ID: …`; `codex exec resume <unknown>` exits 1 with `no rollout found for thread id …` (for §4).

## Review Focus

1. **A fire whose output carries no usage** (owner flag `--output-format text` in `CLAUDE_FLAGS`, a crash before the result line, a killed fire): ingest must still record the dispatch with `null` token fields and a `note`, and `status` must mark that phase incomplete. Pinned in Task 2 (parsers return all-`None`) and Task 5 (`test_ingest_records_nulls_when_the_log_slice_has_no_usage`).
2. **Log slice contamination** — stderr lines (hook failures, warnings) interleaved with the JSON, and earlier fires' JSON before `$FIRE_LOG0`: the parser must read only bytes after the offset and skip non-JSON lines. Pinned in Task 2 (`test_claude_usage_skips_non_json_lines`) and Task 5 (`test_ingest_reads_only_bytes_after_the_offset`).
3. **A recording failure must never cost the review or the fire** (run missing, lock contention, schema error): `conductor review` still prints the review and exits 0; the driver's ingest line can never change the fire's exit status. Pinned in Task 8 (`test_review_still_prints_when_recording_fails`) and Task 6 (driver text: `|| true`, rc preserved).
4. **Reviewing a stale checkout** — local `HEAD` is not the PR head: the review would describe code that is not the PR. `conductor review` must refuse with exit 2 before launching a host. Pinned in Task 8 (`test_review_refuses_a_checkout_that_is_not_the_pr_head`).
5. **A hung reviewer host**: the call must end at its timeout with the process group killed, exit 4, and a `timeout` dispatch recorded. Pinned in Task 7 (`test_run_bounded_kills_the_group_on_timeout`) and Task 8 (`test_review_times_out_and_records_it`).

## File Structure

| File | Responsibility |
| --- | --- |
| `docs/reviews/2026-09-30-host-usage-ground-truth.md` (create) | The probe record behind every parser and argv in this plan |
| `tests/conductor/fixtures/claude-print-json-2.1.286.json`, `claude-print-json-subagent-2.1.286.json`, `codex-exec-json-0.156.1.jsonl` (create) | Recorded host outputs the parser tests replay |
| `conductor/hosts/base.py` (modify) | `Usage` dataclass; protocol members `usage_from_output`, revised `reviewer_argv` signature |
| `conductor/hosts/claude.py`, `conductor/hosts/codex.py` (modify) | Each host's own `usage_from_output`, `executable`, `reviewer_argv` |
| `conductor/hosts/bounded.py` (create) | `run_bounded`: one time-bounded, process-group-killing child run |
| `conductor/core/schema.py` (modify) | `validate_dispatch`, called from `validate_run` for each entry |
| `conductor/dispatches.py` (create) | `make`, `append`, `phase_totals` |
| `conductor/lifecycle.py` (modify) | `status` prints and emits `usage` totals |
| `conductor/usage_cmd.py` (create) | `conductor usage ingest` — worker-fire recording from the driver log |
| `conductor/resume_script.py` (modify) | fire command emits JSON; post-fire ingest line; `TEMPLATE_VERSION` 12 → 13 |
| `conductor/review_cmd.py` (create) | `conductor review <pr>` — launch, bound, record, print |
| `conductor/merge_gate.py` (modify) | expose `closes_issue(body)` next to `_CLOSES_RE` |
| `bin/conductor` (modify) | route `usage` and `review` |
| `skills/autodev/SKILL.md` (modify) | step 5/6 invoke `conductor review` instead of the review wrapper |
| `README.md` (modify) | CLI reference rows for `usage ingest`, `review`; `status` row mention |
| `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` (modify) | version 0.10.0 → 0.11.0 |

---

### Task 1: Ground-truth record and fixtures

**Files:**
- Create: `docs/reviews/2026-09-30-host-usage-ground-truth.md`
- Create: `tests/conductor/fixtures/claude-print-json-2.1.286.json`
- Create: `tests/conductor/fixtures/claude-print-json-subagent-2.1.286.json`
- Create: `tests/conductor/fixtures/codex-exec-json-0.156.1.jsonl`

**Interfaces:**
- Produces: the three fixture paths above, consumed by Task 2 tests.

- [ ] **Step 1:** Write the ground-truth doc: host versions, exact commands run, the six facts listed under "Verified host facts" above with the observed numbers, and the two resume observations (claude1 → claude2 `modelUsage` 21550 → 60544 cache-read; codex `total_token_usage` 34072 → 80676 with `last_token_usage.input_tokens` 46604).
- [ ] **Step 2:** Save the probe outputs as fixtures. In the Codex JSONL, replace the home directory in config-warning `error` items with `<HOME>`; keep one such item so the parser test proves warnings are ignored.
- [ ] **Step 3:** Commit.

```bash
git add docs/reviews/2026-09-30-host-usage-ground-truth.md tests/conductor/fixtures/claude-print-json-2.1.286.json tests/conductor/fixtures/claude-print-json-subagent-2.1.286.json tests/conductor/fixtures/codex-exec-json-0.156.1.jsonl
git commit -m "docs/reviews/2026-09-30-host-usage-ground-truth.md, tests/conductor/fixtures/{claude-print-json*,codex-exec-json*} — new"
```

### Task 2: `Usage` and per-host parsers

**Files:**
- Modify: `conductor/hosts/base.py` (after `DispatchResult`; protocol member list)
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_usage_parsers.py` (create)

**Interfaces:**
- Produces:

```python
# conductor/hosts/base.py
@dataclass(frozen=True)
class Usage:
    input_tokens: int | None         # every prompt token, cached ones included
    cached_input_tokens: int | None  # the part served from cache
    cache_write_tokens: int | None   # tokens written to cache (billed above base on Claude)
    output_tokens: int | None
    session_id: str | None
    result_text: str | None
    is_error: bool                   # True when no result was found or the host said so

    @classmethod
    def unknown(cls) -> "Usage": ...  # all None, is_error=True

# HostAdapter protocol, new member:
def usage_from_output(self, text: str) -> Usage: ...
```

- [ ] **Step 1: Write the failing tests**

```python
"""Each host's machine-readable output -> one `Usage`, replayed from recorded 2026-09-30 probes."""

from __future__ import annotations

import json
import pathlib

from conductor.hosts import base

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_claude_usage_sums_model_usage_not_the_last_call():
    raw = _text("claude-print-json-subagent-2.1.286.json")
    doc = json.loads(raw)
    mu = doc["modelUsage"].values()
    u = base.load("claude").usage_from_output(raw)
    assert u.cached_input_tokens == sum(m["cacheReadInputTokens"] for m in mu)
    assert u.cached_input_tokens != doc["usage"]["cache_read_input_tokens"]
    assert u.input_tokens == sum(
        m["inputTokens"] + m["cacheReadInputTokens"] + m["cacheCreationInputTokens"] for m in mu
    )
    assert u.cache_write_tokens == sum(m["cacheCreationInputTokens"] for m in mu)
    assert u.output_tokens == sum(m["outputTokens"] for m in mu)
    assert u.session_id == doc["session_id"] and u.result_text == doc["result"]
    assert u.is_error is False


def test_claude_usage_skips_non_json_lines():
    raw = "SessionEnd hook [node x] failed\n" + _text("claude-print-json-2.1.286.json") + "\nnoise\n"
    u = base.load("claude").usage_from_output(raw)
    assert u.result_text == "alpha" and u.output_tokens is not None


def test_claude_usage_without_a_result_is_unknown_not_zero():
    u = base.load("claude").usage_from_output("plain text answer\n")
    assert u == base.Usage.unknown()
    assert u.input_tokens is None and u.output_tokens is None


def test_claude_usage_with_a_result_but_no_model_usage_records_null_tokens():
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "ok", "session_id": "s"})
    u = base.load("claude").usage_from_output(raw)
    assert u.result_text == "ok" and u.input_tokens is None and u.is_error is False


def test_claude_error_result_is_an_error():
    raw = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "session_id": "s", "modelUsage": {}})
    assert base.load("claude").usage_from_output(raw).is_error is True


def test_codex_usage_reads_turn_completed_and_ignores_warning_items():
    raw = _text("codex-exec-json-0.156.1.jsonl")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    turn = [e for e in events if e["type"] == "turn.completed"][-1]["usage"]
    u = base.load("codex").usage_from_output(raw)
    assert u.input_tokens == turn["input_tokens"]
    assert u.cached_input_tokens == turn["cached_input_tokens"]
    assert u.cache_write_tokens == turn["cache_write_input_tokens"]
    assert u.output_tokens == turn["output_tokens"]
    assert u.session_id == events[0]["thread_id"]
    assert u.result_text == "alpha" and u.is_error is False


def test_codex_usage_without_turn_completed_is_an_error_with_null_tokens():
    raw = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    u = base.load("codex").usage_from_output(raw)
    assert u.is_error is True and u.input_tokens is None and u.session_id == "t"


def test_codex_turn_failed_is_an_error():
    raw = ('{"type":"thread.started","thread_id":"t"}\n'
           '{"type":"turn.failed","error":{"message":"usage limit"}}\n')
    assert base.load("codex").usage_from_output(raw).is_error is True


def test_a_missing_usage_field_is_null_not_zero():
    raw = ('{"type":"thread.started","thread_id":"t"}\n'
           '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'
           '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}\n')
    u = base.load("codex").usage_from_output(raw)
    assert u.input_tokens == 5 and u.cached_input_tokens is None and u.cache_write_tokens is None
```

- [ ] **Step 2:** Run `pytest tests/conductor/hosts/test_usage_parsers.py -v`. Expected: FAIL (`AttributeError: ... has no attribute 'Usage'` / `usage_from_output`).
- [ ] **Step 3: Implement.** In `base.py` add `Usage` (above) and the protocol member. In each adapter module, add the method; keep the JSON line scanner private to each module (it is ten lines; the no-shared-host-code rule covers output parsing as much as argv).

```python
# conductor/hosts/claude.py — ClaudeAdapter
def usage_from_output(self, text: str) -> base.Usage:
    """`claude -p --output-format json` -> Usage. Totals come from `modelUsage`, which covers
    every model call in the invocation including subagents; top-level `usage` is only the last
    main-thread call (ground truth 2026-09-30 fact 2)."""
    result = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("type") == "result":
            result = doc
            break
    if result is None:
        return base.Usage.unknown()
    models = result.get("modelUsage")
    fields = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
    sums: dict[str, int | None] = dict.fromkeys(fields)
    if isinstance(models, dict) and models:
        for f in fields:
            vals = [m.get(f) for m in models.values() if isinstance(m, dict)]
            sums[f] = sum(vals) if vals and all(isinstance(v, int) for v in vals) else None
    fresh, read, write = sums["inputTokens"], sums["cacheReadInputTokens"], sums["cacheCreationInputTokens"]
    total_in = fresh + read + write if None not in (fresh, read, write) else None
    return base.Usage(
        input_tokens=total_in,
        cached_input_tokens=read,
        cache_write_tokens=write,
        output_tokens=sums["outputTokens"],
        session_id=result.get("session_id") if isinstance(result.get("session_id"), str) else None,
        result_text=result.get("result") if isinstance(result.get("result"), str) else None,
        is_error=bool(result.get("is_error")) or result.get("subtype") != "success",
    )
```

```python
# conductor/hosts/codex.py — CodexAdapter
def usage_from_output(self, text: str) -> base.Usage:
    """`codex exec --json` JSONL -> Usage. `turn.completed.usage.input_tokens` already includes
    cached tokens. `item.completed` items of type `error` are config warnings and are ignored;
    a `turn.failed` or top-level `error` event, or no `turn.completed`, is an error. On a resumed
    thread these numbers are the thread's running total (ground truth fact 4); Phase A never
    resumes."""
    thread = message = None
    usage: dict | None = None
    failed = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            thread = ev["thread_id"]
        elif kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                message = item["text"]
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif kind in ("turn.failed", "error"):
            failed = True

    def _n(key: str) -> int | None:
        v = (usage or {}).get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return base.Usage(
        input_tokens=_n("input_tokens"),
        cached_input_tokens=_n("cached_input_tokens"),
        cache_write_tokens=_n("cache_write_input_tokens"),
        output_tokens=_n("output_tokens"),
        session_id=thread,
        result_text=message,
        is_error=failed or usage is None,
    )
```

- [ ] **Step 4:** Run `pytest tests/conductor/hosts/test_usage_parsers.py tests/conductor/hosts/test_registry.py -v`. Expected: PASS. Add `"usage_from_output"` to the expected set in `test_the_protocol_declares_every_member_the_adapters_must_implement`.
- [ ] **Step 5:** Commit (`conductor/hosts/{base,claude,codex}.py`, `tests/conductor/hosts/test_usage_parsers.py`, `tests/conductor/hosts/test_registry.py`), message listing files with line ranges.

### Task 3: Dispatch record — schema, append, totals

**Files:**
- Modify: `conductor/core/schema.py` (add `DISPATCH_ROLES`, `DISPATCH_OUTCOMES`, `validate_dispatch`; call it from `validate_run` after the list-type loop)
- Create: `conductor/dispatches.py`
- Test: `tests/conductor/test_dispatches.py` (create); `tests/conductor/core/test_schema.py` (add cases)

**Interfaces:**
- Consumes: `base.Usage` (Task 2), `runstate.update`.
- Produces:

```python
# conductor/core/schema.py
DISPATCH_ROLES = ("worker", "reviewer")
DISPATCH_OUTCOMES = ("ok", "error", "timeout")
DISPATCH_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
def validate_dispatch(entry: object) -> dict: ...  # raises SchemaError

# conductor/dispatches.py
def make(*, host: str, role: str, phase_id: str | None, head_sha: str | None,
         usage: base.Usage, wall_s: float, outcome: str, note: str | None = None,
         now: str | None = None) -> dict: ...
def append(state_root: str, run_key: str, entry: dict) -> dict: ...  # returns the committed run
def phase_totals(dispatches: list[dict]) -> dict[str, dict[str, dict]]: ...
UNATTRIBUTED = "unattributed"
```

A dispatch entry is exactly: `host`, `role`, `phase_id`, `head_sha`, the four token fields, `wall_s`, `outcome`, `note`, `recorded_at`. Token fields are `int >= 0` or `None`; `host` in `HOST_IDS`; `phase_id`/`head_sha`/`note` are `str` or `None`; `wall_s` is a non-negative number; `recorded_at` a non-empty string.

`phase_totals` returns `{phase_key: {role: {"dispatches": int, "input_tokens": int, "cached_input_tokens": int, "cache_write_tokens": int, "output_tokens": int, "wall_s": float, "complete": bool}}}`; `phase_key` is `phase_id` or `"unattributed"`; `complete` is False when any summed entry had `None` in any token field (nulls are skipped in the sums, never counted as 0 silently — `complete` says so).

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import pytest

from conductor import dispatches
from conductor.core import schema
from conductor.hosts import base


def _u(i=100, c=60, w=10, o=5):
    return base.Usage(i, c, w, o, "s", "text", False)


def test_make_builds_a_valid_entry_with_nulls_preserved():
    e = dispatches.make(host="codex", role="reviewer", phase_id="12", head_sha="abc",
                        usage=base.Usage.unknown(), wall_s=3.5, outcome="error",
                        note="no-usage", now="2026-09-30T00:00:00+00:00")
    assert schema.validate_dispatch(e) is e
    assert e["input_tokens"] is None and e["output_tokens"] is None
    assert set(e) == {"host", "role", "phase_id", "head_sha", *schema.DISPATCH_TOKEN_FIELDS,
                      "wall_s", "outcome", "note", "recorded_at"}


@pytest.mark.parametrize("field,value", [
    ("role", "author"), ("host", "gpt"), ("outcome", "fine"), ("input_tokens", -1),
    ("output_tokens", 1.5), ("wall_s", -0.1), ("phase_id", 12), ("recorded_at", ""),
])
def test_validate_dispatch_rejects_bad_values(field, value):
    e = dispatches.make(host="claude", role="worker", phase_id=None, head_sha=None,
                        usage=_u(), wall_s=1, outcome="ok", now="t")
    e[field] = value
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch(e)


def test_validate_run_rejects_a_bad_dispatch_entry(run_doc):
    run_doc["dispatches"] = [{"host": "claude"}]
    with pytest.raises(schema.SchemaError):
        schema.validate_run(run_doc)


def test_phase_totals_split_by_phase_and_role_and_mark_nulls_incomplete():
    ds = [
        dispatches.make(host="claude", role="worker", phase_id="3", head_sha=None,
                        usage=_u(), wall_s=10, outcome="ok", now="t"),
        dispatches.make(host="codex", role="reviewer", phase_id="3", head_sha="a",
                        usage=_u(200, 150, 0, 7), wall_s=4, outcome="ok", now="t"),
        dispatches.make(host="codex", role="reviewer", phase_id="3", head_sha="b",
                        usage=base.Usage.unknown(), wall_s=2, outcome="timeout", now="t"),
        dispatches.make(host="claude", role="worker", phase_id=None, head_sha=None,
                        usage=_u(1, 0, 0, 1), wall_s=1, outcome="ok", now="t"),
    ]
    t = dispatches.phase_totals(ds)
    assert t["3"]["worker"] == {"dispatches": 1, "input_tokens": 100, "cached_input_tokens": 60,
                                "cache_write_tokens": 10, "output_tokens": 5, "wall_s": 10.0,
                                "complete": True}
    assert t["3"]["reviewer"]["dispatches"] == 2
    assert t["3"]["reviewer"]["input_tokens"] == 200
    assert t["3"]["reviewer"]["complete"] is False
    assert t[dispatches.UNATTRIBUTED]["worker"]["input_tokens"] == 1


def test_append_commits_under_the_run_revision(state_root, run_key_value):
    before = dispatches_run(state_root, run_key_value)["revision"]
    e = dispatches.make(host="claude", role="worker", phase_id="1", head_sha=None,
                        usage=_u(), wall_s=1, outcome="ok", now="t")
    after = dispatches.append(state_root, run_key_value, e)
    assert after["revision"] == before + 1 and after["dispatches"][-1] == e
```

`run_doc`, `state_root`, `run_key_value`, and `dispatches_run` are local fixtures/helpers in this test file: build the run with `schema.new_run_doc(...)` + `runstate.create(...)` exactly as `tests/conductor/core/test_schema.py` and `tests/conductor/core/test_runstate.py` already do (copy their helper, do not import test modules); `dispatches_run` is `runstate.load`.

- [ ] **Step 2:** `pytest tests/conductor/test_dispatches.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `validate_dispatch` in `schema.py` (explicit checks per the field rules above, `SchemaError` messages naming the field and value), call it for every entry of `doc["dispatches"]` in `validate_run`. Implement `dispatches.py`:

```python
"""Per-dispatch token accounting in run.json `dispatches` (sustained-context spec §3).

Derived data for the owner's go/no-go on §4/§5. No gate path reads it."""

from __future__ import annotations

import datetime

from conductor.core import runstate, schema
from conductor.hosts import base

UNATTRIBUTED = "unattributed"


def make(*, host, role, phase_id, head_sha, usage: base.Usage, wall_s, outcome, note=None, now=None) -> dict:
    entry = {
        "host": host, "role": role, "phase_id": phase_id, "head_sha": head_sha,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "wall_s": round(float(wall_s), 3), "outcome": outcome, "note": note,
        "recorded_at": now or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return schema.validate_dispatch(entry)


def append(state_root: str, run_key: str, entry: dict) -> dict:
    schema.validate_dispatch(entry)

    def mutate(doc: dict) -> dict:
        doc["dispatches"] = [*doc["dispatches"], entry]
        return doc

    return runstate.update(state_root, run_key, mutate)


def phase_totals(dispatches: list[dict]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for d in dispatches:
        key = d.get("phase_id") or UNATTRIBUTED
        row = out.setdefault(key, {}).setdefault(d["role"], {
            "dispatches": 0, **dict.fromkeys(schema.DISPATCH_TOKEN_FIELDS, 0),
            "wall_s": 0.0, "complete": True,
        })
        row["dispatches"] += 1
        row["wall_s"] = round(row["wall_s"] + float(d["wall_s"]), 3)
        for f in schema.DISPATCH_TOKEN_FIELDS:
            if d[f] is None:
                row["complete"] = False
            else:
                row[f] += d[f]
    return out
```

- [ ] **Step 4:** `pytest tests/conductor/test_dispatches.py tests/conductor/core/test_schema.py tests/conductor/core/test_runstate.py -v` → PASS.
- [ ] **Step 5:** Commit.

### Task 4: `conductor status` reports usage

**Files:**
- Modify: `conductor/lifecycle.py` (`cmd_status`, ~lines 308-360)
- Test: `tests/conductor/test_lifecycle.py` (add two tests beside the existing `status` tests, ~line 353)

**Interfaces:**
- Consumes: `dispatches.phase_totals` (Task 3), `dispatches.append`.
- Produces: `report["usage"]` = `phase_totals(run["dispatches"])` in `--json`; a text block after the existing fields.

Text format (one line per phase and role, phases in insertion order, `unattributed` last):

```
usage (tokens: input / cached / cache-write / output; wall s):
  phase 3   worker     1   100 / 60 / 10 / 5   10.0
  phase 3   reviewer   2   200 / 150 / 0 / 7   6.0   INCOMPLETE (a dispatch reported no usage)
```

With no dispatches: `usage: none recorded`.

- [ ] **Step 1: Failing tests**

```python
def test_status_json_carries_per_phase_usage_by_role(project, capsys) -> None:
    dispatches.append(project.state_root, project.run_key, dispatches.make(
        host="codex", role="reviewer", phase_id="7", head_sha="abc",
        usage=base.Usage(10, 4, 0, 2, "s", "r", False), wall_s=1.0, outcome="ok"))
    assert project.verb("status", "--run", project.run_key, "--json") == 0
    usage = json.loads(capsys.readouterr().out)["usage"]
    assert usage["7"]["reviewer"]["input_tokens"] == 10
    assert usage["7"]["reviewer"]["complete"] is True


def test_status_text_marks_a_phase_with_missing_usage_incomplete(project, capsys) -> None:
    dispatches.append(project.state_root, project.run_key, dispatches.make(
        host="claude", role="worker", phase_id="7", head_sha=None,
        usage=base.Usage.unknown(), wall_s=1.0, outcome="error", note="no-usage"))
    assert project.verb("status", "--run", project.run_key) == 0
    out = capsys.readouterr().out
    assert "phase 7" in out and "worker" in out and "INCOMPLETE" in out
```

- [ ] **Step 2:** Run those two tests → FAIL (`KeyError: 'usage'` / missing text).
- [ ] **Step 3:** Implement in `cmd_status`: `report["usage"] = dispatches.phase_totals(run.get("dispatches") or [])`; after the existing text loop, print the block above. Import `from conductor import dispatches`.
- [ ] **Step 4:** `pytest tests/conductor/test_lifecycle.py -k status -v` → PASS.
- [ ] **Step 5:** Commit.

### Task 5: `conductor usage ingest` — record one worker fire from the driver log

**Files:**
- Create: `conductor/usage_cmd.py`
- Modify: `bin/conductor` (add `usage)` route beside `status)`, same `PYTHONPATH` form)
- Test: `tests/conductor/test_usage_cmd.py` (create)

**Interfaces:**
- Consumes: `resolve.resolve`, `hosts.base.load(host).usage_from_output`, `dispatches.make/append`.
- Produces: CLI

```
conductor usage ingest --project <root> --host claude|codex --log <path> --offset <bytes>
                       --wall-s <seconds> --rc <fire exit status>
```

Behaviour:
1. Read `<log>` from byte `offset` to EOF (binary read, decode UTF-8 with `errors="replace"`). An offset past EOF reads nothing.
2. `usage = adapter.usage_from_output(slice)`.
3. `outcome`: `timeout` if `rc` in (124, 137); else `error` if `rc != 0`; else `ok`.
4. `note`: `"no-usage-in-output"` when `usage.input_tokens is None`, else `None`.
5. Phase and head: read `<project>/.conductor/handoff.md` only if its mtime is at or after `now - wall_s - 1`; take `phase_id` from `phase issue #(\d+)` and `head_sha` from `**Last unit:** <a>..<b>` (the `<b>`); otherwise both `None`.
6. Resolve the run with `resolve.resolve(start=project)`; append the dispatch.
7. Print one line `<iso-ts> usage-recorded role=worker phase=<id|-> input=<n|null> output=<n|null>` and exit 0. Any failure (no run, ambiguous run, lock/revision error, unreadable log): print `<iso-ts> usage-unrecorded reason=<short reason>` and exit 1. Never print the strings `fire-end` or `driver-unresolved` (`conductor driver status` scans the log for them).

- [ ] **Step 1: Failing tests** — build a project with a run exactly as `tests/conductor/test_lifecycle.py`'s `project` fixture does (copy the minimal parts: git repo, `conductor run new` via `run_cmd.main([...])`), write a fake log, call `usage_cmd.main([...])`:

```python
def test_ingest_reads_only_bytes_after_the_offset(proj, capsys):
    old = CLAUDE_FIXTURE_TEXT.replace('"alpha"', '"previous fire"')
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text("2026 fire-start posture=supervised\n" + old + "\n")
    offset = log.stat().st_size
    with log.open("a") as f:
        f.write("SessionEnd hook failed\n" + CLAUDE_SUBAGENT_FIXTURE_TEXT + "\n")
    assert usage_cmd.main(["ingest", "--project", str(proj.root), "--host", "claude",
                           "--log", str(log), "--offset", str(offset), "--wall-s", "40",
                           "--rc", "0"]) == 0
    d = proj.run["dispatches"][-1]
    assert d["role"] == "worker" and d["outcome"] == "ok"
    assert d["cached_input_tokens"] == SUBAGENT_CACHE_READ_SUM


def test_ingest_records_nulls_when_the_log_slice_has_no_usage(proj):
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text("plain text worker output\n")
    assert usage_cmd.main(["ingest", "--project", str(proj.root), "--host", "claude",
                           "--log", str(log), "--offset", "0", "--wall-s", "5",
                           "--rc", "137"]) == 0
    d = proj.run["dispatches"][-1]
    assert d["input_tokens"] is None and d["outcome"] == "timeout"
    assert d["note"] == "no-usage-in-output"


def test_ingest_takes_phase_and_head_from_a_fresh_handoff_only(proj):
    handoff = proj.root / ".conductor" / "handoff.md"
    handoff.write_text("**Active:** plan=p; milestone=#1; phase issue #42 (in-progress)\n"
                       "**Last unit:** aaa111..bbb222 — did things\n")
    ...  # ingest with wall-s 60 -> phase_id "42", head_sha "bbb222"
    os.utime(handoff, (0, 0))
    ...  # ingest again -> phase_id None, head_sha None


def test_ingest_without_a_run_reports_unrecorded_and_exits_1(tmp_path, capsys):
    ...  # a bare git repo with no run: exit 1, stdout contains "usage-unrecorded reason="
    # and neither "fire-end" nor "driver-unresolved"
```

(Write the `...` bodies out in full when implementing; they follow the first test's shape. `CLAUDE_FIXTURE_TEXT` etc. are read from `tests/conductor/fixtures/` at module top; `SUBAGENT_CACHE_READ_SUM` is computed from the fixture's `modelUsage`, not hard-coded.)

- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3:** Implement `usage_cmd.py` with `argparse` (subcommand `ingest`), per the behaviour list. Add the `bin/conductor` route: `usage) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.usage_cmd "$@" ;;`.
- [ ] **Step 4:** `pytest tests/conductor/test_usage_cmd.py -v` → PASS.
- [ ] **Step 5:** Commit.

### Task 6: The driver records every worker fire

**Files:**
- Modify: `conductor/hosts/claude.py` `resume_fire_command` (~line 126), `conductor/hosts/codex.py` `resume_fire_command` (~line 1050)
- Modify: `conductor/resume_script.py` — the template tail (~lines 790-797) and `TEMPLATE_VERSION` (line 44) 12 → 13
- Test: `tests/conductor/hosts/test_driver_text.py`, `tests/conductor/test_resume_script.py` (update the expectations that pin the fire line; add the tests below)

**Interfaces:**
- Consumes: `conductor usage ingest` (Task 5).
- Produces: fire commands
  - Claude: `"$CLAUDE_BIN" -p "/conductor:autodev" --output-format json "$@"`
  - Codex: `"$CODEX_BIN" exec --json --cd "$WORKTREE" "$@" "Read $CONDUCTOR_SOURCE/skills/autodev/SKILL.md and execute it."`
  - After `printf '%s fire-end rc=%s\n' …` and before `exit "$rc"`:

```bash
# USAGE ACCOUNTING (sustained-context spec §3): record this fire's tokens from its own slice of
# the log. Best-effort and bounded: it can never change the fire's exit status.
if command -v timeout >/dev/null 2>&1; then
    timeout 60 "$CONDUCTOR" usage ingest --project "$PROJECT" --host <id> --log "$LOG" \
        --offset "$FIRE_LOG0" --wall-s "$(( SECONDS - fire_started ))" --rc "$rc" >> "$LOG" 2>&1 || true
fi
```

(`<id>` is rendered from `h.id` by the template. Confirm `fire_started` is set unconditionally before this point in the rendered script; if the watchdog sets it only on one path, set `FIRE_T0=$SECONDS` next to `FIRE_LOG0` and use that instead.)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("host_id,flag", [("claude", "--output-format json"), ("codex", "exec --json")])
def test_fire_command_emits_machine_readable_usage(host_id, flag):
    assert flag in base.load(host_id).resume_fire_command()


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_driver_ingests_usage_after_fire_end_and_preserves_rc(host_id, tmp_path):
    text = resume_script.render_for_host(...)  # use the same render entry the existing driver-text tests use
    end = text.index("fire-end rc=")
    ingest = text.index("usage ingest")
    exit_line = text.rindex('exit "$rc"')
    assert end < ingest < exit_line
    assert f"--host {host_id}" in text[ingest:exit_line]
    assert "|| true" in text[ingest:exit_line]
    assert "timeout 60" in text[ingest - 200:ingest]
```

Also add one end-to-end driver test in `tests/conductor/test_resume_script.py`, following the existing pattern there that runs the rendered script with a stub host binary: the stub prints the recorded Claude fixture and exits 0; assert the run's last dispatch is `role == "worker"` with the fixture's token sums, and the script's exit status is the stub's (repeat with the stub exiting 3 and assert the driver exits 3 and the dispatch outcome is `error`).

- [ ] **Step 2:** Run the new tests → FAIL.
- [ ] **Step 3:** Implement the two fire-command edits (update their docstrings: the output is now JSON in the log, and why), the template tail, and bump `TEMPLATE_VERSION` to 13 (the start skill's reconcile regenerates stale drivers via `resume-script verify`).
- [ ] **Step 4:** `pytest tests/conductor/hosts/test_driver_text.py tests/conductor/test_resume_script.py tests/conductor/test_driver.py -v` → PASS (update any expectation that pinned the old fire line byte-for-byte; do not loosen unrelated assertions).
- [ ] **Step 5:** Commit.

### Task 7: Bounded runner, `executable`, `reviewer_argv`

**Files:**
- Create: `conductor/hosts/bounded.py`
- Modify: `conductor/hosts/base.py` (protocol: `reviewer_argv` signature), `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_bounded.py`, `tests/conductor/hosts/test_reviewer_argv.py` (create)

**Interfaces:**
- Produces:

```python
# conductor/hosts/bounded.py
@dataclass(frozen=True)
class Bounded:
    returncode: int | None   # None when killed
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

def run_bounded(argv: list[str], *, cwd: str, timeout: float, grace: float = 5.0,
                env: Mapping[str, str] | None = None) -> Bounded: ...

# HostAdapter protocol (replaces the unimplemented declaration)
def executable(self) -> str: ...   # shutil.which(self.id) or raise HostUnavailable
def reviewer_argv(self, prompt: str, *, project_root: str) -> list[str]: ...
```

`run_bounded`: `subprocess.Popen(argv, cwd=cwd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, text=True, errors="replace", start_new_session=True)`; `communicate(timeout=timeout)`; on `TimeoutExpired`: `os.killpg(pgid, SIGTERM)`, `communicate(timeout=grace)`, on a second expiry `os.killpg(pgid, SIGKILL)` then `communicate()`; `ProcessLookupError` from `killpg` is ignored. Stdin is `/dev/null` because Codex subcommands hang on an open stdin (ground truth 2026-08-12).

Argv (each built in its own module; `reject_flaglike_prompt(prompt)` first):
- Claude: `[self.executable(), "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk", "--allowedTools=Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"]`
- Codex: `[self.executable(), "exec", "--json", "--sandbox", "read-only", "--cd", project_root, prompt]`

`executable()` error text: ``f"`{self.id}` is not on PATH. Under cron, extend PATH in <project>/.conductor/resume-env.sh."``

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/hosts/test_bounded.py
def test_run_bounded_returns_output_and_status(tmp_path):
    r = bounded.run_bounded([sys.executable, "-c", "print('hi'); import sys; sys.exit(3)"],
                            cwd=str(tmp_path), timeout=10)
    assert r.returncode == 3 and r.stdout.strip() == "hi" and not r.timed_out


def test_run_bounded_kills_the_group_on_timeout(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = ("import subprocess,sys,time;"
              f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
              f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(60)")
    r = bounded.run_bounded([sys.executable, "-c", script], cwd=str(tmp_path), timeout=1, grace=1)
    assert r.timed_out and r.returncode is None and r.duration_s < 10
    grandchild = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


def test_run_bounded_gives_the_child_no_stdin(tmp_path):
    r = bounded.run_bounded([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
                            cwd=str(tmp_path), timeout=10)
    assert r.stdout.strip() == "''"
```

(The grandchild may be reaped as a zombie of init; if `os.kill(pid, 0)` succeeds on a zombie on this platform, instead read `/proc/<pid>/stat` and assert state `Z` or absence.)

```python
# tests/conductor/hosts/test_reviewer_argv.py
def test_claude_reviewer_is_read_only_and_json(monkeypatch, tmp_path):
    stub_on_path(monkeypatch, tmp_path, "claude")
    argv = base.load("claude").reviewer_argv("review PR 5", project_root=str(tmp_path))
    assert argv[1:3] == ["-p", "review PR 5"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    allowed = [a for a in argv if a.startswith("--allowedTools=")]
    assert len(allowed) == 1 and "Write" not in allowed[0] and "Edit" not in allowed[0]
    assert "--dangerously-skip-permissions" not in argv


def test_codex_reviewer_is_read_only_sandboxed_json(monkeypatch, tmp_path):
    codex_stub.put_on_path(monkeypatch, tmp_path / "stub-bin")
    argv = base.load("codex").reviewer_argv("review PR 5", project_root=str(tmp_path))
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[-1] == "review PR 5" and "-p" not in argv


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_reviewer_argv_refuses_a_flaglike_prompt(host_id, monkeypatch, tmp_path):
    ...  # stub on PATH; pytest.raises(ValueError) for prompt "--help"


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_executable_off_path_is_host_unavailable(host_id, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(base.HostUnavailable, match="resume-env.sh"):
        base.load(host_id).executable()
```

`stub_on_path` writes an executable `claude` shell stub (`#!/bin/sh\nexit 0`) into a tmp dir and prepends it to `PATH`. For the Codex off-PATH case, `tests/conftest.py`'s refusing `codex` is on `PATH`; setting `PATH` to an empty tmp dir removes it, which is what the test wants.

- [ ] **Step 2:** Run both files → FAIL.
- [ ] **Step 3:** Implement `bounded.py`, the protocol signature change, and each adapter's `executable` and `reviewer_argv`.
- [ ] **Step 4:** `pytest tests/conductor/hosts/ -v` → PASS (includes the structural no-shared-argv and no-`-p`-in-codex tests).
- [ ] **Step 5:** Commit.

### Task 8: `conductor review <pr>`

**Files:**
- Create: `conductor/review_cmd.py`
- Modify: `conductor/merge_gate.py` (add `closes_issue(body: str) -> str | None` beside `_CLOSES_RE`; capture the digits with a group)
- Modify: `bin/conductor` (route `review)`)
- Test: `tests/conductor/test_review_cmd.py` (create); `tests/conductor/test_merge_gate.py` (one `closes_issue` test)

**Interfaces:**
- Consumes: `runhost.resolve`, `base.opposite`, `base.load(h).reviewer_argv/usage_from_output`, `bounded.run_bounded`, `dispatches.make/append`, `resolve.resolve`, `remote.resolve`.
- Produces: CLI `conductor review <pr> --brief <file> [--run <key>] [--timeout <s>]`

Behaviour, in order:
1. Resolve the run (`resolve.resolve(run_key=args.run, start=cwd)`). Reviewer host = `run["reviewer_host"]` if set, else `base.opposite(runhost.resolve(project_root))`.
2. `gh pr view <pr> --json number,title,body,url,baseRefName,headRefOid` (run from the project root, bounded with `run_bounded`, 60 s).
3. `git rev-parse HEAD` must equal `headRefOid`; otherwise print `checkout-stale: HEAD <x> is not PR #<n> head <y>; push or check out the PR head first` to stderr and exit 2. No host is launched.
4. `base_sha = git merge-base HEAD <remote>/<baseRefName>` after `git fetch <remote> <baseRefName>` (remote from `remote.resolve(root)`).
5. Brief: read `--brief` (the phase's Spec sections and ADRs, written by the worker). Over 64 KiB → exit 2 `brief-too-large` (argv strings are capped at 128 KiB by the kernel).
6. Prompt (a module constant template):

```
You are the {reviewer} reviewer for a Conductor phase PR, reviewing work another host wrote.
Read-only: do not modify any file. Do not run the project's full test suite.

PR #{number}: {title}
URL: {url}
Head: {head_sha}   Base: {base_sha}
Review the full change with: git diff {base_sha}..{head_sha}
Read any file in this checkout you need for context.

Review it against the phase brief below: correctness, spec and ADR conformance, tests that
prove the behaviour, security. Report findings by severity (P0 blocker, P1 must-fix, P2
should-fix, P3 nit) with file:line and a one-line fix. End with exactly one line:
VERDICT: APPROVE | VERDICT: CHANGES REQUESTED

--- phase brief ---
{brief}
```

7. `run_bounded(adapter.reviewer_argv(prompt, project_root=root), cwd=root, timeout=args.timeout)`; default timeout `CONDUCTOR_REVIEW_TIMEOUT_S` env or 2400.
8. `usage = adapter.usage_from_output(result.stdout)`. Outcome: `timeout` if timed out; `error` if `returncode != 0` or `usage.is_error` or `usage.result_text` is empty; else `ok`.
9. Record: `dispatches.make(host=reviewer, role="reviewer", phase_id=merge_gate.closes_issue(body), head_sha=headRefOid, usage=usage, wall_s=result.duration_s, outcome=…, note=None or "no-usage-in-output")` then `dispatches.append`. Any exception while recording: print `usage-unrecorded reason=<…>` to stderr and continue.
10. Exit: `ok` → print `usage.result_text` to stdout, exit 0. `error` → print the last 40 lines of the host's stderr and stdout to stderr, exit 3. `timeout` → print `review-timeout after <n>s (<host>)` to stderr, exit 4.

Posting stays with the worker (Task 9): Conductor prints; the worker posts with the configured marker. This keeps the gate's provenance rules (`CONDUCTOR_REVIEW_AUTHOR`) exactly as they are.

- [ ] **Step 1: Failing tests** — a project with a run and a real local git remote (copy the `project` fixture shape from `tests/conductor/test_lifecycle.py`), a recording fake `gh` on `PATH` answering `pr view` with a JSON whose `headRefOid` is the local HEAD, and a stub reviewer binary on `PATH` (for Codex, use `codex_stub` extended with an `exec` branch that prints the recorded `codex-exec-json-0.156.1.jsonl` fixture; for Claude, a shell stub that cats the Claude fixture):

```python
def test_review_prints_the_review_and_records_a_reviewer_dispatch(proj, capsys): ...
    # exit 0; stdout == "alpha"; last dispatch role "reviewer", host == reviewer host,
    # phase_id == the PR body's Closes number, head_sha == headRefOid, tokens == fixture's

def test_review_refuses_a_checkout_that_is_not_the_pr_head(proj, capsys): ...
    # gh answers a different headRefOid: exit 2, "checkout-stale" on stderr,
    # the stub reviewer's invocation log is empty, no dispatch appended

def test_review_times_out_and_records_it(proj, capsys): ...
    # stub sleeps 60; --timeout 1: exit 4; last dispatch outcome "timeout", tokens None

def test_review_host_failure_exits_3_with_the_hosts_output(proj, capsys): ...
    # stub prints "usage limit reached" to stderr and exits 1: exit 3,
    # stderr contains "usage limit reached"; dispatch outcome "error"

def test_review_still_prints_when_recording_fails(proj, capsys, monkeypatch): ...
    # monkeypatch dispatches.append to raise RevisionConflict: exit 0, review on stdout,
    # "usage-unrecorded" on stderr

def test_review_refuses_an_oversized_brief(proj, capsys): ...
    # 70_000-byte brief: exit 2, "brief-too-large", no host launched

def test_no_gate_path_reads_dispatches():
    for mod in ("conductor/merge_gate.py", "conductor/merge_cmd.py"):
        assert "dispatches" not in (ROOT / mod).read_text()
```

Write each body in full when implementing; the comments state the exact assertions.

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `review_cmd.py` per the behaviour list, `merge_gate.closes_issue`, and the `bin/conductor` route.
- [ ] **Step 4:** `pytest tests/conductor/test_review_cmd.py tests/conductor/test_merge_gate.py -v` → PASS.
- [ ] **Step 5:** Commit.

### Task 9: Worker uses `conductor review`; docs; version

**Files:**
- Modify: `skills/autodev/SKILL.md` step 5 (~lines 202-208) and step 6 (~line 236)
- Modify: `README.md` CLI reference table (~line 417) and the `conductor status` description if present
- Modify: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` (`"version": "0.11.0"`)
- Test: `tests/conductor/test_skill_outputs.py` or the skill-corpus test that pins step 5's wording (find with `git grep -n "Opposite-host review" tests`); `tests/conductor/test_readme_contract.py` (add a row check)

- [ ] **Step 1: Failing tests**

```python
# tests/conductor/test_readme_contract.py
def test_cli_reference_documents_the_review_and_usage_verbs():
    ref = _section(_readme(), "## CLI reference")
    assert "`conductor review <pr> --brief <file>" in ref
    assert "`conductor usage ingest" in ref


# in the skill-text test module
def test_autodev_opposite_host_review_goes_through_conductor_review():
    text = read_skill("autodev")
    step5 = text[text.index("5. **Opposite-host review.**"):text.index("6. `receiving-code-review`")]
    assert "conductor review <pr> --brief" in step5
    assert "Usage-limit fallback" in step5  # the fallback rules survive unchanged
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Rewrite step 5's first sentence: write the phase brief (its Spec sections and ADRs, verbatim) to a temp file, run `conductor review <pr> --brief <file>` from the phase worktree after pushing, and post its stdout as the PR comment starting with the gate's review marker. Map exit codes: 2 → fix the precondition it names (push, check out the PR head, shorten the brief) and re-run; 3 → read the host output it printed and apply the existing usage-limit / transient-retry rules unchanged; 4 → treat as transient (retry once, then the same fallback). Keep every usage-limit fallback and provenance paragraph as is. Step 6: "the opposite host re-reviews" → "re-run `conductor review` for the final state". Add README rows:
  - `conductor review <pr> --brief <file> [--run <key>] [--timeout <s>]` — launches the run's reviewer host read-only and time-bounded against the PR head (refuses a stale checkout, exit 2), prints the review, records a `reviewer` dispatch; exit 3 host failure, 4 timeout.
  - `conductor usage ingest --project <root> --host <id> --log <path> --offset <n> --wall-s <s> --rc <n>` — the driver's post-fire step: records the fire's token usage from its slice of the log; exit 1 (and a `usage-unrecorded` line) when it cannot.
  - Extend the `status` description: per-phase token totals by role, `INCOMPLETE` where a host reported none.
  Bump both manifests to 0.11.0.
- [ ] **Step 4:** Run the two test files → PASS. Then the full suite once: `pytest -q` and `ruff check . && ruff format --check . && pyright .` → all green.
- [ ] **Step 5:** Commit; push the branch; open the PR (base `main`); run the codex review per the owner's standing rule.

---

## Follow-on plans (not in this plan)

- **Measurement check (spec §8):** after this ships, collect `conductor status --json` usage for at least three real phases and record them in `docs/reviews/`. The owner then decides go / no-go for §4 and §5 (spec §3).
- **§4 per-phase reviewer session** (if go): plan to be written then. It adds the `reviewer_session` record, resume variants of `reviewer_argv` (`claude -p --resume <id>`, `codex exec resume <id>` run with `cwd=` because `resume` has no `--cd`, and `-c sandbox_mode="read-only"` because it has no `--sandbox`), cumulative-usage subtraction (ground truth fact 4), the rule-3 fallbacks, and the dated note in `docs/plans/2026-08-10-plan-04-host-adapters.md` (spec §7).
- **§5 run digest** (if go): plan to be written then.

## Self-review

- Spec coverage: §3 bullets 1-3 → Tasks 2, 3, 5, 6, 8 (record), Task 4 (status). §3 null rule → Task 2/3/5 tests. §6 row 1 (null usage → incomplete) → Tasks 3/4. §8 unit/contract → Tasks 2-8 plus `test_no_gate_path_reads_dispatches`. §8 opt-in live and measurement check, §4, §5, §7 → deferred by §3's own gate (Follow-on plans).
- Types: `Usage` fields, `dispatches.make` keywords, `phase_totals` shape, and `reviewer_argv(prompt, *, project_root)` are identical wherever used.
