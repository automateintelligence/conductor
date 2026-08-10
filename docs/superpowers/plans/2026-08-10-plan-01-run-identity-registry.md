# Run Identity and Project Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Conductor's flat `.conductor/` state with a per-run registry keyed by a deterministic run key, so several specs can be conducted in one repository with independent goals, gates, manifests, baselines, and results — and so every later plan resolves run state through one function instead of ambient files.

**Architecture:** A new `conductor/core/` package owns state. `project.json` is the per-repository registry (spec path → ordered generations → run key); `.conductor/runs/<run-key>/run.json` is the per-run record. Every write is atomic (temp + fsync + replace); `project.json` mutations are guarded by `project.lock` plus a revision compare-and-swap, `run.json` mutations by `state.lock` plus its own revision. Writes that must span both files go through a journalled project transaction so a crash cannot leave a split identity. A single resolver turns "which run is this?" into a run key, and refuses ambiguity instead of guessing from `.conductor/run_branch`, `.conductor/goal.md`, or `CONDUCTOR_GATE_*`.

**Tech Stack:** Python 3.12 standard library only (`fcntl`, `hashlib`, `json`, `os`, `secrets`, `subprocess`, `tempfile`), pytest, ruff, pyright, git plumbing.

**Source design:** `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md` §"Project and run identity", the atomic-write/revision/transaction/lock-order paragraphs of §"Failure handling", and the first eight bullets plus the tracked-path bullet of §"Unit and contract tests".

**Roadmap:** `docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md` (this is Plan 01 of 11).

## Global Constraints

- **Host floor:** Claude Code `2.1.224`, Codex CLI `0.147.0`.
- **Canonical editable checkout:** `~/programming/conductor`. Old path quarantined at `~/.claude/conductor.quarantine-2026-08-10`, **no symlink left behind**.
- **Run key format:** `<spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]`. Generation 1 omits the `-g<N>` component.
- **Run integration branch:** `conductor/run-<run-key>`. **Phase branch:** `conductor/<run-key>/phase-<phase-id>`.
- **Run status vocabulary (exactly these six):** `active`, `checkpointed`, `blocked`, `awaiting-team-merge`, `terminal`, `failed`.
- **Review policy vocabulary (exactly these three):** `opposite-required`, `same-host-fallback-allowed`, `blocked-pending-opposite-host`.
- **Global lock order:** `migration.lock` (when applicable), then `project.lock` (when applicable), then `owner.lock`, then `state.lock`. Multi-run project operations acquire run locks in **sorted run-key order**.
- **Lease defaults:** 120 second lease, renewed at least every 30 seconds; a repository may lengthen these but may not configure a renewal interval greater than one quarter of the lease duration. *(Plan 01 stores the fields; Plan 02 enforces the semantics.)*
- **Review freshness:** default maximum review age 24 hours. **Per-fire context budget:** default `0.60`.
- **Conductor never merges to the repository default branch.**
- **Every state write** uses a sibling temporary file, flush, fsync, and atomic replace. `run.json` writes additionally require `state.lock` and the current revision; `project.json` mutations require `project.lock` and its current revision.
- **Every actionable failure reports:** run key and current state, the failed invariant or operation, the affected branch/worktree/pull request/state path, whether any write occurred, and the exact inspect/retry/takeover/migrate/recovery command.
- **The per-run `resume-env` is mode `0600` and contains no secrets.**
- **Tooling gates:** `ruff check . && ruff format --check .`, `pyright .`, `pytest -q`. Python 3.12.

## Working agreements for this plan

- **Do not reformat files this plan does not touch.** `ruff format --check .` currently fails on 11 pre-existing files. Run `ruff format` on **only** the files you create or modify, and verify with `ruff format --check <those files>`. A whole-repo `ruff format` would bury this plan's diff.
- **The done-gate is frozen.** `assertions/manifest.yaml` and `assertions/self_enforcement/` hold assertions A1–A16 under `conductor/freeze.py`. This plan must not weaken or remove any of them. Task 11 touches `conductor/paths.py`, which A12 (`test_a12_skills_call_resolvers.py`) observes — re-run `conductor gate verify` after Task 11 and treat a failure as a real regression, not a baseline to refresh.
- **Backwards compatibility window.** Legacy flat `.conductor/{goal.md,run_branch,resume-env.sh}` state stays readable in this plan. Plan 03 removes the fallback. Every function added here that takes a run key must ignore legacy files entirely; only the no-run-key code paths may consult them.
- **Commit granularity.** One commit per task, message style: files modified with line numbers plus one or two bullets on what was done.

## File Structure

**New package — `conductor/core/`** (state ownership; no host-specific strings, no slash commands, no `CLAUDE_PLUGIN_ROOT`):

| File | Responsibility |
| --- | --- |
| `conductor/core/__init__.py` | empty package marker |
| `conductor/core/atomic.py` | durable writes: temp + flush + fsync + replace + directory fsync; JSON helpers |
| `conductor/core/locks.py` | `flock`-based advisory locks and the global lock-order invariant |
| `conductor/core/names.py` | THE definition of the two names a run key determines: `assertions/<key>` and `conductor/run-<key>`. A leaf module — imports nothing from `paths` or `runkey`, so `paths.py` can use it without a cycle (`runkey` already imports `paths.spec_slug`) |
| `conductor/core/runkey.py` | spec-path normalization, path hash, run key, generation suffixes |
| `conductor/core/schema.py` | `project.json` / `run.json` shapes, the status and review-policy vocabularies, status transitions |
| `conductor/core/workstation.py` | the random host-neutral installation ID shared by both adapters |
| `conductor/core/registry.py` | `project.json` load/init/commit/update and the spec-path → generations mapping helpers |
| `conductor/core/transaction.py` | journalled cross-file writes with complete-or-reverse recovery |
| `conductor/core/runstate.py` | `run.json` create/load/commit/update/set_status under `state.lock` |
| `conductor/core/resolve.py` | canonical repo root and state root, active-run listing, run-key resolution, gate lookup for a run |
| `conductor/core/hygiene.py` | refuse tracked `.conductor` / `.worktrees`, establish and recheck the local git exclude |
| `conductor/core/repoint.py` | `repoint-spec`: move a spec within the repo while keeping the run key |
| `conductor/run_cmd.py` | the `conductor run` CLI verb group |

**Modified:**

| File | Change |
| --- | --- |
| `conductor/paths.py` | add `run_gate_dir`; give `resolve_gate` a run-key mode that ignores ambient files and env |
| `conductor/resume_script.py:58-77` | `main_root` delegates to `conductor.core.resolve.repo_root` (one implementation of the git-common-dir walk) |
| `bin/conductor` | add the `run` verb; let `gate-dir` accept `--run <key>`; extend the usage text |

**Tests:**

| File | Covers |
| --- | --- |
| `tests/conductor/core/__init__.py` | package marker |
| `tests/conductor/conftest.py` | the `git_env` / `git` / `git_repo` fixtures (a real isolated repository). Lives at `tests/conductor/`, **not** `tests/conductor/core/`, so Tasks 13 and 14 in `tests/conductor/` see it too — a conftest applies to its own directory and every subdirectory, which removes any need for `pytest_plugins` |
| `tests/conductor/core/test_atomic.py` | Task 1 |
| `tests/conductor/core/test_locks.py` | Task 2 |
| `tests/conductor/core/test_runkey.py` | Task 3 |
| `tests/conductor/core/test_schema.py` | Task 4 |
| `tests/conductor/core/test_workstation.py` | Task 5 |
| `tests/conductor/core/test_registry.py` | Task 6 |
| `tests/conductor/core/test_transaction.py` | Task 7 |
| `tests/conductor/core/test_runstate.py` | Task 8 |
| `tests/conductor/core/test_resolve.py` | Task 9 |
| `tests/conductor/core/test_hygiene.py` | Task 10 |
| `tests/conductor/test_gate_paths.py` | Task 11 (extend the existing file) |
| `tests/conductor/core/test_repoint.py` | Task 12 |
| `tests/conductor/test_run_cmd.py` | Task 13 |
| `tests/conductor/test_multi_run_isolation.py` | Task 14 |

---

### Task 1: Durable atomic writes

**Files:**
- Create: `conductor/core/__init__.py`
- Create: `conductor/core/atomic.py`
- Create: `tests/conductor/core/__init__.py`
- Test: `tests/conductor/core/test_atomic.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `write_atomic(path: str, data: str | bytes, *, mode: int = 0o644) -> None`
  - `write_json_atomic(path: str, doc: dict, *, mode: int = 0o644) -> None`
  - `read_json(path: str) -> dict | None` — `None` only when the file is absent; malformed JSON raises `ValueError`.

- [ ] **Step 1: Create the empty package markers**

```bash
mkdir -p conductor/core tests/conductor/core
touch conductor/core/__init__.py tests/conductor/core/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/conductor/core/test_atomic.py`:

```python
"""Durable state writes (design §"Failure handling"): sibling temp file, flush, fsync,
atomic replace. A crash must leave either the old bytes or the new ones — never a torn file."""

from __future__ import annotations

import stat

import pytest

from conductor.core import atomic


def test_write_atomic_replaces_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old\n")
    atomic.write_atomic(str(target), "new\n")
    assert target.read_text() == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_failed_replace_leaves_the_old_bytes_and_removes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old\n")

    class Boom(RuntimeError):
        pass

    def explode(*_args, **_kwargs):
        raise Boom("replace failed")

    monkeypatch.setattr(atomic.os, "replace", explode)
    with pytest.raises(Boom):
        atomic.write_atomic(str(target), "new\n")
    assert target.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_write_atomic_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "runs" / "alpha-1a2b3c4d" / "run.json"
    atomic.write_atomic(str(target), "{}\n")
    assert target.read_text() == "{}\n"


def test_write_atomic_honours_mode(tmp_path):
    target = tmp_path / "resume-env"
    atomic.write_atomic(str(target), "x\n", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_json_atomic_round_trips_with_stable_key_order(tmp_path):
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"b": 1, "a": 2})
    assert atomic.read_json(str(target)) == {"a": 2, "b": 1}
    assert target.read_text().index('"a"') < target.read_text().index('"b"')


def test_read_json_returns_none_only_for_an_absent_file(tmp_path):
    assert atomic.read_json(str(tmp_path / "missing.json")) is None


def test_read_json_raises_on_malformed_content(tmp_path):
    target = tmp_path / "project.json"
    target.write_text("{not json")
    with pytest.raises(ValueError):
        atomic.read_json(str(target))
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_atomic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.atomic'`

- [ ] **Step 4: Write the implementation**

Create `conductor/core/atomic.py`:

```python
"""Durable state writes.

Every Conductor state file (``project.json``, ``run.json``, transaction journals, ownership
records) is written the same way: sibling temporary file, write, flush, fsync, atomic replace,
then fsync the containing directory. A crash therefore leaves either the previous bytes or the
complete new ones — never a truncated file that reads as corrupt state on the next heartbeat.

Locking and revision checks live in ``locks``/``registry``/``runstate``; this module only
guarantees the bytes.
"""

from __future__ import annotations

import json
import os
import tempfile


def _fsync_dir(directory: str) -> None:
    """fsync the directory entry so the rename itself is durable, not just the file bytes."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: str, data: str | bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` durably. Creates missing parents. On any failure the temp
    file is removed and ``path`` keeps its previous contents."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".conductor-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def write_json_atomic(path: str, doc: dict, *, mode: int = 0o644) -> None:
    """Write ``doc`` as sorted, indented JSON. Sorted keys keep diffs and digests stable."""
    write_atomic(path, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: str) -> dict | None:
    """The document at ``path``, or ``None`` when the file does not exist.

    Malformed JSON raises rather than returning ``None``: an unreadable state file is a
    fail-closed condition, and silently treating it as "absent" would let a caller mint fresh
    state on top of a corrupted run."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_atomic.py -q`
Expected: PASS (7 passed)

- [ ] **Step 6: Lint and typecheck the new files**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: all clean. If `ruff format --check` complains, run `ruff format conductor/core tests/conductor/core` and re-run.

- [ ] **Step 7: Commit**

```bash
git add conductor/core/__init__.py conductor/core/atomic.py tests/conductor/core/__init__.py tests/conductor/core/test_atomic.py
git commit -m "conductor/core/atomic.py:1-70 — durable state writes

- temp + flush + fsync + atomic replace + directory fsync; failed write keeps old bytes
- read_json returns None only for an absent file; malformed JSON raises (fail-closed)"
```

---

### Task 2: Advisory locks and the global lock order

**Files:**
- Create: `conductor/core/locks.py`
- Test: `tests/conductor/core/test_locks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LOCK_ORDER: tuple[str, ...]` — `("migration", "project", "owner", "state")`
  - `class LockOrderError(RuntimeError)`, `class LockTimeout(RuntimeError)`
  - `hold(path: str, *, kind: str, run_key: str | None = None, timeout: float = 30.0, poll: float = 0.05)` — context manager yielding the open file descriptor.

**Design note for the implementer:** `flock` is per open-file-description, so two `os.open()` calls in the same process **do** conflict. Re-entrant acquisition of the same lock would therefore deadlock until the timeout. The order bookkeeping turns that deadlock into an immediate, named `LockOrderError`.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_locks.py`:

```python
"""The global lock order (design §"Failure handling"): migration.lock, then project.lock, then
owner.lock, then state.lock; multi-run project operations take run locks in sorted run-key order.

A lock-order violation is a deadlock waiting to happen, so it must fail loudly at acquisition
rather than block until a timeout."""

from __future__ import annotations

import errno

import pytest

from conductor.core import locks


def test_lock_order_is_the_documented_sequence():
    assert locks.LOCK_ORDER == ("migration", "project", "owner", "state")


def test_locks_may_be_taken_in_increasing_order(tmp_path):
    with locks.hold(str(tmp_path / "project.lock"), kind="project"):
        with locks.hold(str(tmp_path / "owner.lock"), kind="owner"):
            with locks.hold(str(tmp_path / "state.lock"), kind="state"):
                pass


def test_taking_a_lower_lock_while_holding_a_higher_one_is_refused(tmp_path):
    with locks.hold(str(tmp_path / "state.lock"), kind="state"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(str(tmp_path / "project.lock"), kind="project"):
                pass
    assert "project" in str(excinfo.value) and "state" in str(excinfo.value)


def test_reentrant_acquisition_of_the_same_lock_is_refused(tmp_path):
    path = str(tmp_path / "project.lock")
    with locks.hold(path, kind="project"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(path, kind="project"):
                pass
    assert "re-entrant" in str(excinfo.value)


def test_run_locks_of_the_same_kind_must_be_taken_in_sorted_run_key_order(tmp_path):
    with locks.hold(str(tmp_path / "b.lock"), kind="owner", run_key="beta-2222"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(str(tmp_path / "a.lock"), kind="owner", run_key="alpha-1111"):
                pass
    assert "sorted run-key order" in str(excinfo.value)
    with locks.hold(str(tmp_path / "a2.lock"), kind="owner", run_key="alpha-1111"):
        with locks.hold(str(tmp_path / "b2.lock"), kind="owner", run_key="beta-2222"):
            pass


def test_an_unknown_lock_kind_is_refused(tmp_path):
    with pytest.raises(locks.LockOrderError):
        with locks.hold(str(tmp_path / "x.lock"), kind="mystery"):
            pass


def test_a_busy_lock_times_out_with_the_path_named(tmp_path, monkeypatch):
    def always_busy(*_args, **_kwargs):
        raise OSError(errno.EAGAIN, "would block")

    monkeypatch.setattr(locks.fcntl, "flock", always_busy)
    path = str(tmp_path / "project.lock")
    with pytest.raises(locks.LockTimeout) as excinfo:
        with locks.hold(path, kind="project", timeout=0.05, poll=0.01):
            pass
    assert path in str(excinfo.value)


def test_the_held_set_is_cleared_after_a_lock_is_released(tmp_path):
    with locks.hold(str(tmp_path / "state.lock"), kind="state"):
        pass
    with locks.hold(str(tmp_path / "project.lock"), kind="project"):
        pass
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_locks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.locks'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/locks.py`:

```python
"""Advisory file locks and the one global lock order.

Design §"Failure handling" fixes the order: ``migration.lock`` when applicable, then
``project.lock`` when applicable, then ``owner.lock``, then ``state.lock``; multi-run project
operations acquire run locks in sorted run-key order. Nothing enforces that but code, and a
violation shows up as an intermittent deadlock between a heartbeat and a takeover — the worst
possible failure mode for an unattended run. So the order is tracked per context and violations
raise immediately, naming both locks.

``flock`` is per open-file-description: two ``os.open`` calls in one process genuinely conflict.
Re-entrant acquisition is therefore refused rather than left to block until the timeout.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import fcntl
import os
import time
from collections.abc import Iterator

LOCK_ORDER = ("migration", "project", "owner", "state")

_held: contextvars.ContextVar[tuple[tuple[str, int, str | None], ...]] = (
    contextvars.ContextVar("conductor_locks_held", default=())
)


class LockOrderError(RuntimeError):
    """A lock was requested out of the global order, re-entrantly, or under an unknown kind."""


class LockTimeout(RuntimeError):
    """Another holder kept the lock for the whole timeout window."""


def _rank(kind: str) -> int:
    try:
        return LOCK_ORDER.index(kind)
    except ValueError:
        raise LockOrderError(
            f"unknown lock kind {kind!r}; expected one of {LOCK_ORDER}"
        ) from None


def _check_order(kind: str, rank: int, run_key: str | None) -> None:
    for held_kind, held_rank, held_run in _held.get():
        if held_rank > rank:
            raise LockOrderError(
                f"lock-order violation: cannot take the {kind} lock while holding the "
                f"{held_kind} lock; the order is {' -> '.join(LOCK_ORDER)}"
            )
        if held_rank == rank and held_run == run_key:
            raise LockOrderError(
                f"re-entrant acquisition of the {kind} lock"
                + (f" for run {run_key!r}" if run_key else "")
                + " — flock is per open-file-description and this would deadlock"
            )
        if held_rank == rank and run_key is not None and held_run is not None:
            if run_key < held_run:
                raise LockOrderError(
                    f"lock-order violation: {kind} lock for {run_key!r} requested after "
                    f"{held_run!r}; multi-run operations take locks in sorted run-key order"
                )


@contextlib.contextmanager
def hold(
    path: str,
    *,
    kind: str,
    run_key: str | None = None,
    timeout: float = 30.0,
    poll: float = 0.05,
) -> Iterator[int]:
    """Hold an exclusive advisory lock at ``path`` for the block's duration.

    ``kind`` must be one of ``LOCK_ORDER``; ``run_key`` distinguishes per-run locks of the same
    kind so their sorted-order requirement can be checked."""
    rank = _rank(kind)
    _check_order(kind, rank, run_key)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"{kind} lock still held by another process after {timeout}s: {path}"
                    ) from None
                time.sleep(poll)
        token = _held.set(_held.get() + ((kind, rank, run_key),))
        try:
            yield fd
        finally:
            _held.reset(token)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_locks.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/locks.py tests/conductor/core/test_locks.py
git commit -m "conductor/core/locks.py:1-130 — flock helpers + the global lock order

- migration -> project -> owner -> state, run locks in sorted run-key order
- out-of-order, re-entrant and unknown-kind acquisitions raise LockOrderError immediately"
```

---

### Task 3: Run-key derivation and generations

**Files:**
- Create: `conductor/core/runkey.py`
- Test: `tests/conductor/core/test_runkey.py`

**Interfaces:**
- Consumes: `conductor.paths.spec_slug(spec_path: str) -> str` (existing, `conductor/paths.py:52`).
- Produces:
  - `HASH_LEN: int` (8)
  - `normalize_spec_path(repo_root: str, spec_path: str) -> str`
  - `path_hash(normalized_spec_path: str) -> str`
  - `run_key(normalized_spec_path: str, generation: int = 1) -> str`
  - `parse_generation(key: str) -> int`
  - `is_safe_run_key(key: str) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_runkey.py`:

```python
"""The deterministic run key (design §"Project and run identity"):

    <spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]

The relative-path hash is what stops two specs with the same filename in different directories
from colliding, and what keeps the key stable when the repository or worktree moves. Generation 1
omits the suffix; generation 2 and later carry -g2, -g3, ... so branches, worktrees, gate
directories, and run directories are generation-distinct."""

from __future__ import annotations

import pytest

from conductor import paths
from conductor.core import runkey


def test_key_is_the_slug_plus_an_eight_character_path_hash():
    rel = "docs/specs/2026-08-10-codex-dual-host-conductor-design.md"
    key = runkey.run_key(rel)
    slug = paths.spec_slug(rel)
    assert key.startswith(f"{slug}-")
    assert len(key) == len(slug) + 1 + runkey.HASH_LEN
    assert runkey.HASH_LEN == 8


def test_key_is_deterministic():
    rel = "docs/specs/alpha.md"
    assert runkey.run_key(rel) == runkey.run_key(rel)


def test_same_filename_in_different_directories_does_not_collide():
    assert runkey.run_key("docs/specs/alpha.md") != runkey.run_key("other/specs/alpha.md")


def test_generation_one_omits_the_suffix_and_later_generations_carry_it():
    rel = "docs/specs/alpha.md"
    base = runkey.run_key(rel)
    assert runkey.run_key(rel, 1) == base
    assert runkey.run_key(rel, 2) == f"{base}-g2"
    assert runkey.run_key(rel, 11) == f"{base}-g11"


def test_parse_generation_round_trips():
    rel = "docs/specs/alpha.md"
    for generation in (1, 2, 3, 17):
        assert runkey.parse_generation(runkey.run_key(rel, generation)) == generation


def test_generation_below_one_is_refused():
    with pytest.raises(ValueError):
        runkey.run_key("docs/specs/alpha.md", 0)


def test_normalize_is_repository_relative_and_survives_relocation(tmp_path):
    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    for root in (first, second):
        (root / "docs" / "specs").mkdir(parents=True)
        (root / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    rel_a = runkey.normalize_spec_path(str(first), str(first / "docs/specs/alpha.md"))
    rel_b = runkey.normalize_spec_path(str(second), "docs/specs/alpha.md")
    assert rel_a == rel_b == "docs/specs/alpha.md"
    assert runkey.run_key(rel_a) == runkey.run_key(rel_b)


def test_normalize_collapses_redundant_path_segments(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "specs").mkdir(parents=True)
    assert (
        runkey.normalize_spec_path(str(root), "./docs/../docs/specs/alpha.md")
        == "docs/specs/alpha.md"
    )


def test_normalize_refuses_a_path_outside_the_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError) as excinfo:
        runkey.normalize_spec_path(str(root), "../elsewhere/alpha.md")
    assert "outside the repository" in str(excinfo.value)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
def test_normalize_rescues_a_repository_reached_through_a_symlink_alias(tmp_path):
    """`root` is realpath'd but an absolute spec path is not, so a repo reached through a
    symlinked alias would otherwise report an in-repo file as outside."""
    actual = tmp_path / "actual"
    (actual / "docs" / "specs").mkdir(parents=True)
    (actual / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    alias = tmp_path / "alias"
    os.symlink(actual, alias)
    through_alias = runkey.normalize_spec_path(
        str(alias), str(alias / "docs" / "specs" / "alpha.md")
    )
    through_real = runkey.normalize_spec_path(
        str(actual), str(actual / "docs" / "specs" / "alpha.md")
    )
    assert through_alias == through_real == "docs/specs/alpha.md"
    assert runkey.run_key(through_alias) == runkey.run_key(through_real)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
def test_the_symlink_retry_does_not_weaken_the_outside_repository_guard(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alpha.md").write_text("# alpha\n")
    with pytest.raises(ValueError) as excinfo:
        runkey.normalize_spec_path(str(root), str(outside / "alpha.md"))
    assert "outside the repository" in str(excinfo.value)


def test_is_safe_run_key_accepts_generated_keys_and_rejects_traversal():
    assert runkey.is_safe_run_key(runkey.run_key("docs/specs/alpha.md", 3))
    assert not runkey.is_safe_run_key("../outside")
    assert not runkey.is_safe_run_key("a/b")
    assert not runkey.is_safe_run_key("")
    assert not runkey.is_safe_run_key("-leading")
    assert not runkey.is_safe_run_key("alpha.lock")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_runkey.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.runkey'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/runkey.py`:

```python
"""The deterministic run key.

    <spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]

Two properties earn the hash. First, ``spec_slug`` carries only the filename stem, so
``docs/specs/alpha.md`` and ``vendor/specs/alpha.md`` would otherwise map to the same run — the
relative-path hash separates them. Second, the hash is taken over the *repository-relative* path,
so moving the repository or conducting from a linked worktree does not change the key, and every
branch, worktree, gate directory, and run directory keeps its name.

Generation 1 omits the suffix so existing single-generation names stay short; generation 2 and
later append ``-g2``, ``-g3`` and so on, and that suffix is part of every derived name.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re

from conductor.paths import spec_slug

HASH_LEN = 8

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
# Generation 1 carries no suffix, so only 2 and up are encoded — but the range is on the WHOLE
# number, not its first digit. `[2-9]\d*` would silently parse -g10, -g11, -g17 as generation 1.
_GEN_RE = re.compile(r"-g([1-9]\d*)\Z")


def _escapes_repo(relative: str) -> bool:
    """Whether a computed relative path leaves the repository root."""
    return relative == ".." or relative.startswith(".." + os.sep)


def normalize_spec_path(repo_root: str, spec_path: str) -> str:
    """The repository-relative POSIX path the key hashes.

    Accepts an absolute path or one already relative to ``repo_root``. Redundant segments are
    collapsed. A path that escapes the repository is refused: its key would not be reproducible
    from a different checkout."""
    root = os.path.realpath(repo_root)
    absolute = (
        os.path.normpath(spec_path)
        if os.path.isabs(spec_path)
        else os.path.normpath(os.path.join(root, spec_path))
    )
    relative = os.path.relpath(absolute, root)
    if _escapes_repo(relative):
        # The caller may have reached the repository through a symlinked alias (a symlinked
        # home, /tmp on macOS, a WSL mount): ``root`` is realpath'd but an absolute
        # ``spec_path`` is not, so relpath would compare a resolved path against an unresolved
        # one and report a file inside the repo as outside. Resolve once and retry before
        # refusing. A spec that is ITSELF a symlink still keeps its in-repo path — this runs
        # only on the refusal path, so it rescues an alias and never relocates a spec that
        # already resolved inside the repository.
        relative = os.path.relpath(os.path.realpath(absolute), root)
    if _escapes_repo(relative):
        raise ValueError(
            f"spec path is outside the repository: {spec_path!r} is not under {root!r}"
        )
    return pathlib.PurePath(relative).as_posix()


def path_hash(normalized_spec_path: str) -> str:
    """The short hash component: the first ``HASH_LEN`` hex characters of the path's sha256."""
    return hashlib.sha256(normalized_spec_path.encode("utf-8")).hexdigest()[:HASH_LEN]


def run_key(normalized_spec_path: str, generation: int = 1) -> str:
    """The run key for a normalized spec path at ``generation`` (1-based)."""
    if generation < 1:
        raise ValueError(f"generation must be >= 1, got {generation}")
    base = f"{spec_slug(normalized_spec_path)}-{path_hash(normalized_spec_path)}"
    return base if generation == 1 else f"{base}-g{generation}"


def parse_generation(key: str) -> int:
    """The generation encoded in ``key``; an absent suffix means generation 1."""
    match = _GEN_RE.search(key)
    return int(match.group(1)) if match else 1


def is_safe_run_key(key: str) -> bool:
    """Whether ``key`` is safe as a single filesystem component and git ref segment: starts
    alphanumeric, contains only ``[a-z0-9._-]``, no separators, no ``..``, not ``*.lock``."""
    return bool(_KEY_RE.match(key)) and ".." not in key and not key.endswith(".lock")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_runkey.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/runkey.py tests/conductor/core/test_runkey.py
git commit -m "conductor/core/runkey.py:1-95 — deterministic run keys with generations

- <spec-slug>-<8-hex path hash>[-g<N>]; equal filenames in different dirs no longer collide
- normalize_spec_path is repository-relative, so the key survives moving the checkout"
```

---

### Task 4: State schema, vocabularies, and status transitions

**Files:**
- Create: `conductor/core/schema.py`
- Test: `tests/conductor/core/test_schema.py`

**Interfaces:**
- Consumes: `conductor.core.runkey.is_safe_run_key`, `conductor.core.runkey.parse_generation`.
- Produces:
  - `SCHEMA_VERSION: int` (2)
  - `RUN_STATUSES`, `ACTIVE_STATUSES`, `TERMINAL_STATUSES`, `RESUMABLE_STATUSES`, `REVIEW_POLICIES`, `IDENTITY_SCHEMES` — all `tuple[str, ...]`
  - `class SchemaError(ValueError)`
  - `validate_run(doc: dict) -> dict`, `validate_project(doc: dict) -> dict`
  - `new_run_doc(*, run_key, generation, spec_path, workstation_id, integration_branch, gate_dir, spec_digest, now, identity_scheme="path-hash-v2") -> dict`
  - `new_project_doc(*, workstation_id, repo_identity) -> dict`
  - `is_active(status: str) -> bool`
  - `assert_transition(old: str, new: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_schema.py`:

```python
"""run.json / project.json shapes and the exact state vocabularies (design §"Project and run
identity").

The vocabularies are closed sets on purpose: a typo'd status is how an unattended run silently
stops counting as active. Transitions are checked too — a run becomes ``failed`` only when a
recorded invariant violation leaves no safe retry, and recoverable stops use ``blocked``."""

from __future__ import annotations

import copy

import pytest

from conductor.core import runkey, schema

NOW = "2026-08-10T12:00:00+00:00"


def _run(**overrides):
    rel = "docs/specs/alpha.md"
    key = runkey.run_key(rel)
    doc = schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=rel,
        workstation_id="0123456789abcdef0123456789abcdef",
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )
    doc.update(overrides)
    return doc


def test_vocabularies_are_exactly_the_design_sets():
    assert schema.RUN_STATUSES == (
        "active",
        "checkpointed",
        "blocked",
        "awaiting-team-merge",
        "terminal",
        "failed",
    )
    assert schema.ACTIVE_STATUSES == ("active", "checkpointed", "blocked")
    assert schema.TERMINAL_STATUSES == ("terminal", "failed")
    assert schema.REVIEW_POLICIES == (
        "opposite-required",
        "same-host-fallback-allowed",
        "blocked-pending-opposite-host",
    )
    assert schema.IDENTITY_SCHEMES == ("path-hash-v2", "legacy-slug-v1")


def test_is_active_classifies_the_three_active_statuses_only():
    assert [s for s in schema.RUN_STATUSES if schema.is_active(s)] == [
        "active",
        "checkpointed",
        "blocked",
    ]


def test_a_new_run_doc_validates_and_defaults_to_opposite_required_review():
    doc = _run()
    assert schema.validate_run(doc) == doc
    assert doc["status"] == "active"
    assert doc["review_policy"] == "opposite-required"
    assert doc["revision"] == 0
    assert doc["identity_scheme"] == "path-hash-v2"


def test_run_doc_carries_every_field_later_plans_populate():
    doc = _run()
    for field in (
        "current_phase",
        "phase_ids",
        "plan_digest",
        "ledger_ref",
        "goal_digest",
        "assertion_digest",
        "worker_host",
        "reviewer_host",
        "phase_reviews",
        "last_review_head_sha",
        "dispatches",
        "github",
        "heartbeat",
        "lease",
        "integration_worktree",
        "phase_branch",
        "phase_worktree",
        "last_reconciled_at",
        "last_checkpoint_at",
        "completed_at",
        "failed_at",
        "path_history",
    ):
        assert field in doc, field


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "running"),
        ("review_policy", "whatever"),
        ("identity_scheme", "v3"),
        ("run_key", "../escape"),
        ("generation", 0),
        ("gate_dir", "/etc/passwd"),
        ("gate_dir", "assertions/../outside"),
        ("integration_branch", ""),
        ("revision", -1),
    ],
)
def test_invalid_run_fields_are_refused(field, value):
    doc = _run(**{field: value})
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


def test_generation_must_agree_with_the_suffix_in_the_run_key():
    doc = _run(generation=2)
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(doc)
    assert "generation" in str(excinfo.value)


def test_a_missing_required_field_is_refused():
    doc = _run()
    del doc["spec_path"]
    with pytest.raises(schema.SchemaError):
        schema.validate_run(doc)


def test_recoverable_and_unrecoverable_transitions():
    schema.assert_transition("active", "checkpointed")
    schema.assert_transition("active", "blocked")
    schema.assert_transition("checkpointed", "active")
    schema.assert_transition("blocked", "active")
    schema.assert_transition("active", "awaiting-team-merge")
    schema.assert_transition("awaiting-team-merge", "blocked")
    schema.assert_transition("awaiting-team-merge", "terminal")
    schema.assert_transition("active", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("terminal", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("failed", "active")
    with pytest.raises(schema.SchemaError):
        schema.assert_transition("active", "terminal")


def test_project_doc_validates_and_allows_one_nonterminal_generation():
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": "git@example.invalid:x/y.git"},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [
            {"run_key": key, "generation": 1, "status": "terminal"},
            {"run_key": f"{key}-g2", "generation": 2, "status": "active"},
        ],
        "current": f"{key}-g2",
        "path_history": [],
    }
    assert schema.validate_project(doc) == doc


def test_two_nonterminal_generations_for_one_spec_are_refused():
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [
            {"run_key": key, "generation": 1, "status": "active"},
            {"run_key": f"{key}-g2", "generation": 2, "status": "blocked"},
        ],
        "current": f"{key}-g2",
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_project(doc)
    assert "nonterminal" in str(excinfo.value)


def test_current_must_name_the_nonterminal_generation():
    key = runkey.run_key("docs/specs/alpha.md")
    doc = schema.new_project_doc(
        workstation_id="0123456789abcdef0123456789abcdef",
        repo_identity={"root_commit": "abc", "origin_url": None},
    )
    doc["specs"]["docs/specs/alpha.md"] = {
        "generations": [{"run_key": key, "generation": 1, "status": "active"}],
        "current": None,
        "path_history": [],
    }
    with pytest.raises(schema.SchemaError):
        schema.validate_project(doc)


def test_validate_does_not_mutate_its_input():
    doc = _run()
    before = copy.deepcopy(doc)
    schema.validate_run(doc)
    assert doc == before
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.schema'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/schema.py`:

```python
"""``project.json`` and ``run.json`` shapes, the closed state vocabularies, and the legal status
transitions (design §"Project and run identity").

The vocabularies are closed sets because a typo'd status is exactly how an unattended run stops
counting as active without anyone noticing: ``active``, ``checkpointed`` and ``blocked`` count as
active for run-key disambiguation and manual autodev; ``awaiting-team-merge``, ``terminal`` and
``failed`` do not. A run becomes ``failed`` only when a recorded invariant violation leaves no
safe retry — every recoverable stop uses ``blocked``, which ``resume`` can return to ``active``.

``new_run_doc`` deliberately writes every field later plans populate, as ``None`` or an empty
container. Growing the document later would mean each plan reasoning about absent keys; a fixed
shape means ``validate_run`` is the only place that knows the schema.
"""

from __future__ import annotations

import copy
import re

from conductor.core.runkey import is_safe_run_key, parse_generation

SCHEMA_VERSION = 2

RUN_STATUSES = (
    "active",
    "checkpointed",
    "blocked",
    "awaiting-team-merge",
    "terminal",
    "failed",
)
ACTIVE_STATUSES = ("active", "checkpointed", "blocked")
TERMINAL_STATUSES = ("terminal", "failed")
RESUMABLE_STATUSES = ("checkpointed", "blocked", "awaiting-team-merge")
REVIEW_POLICIES = (
    "opposite-required",
    "same-host-fallback-allowed",
    "blocked-pending-opposite-host",
)
IDENTITY_SCHEMES = ("path-hash-v2", "legacy-slug-v1")

# active -> terminal is absent on purpose: only `conductor finish` completes a run, and it runs
# from awaiting-team-merge after proving the final pull request merged.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"checkpointed", "blocked", "awaiting-team-merge", "failed"}),
    "checkpointed": frozenset({"active", "blocked", "failed"}),
    "blocked": frozenset({"active", "failed"}),
    "awaiting-team-merge": frozenset({"active", "blocked", "terminal", "failed"}),
    "terminal": frozenset(),
    "failed": frozenset(),
}

_RUN_REQUIRED = (
    "schema_version",
    "revision",
    "run_key",
    "generation",
    "identity_scheme",
    "spec_path",
    "spec_digest",
    "path_history",
    "status",
    "workstation_id",
    "integration_branch",
    "integration_worktree",
    "gate_dir",
    "phase_branch",
    "phase_worktree",
    "current_phase",
    "phase_ids",
    "plan_digest",
    "ledger_ref",
    "goal_digest",
    "assertion_digest",
    "worker_host",
    "reviewer_host",
    "review_policy",
    "phase_reviews",
    "last_review_head_sha",
    "last_worker_host",
    "last_reviewer_host",
    "dispatches",
    "github",
    "heartbeat",
    "lease",
    "last_reconciled_at",
    "last_checkpoint_at",
    "created_at",
    "updated_at",
    "completed_at",
    "failed_at",
)

_GATE_DIR_RE = re.compile(r"assertions/([a-z0-9][a-z0-9._-]*)\Z")


class SchemaError(ValueError):
    """A state document violates the schema or a closed vocabulary."""


def is_active(status: str) -> bool:
    """Whether ``status`` counts as active for run-key disambiguation and manual autodev."""
    return status in ACTIVE_STATUSES


def assert_transition(old: str, new: str) -> None:
    """Raise unless ``old -> new`` is a legal status transition. Same-to-same is always legal so
    a reconcile may rewrite the current status without a special case."""
    if old not in RUN_STATUSES:
        raise SchemaError(f"unknown current status {old!r}; expected one of {RUN_STATUSES}")
    if new not in RUN_STATUSES:
        raise SchemaError(f"unknown target status {new!r}; expected one of {RUN_STATUSES}")
    if old == new:
        return
    if new not in _TRANSITIONS[old]:
        raise SchemaError(
            f"illegal status transition {old!r} -> {new!r}; legal targets from {old!r} are "
            f"{sorted(_TRANSITIONS[old]) or 'none (final state)'}"
        )


def new_project_doc(*, workstation_id: str, repo_identity: dict) -> dict:
    """A fresh registry with no spec mappings."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "workstation_id": workstation_id,
        "workstation_history": [],
        "repo_identity": dict(repo_identity),
        "specs": {},
    }


def new_run_doc(
    *,
    run_key: str,
    generation: int,
    spec_path: str,
    workstation_id: str,
    integration_branch: str,
    gate_dir: str,
    spec_digest: str,
    now: str,
    identity_scheme: str = "path-hash-v2",
) -> dict:
    """A fresh run record. Fields later plans own are present and empty so the shape never
    changes underneath them."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_key": run_key,
        "generation": generation,
        "identity_scheme": identity_scheme,
        "spec_path": spec_path,
        "spec_digest": spec_digest,
        "path_history": [],
        "status": "active",
        "workstation_id": workstation_id,
        "integration_branch": integration_branch,
        "integration_worktree": None,
        "gate_dir": gate_dir,
        "phase_branch": None,
        "phase_worktree": None,
        "current_phase": None,
        "phase_ids": [],
        "plan_digest": None,
        "ledger_ref": None,
        "goal_digest": None,
        "assertion_digest": None,
        "worker_host": None,
        "reviewer_host": None,
        "review_policy": "opposite-required",
        "phase_reviews": [],
        "last_review_head_sha": None,
        "last_worker_host": None,
        "last_reviewer_host": None,
        "dispatches": [],
        "github": {"issue": None, "phase_prs": {}, "final_pr": None},
        "heartbeat": {"schedule_id": None, "process_identity": None},
        "lease": {"owner": None, "expires_at": None, "renewed_at": None},
        "last_reconciled_at": None,
        "last_checkpoint_at": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "failed_at": None,
    }


def _require_int(doc: dict, field: str, minimum: int) -> int:
    value = doc.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SchemaError(f"{field} must be an integer >= {minimum}, got {value!r}")
    return value


def validate_run(doc: dict) -> dict:
    """Return ``doc`` unchanged if it is a legal run record, else raise ``SchemaError``."""
    if not isinstance(doc, dict):
        raise SchemaError(f"run document must be a mapping, got {type(doc).__name__}")
    missing = [field for field in _RUN_REQUIRED if field not in doc]
    if missing:
        raise SchemaError(f"run document is missing required field(s): {', '.join(missing)}")
    _require_int(doc, "schema_version", 1)
    _require_int(doc, "revision", 0)
    generation = _require_int(doc, "generation", 1)
    key = doc["run_key"]
    if not isinstance(key, str) or not is_safe_run_key(key):
        raise SchemaError(f"run_key {key!r} is not a safe single path/ref segment")
    if parse_generation(key) != generation:
        raise SchemaError(
            f"generation {generation} disagrees with run_key {key!r} "
            f"(the key encodes generation {parse_generation(key)})"
        )
    if doc["identity_scheme"] not in IDENTITY_SCHEMES:
        raise SchemaError(
            f"identity_scheme {doc['identity_scheme']!r}; expected one of {IDENTITY_SCHEMES}"
        )
    if doc["status"] not in RUN_STATUSES:
        raise SchemaError(f"status {doc['status']!r}; expected one of {RUN_STATUSES}")
    if doc["review_policy"] not in REVIEW_POLICIES:
        raise SchemaError(
            f"review_policy {doc['review_policy']!r}; expected one of {REVIEW_POLICIES}"
        )
    for field in ("spec_path", "integration_branch", "workstation_id"):
        value = doc[field]
        if not isinstance(value, str) or not value:
            raise SchemaError(f"{field} must be a non-empty string, got {value!r}")
    gate_dir = doc["gate_dir"]
    if not isinstance(gate_dir, str) or not _GATE_DIR_RE.match(gate_dir):
        raise SchemaError(
            f"gate_dir {gate_dir!r} must be 'assertions/<single-safe-segment>' relative to the "
            "repository root"
        )
    if doc["identity_scheme"] == "path-hash-v2":
        # The run key is the SINGLE source of both derived names, so a record whose identity and
        # derived paths have diverged is a corrupt record, not a valid variant. Scheme-conditional
        # on purpose: a `legacy-slug-v1` run migrated by Plan 03 deliberately retains the gate
        # directory and branch names it had BEFORE migration, which need not follow the derived
        # form — an unconditional check would make every migrated run unvalidatable.
        want_gate = f"assertions/{key}"
        if gate_dir != want_gate:
            raise SchemaError(
                f"gate_dir {gate_dir!r} disagrees with run_key {key!r}; expected {want_gate!r}"
            )
        want_branch = f"conductor/run-{key}"
        if doc["integration_branch"] != want_branch:
            raise SchemaError(
                f"integration_branch {doc['integration_branch']!r} disagrees with run_key "
                f"{key!r}; expected {want_branch!r}"
            )
    for field in ("path_history", "phase_ids", "phase_reviews", "dispatches"):
        if not isinstance(doc[field], list):
            raise SchemaError(f"{field} must be a list, got {type(doc[field]).__name__}")
    for field in ("github", "heartbeat", "lease"):
        if not isinstance(doc[field], dict):
            raise SchemaError(f"{field} must be a mapping, got {type(doc[field]).__name__}")
    return doc


def validate_project(doc: dict) -> dict:
    """Return ``doc`` unchanged if it is a legal registry, else raise ``SchemaError``.

    Enforces the design's central mapping rule: each spec path holds an ordered generation list
    with **at most one nonterminal run**, and ``current`` names exactly that run (or is ``None``
    when every generation is terminal)."""
    if not isinstance(doc, dict):
        raise SchemaError(f"project document must be a mapping, got {type(doc).__name__}")
    for field in ("schema_version", "revision", "workstation_id", "repo_identity", "specs"):
        if field not in doc:
            raise SchemaError(f"project document is missing required field {field!r}")
    _require_int(doc, "schema_version", 1)
    _require_int(doc, "revision", 0)
    if not isinstance(doc["workstation_id"], str) or not doc["workstation_id"]:
        raise SchemaError(f"workstation_id must be a non-empty string, got {doc['workstation_id']!r}")
    if not isinstance(doc["repo_identity"], dict):
        raise SchemaError("repo_identity must be a mapping")
    if not isinstance(doc.get("workstation_history"), list):
        raise SchemaError("workstation_history must be a list")
    specs = doc["specs"]
    if not isinstance(specs, dict):
        raise SchemaError("specs must be a mapping of normalized spec path -> mapping")
    seen: dict[str, str] = {}
    for spec_path, mapping in specs.items():
        if not isinstance(mapping, dict):
            raise SchemaError(f"specs[{spec_path!r}] must be a mapping")
        generations = mapping.get("generations")
        if not isinstance(generations, list) or not generations:
            raise SchemaError(f"specs[{spec_path!r}].generations must be a non-empty list")
        if not isinstance(mapping.get("path_history"), list):
            raise SchemaError(f"specs[{spec_path!r}].path_history must be a list")
        numbers = []
        nonterminal = []
        for entry in generations:
            if not isinstance(entry, dict):
                raise SchemaError(f"specs[{spec_path!r}].generations entries must be mappings")
            key = entry.get("run_key")
            if not isinstance(key, str) or not is_safe_run_key(key):
                raise SchemaError(f"specs[{spec_path!r}] has unsafe run_key {key!r}")
            if key in seen:
                raise SchemaError(
                    f"run_key {key!r} is mapped by both {seen[key]!r} and {spec_path!r}"
                )
            seen[key] = spec_path
            generation = entry.get("generation")
            if not isinstance(generation, int) or generation < 1:
                raise SchemaError(f"specs[{spec_path!r}] has invalid generation {generation!r}")
            if parse_generation(key) != generation:
                raise SchemaError(
                    f"specs[{spec_path!r}] run_key {key!r} disagrees with generation {generation}"
                )
            numbers.append(generation)
            status = entry.get("status")
            if status not in RUN_STATUSES:
                raise SchemaError(f"specs[{spec_path!r}] has status {status!r}")
            if status not in TERMINAL_STATUSES:
                nonterminal.append(key)
        if len(numbers) != len(set(numbers)):
            duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
            raise SchemaError(
                f"specs[{spec_path!r}] has duplicate generation number(s) "
                f"{duplicates}; each generation appears at most once"
            )
        if numbers != sorted(numbers):
            raise SchemaError(f"specs[{spec_path!r}].generations must be in ascending order")
        if len(nonterminal) > 1:
            raise SchemaError(
                f"specs[{spec_path!r}] has {len(nonterminal)} nonterminal generations "
                f"({', '.join(nonterminal)}); at most one is allowed"
            )
        current = mapping.get("current")
        expected = nonterminal[0] if nonterminal else None
        if current != expected:
            raise SchemaError(
                f"specs[{spec_path!r}].current is {current!r} but the nonterminal generation is "
                f"{expected!r}"
            )
    return doc


def clone(doc: dict) -> dict:
    """A deep copy, so a caller's mutate callback cannot alter the on-disk snapshot in place."""
    return copy.deepcopy(doc)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_schema.py -q`
Expected: PASS (all parametrized cases green)

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/schema.py tests/conductor/core/test_schema.py
git commit -m "conductor/core/schema.py:1-330 — run/project schemas and closed vocabularies

- six statuses, three review policies, two identity schemes; active = active|checkpointed|blocked
- validate_project enforces at most one nonterminal generation per spec and current == that run
- assert_transition refuses active->terminal; only finish completes from awaiting-team-merge"
```

---

### Task 5: Workstation identity

**Files:**
- Create: `conductor/core/workstation.py`
- Test: `tests/conductor/core/test_workstation.py`

**Interfaces:**
- Consumes: `conductor.core.atomic.read_json`.
- Produces:
  - `config_home() -> str`
  - `installation_file() -> str`
  - `workstation_id() -> str`

**Why this is its own module:** design line 111 requires a *random* Conductor installation ID stored in host-neutral user configuration and shared by both adapters, explicitly **not** derived from personal or hardware data. Plan 02's `conductor project rebind` compares it, so it must be created exactly once and never regenerated.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_workstation.py`:

```python
"""The workstation identity (design §"Project and run identity"): a random Conductor installation
ID in host-neutral user configuration, shared by the Claude and Codex adapters.

It must not be derived from personal or hardware data, and it must never be regenerated — Plan 02's
rebind compares a project's recorded workstation against this value to refuse cross-machine
takeover."""

from __future__ import annotations

import getpass
import os
import socket
import stat

from conductor.core import workstation


def test_config_home_prefers_the_conductor_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    assert workstation.config_home() == str(tmp_path / "cfg")


def test_config_home_falls_back_to_xdg_then_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert workstation.config_home() == str(tmp_path / "xdg" / "conductor")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert workstation.config_home() == str(tmp_path / "home" / ".config" / "conductor")


def test_the_id_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    first = workstation.workstation_id()
    assert first and len(first) == 32
    assert workstation.workstation_id() == first


def test_the_id_is_random_and_not_derived_from_user_or_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "a"))
    first = workstation.workstation_id()
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "b"))
    second = workstation.workstation_id()
    assert first != second
    for leak in (getpass.getuser(), socket.gethostname(), os.path.expanduser("~")):
        assert leak.lower() not in first.lower()


def test_the_installation_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    workstation.workstation_id()
    mode = stat.S_IMODE(os.stat(workstation.installation_file()).st_mode)
    assert mode == 0o600


def test_a_concurrent_creator_wins_and_both_callers_agree(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    os.makedirs(str(tmp_path / "cfg"), exist_ok=True)
    with open(workstation.installation_file(), "w", encoding="utf-8") as handle:
        handle.write('{"schema_version": 1, "workstation_id": "deadbeef" }')
    assert workstation.workstation_id() == "deadbeef"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_workstation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.workstation'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/workstation.py`:

```python
"""The workstation identity shared by both host adapters.

Design §"Project and run identity": ``project.json`` records the workstation that owns a
project's local state, and a checkout whose registry names a *different* workstation refuses
automatic takeover. That identity is a random Conductor installation ID in host-neutral user
configuration — deliberately not a hostname, username, MAC address, or machine-id, so the file
carries nothing personal and copying a home directory does not silently authorize takeover.

Created exactly once. Creation uses ``O_EXCL`` so two adapters racing on first use converge on
one value instead of overwriting each other.
"""

from __future__ import annotations

import json
import os
import secrets

from conductor.core import atomic

SCHEMA_VERSION = 1


def config_home() -> str:
    """The host-neutral Conductor config directory: ``$CONDUCTOR_CONFIG_HOME``, else
    ``$XDG_CONFIG_HOME/conductor``, else ``~/.config/conductor``."""
    override = os.environ.get("CONDUCTOR_CONFIG_HOME")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "conductor")


def installation_file() -> str:
    """Where the installation ID lives."""
    return os.path.join(config_home(), "installation.json")


def workstation_id() -> str:
    """Read the installation ID, creating it on first use. Mode 0600, no personal data."""
    path = installation_file()
    existing = atomic.read_json(path)
    if isinstance(existing, dict):
        value = existing.get("workstation_id")
        if isinstance(value, str) and value:
            return value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    candidate = secrets.token_hex(16)
    payload = json.dumps(
        {"schema_version": SCHEMA_VERSION, "workstation_id": candidate},
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another adapter created it between our read and our create: theirs wins.
        winner = atomic.read_json(path)
        if isinstance(winner, dict) and isinstance(winner.get("workstation_id"), str):
            return winner["workstation_id"]
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return candidate
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_workstation.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/workstation.py tests/conductor/core/test_workstation.py
git commit -m "conductor/core/workstation.py:1-70 — random host-neutral installation ID

- \$CONDUCTOR_CONFIG_HOME > \$XDG_CONFIG_HOME/conductor > ~/.config/conductor, file mode 0600
- O_EXCL create so racing adapters converge on one value; no hostname/user/hardware input"
```

---

### Task 6: Journalled project transactions

**Files:**
- Create: `conductor/core/transaction.py`
- Test: `tests/conductor/core/test_transaction.py`

**Interfaces:**
- Consumes: `conductor.core.atomic.{read_json,write_json_atomic}`.
- Produces:
  - `txn_dir(state_root: str) -> str`, `journal_path(state_root: str, txn_id: str) -> str`
  - `prepare(state_root: str, txn_id: str, entries: list[dict]) -> str`
  - `commit(state_root: str, txn_id: str) -> None`
  - `apply(state_root: str, txn_id: str) -> None`
  - `pending(state_root: str) -> list[str]`
  - `recover(state_root: str) -> list[str]`
- Entry shape: `{"path": <absolute path>, "before": <dict|None>, "after": <dict|None>}`. `None` on either side means "the file does not exist in that state".

**Why this exists:** `write_json_atomic` makes each file durable, but a crash *between* the `project.json` write and the `run.json` write leaves a registry that maps a spec to a run key the run record disagrees with. Design line 450 requires the journal be written and fsynced first, and every project entry point complete or reverse an unfinished transaction before reading mappings.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_transaction.py`:

```python
"""Journalled cross-file writes (design §"Failure handling").

An operation that updates project.json and one or more run.json files writes and fsyncs a project
transaction first; every project entry point completes or reverses an unfinished transaction
before reading mappings, so a crash cannot leave a silently split identity.

Crash points are simulated by stopping after prepare / after commit / mid-apply and then calling
recover, which is what the next entry point would do."""

from __future__ import annotations

import json
import os

import pytest

from conductor.core import atomic, transaction


def _entry(path, before, after):
    return {"path": str(path), "before": before, "after": after}


def test_prepare_writes_a_journal_without_touching_the_targets(tmp_path):
    state_root = tmp_path / ".conductor"
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"revision": 1})
    transaction.prepare(
        str(state_root), "txn-1", [_entry(target, {"revision": 1}, {"revision": 2})]
    )
    assert atomic.read_json(str(target)) == {"revision": 1}
    journal = json.loads(open(transaction.journal_path(str(state_root), "txn-1")).read())
    assert journal["state"] == "prepared"
    assert transaction.pending(str(state_root)) == ["txn-1"]


def test_commit_then_apply_writes_the_after_images_and_clears_the_journal(tmp_path):
    state_root = tmp_path / ".conductor"
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"revision": 1})
    transaction.prepare(
        str(state_root), "txn-1", [_entry(target, {"revision": 1}, {"revision": 2})]
    )
    transaction.commit(str(state_root), "txn-1")
    transaction.apply(str(state_root), "txn-1")
    assert atomic.read_json(str(target)) == {"revision": 2}
    assert transaction.pending(str(state_root)) == []


def test_crash_after_prepare_reverses_to_the_before_images(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(run, {"spec_path": "docs/specs/old.md"}, {"spec_path": "docs/specs/new.md"}),
        ],
    )
    # crash here — no commit
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 1}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/old.md"}


def test_crash_after_commit_rolls_forward(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(run, {"spec_path": "docs/specs/old.md"}, {"spec_path": "docs/specs/new.md"}),
        ],
    )
    transaction.commit(str(state_root), "txn-1")
    # crash here — apply never ran
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 2}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/new.md"}


def test_crash_midway_through_apply_completes_the_roll_forward(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(run, {"spec_path": "docs/specs/old.md"}, {"spec_path": "docs/specs/new.md"}),
        ],
    )
    transaction.commit(str(state_root), "txn-1")
    atomic.write_json_atomic(str(project), {"revision": 2})  # first target landed, then crash
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 2}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/new.md"}


def test_recover_handles_creation_and_deletion_entries(tmp_path):
    state_root = tmp_path / ".conductor"
    created = tmp_path / "created.json"
    transaction.prepare(str(state_root), "txn-1", [_entry(created, None, {"a": 1})])
    transaction.recover(str(state_root))  # prepared -> reverse -> file must not exist
    assert not created.exists()
    transaction.prepare(str(state_root), "txn-2", [_entry(created, None, {"a": 1})])
    transaction.commit(str(state_root), "txn-2")
    transaction.recover(str(state_root))
    assert atomic.read_json(str(created)) == {"a": 1}
    transaction.prepare(str(state_root), "txn-3", [_entry(created, {"a": 1}, None)])
    transaction.commit(str(state_root), "txn-3")
    transaction.recover(str(state_root))
    assert not created.exists()


def test_recover_is_idempotent_and_processes_journals_in_sorted_order(tmp_path):
    state_root = tmp_path / ".conductor"
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    transaction.prepare(str(state_root), "txn-2", [_entry(second, None, {"n": 2})])
    transaction.commit(str(state_root), "txn-2")
    transaction.prepare(str(state_root), "txn-1", [_entry(first, None, {"n": 1})])
    transaction.commit(str(state_root), "txn-1")
    assert transaction.recover(str(state_root)) == ["txn-1", "txn-2"]
    assert transaction.recover(str(state_root)) == []
    assert atomic.read_json(str(first)) == {"n": 1}
    assert atomic.read_json(str(second)) == {"n": 2}


def test_recover_on_a_clean_state_root_does_nothing(tmp_path):
    assert transaction.recover(str(tmp_path / ".conductor")) == []


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"path": "relative/project.json", "before": None, "after": {"a": 1}}],
        [{"path": "/abs/project.json", "before": None, "after": None}],
        [{"before": None, "after": {"a": 1}}],
    ],
)
def test_prepare_refuses_malformed_entries(tmp_path, entries):
    with pytest.raises(ValueError):
        transaction.prepare(str(tmp_path / ".conductor"), "txn-1", entries)


def test_prepare_refuses_an_unsafe_transaction_id(tmp_path):
    with pytest.raises(ValueError):
        transaction.prepare(
            str(tmp_path / ".conductor"),
            "../escape",
            [{"path": str(tmp_path / "x.json"), "before": None, "after": {"a": 1}}],
        )


def test_a_journal_that_cannot_be_parsed_fails_closed(tmp_path):
    state_root = tmp_path / ".conductor"
    os.makedirs(transaction.txn_dir(str(state_root)), exist_ok=True)
    with open(transaction.journal_path(str(state_root), "txn-1"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with pytest.raises(ValueError):
        transaction.recover(str(state_root))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_transaction.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.transaction'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/transaction.py`:

```python
"""Journalled cross-file state writes.

``atomic.write_json_atomic`` makes each file durable on its own, but an operation that touches
``project.json`` and one or more ``run.json`` files can still crash *between* them and leave a
registry mapping a spec to a run key the run record disagrees with. Design §"Failure handling"
therefore requires the journal be written and fsynced first, and every project entry point
complete or reverse an unfinished transaction before reading mappings.

Lifecycle:

    prepare(...)   journal written with state="prepared" and every before/after image
    commit(...)    journal flipped to state="committed" — the point of no return
    apply(...)     after images written, journal removed

``recover`` reverses a ``prepared`` journal (restore the before images) and rolls a ``committed``
one forward (write the after images). Both directions are idempotent, so recovering twice, or
recovering a transaction that had already half-applied, converges on the same state.

This module takes no locks: it is used from inside operations that already hold ``project.lock``
and the relevant ``state.lock``, and taking them again would be a re-entrant acquisition.
"""

from __future__ import annotations

import os
import re

from conductor.core import atomic

SCHEMA_VERSION = 1

_TXN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def txn_dir(state_root: str) -> str:
    """Where journals live: ``<state_root>/transactions``."""
    return os.path.join(state_root, "transactions")


def journal_path(state_root: str, txn_id: str) -> str:
    """The journal file for one transaction."""
    return os.path.join(txn_dir(state_root), f"{txn_id}.json")


def _check_id(txn_id: str) -> None:
    if not isinstance(txn_id, str) or not _TXN_ID_RE.match(txn_id) or ".." in txn_id:
        raise ValueError(f"unsafe transaction id {txn_id!r}")


def _check_entries(entries: list[dict]) -> None:
    if not isinstance(entries, list) or not entries:
        raise ValueError("a transaction needs at least one entry")
    for entry in entries:
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(f"transaction entry must be a mapping with 'path': {entry!r}")
        path = entry["path"]
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError(f"transaction entry path must be absolute, got {path!r}")
        before, after = entry.get("before"), entry.get("after")
        for label, value in (("before", before), ("after", after)):
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"transaction entry {label!r} must be a mapping or None")
        if before is None and after is None:
            raise ValueError(f"transaction entry for {path!r} is a no-op (before and after None)")


def prepare(state_root: str, txn_id: str, entries: list[dict]) -> str:
    """Record the intended write and fsync it. Targets are untouched. Returns the journal path."""
    _check_id(txn_id)
    _check_entries(entries)
    path = journal_path(state_root, txn_id)
    atomic.write_json_atomic(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "txn_id": txn_id,
            "state": "prepared",
            "entries": [
                {"path": e["path"], "before": e.get("before"), "after": e.get("after")}
                for e in entries
            ],
        },
    )
    return path


def _load_journal(state_root: str, txn_id: str) -> dict:
    doc = atomic.read_json(journal_path(state_root, txn_id))
    if doc is None:
        raise ValueError(f"no transaction journal for {txn_id!r} under {txn_dir(state_root)}")
    if doc.get("state") not in ("prepared", "committed"):
        raise ValueError(f"transaction {txn_id!r} has unknown state {doc.get('state')!r}")
    return doc


def commit(state_root: str, txn_id: str) -> None:
    """Flip the journal to ``committed``: from here recovery rolls forward, never back."""
    doc = _load_journal(state_root, txn_id)
    doc["state"] = "committed"
    atomic.write_json_atomic(journal_path(state_root, txn_id), doc)


def _write_image(path: str, image: dict | None) -> None:
    if image is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    atomic.write_json_atomic(path, image)


def apply(state_root: str, txn_id: str) -> None:
    """Write every after image and remove the journal. Only valid once committed."""
    doc = _load_journal(state_root, txn_id)
    if doc["state"] != "committed":
        raise ValueError(f"transaction {txn_id!r} is {doc['state']!r}; commit before apply")
    for entry in doc["entries"]:
        _write_image(entry["path"], entry.get("after"))
    os.unlink(journal_path(state_root, txn_id))


def pending(state_root: str) -> list[str]:
    """Transaction ids with an unfinished journal, sorted."""
    directory = txn_dir(state_root)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(n[: -len(".json")] for n in names if n.endswith(".json"))


def recover(state_root: str) -> list[str]:
    """Complete or reverse every unfinished transaction; return the ids handled, sorted.

    Called by every project entry point *before* reading mappings. A journal that cannot be
    parsed raises rather than being skipped — an unreadable journal means the split-identity
    question is unanswerable, which is a fail-closed condition, not a clean state."""
    handled: list[str] = []
    for txn_id in pending(state_root):
        doc = _load_journal(state_root, txn_id)
        forward = doc["state"] == "committed"
        for entry in doc["entries"]:
            _write_image(entry["path"], entry.get("after") if forward else entry.get("before"))
        os.unlink(journal_path(state_root, txn_id))
        handled.append(txn_id)
    return handled
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_transaction.py -q`
Expected: PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/transaction.py tests/conductor/core/test_transaction.py
git commit -m "conductor/core/transaction.py:1-175 — journalled cross-file state writes

- prepare/commit/apply with before+after images; recover reverses prepared, rolls committed forward
- recovery is idempotent and fails closed on an unparseable journal"
```

---

### Task 7: The project registry with revision compare-and-swap

**Files:**
- Create: `conductor/core/registry.py`
- Test: `tests/conductor/core/test_registry.py`

**Interfaces:**
- Consumes: `atomic`, `locks`, `schema`, `transaction`, `runkey.parse_generation`.
- Produces:
  - `class RegistryMissing(RuntimeError)`, `class RevisionConflict(RuntimeError)`
  - `registry_path(state_root) -> str`, `lock_path(state_root) -> str`
  - `load(state_root) -> dict | None`
  - `init(state_root, *, workstation_id, repo_identity) -> dict`
  - `commit(state_root, doc, *, expect_revision) -> dict`
  - `update(state_root, mutate, *, attempts=5) -> dict`
  - `mapping(doc, normalized_spec_path) -> dict | None`
  - `current_run_key(doc, normalized_spec_path) -> str | None`
  - `next_generation(doc, normalized_spec_path) -> int`
  - `find_run(doc, run_key) -> tuple[str, dict] | None`
  - `run_keys(doc) -> list[str]`
  - `register(doc, *, spec, run_key, generation) -> dict`
  - `mirror_status(doc, run_key, status) -> dict`

**Authority note to keep in the docstring:** the per-generation `status` in `project.json` is a **mirror** for the `--new-run` policy and for cheap listing. `run.json` is authoritative. `resolve.active_run_keys` reads `run.json`, never this mirror.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_registry.py`:

```python
"""project.json — one registry per project (design §"Project and run identity").

Records the state schema, its own monotonic revision, stable repository identity,
normalized-spec-path-to-run-key mappings, and the workstation that owns this project-local state.
Each spec-path mapping is an ordered generation list with at most one nonterminal run designated
current.

Mutations are guarded by project.lock plus a revision compare-and-swap: a stale writer re-reads
and retries rather than replacing a newer value."""

from __future__ import annotations

import pytest

from conductor.core import registry, runkey, schema, transaction

WORKSTATION = "0123456789abcdef0123456789abcdef"
IDENTITY = {"root_commit": "abc123", "origin_url": "git@example.invalid:x/y.git"}
ALPHA = "docs/specs/alpha.md"
BETA = "docs/specs/beta.md"


@pytest.fixture
def state_root(tmp_path):
    root = str(tmp_path / ".conductor")
    registry.init(root, workstation_id=WORKSTATION, repo_identity=IDENTITY)
    return root


def test_load_returns_none_before_init(tmp_path):
    assert registry.load(str(tmp_path / ".conductor")) is None


def test_init_is_idempotent_and_does_not_bump_the_revision(tmp_path):
    root = str(tmp_path / ".conductor")
    first = registry.init(root, workstation_id=WORKSTATION, repo_identity=IDENTITY)
    second = registry.init(root, workstation_id="different", repo_identity={})
    assert first == second
    assert second["revision"] == 0
    assert second["workstation_id"] == WORKSTATION


def test_register_maps_a_spec_to_its_first_generation(state_root):
    key = runkey.run_key(ALPHA)
    doc = registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    assert doc["revision"] == 1
    assert registry.current_run_key(doc, ALPHA) == key
    assert registry.run_keys(doc) == [key]
    assert registry.find_run(doc, key) == (ALPHA, doc["specs"][ALPHA]["generations"][0])


def test_next_generation_is_one_for_an_unmapped_spec(state_root):
    assert registry.next_generation(registry.load(state_root), ALPHA) == 1


def test_next_generation_follows_the_highest_recorded_generation(state_root):
    key = runkey.run_key(ALPHA)
    doc = registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    doc = registry.update(state_root, lambda d: registry.mirror_status(d, key, "terminal"))
    assert registry.next_generation(doc, ALPHA) == 2
    doc = registry.update(
        state_root,
        lambda d: registry.register(d, spec=ALPHA, run_key=f"{key}-g2", generation=2),
    )
    assert registry.current_run_key(doc, ALPHA) == f"{key}-g2"
    assert registry.next_generation(doc, ALPHA) == 3


def test_registering_a_second_nonterminal_generation_is_refused(state_root):
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    with pytest.raises(schema.SchemaError):
        registry.update(
            state_root,
            lambda d: registry.register(d, spec=ALPHA, run_key=f"{key}-g2", generation=2),
        )


def test_mirror_status_moves_current_when_a_run_becomes_terminal(state_root):
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    doc = registry.update(state_root, lambda d: registry.mirror_status(d, key, "terminal"))
    assert registry.current_run_key(doc, ALPHA) is None


def test_two_specs_hold_independent_mappings(state_root):
    alpha_key, beta_key = runkey.run_key(ALPHA), runkey.run_key(BETA)
    registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=alpha_key, generation=1)
    )
    doc = registry.update(
        state_root, lambda d: registry.register(d, spec=BETA, run_key=beta_key, generation=1)
    )
    assert registry.run_keys(doc) == sorted([alpha_key, beta_key])
    assert registry.current_run_key(doc, ALPHA) == alpha_key
    assert registry.current_run_key(doc, BETA) == beta_key


def test_commit_with_a_stale_revision_is_refused_and_writes_nothing(state_root):
    stale = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    with pytest.raises(registry.RevisionConflict) as excinfo:
        registry.commit(
            state_root,
            registry.register(stale, spec=BETA, run_key=runkey.run_key(BETA), generation=1),
            expect_revision=0,
        )
    assert "revision" in str(excinfo.value)
    assert registry.run_keys(registry.load(state_root)) == [key]


def test_update_re_reads_and_retries_after_a_concurrent_write(state_root):
    """The first mutate call sees revision 0; a concurrent writer lands revision 1 underneath it;
    update must re-read and apply the mutation on top of the newer value, not replace it."""
    beta_key = runkey.run_key(BETA)
    calls = {"n": 0}

    def mutate(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            registry.commit(
                state_root,
                registry.register(
                    registry.load(state_root),
                    spec=ALPHA,
                    run_key=runkey.run_key(ALPHA),
                    generation=1,
                ),
                expect_revision=0,
            )
        return registry.register(doc, spec=BETA, run_key=beta_key, generation=1)

    result = registry.update(state_root, mutate)
    assert calls["n"] == 2
    assert result["revision"] == 2
    assert registry.run_keys(result) == sorted([runkey.run_key(ALPHA), beta_key])


def test_update_gives_up_after_the_attempt_budget(state_root):
    def mutate(doc):
        registry.commit(
            state_root, registry.load(state_root), expect_revision=registry.load(state_root)["revision"]
        )
        return doc

    with pytest.raises(registry.RevisionConflict):
        registry.update(state_root, mutate, attempts=2)


def test_update_on_a_missing_registry_names_the_init_path(tmp_path):
    with pytest.raises(registry.RegistryMissing) as excinfo:
        registry.update(str(tmp_path / ".conductor"), lambda d: d)
    assert "conductor run new" in str(excinfo.value)


def test_commit_completes_an_unfinished_transaction_before_reading(state_root):
    """Design line 450: every project entry point completes or reverses an unfinished transaction
    before reading mappings, so a crash cannot leave a silently split identity."""
    doc = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    # schema.clone, NOT dict(doc): a shallow copy leaves after["specs"] aliasing doc["specs"],
    # so register() mutates both and the journal's before/after images become identical apart
    # from `revision`. The test would then pass even if recover() rolled a committed journal
    # BACKWARD — it would be asserting nothing about the direction it claims to check.
    after = registry.register(schema.clone(doc), spec=ALPHA, run_key=key, generation=1)
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-repoint",
        [{"path": registry.registry_path(state_root), "before": doc, "after": after}],
    )
    transaction.commit(state_root, "txn-repoint")
    refreshed = registry.update(state_root, lambda d: d)
    assert registry.current_run_key(refreshed, ALPHA) == key
    assert transaction.pending(state_root) == []


def test_init_completes_an_unfinished_transaction_before_reading(tmp_path):
    """`init` is a project entry point too, and unlike `commit` it has no compare-and-swap gate
    to catch a stale read — so it must recover before reading, and that must be tested through
    `init` itself."""
    state_root = str(tmp_path / ".conductor")
    registry.init(state_root, workstation_id=WORKSTATION, repo_identity=IDENTITY)
    doc = registry.load(state_root)
    key = runkey.run_key(ALPHA)
    after = registry.register(schema.clone(doc), spec=ALPHA, run_key=key, generation=1)
    after["revision"] = 1
    transaction.prepare(
        state_root,
        "txn-init",
        [{"path": registry.registry_path(state_root), "before": doc, "after": after}],
    )
    transaction.commit(state_root, "txn-init")  # committed but NOT applied — the crash state
    # No other registry call may precede this init(): any earlier call would recover the
    # transaction itself, and the test would pass whether or not init() was fixed.
    recovered = registry.init(
        state_root, workstation_id=WORKSTATION, repo_identity=IDENTITY
    )
    assert registry.current_run_key(recovered, ALPHA) == key  # the RETURNED doc, not a re-read
    assert transaction.pending(state_root) == []


def test_the_mutate_callback_cannot_alter_the_on_disk_snapshot(state_root):
    def mutate(doc):
        doc["specs"]["docs/specs/ghost.md"] = {"generations": [], "current": None}
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        registry.update(state_root, mutate)
    assert registry.load(state_root)["specs"] == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.registry'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/registry.py`:

```python
"""``project.json`` — one registry per project.

It records the state schema, its own monotonic revision, stable repository identity, the
normalized-spec-path-to-run-key mappings, and the workstation identity that owns this
project-local state. Each spec-path mapping is an ordered generation list with at most one
nonterminal run designated ``current``; starting a path whose latest generation is terminal
requires an explicit new generation.

Mutations are read-modify-write under ``project.lock`` with a revision compare-and-swap. The lock
serializes writers on one machine; the revision check catches a writer that read the document
*before* taking the lock — which ``update`` does on purpose, so the retry path is the normal path
rather than an untested branch.

AUTHORITY: the per-generation ``status`` recorded here is a MIRROR, kept for the new-generation
policy and cheap listing. ``run.json`` is the authority for run status, and
``resolve.active_run_keys`` reads run records rather than this mirror. Never let a decision that
matters hang off the mirror alone.
"""

from __future__ import annotations

from collections.abc import Callable

from conductor.core import atomic, locks, schema, transaction


class RegistryMissing(RuntimeError):
    """No ``project.json`` under this state root."""


class RevisionConflict(RuntimeError):
    """``project.json`` advanced underneath a writer that read an older revision."""


def registry_path(state_root: str) -> str:
    return f"{state_root}/project.json"


def lock_path(state_root: str) -> str:
    return f"{state_root}/project.lock"


def load(state_root: str) -> dict | None:
    """The registry, or ``None`` when this project has none yet."""
    return atomic.read_json(registry_path(state_root))


def init(state_root: str, *, workstation_id: str, repo_identity: dict) -> dict:
    """Create the registry if absent; return the existing one otherwise. Never overwrites, so a
    second caller cannot reset the workstation identity or drop mappings."""
    with locks.hold(lock_path(state_root), kind="project"):
        # Recover BEFORE reading, like `commit`. This is a project entry point, and unlike
        # `commit` it has no compare-and-swap gate to catch a stale read — an idempotent
        # get-or-create called after a crash left a committed-but-unapplied journal would
        # otherwise hand the caller the pre-recovery document and never notice.
        transaction.recover(state_root)
        existing = load(state_root)
        if existing is not None:
            return schema.validate_project(existing)
        doc = schema.new_project_doc(
            workstation_id=workstation_id, repo_identity=repo_identity
        )
        atomic.write_json_atomic(registry_path(state_root), doc)
        return doc


def commit(state_root: str, doc: dict, *, expect_revision: int) -> dict:
    """Write ``doc`` if the on-disk revision still equals ``expect_revision``.

    Completes or reverses any unfinished transaction first, then validates, then bumps the
    revision. Raises ``RevisionConflict`` without writing when the document moved."""
    with locks.hold(lock_path(state_root), kind="project"):
        transaction.recover(state_root)
        current = load(state_root)
        if current is None:
            raise RegistryMissing(
                f"no project registry at {registry_path(state_root)}; no write occurred. "
                "Create one with: conductor run new <spec.md>"
            )
        if current["revision"] != expect_revision:
            raise RevisionConflict(
                f"project.json moved from revision {expect_revision} to {current['revision']} "
                f"at {registry_path(state_root)}; no write occurred. Re-read and retry."
            )
        proposed = dict(doc)
        proposed["revision"] = expect_revision + 1
        schema.validate_project(proposed)
        atomic.write_json_atomic(registry_path(state_root), proposed)
        return proposed


def update(
    state_root: str, mutate: Callable[[dict], dict], *, attempts: int = 5
) -> dict:
    """Apply ``mutate`` to a private copy of the registry and commit it, retrying on conflict.

    ``mutate`` receives a deep copy, must return the new document, and must not change
    ``revision`` — ``commit`` owns that."""
    last: RevisionConflict | None = None
    for _ in range(max(1, attempts)):
        current = load(state_root)
        if current is None:
            raise RegistryMissing(
                f"no project registry at {registry_path(state_root)}; no write occurred. "
                "Create one with: conductor run new <spec.md>"
            )
        expect = current["revision"]
        try:
            return commit(state_root, mutate(schema.clone(current)), expect_revision=expect)
        except RevisionConflict as exc:
            last = exc
    raise RevisionConflict(
        f"project.json at {registry_path(state_root)} changed under {attempts} attempts; "
        f"no write occurred. Last conflict: {last}"
    )


def mapping(doc: dict, normalized_spec_path: str) -> dict | None:
    """This spec path's mapping, or ``None``."""
    value = doc.get("specs", {}).get(normalized_spec_path)
    return value if isinstance(value, dict) else None


def current_run_key(doc: dict, normalized_spec_path: str) -> str | None:
    """The nonterminal run key for this spec path, or ``None`` when every generation ended."""
    entry = mapping(doc, normalized_spec_path)
    return entry.get("current") if entry else None


def next_generation(doc: dict, normalized_spec_path: str) -> int:
    """The generation a new run for this spec path would take."""
    entry = mapping(doc, normalized_spec_path)
    if not entry or not entry.get("generations"):
        return 1
    return max(int(g["generation"]) for g in entry["generations"]) + 1


def run_keys(doc: dict) -> list[str]:
    """Every run key in the registry, sorted."""
    return sorted(
        g["run_key"]
        for entry in doc.get("specs", {}).values()
        for g in entry.get("generations", [])
    )


def find_run(doc: dict, run_key: str) -> tuple[str, dict] | None:
    """``(spec_path, generation_entry)`` for ``run_key``, or ``None``."""
    for spec_path, entry in doc.get("specs", {}).items():
        for generation in entry.get("generations", []):
            if generation.get("run_key") == run_key:
                return spec_path, generation
    return None


def register(doc: dict, *, spec: str, run_key: str, generation: int) -> dict:
    """Append a generation for ``spec`` and make it current. Pure: mutates and returns ``doc``.

    Validation of the "at most one nonterminal generation" rule happens in
    ``schema.validate_project`` during ``commit``, so an attempt to register a second live
    generation is refused there rather than silently accepted here."""
    entry = doc.setdefault("specs", {}).setdefault(
        spec, {"generations": [], "current": None, "path_history": []}
    )
    entry.setdefault("path_history", [])
    entry["generations"].append(
        {"run_key": run_key, "generation": generation, "status": "active"}
    )
    entry["generations"].sort(key=lambda g: g["generation"])
    entry["current"] = run_key
    return doc


def mirror_status(doc: dict, run_key: str, status: str) -> dict:
    """Refresh the status mirror for ``run_key`` and recompute ``current``. Pure."""
    if status not in schema.RUN_STATUSES:
        raise schema.SchemaError(
            f"status {status!r}; expected one of {schema.RUN_STATUSES}"
        )
    found = find_run(doc, run_key)
    if found is None:
        raise KeyError(f"run {run_key!r} is not registered")
    spec_path, generation = found
    generation["status"] = status
    entry = doc["specs"][spec_path]
    live = [
        g["run_key"]
        for g in entry["generations"]
        if g["status"] not in schema.TERMINAL_STATUSES
    ]
    entry["current"] = live[0] if live else None
    return doc
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_registry.py -q`
Expected: PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/registry.py tests/conductor/core/test_registry.py
git commit -m "conductor/core/registry.py:1-225 — project.json with revision compare-and-swap

- spec path -> ordered generations -> run key, at most one nonterminal generation as current
- update() re-reads and retries on conflict; commit() recovers unfinished transactions first
- per-generation status is a documented mirror; run.json stays authoritative"
```

---

### Task 8: Per-run state

**Files:**
- Create: `conductor/core/runstate.py`
- Test: `tests/conductor/core/test_runstate.py`

**Interfaces:**
- Consumes: `atomic`, `locks`, `schema`, `runkey.is_safe_run_key`.
- Produces:
  - `class RunMissing(RuntimeError)`, `class RunExists(RuntimeError)`, `class RevisionConflict(RuntimeError)`
  - `run_dir(state_root, run_key) -> str`, `run_path(state_root, run_key) -> str`, `state_lock_path(state_root, run_key) -> str`, `owner_lock_path(state_root, run_key) -> str`
  - `load(state_root, run_key) -> dict | None`
  - `create(state_root, run_key, doc) -> dict`
  - `commit(state_root, run_key, doc, *, expect_revision) -> dict`
  - `update(state_root, run_key, mutate, *, attempts=5) -> dict`
  - `set_status(state_root, run_key, status) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_runstate.py`:

```python
"""Per-run state: <project>/.conductor/runs/<run-key>/run.json (design §"Project and run
identity").

Every mutation is a read-modify-write guarded by the short-lived state mutex and the state
revision. A stale writer re-reads and retries rather than replacing a newer value; atomic replace
prevents torn files, the revision prevents lost updates."""

from __future__ import annotations

import os

import pytest

from conductor.core import runkey, runstate, schema

WORKSTATION = "0123456789abcdef0123456789abcdef"
ALPHA = "docs/specs/alpha.md"
NOW = "2026-08-10T12:00:00+00:00"


def _doc(key):
    return schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=ALPHA,
        workstation_id=WORKSTATION,
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )


@pytest.fixture
def run(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    runstate.create(state_root, key, _doc(key))
    return state_root, key


def test_paths_are_namespaced_under_the_run_key(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    assert runstate.run_dir(state_root, key) == os.path.join(state_root, "runs", key)
    assert runstate.run_path(state_root, key).endswith(f"runs/{key}/run.json")
    assert runstate.state_lock_path(state_root, key).endswith(f"runs/{key}/state.lock")
    assert runstate.owner_lock_path(state_root, key).endswith(f"runs/{key}/owner.lock")


def test_an_unsafe_run_key_never_reaches_the_filesystem(tmp_path):
    with pytest.raises(ValueError):
        runstate.run_dir(str(tmp_path / ".conductor"), "../escape")


def test_load_returns_none_for_an_unknown_run(tmp_path):
    assert runstate.load(str(tmp_path / ".conductor"), runkey.run_key(ALPHA)) is None


def test_create_writes_a_validated_record_and_refuses_to_overwrite(run):
    state_root, key = run
    assert runstate.load(state_root, key)["run_key"] == key
    with pytest.raises(runstate.RunExists):
        runstate.create(state_root, key, _doc(key))


def test_create_refuses_a_document_whose_key_disagrees(tmp_path):
    state_root = str(tmp_path / ".conductor")
    key = runkey.run_key(ALPHA)
    with pytest.raises(ValueError):
        runstate.create(state_root, runkey.run_key("docs/specs/beta.md"), _doc(key))


def test_update_bumps_the_revision_and_refreshes_updated_at(run):
    state_root, key = run
    doc = runstate.update(state_root, key, lambda d: {**d, "current_phase": "phase-1"})
    assert doc["revision"] == 1
    assert doc["current_phase"] == "phase-1"
    assert doc["updated_at"] != NOW


def test_commit_with_a_stale_revision_is_refused_and_writes_nothing(run):
    state_root, key = run
    stale = runstate.load(state_root, key)
    runstate.update(state_root, key, lambda d: {**d, "current_phase": "phase-1"})
    with pytest.raises(runstate.RevisionConflict):
        runstate.commit(state_root, key, {**stale, "current_phase": "phase-9"}, expect_revision=0)
    assert runstate.load(state_root, key)["current_phase"] == "phase-1"


def test_update_re_reads_and_retries_after_a_concurrent_write(run):
    state_root, key = run
    calls = {"n": 0}

    def mutate(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            current = runstate.load(state_root, key)
            runstate.commit(
                state_root, key, {**current, "ledger_ref": "#42"}, expect_revision=current["revision"]
            )
        return {**doc, "current_phase": "phase-1"}

    result = runstate.update(state_root, key, mutate)
    assert calls["n"] == 2
    assert result["revision"] == 2
    assert result["ledger_ref"] == "#42"
    assert result["current_phase"] == "phase-1"


def test_an_invalid_mutation_is_refused_before_it_reaches_disk(run):
    state_root, key = run
    with pytest.raises(schema.SchemaError):
        runstate.update(state_root, key, lambda d: {**d, "status": "running"})
    assert runstate.load(state_root, key)["status"] == "active"


def test_set_status_enforces_the_transition_table(run):
    state_root, key = run
    assert runstate.set_status(state_root, key, "checkpointed")["status"] == "checkpointed"
    assert runstate.set_status(state_root, key, "active")["status"] == "active"
    assert runstate.set_status(state_root, key, "awaiting-team-merge")["status"] == (
        "awaiting-team-merge"
    )
    assert runstate.set_status(state_root, key, "terminal")["status"] == "terminal"
    with pytest.raises(schema.SchemaError):
        runstate.set_status(state_root, key, "active")


def test_set_status_stamps_the_completion_and_failure_timestamps(run):
    state_root, key = run
    runstate.set_status(state_root, key, "awaiting-team-merge")
    doc = runstate.set_status(state_root, key, "terminal")
    assert doc["completed_at"] and doc["failed_at"] is None
    state_root2, key2 = state_root, runkey.run_key("docs/specs/beta.md")
    other = _doc(key2)
    other["spec_path"] = "docs/specs/beta.md"
    other["integration_branch"] = f"conductor/run-{key2}"
    other["gate_dir"] = f"assertions/{key2}"
    runstate.create(state_root2, key2, other)
    failed = runstate.set_status(state_root2, key2, "failed")
    assert failed["failed_at"] and failed["completed_at"] is None


def test_update_on_a_missing_run_names_the_listing_command(tmp_path):
    with pytest.raises(runstate.RunMissing) as excinfo:
        runstate.update(str(tmp_path / ".conductor"), runkey.run_key(ALPHA), lambda d: d)
    assert "conductor run list --all" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_runstate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.runstate'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/runstate.py`:

```python
"""``<project>/.conductor/runs/<run-key>/run.json`` — one record per run.

Every mutation is a read-modify-write guarded by the short-lived ``state.lock`` and the state
revision (design §"Project and run identity"). Atomic replace prevents torn files; the revision
prevents lost updates when two short-lived host processes overlap. As with the registry,
``update`` reads *before* taking the lock so the retry path is exercised in normal operation.

``state.lock`` is only for run.json mutation. ``owner.lock`` — created here so the path is
single-sourced — is the execution-ownership lock and belongs to Plan 02; nothing in this module
interprets it.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable

from conductor.core import atomic, locks, schema
from conductor.core.runkey import is_safe_run_key


class RunMissing(RuntimeError):
    """No run record for this key under this state root."""


class RunExists(RuntimeError):
    """A run record already exists; creating one would discard history."""


class RevisionConflict(RuntimeError):
    """``run.json`` advanced underneath a writer that read an older revision."""


def _checked(run_key: str) -> str:
    if not isinstance(run_key, str) or not is_safe_run_key(run_key):
        raise ValueError(f"unsafe run key {run_key!r}; refusing to build a state path from it")
    return run_key


def run_dir(state_root: str, run_key: str) -> str:
    return os.path.join(state_root, "runs", _checked(run_key))


def run_path(state_root: str, run_key: str) -> str:
    return os.path.join(run_dir(state_root, run_key), "run.json")


def state_lock_path(state_root: str, run_key: str) -> str:
    return os.path.join(run_dir(state_root, run_key), "state.lock")


def owner_lock_path(state_root: str, run_key: str) -> str:
    """The execution-ownership lock. Plan 02 owns its semantics; the path lives here so both
    plans cannot disagree about where it is."""
    return os.path.join(run_dir(state_root, run_key), "owner.lock")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(state_root: str, run_key: str) -> dict | None:
    """The run record, or ``None`` when this run does not exist."""
    return atomic.read_json(run_path(state_root, run_key))


def create(state_root: str, run_key: str, doc: dict) -> dict:
    """Write a new run record. Refuses to overwrite an existing one — a new generation gets a
    new run key, and history is never replaced."""
    _checked(run_key)
    if doc.get("run_key") != run_key:
        raise ValueError(
            f"run document declares run_key {doc.get('run_key')!r} but is being written as "
            f"{run_key!r}"
        )
    schema.validate_run(doc)
    os.makedirs(run_dir(state_root, run_key), exist_ok=True)
    with locks.hold(state_lock_path(state_root, run_key), kind="state", run_key=run_key):
        if load(state_root, run_key) is not None:
            raise RunExists(
                f"run {run_key!r} already exists at {run_path(state_root, run_key)}; no write "
                "occurred. Start a new generation with: conductor run new <spec.md> --new-run"
            )
        atomic.write_json_atomic(run_path(state_root, run_key), doc)
    return doc


def commit(state_root: str, run_key: str, doc: dict, *, expect_revision: int) -> dict:
    """Write ``doc`` if the on-disk revision still equals ``expect_revision``."""
    _checked(run_key)
    with locks.hold(state_lock_path(state_root, run_key), kind="state", run_key=run_key):
        current = load(state_root, run_key)
        if current is None:
            raise RunMissing(
                f"no run record at {run_path(state_root, run_key)}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        if current["revision"] != expect_revision:
            raise RevisionConflict(
                f"run {run_key!r} moved from revision {expect_revision} to "
                f"{current['revision']} at {run_path(state_root, run_key)}; no write occurred. "
                "Re-read and retry."
            )
        proposed = dict(doc)
        proposed["revision"] = expect_revision + 1
        proposed["updated_at"] = _now()
        schema.validate_run(proposed)
        atomic.write_json_atomic(run_path(state_root, run_key), proposed)
        return proposed


def update(
    state_root: str, run_key: str, mutate: Callable[[dict], dict], *, attempts: int = 5
) -> dict:
    """Apply ``mutate`` to a private copy of the run record and commit it, retrying on conflict.

    ``mutate`` must not change ``revision`` or ``updated_at`` — ``commit`` owns both."""
    last: RevisionConflict | None = None
    for _ in range(max(1, attempts)):
        current = load(state_root, run_key)
        if current is None:
            raise RunMissing(
                f"no run record at {run_path(state_root, run_key)}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        expect = current["revision"]
        try:
            return commit(
                state_root, run_key, mutate(schema.clone(current)), expect_revision=expect
            )
        except RevisionConflict as exc:
            last = exc
    raise RevisionConflict(
        f"run {run_key!r} changed under {attempts} attempts; no write occurred. "
        f"Last conflict: {last}"
    )


def set_status(state_root: str, run_key: str, status: str) -> dict:
    """Move the run to ``status`` if the transition is legal, stamping the matching timestamp."""

    def mutate(doc: dict) -> dict:
        schema.assert_transition(doc["status"], status)
        doc["status"] = status
        if status == "terminal":
            doc["completed_at"] = _now()
        elif status == "failed":
            doc["failed_at"] = _now()
        return doc

    return update(state_root, run_key, mutate)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_runstate.py -q`
Expected: PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/runstate.py tests/conductor/core/test_runstate.py
git commit -m "conductor/core/runstate.py:1-200 — per-run run.json under state.lock

- create/commit/update/set_status with revision CAS and retry; create never overwrites history
- run_dir/state_lock_path/owner_lock_path refuse an unsafe run key before touching the filesystem"
```

---

### Task 9: Canonical state root and run-key resolution

**Files:**
- Create: `conductor/core/resolve.py`
- Create: `tests/conductor/conftest.py` — note the location: `tests/conductor/`, **not** `tests/conductor/core/`. A conftest applies to its own directory and all subdirectories, so putting it one level up lets Tasks 11, 13 and 14 (which live in `tests/conductor/`) use the same fixtures without `pytest_plugins`, which pytest deprecates outside the rootdir conftest.
- Modify: `conductor/resume_script.py:58-77`
- Test: `tests/conductor/core/test_resolve.py`

**Interfaces:**
- Consumes: `registry`, `runstate`, `schema`.
- Produces:
  - `class RunAmbiguous(RuntimeError)`, `class RunNotFound(RuntimeError)`
  - `class RunResolution(NamedTuple)` with fields `state_root, repo_root, run_key, run_dir, run`
  - `repo_root(start: str | None = None) -> str`
  - `state_root(start: str | None = None) -> str`
  - `repo_identity(repo_root: str) -> dict`
  - `active_run_keys(state_root: str) -> list[str]`
  - `resolve(*, run_key: str | None = None, start: str | None = None) -> RunResolution`

**Behaviour contract (design lines 148, 152, 180):** the canonical state root comes from the Git common directory, so starting from a linked worktree finds the same root as the main checkout. When an invocation carries a run key, that key alone determines the run — `.conductor/run_branch`, `.conductor/goal.md`, and `CONDUCTOR_GATE_*` are ignored, not consulted as fallback. Without a key, resolution succeeds only when exactly one active run exists; otherwise it fails listing the available keys and the exact commands.

- [ ] **Step 1: Write the shared git fixture**

Create `tests/conductor/conftest.py` (one level **above** `core/`, so every test under
`tests/conductor/` inherits these fixtures):

```python
"""Shared fixtures for the conductor test suite.

Conductor resolves its canonical state root from git plumbing (``--git-common-dir``) so that
starting from a linked worktree finds the same root as the main checkout. Mocking git would test
the mock, so these tests build real, isolated repositories."""

from __future__ import annotations

import os
import subprocess

import pytest


@pytest.fixture
def git_env(tmp_path):
    """Git environment isolated from the developer's global and system configuration."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Conductor Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Conductor Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


@pytest.fixture
def git(git_env):
    """Run a git command inside a repository, raising on failure."""

    def _git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
            timeout=30,
        )

    return _git


@pytest.fixture
def git_repo(tmp_path, git_env, git):
    """A repository on ``main`` with two committed specs."""
    root = tmp_path / "repo"
    (root / "docs" / "specs").mkdir(parents=True)
    (root / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    (root / "docs" / "specs" / "beta.md").write_text("# beta\n")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root
```

- [ ] **Step 2: Write the failing test**

Create `tests/conductor/core/test_resolve.py`:

```python
"""Canonical state root and run-key resolution (design §"Project and run identity").

Conductor resolves one canonical state root from the repository's Git common directory, so
starting from a linked worktree still finds that same root. When an invocation carries a run key,
that key alone determines the run: legacy .conductor/run_branch, legacy .conductor/goal.md, and
ambient gate environment variables are ignored rather than consulted as fallback. Without a key,
resolution succeeds only when exactly one active run exists."""

from __future__ import annotations

import os

import pytest

from conductor.core import registry, resolve, runkey, runstate, schema

WORKSTATION = "0123456789abcdef0123456789abcdef"
NOW = "2026-08-10T12:00:00+00:00"


def _make_run(state_root, spec, *, status="active"):
    key = runkey.run_key(spec)
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path=spec,
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest="a" * 64,
            now=NOW,
        ),
    )
    registry.update(
        state_root, lambda d: registry.register(d, spec=spec, run_key=key, generation=1)
    )
    if status != "active":
        runstate.set_status(state_root, key, status)
        registry.update(state_root, lambda d: registry.mirror_status(d, key, status))
    return key


@pytest.fixture
def project(git_repo):
    root = str(git_repo)
    state_root = resolve.state_root(root)
    registry.init(
        state_root,
        workstation_id=WORKSTATION,
        repo_identity=resolve.repo_identity(root),
    )
    return root, state_root


def test_repo_root_is_the_main_checkout_from_a_linked_worktree(git_repo, git, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    assert resolve.repo_root(str(linked)) == os.path.realpath(str(git_repo))
    assert resolve.state_root(str(linked)) == resolve.state_root(str(git_repo))


def test_state_root_is_dot_conductor_under_the_main_checkout(git_repo):
    assert resolve.state_root(str(git_repo)) == os.path.join(
        os.path.realpath(str(git_repo)), ".conductor"
    )


def test_repo_identity_records_the_root_commit(git_repo):
    identity = resolve.repo_identity(str(git_repo))
    assert identity["root_commit"] and len(identity["root_commit"]) == 40
    assert "origin_url" in identity


def test_an_explicit_run_key_resolves_regardless_of_ambient_files(project, monkeypatch):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    other = _make_run(state_root, "docs/specs/beta.md")
    os.makedirs(os.path.join(root, ".conductor"), exist_ok=True)
    with open(os.path.join(root, ".conductor", "run_branch"), "w", encoding="utf-8") as fh:
        fh.write("conductor/run-something-else\n")
    with open(os.path.join(root, ".conductor", "goal.md"), "w", encoding="utf-8") as fh:
        fh.write("docs/specs/gamma.md\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked")
    resolution = resolve.resolve(run_key=key, start=root)
    assert resolution.run_key == key
    assert resolution.run["spec_path"] == "docs/specs/alpha.md"
    assert resolution.run_dir == runstate.run_dir(state_root, key)
    assert resolve.resolve(run_key=other, start=root).run["spec_path"] == "docs/specs/beta.md"


def test_an_unknown_explicit_run_key_fails_with_the_listing_command(project):
    root, _ = project
    with pytest.raises(resolve.RunNotFound) as excinfo:
        resolve.resolve(run_key="not-a-run-0badf00d", start=root)
    assert "conductor run list --all" in str(excinfo.value)


def test_no_key_resolves_when_exactly_one_run_is_active(project):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    _make_run(state_root, "docs/specs/beta.md", status="terminal")
    assert resolve.resolve(start=root).run_key == key


def test_no_key_with_two_active_runs_fails_listing_the_keys_and_commands(project):
    root, state_root = project
    first = _make_run(state_root, "docs/specs/alpha.md")
    second = _make_run(state_root, "docs/specs/beta.md")
    with pytest.raises(resolve.RunAmbiguous) as excinfo:
        resolve.resolve(start=root)
    message = str(excinfo.value)
    assert first in message and second in message
    assert f"--run {first}" in message and f"--run {second}" in message


def test_no_key_with_no_active_run_fails_with_the_creation_command(project):
    root, state_root = project
    _make_run(state_root, "docs/specs/alpha.md", status="terminal")
    with pytest.raises(resolve.RunNotFound) as excinfo:
        resolve.resolve(start=root)
    assert "conductor run new" in str(excinfo.value)


def test_checkpointed_and_blocked_count_as_active_but_awaiting_team_merge_does_not(project):
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    for status in ("checkpointed", "blocked"):
        runstate.set_status(state_root, key, status)
        assert resolve.active_run_keys(state_root) == [key]
        runstate.set_status(state_root, key, "active")
    runstate.set_status(state_root, key, "awaiting-team-merge")
    assert resolve.active_run_keys(state_root) == []


def test_active_run_keys_reads_run_json_not_the_registry_mirror(project):
    """The registry status is a mirror; run.json is authoritative. A stale mirror must not make a
    terminal run look active or an active run look gone."""
    root, state_root = project
    key = _make_run(state_root, "docs/specs/alpha.md")
    registry.update(state_root, lambda d: registry.mirror_status(d, key, "terminal"))
    assert resolve.active_run_keys(state_root) == [key]


def test_resume_script_main_root_delegates_to_the_same_resolver(git_repo, git, tmp_path):
    from conductor import resume_script

    linked = tmp_path / "linked2"
    git(git_repo, "worktree", "add", "-q", "-b", "side2", str(linked))
    assert resume_script.main_root(str(linked)) == resolve.repo_root(str(linked))
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_resolve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.resolve'`

- [ ] **Step 4: Write the implementation**

Create `conductor/core/resolve.py`:

```python
"""Where a run's state lives, and which run an invocation means.

Two decisions live here, and nowhere else:

1. THE CANONICAL STATE ROOT. Resolved from the repository's Git common directory, so starting
   from a linked worktree finds the same ``<main-checkout>/.conductor`` as the main checkout.
   Phase and integration worktrees therefore never grow their own registries.

2. WHICH RUN. When an invocation carries a run key, that key alone determines the run — legacy
   ``.conductor/run_branch``, legacy ``.conductor/goal.md``, and ambient ``CONDUCTOR_GATE_*``
   variables are ignored rather than consulted as fallback. Without a key, resolution succeeds
   only when exactly one ACTIVE run exists; zero or several fail with the available keys and the
   exact commands, because guessing is how a fire lands work on the wrong run's branch.
"""

from __future__ import annotations

import os
import subprocess
from typing import NamedTuple

from conductor.core import registry, runstate, schema

_GIT_TIMEOUT = 30.0


class RunNotFound(RuntimeError):
    """The named run does not exist, or no active run exists to default to."""


class RunAmbiguous(RuntimeError):
    """Several runs are active and the invocation carried no run key."""


class RunResolution(NamedTuple):
    """A fully-resolved run: where its state lives and what it currently says."""

    state_root: str
    repo_root: str
    run_key: str
    run_dir: str
    run: dict


def repo_root(start: str | None = None) -> str:
    """The MAIN checkout root for any path inside the repository.

    ``--git-common-dir`` is identical from the owner checkout and from a linked run worktree;
    ``--show-toplevel`` is not, which is why it is not used here."""
    base = start or os.environ.get("CONDUCTOR_HOME") or os.getcwd()
    common = subprocess.run(
        ["git", "-C", base, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    ).stdout.strip()
    return os.path.dirname(common)


def state_root(start: str | None = None) -> str:
    """The canonical project-local state root: ``<main-checkout>/.conductor``."""
    return os.path.join(repo_root(start), ".conductor")


def repo_identity(root: str) -> dict:
    """Stable repository identity for ``project.json``: the oldest root commit plus the
    configured origin URL. The root commit survives renaming the checkout or changing remotes;
    the URL is recorded for diagnostics only. ``rev-list`` prints newest first, so a repository
    with several roots (a grafted import) still yields the same oldest one every time."""

    def _capture(*args: str) -> str | None:
        out = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if out.returncode != 0:
            return None
        lines = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
        return lines[-1] if lines else None

    return {
        "root_commit": _capture("rev-list", "--max-parents=0", "HEAD"),
        "origin_url": _capture("remote", "get-url", "origin"),
    }


def active_run_keys(root: str) -> list[str]:
    """Run keys whose RUN RECORD says active, checkpointed, or blocked, sorted.

    Reads ``run.json`` rather than the registry's status mirror: the mirror exists for the
    new-generation policy and cheap listing, and a stale mirror must never decide which run a
    bare command operates on."""
    doc = registry.load(root)
    if doc is None:
        return []
    keys = []
    for key in registry.run_keys(doc):
        run = runstate.load(root, key)
        if run is not None and schema.is_active(str(run.get("status", ""))):
            keys.append(key)
    return sorted(keys)


def resolve(*, run_key: str | None = None, start: str | None = None) -> RunResolution:
    """The run this invocation means."""
    root = repo_root(start)
    sroot = os.path.join(root, ".conductor")
    if run_key is not None:
        run = runstate.load(sroot, run_key)
        if run is None:
            raise RunNotFound(
                f"no run {run_key!r} under {sroot}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        return RunResolution(sroot, root, run_key, runstate.run_dir(sroot, run_key), run)
    active = active_run_keys(sroot)
    if len(active) == 1:
        key = active[0]
        run = runstate.load(sroot, key)
        assert run is not None  # active_run_keys only returns keys whose record loaded
        return RunResolution(sroot, root, key, runstate.run_dir(sroot, key), run)
    if not active:
        raise RunNotFound(
            f"no active run under {sroot}; no write occurred. "
            "Start one with: conductor run new <spec.md>  |  inspect history with: "
            "conductor run list --all"
        )
    listing = "\n".join(f"  conductor run show --run {key}" for key in active)
    raise RunAmbiguous(
        f"{len(active)} active runs under {sroot} and no --run given; no write occurred.\n"
        f"Active run keys: {', '.join(active)}\n"
        f"Re-run the command with one of:\n{listing}"
    )
```

- [ ] **Step 5: Point `resume_script.main_root` at the shared resolver**

In `conductor/resume_script.py`, replace the body of `main_root` (currently `conductor/resume_script.py:58-77`) with a delegation, keeping the docstring's warning:

```python
def main_root(path: str) -> str:
    """The MAIN-checkout root for any path inside the repo: dirname of
    `git rev-parse --path-format=absolute --git-common-dir`. IDENTICAL whether computed
    from the owner checkout or a linked run worktree (`--show-toplevel` is NOT — it
    returns the worktree path there, so install and removal would disagree).

    Delegates to `conductor.core.resolve.repo_root` so the driver, the resume script, and the
    run resolver cannot disagree about which checkout owns a project's state."""
    from conductor.core.resolve import repo_root

    return repo_root(path)
```

The import stays function-local: `conductor.core.resolve` imports `registry`/`runstate`, and
`resume_script` is imported by `conductor/driver.py` at module scope, so a top-level import would
widen the driver's import graph for no benefit. `repo_root` keeps `check=True` and `timeout=30`,
so `driver.main`'s existing `subprocess.CalledProcessError` handler (`conductor/driver.py:230`)
still fires unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/conductor/core/test_resolve.py tests/conductor/test_driver.py tests/conductor/test_resume_script.py -q`
Expected: PASS — the new resolution tests plus the untouched driver and resume-script suites.

- [ ] **Step 7: Lint and typecheck**

Run: `ruff check conductor tests/conductor/core && ruff format --check conductor/core conductor/resume_script.py tests/conductor/core && pyright conductor/core conductor/resume_script.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add conductor/core/resolve.py conductor/resume_script.py tests/conductor/conftest.py tests/conductor/core/test_resolve.py
git commit -m "conductor/core/resolve.py:1-160, conductor/resume_script.py:58-70 — run resolution

- canonical state root from --git-common-dir; a linked worktree finds the main checkout's root
- an explicit run key wins outright; ambient run_branch/goal.md/CONDUCTOR_GATE_* are ignored
- no key resolves only with exactly one active run, else lists keys and exact commands
- resume_script.main_root delegates to resolve.repo_root (one git-common-dir walk)"
```

---

### Task 10: Repository hygiene preflight

**Files:**
- Create: `conductor/core/hygiene.py`
- Test: `tests/conductor/core/test_hygiene.py`

**Interfaces:**
- Consumes: `atomic.write_atomic`.
- Produces:
  - `class TrackedStateError(RuntimeError)`
  - `STATE_PATHS: tuple[str, ...]` — `(".conductor", ".worktrees")`
  - `tracked_state_paths(repo_root: str) -> list[str]`
  - `assert_state_paths_untracked(repo_root: str) -> None`
  - `is_ignored(repo_root: str, relative: str) -> bool`
  - `ensure_local_exclude(repo_root: str) -> None`

**Contract (design line 154):** before creating project-local worktrees or support state, verify `.worktrees/` and `.conductor/` are not tracked by Git. If either is already tracked, fail closed and report the exact `git rm -r --cached` recovery command. Otherwise establish a local Git exclude and **recheck** it.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_hygiene.py`:

```python
"""Repository hygiene before any project-local state is created (design §"Project and run
identity").

Tracked .conductor/ or .worktrees/ paths would put a run's private state — locks, leases,
heartbeat records — into the repository history and onto every collaborator's checkout. Preflight
fails closed with the exact recovery command, then establishes a local git exclude and rechecks
it."""

from __future__ import annotations

import os

import pytest

from conductor.core import hygiene


def test_state_paths_are_the_two_documented_directories():
    assert hygiene.STATE_PATHS == (".conductor", ".worktrees")


def test_a_clean_repository_passes(git_repo):
    hygiene.assert_state_paths_untracked(str(git_repo))
    assert hygiene.tracked_state_paths(str(git_repo)) == []


def test_a_tracked_state_path_fails_closed_with_the_exact_recovery_command(git_repo, git):
    (git_repo / ".conductor").mkdir()
    (git_repo / ".conductor" / "goal.md").write_text("goal\n")
    git(git_repo, "add", "-f", ".conductor/goal.md")
    git(git_repo, "commit", "-qm", "oops")
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(git_repo))
    message = str(excinfo.value)
    assert ".conductor/goal.md" in message
    assert f"git -C {git_repo} rm -r --cached .conductor .worktrees" in message
    assert "No write occurred" in message


def test_a_tracked_worktrees_path_is_caught_too(git_repo, git):
    (git_repo / ".worktrees").mkdir()
    (git_repo / ".worktrees" / "keep.txt").write_text("x\n")
    git(git_repo, "add", "-f", ".worktrees/keep.txt")
    git(git_repo, "commit", "-qm", "oops")
    with pytest.raises(hygiene.TrackedStateError):
        hygiene.assert_state_paths_untracked(str(git_repo))


def test_ensure_local_exclude_makes_both_paths_ignored(git_repo):
    assert not hygiene.is_ignored(str(git_repo), ".conductor/project.json")
    hygiene.ensure_local_exclude(str(git_repo))
    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")
    assert hygiene.is_ignored(str(git_repo), ".worktrees/conductor/x/integration")


def test_ensure_local_exclude_is_idempotent(git_repo):
    hygiene.ensure_local_exclude(str(git_repo))
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    first = open(exclude, encoding="utf-8").read()
    hygiene.ensure_local_exclude(str(git_repo))
    assert open(exclude, encoding="utf-8").read() == first


def test_ensure_local_exclude_preserves_existing_exclude_content(git_repo):
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "w", encoding="utf-8") as fh:
        fh.write("# my own rules\n*.scratch\n")
    hygiene.ensure_local_exclude(str(git_repo))
    body = open(exclude, encoding="utf-8").read()
    assert "*.scratch" in body and "/.conductor/" in body


def test_ensure_local_exclude_is_a_no_op_when_gitignore_already_covers_them(git_repo, git):
    (git_repo / ".gitignore").write_text(".conductor/\n.worktrees/\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ignore run state")
    hygiene.ensure_local_exclude(str(git_repo))
    exclude = os.path.join(str(git_repo), ".git", "info", "exclude")
    assert not os.path.exists(exclude) or "/.conductor/" not in open(exclude, encoding="utf-8").read()
    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")


def test_a_non_repository_reports_the_git_failure(tmp_path):
    with pytest.raises(hygiene.TrackedStateError) as excinfo:
        hygiene.assert_state_paths_untracked(str(tmp_path))
    assert "git ls-files" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_hygiene.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.core.hygiene'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/hygiene.py`:

```python
"""Repository hygiene for project-local run state.

Design §"Project and run identity": before creating project-local worktrees or support state,
verify that ``.worktrees/`` and ``.conductor/`` are not tracked by Git. If either is already
tracked, fail closed and report the exact ``git rm -r --cached`` recovery command; otherwise
establish a local Git exclude and recheck it.

Tracked run state is not a cosmetic problem. ``owner.lock``, ``state.lock``, leases, heartbeat
records and compaction markers are per-machine facts; committing them puts one workstation's
ownership claims into every collaborator's checkout and into the run's own pull requests.
Assertions stay tracked — they are the run's audited evidence, not its scratch state.
"""

from __future__ import annotations

import os
import subprocess

from conductor.core import atomic

STATE_PATHS = (".conductor", ".worktrees")
_EXCLUDE_LINES = ("/.conductor/", "/.worktrees/")
_EXCLUDE_HEADER = "# conductor: project-local run state is per-machine and never tracked"
# Probe files rather than the bare directories: a trailing-slash ignore pattern only matches a
# directory, and `git check-ignore` cannot classify a path that does not exist yet.
_IGNORE_PROBES = (".conductor/project.json", ".worktrees/conductor/probe/integration")
_GIT_TIMEOUT = 30.0


class TrackedStateError(RuntimeError):
    """Run state is tracked by Git, or could not be proven untracked/ignored."""


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def tracked_state_paths(repo_root: str) -> list[str]:
    """Tracked files under ``.conductor/`` or ``.worktrees/``."""
    out = _git(repo_root, "ls-files", "--", *STATE_PATHS)
    if out.returncode != 0:
        raise TrackedStateError(
            f"git ls-files failed in {repo_root} (rc={out.returncode}): "
            f"{(out.stderr or '').strip()}; no write occurred"
        )
    return [line for line in out.stdout.splitlines() if line.strip()]


def assert_state_paths_untracked(repo_root: str) -> None:
    """Fail closed if any run-state path is tracked, naming the exact recovery commands."""
    tracked = tracked_state_paths(repo_root)
    if not tracked:
        return
    raise TrackedStateError(
        f"refusing to create project-local run state in {repo_root}: "
        f"{len(tracked)} file(s) under {' or '.join(STATE_PATHS)} are tracked by git "
        f"(first: {tracked[0]}). No write occurred. Recover with:\n"
        f"  git -C {repo_root} rm -r --cached {' '.join(STATE_PATHS)}\n"
        f"  git -C {repo_root} commit -m 'stop tracking conductor run state'"
    )


def is_ignored(repo_root: str, relative: str) -> bool:
    """Whether git would ignore ``relative`` in this repository."""
    out = _git(repo_root, "check-ignore", "-q", "--", relative)
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    raise TrackedStateError(
        f"git check-ignore failed in {repo_root} (rc={out.returncode}): "
        f"{(out.stderr or '').strip()}; no write occurred"
    )


def _exclude_file(repo_root: str) -> str:
    common = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise TrackedStateError(
            f"cannot locate the git common directory for {repo_root}: "
            f"{(common.stderr or '').strip()}; no write occurred"
        )
    return os.path.join(common.stdout.strip(), "info", "exclude")


def ensure_local_exclude(repo_root: str) -> None:
    """Make both state paths ignored, then prove it.

    A repository that already ignores them (its own ``.gitignore``, a global excludes file) is
    left untouched — writing redundant rules into someone else's exclude file is noise. The
    recheck is the point: if the paths are still not ignored after the write, that is a
    fail-closed condition, not a warning."""
    if all(is_ignored(repo_root, probe) for probe in _IGNORE_PROBES):
        return
    exclude = _exclude_file(repo_root)
    try:
        with open(exclude, encoding="utf-8") as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _EXCLUDE_LINES if line not in present]
    if missing:
        body = existing if (not existing or existing.endswith("\n")) else existing + "\n"
        body += _EXCLUDE_HEADER + "\n" + "\n".join(missing) + "\n"
        atomic.write_atomic(exclude, body)
    unresolved = [p for p in _IGNORE_PROBES if not is_ignored(repo_root, p)]
    if unresolved:
        raise TrackedStateError(
            f"{', '.join(unresolved)} is still not ignored in {repo_root} after writing "
            f"{exclude}. Run state would be committed. Add these lines to .gitignore and retry:\n"
            + "\n".join(f"  {line}" for line in _EXCLUDE_LINES)
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_hygiene.py -q`
Expected: PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/hygiene.py tests/conductor/core/test_hygiene.py
git commit -m "conductor/core/hygiene.py:1-150 — refuse tracked run state, establish the exclude

- tracked .conductor/.worktrees fails closed with the exact git rm -r --cached recovery command
- ensure_local_exclude is a no-op when already ignored, and rechecks after writing"
```

---

### Task 11: Gate resolution by run key

**Files:**
- Modify: `conductor/paths.py` (add `run_gate_dir`; add the run-key mode to `resolve_gate` at `conductor/paths.py:144-238`; extend the `GateResolution` docstring)
- Modify: `conductor/core/resolve.py` (add `gate_for_run`)
- Test: `tests/conductor/test_gate_paths.py` (extend the existing file)

**Interfaces:**
- Consumes: `runstate.load` (via `resolve.gate_for_run`).
- Produces:
  - `paths.run_gate_dir(repo_root: str, run_key: str) -> str`
  - `paths.resolve_gate(repo_root: str | None = None, *, run_key: str | None = None, run: dict | None = None) -> GateResolution` — in key mode `source == "run_key"`
  - `resolve.gate_for_run(res: RunResolution) -> paths.GateResolution`

**Contract (design lines 150 and 152):** the run key is the single source shared by a new run's integration-branch suffix and its done-gate directory, and the resolver verifies this equality **from `run.json`** rather than recovering it from ambient project files. When an invocation carries a run key, that key alone determines the gate directory, manifest, freeze baseline, and results path.

**Import-direction rule:** `conductor/paths.py` must **not** import `conductor.core.runkey`, `registry`, `runstate`, or `resolve`. `runkey` already imports `paths.spec_slug`, and a back-edge would be a cycle. `resolve_gate` therefore takes the already-loaded run document; `resolve.gate_for_run` is the one place that loads it.

`conductor.core.names` is the deliberate exception and the reason it exists as its own module: it is a leaf that imports nothing from `paths` or `runkey`, so `paths.py` may import it (`from conductor.core.names import derived_names`) without forming a cycle. That is what lets all three call sites — `schema.validate_run`, `paths.resolve_gate`, and `run_cmd.cmd_new` — share one definition of the two formats instead of each carrying a literal copy.

**Tests keep their literals.** The test files in this plan assert `gate_dir == f"assertions/{key}"` and `integration_branch == f"conductor/run-{key}"` directly. Do **not** rewrite those to call `derived_names` — a test that computes its expectation with the same helper it is testing asserts nothing. The independent restatement in the tests is what pins the format.

**Legacy behaviour is unchanged.** With `run_key=None`, `resolve_gate` keeps its existing env → `run_branch` → `goal.md` → flat precedence and its two §5 ambient-dodge checks, so assertion A12 and every existing caller keep passing. Plan 03 removes the legacy branch after migration.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conductor/test_gate_paths.py`:

```python
# --- run-key mode: the key alone governs (design §"Project and run identity") --------------
#
# `pytest`, `os` and `subprocess` are already imported at the top of this file, and the
# `git_repo` fixture comes from tests/conductor/conftest.py (Task 9) — do not re-import or
# redefine either.

from conductor.core import resolve as core_resolve
from conductor.core import runkey, runstate, schema

_NOW = "2026-08-10T12:00:00+00:00"


def _run_doc(spec, *, generation=1, identity_scheme="path-hash-v2", **overrides):
    key = runkey.run_key(spec, generation)
    doc = schema.new_run_doc(
        run_key=key,
        generation=generation,
        spec_path=spec,
        workstation_id="0123456789abcdef0123456789abcdef",
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=_NOW,
        identity_scheme=identity_scheme,
    )
    doc.update(overrides)
    return doc


def test_run_gate_dir_is_assertions_slash_run_key(tmp_path):
    key = runkey.run_key("docs/specs/alpha.md")
    assert paths.run_gate_dir(str(tmp_path), key) == str(tmp_path / "assertions" / key)


def test_run_key_mode_ignores_ambient_files_and_environment(tmp_path, monkeypatch):
    spec = "docs/specs/alpha.md"
    doc = _run_doc(spec)
    key = doc["run_key"]
    _write(tmp_path, ".conductor/run_branch", "conductor/run-hijacked\n")
    _write(tmp_path, ".conductor/goal.md", "docs/specs/gamma.md\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked")
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("CONDUCTOR_MANIFEST", str(tmp_path / "elsewhere" / "manifest.yaml"))
    monkeypatch.setenv("CONDUCTOR_FREEZE_BASELINE", str(tmp_path / "elsewhere" / ".frozen"))
    res = paths.resolve_gate(str(tmp_path), run_key=key, run=doc)
    assert res.source == "run_key"
    assert res.slug == key
    assert res.directory == str(tmp_path / "assertions" / key)
    assert res.manifest == str(tmp_path / "assertions" / key / "manifest.yaml")
    assert res.baseline == str(tmp_path / "assertions" / key / ".frozen")
    assert res.run_dir == str(tmp_path / "assertions" / key / "run")
    assert res.fail_closed is None


def test_two_run_keys_resolve_to_distinct_gates(tmp_path):
    alpha, beta = _run_doc("docs/specs/alpha.md"), _run_doc("docs/specs/beta.md")
    first = paths.resolve_gate(str(tmp_path), run_key=alpha["run_key"], run=alpha)
    second = paths.resolve_gate(str(tmp_path), run_key=beta["run_key"], run=beta)
    assert first.directory != second.directory
    assert first.manifest != second.manifest
    assert first.baseline != second.baseline
    assert first.run_dir != second.run_dir


def test_a_later_generation_gets_its_own_gate(tmp_path):
    first = _run_doc("docs/specs/alpha.md", generation=1)
    second = _run_doc("docs/specs/alpha.md", generation=2)
    assert paths.resolve_gate(str(tmp_path), run_key=first["run_key"], run=first).directory != (
        paths.resolve_gate(str(tmp_path), run_key=second["run_key"], run=second).directory
    )


def test_gate_dir_disagreeing_with_the_run_key_fails_closed(tmp_path):
    doc = _run_doc("docs/specs/alpha.md", gate_dir="assertions/some-other-run")
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "gate_dir" in res.fail_closed
    assert "run.json" in res.fail_closed


def test_integration_branch_disagreeing_with_the_run_key_fails_closed(tmp_path):
    doc = _run_doc("docs/specs/alpha.md", integration_branch="conductor/run-something-else")
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "integration_branch" in res.fail_closed


def test_an_unsafe_recorded_gate_dir_never_becomes_a_path(tmp_path):
    doc = _run_doc("docs/specs/alpha.md")
    doc["gate_dir"] = "assertions/../../outside"
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed
    assert "outside" not in res.directory


def test_a_legacy_slug_run_keeps_its_recorded_names(tmp_path):
    doc = _run_doc("docs/specs/alpha.md", identity_scheme="legacy-slug-v1")
    doc["gate_dir"] = "assertions/self-enforcement"
    doc["integration_branch"] = "conductor/run-self-enforcement"
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed is None
    assert res.directory == str(tmp_path / "assertions" / "self-enforcement")


def test_run_key_mode_requires_the_run_document(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        paths.resolve_gate(str(tmp_path), run_key="alpha-1a2b3c4d")
    assert "runstate.load" in str(excinfo.value)


def test_gate_for_run_loads_the_document_and_resolves(git_repo):
    root = str(git_repo)
    doc = _run_doc("docs/specs/alpha.md")
    runstate.create(core_resolve.state_root(root), doc["run_key"], doc)
    res = core_resolve.resolve(run_key=doc["run_key"], start=root)
    gate = core_resolve.gate_for_run(res)
    assert gate.source == "run_key"
    assert gate.directory == os.path.join(res.repo_root, "assertions", doc["run_key"])


def test_legacy_mode_is_unchanged_when_no_run_key_is_given(tmp_path):
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    _write(tmp_path, ".conductor/goal.md", "docs/specs/alpha.md\n")
    _write(tmp_path, "assertions/alpha/manifest.yaml", "assertions: []\n")
    res = paths.resolve_gate(str(tmp_path))
    assert res.source == "run_branch"
    assert res.directory == str(tmp_path / "assertions" / "alpha")
```

No new fixture is needed: `git_repo` comes from `tests/conductor/conftest.py`, created in Task 9,
which covers this directory.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/conductor/test_gate_paths.py -q`
Expected: FAIL — `AttributeError: module 'conductor.paths' has no attribute 'run_gate_dir'` and `TypeError: resolve_gate() got an unexpected keyword argument 'run_key'`

- [ ] **Step 3: Add `run_gate_dir` to `conductor/paths.py`**

Insert after `has_namespaced_frozen_gate` (`conductor/paths.py:122-127`):

```python
def run_gate_dir(repo_root: str, run_key: str) -> str:
    """The done-gate directory a run key names: ``<repo>/assertions/<run-key>``.

    The run key is the SINGLE source shared by the run's integration-branch suffix
    (``conductor/run-<run-key>``) and this directory, so the two cannot diverge. ``resolve_gate``
    verifies that equality against ``run.json`` rather than re-deriving it from ambient files."""
    return os.path.join(repo_root, "assertions", run_key)
```

- [ ] **Step 4: Add the run-key mode to `resolve_gate`**

Change the signature at `conductor/paths.py:144` and insert the key-mode branch as the first decision, before the existing `env_dir` precedence:

```python
def resolve_gate(
    repo_root: str | None = None,
    *,
    run_key: str | None = None,
    run: dict | None = None,
) -> GateResolution:
    """THE gate-resolution policy — the single decision function for WHERE this run's done-gate
    lives and WHETHER it is dodging a frozen gate.

    RUN-KEY MODE (``run_key`` given). The key alone determines the gate: legacy
    ``.conductor/run_branch``, legacy ``.conductor/goal.md``, and the ambient
    ``CONDUCTOR_GATE_DIR`` / ``CONDUCTOR_GATE_SLUG`` / ``CONDUCTOR_MANIFEST`` /
    ``CONDUCTOR_FREEZE_BASELINE`` variables are IGNORED rather than consulted as fallback
    (design §"Project and run identity"). ``run`` is the loaded ``run.json``; the resolver
    verifies from it that the recorded ``gate_dir`` and ``integration_branch`` agree with the
    key, so a hand-edited or half-migrated record fails closed instead of validating some other
    run's already-green gate. A ``legacy-slug-v1`` run keeps the names migration recorded.

    LEGACY MODE (no ``run_key``) is unchanged; see the precedence and §5 ambient-dodge rules
    below. Plan 03 retires it once every run is migrated.
    """
    root = repo_root or project_root()
    if run_key is not None:
        return _resolve_gate_by_run_key(root, run_key, run)
    flat = os.path.join(root, "assertions")
    # ... existing body from `env_dir = os.environ.get("CONDUCTOR_GATE_DIR")` onward, unchanged
```

Then add the helper immediately above `resolve_gate`:

```python
def _resolve_gate_by_run_key(
    root: str, run_key: str, run: dict | None
) -> GateResolution:
    """Gate resolution when the invocation carries a run key. See ``resolve_gate``."""
    if run is None:
        raise ValueError(
            "resolve_gate(run_key=...) needs the run document; load it with "
            "conductor.core.runstate.load(state_root, run_key) or call "
            "conductor.core.resolve.gate_for_run(resolution)"
        )
    recorded_dir = str(run.get("gate_dir") or "")
    recorded_branch = str(run.get("integration_branch") or "")
    scheme = run.get("identity_scheme")
    prefix = "assertions/"
    segment = recorded_dir[len(prefix) :] if recorded_dir.startswith(prefix) else ""
    fail: str | None = None
    if not segment or not _safe_slug(segment):
        fail = (
            f"run {run_key!r} records gate_dir={recorded_dir!r}, which is not "
            "'assertions/<single-safe-segment>' — repair run.json"
        )
    elif scheme == "path-hash-v2":
        # names.derived_names is THE definition of both formats — never re-write the literals
        # here. conductor/branches.py:1-15 records what happened the last time two callers each
        # derived `conductor/run-<...>` independently: they drifted.
        want_dir, want_branch = derived_names(run_key)
        if recorded_dir != want_dir:
            fail = (
                f"run {run_key!r} records gate_dir={recorded_dir!r}, expected {want_dir!r}; the "
                "run key is the single source of the gate dir and the integration branch — "
                "repair run.json"
            )
        elif recorded_branch != want_branch:
            fail = (
                f"run {run_key!r} records integration_branch={recorded_branch!r}, expected "
                f"{want_branch!r}; the run key is the single source of both — repair run.json"
            )
    elif scheme != "legacy-slug-v1":
        fail = (
            f"run {run_key!r} records unknown identity_scheme {scheme!r}; expected "
            "'path-hash-v2' or 'legacy-slug-v1' — repair run.json"
        )
    directory = os.path.join(root, "assertions", segment) if not fail else os.path.join(root, "assertions")
    return GateResolution(
        directory,
        os.path.join(directory, "manifest.yaml"),
        os.path.join(directory, ".frozen"),
        os.path.join(directory, "run"),
        run_key,
        "run_key",
        fail,
    )
```

Also extend the `GateResolution.source` field comment at `conductor/paths.py:140`:

```python
    source: str  # how selected: run_key|gate_dir_env|explicit_slug|run_branch|goal|flat
```

- [ ] **Step 5: Add `gate_for_run` to `conductor/core/resolve.py`**

Append to `conductor/core/resolve.py` (and add `from conductor import paths` to its imports):

```python
def gate_for_run(res: RunResolution) -> paths.GateResolution:
    """The done-gate this run owns. The one place that pairs a loaded run record with
    ``paths.resolve_gate``'s run-key mode, so no caller has to remember to pass both."""
    return paths.resolve_gate(res.repo_root, run_key=res.run_key, run=res.run)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/conductor/test_gate_paths.py tests/conductor/core -q`
Expected: PASS — the new run-key tests plus every pre-existing legacy-mode test in that file.

- [ ] **Step 7: Prove the frozen done-gate is intact**

Run: `python3 -m pytest -q assertions/self_enforcement/test_a12_skills_call_resolvers.py && ./bin/conductor gate verify`
Expected: PASS and a clean `gate verify`. A failure here is a real regression in the legacy path — fix `resolve_gate`, do not refreeze the baseline.

- [ ] **Step 8: Lint and typecheck**

Run: `ruff check conductor tests/conductor && ruff format --check conductor/paths.py conductor/core tests/conductor/test_gate_paths.py && pyright conductor/paths.py conductor/core`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add conductor/paths.py conductor/core/resolve.py tests/conductor/test_gate_paths.py
git commit -m "conductor/paths.py:122-260, conductor/core/resolve.py:160-170 — gate by run key

- run-key mode resolves assertions/<run-key> and ignores run_branch, goal.md and CONDUCTOR_GATE_*
- verifies gate_dir and integration_branch against the key from run.json; legacy-slug-v1 exempt
- resolve.gate_for_run pairs a loaded run record with the resolver; legacy mode untouched"
```

---

### Task 12: Spec-path repoint

**Files:**
- Create: `conductor/core/repoint.py`
- Test: `tests/conductor/core/test_repoint.py`

**Interfaces:**
- Consumes: `locks`, `registry`, `runstate`, `runkey`, `schema`, `transaction`.
- Produces:
  - `class RepointRefused(RuntimeError)`
  - `content_identity_matches(repo_root: str, run: dict, old_rel: str, new_rel: str) -> bool`
  - `repoint(state_root: str, *, repo_root: str, run_key: str, new_spec_path: str) -> dict` — returns the updated run document.

**Contract (design line 178):** runs only without a live owner; acquires `project.lock`, `owner.lock`, then `state.lock`; verifies that the old and new paths describe the same Git rename or approved digest; rejects mapping collisions; journals then applies the `project.json` and `run.json` updates so recovery completes or reverses both; retains the run key and a path-history audit.

**Locking note:** `repoint` holds `project.lock` and `state.lock` itself, so it must **not** call `registry.commit`/`registry.update`/`runstate.commit` — those take the same locks and would raise `LockOrderError` for re-entrancy. That is exactly why the transaction journal exists: `repoint` builds both after-images and writes them through `transaction`.

**Owner note:** Plan 01 treats a busy `owner.lock` as a flat refusal (`RepointRefused`). Plan 02 replaces that with lease and liveness interpretation; the acquisition order established here does not change.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/core/test_repoint.py`:

```python
"""Renaming or moving a spec within the repository never silently creates a second run
(design §"Project and run identity").

repoint-spec runs only without a live owner, acquires project.lock, owner.lock, then state.lock,
verifies that the old and new paths describe the same Git rename or approved digest, rejects
mapping collisions, and journals then applies the project.json and run.json updates so recovery
completes or reverses both while retaining the run key and a path-history audit."""

from __future__ import annotations

import hashlib
import os

import pytest

from conductor.core import (
    locks,
    registry,
    repoint,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
)

WORKSTATION = "0123456789abcdef0123456789abcdef"
NOW = "2026-08-10T12:00:00+00:00"
ALPHA = "docs/specs/alpha.md"
MOVED = "docs/specs/archive/alpha.md"


@pytest.fixture
def project(git_repo, git):
    root = str(git_repo)
    state_root = resolve.state_root(root)
    registry.init(
        state_root, workstation_id=WORKSTATION, repo_identity=resolve.repo_identity(root)
    )
    key = runkey.run_key(ALPHA)
    digest = hashlib.sha256((git_repo / ALPHA).read_bytes()).hexdigest()
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path=ALPHA,
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{key}",
            gate_dir=f"assertions/{key}",
            spec_digest=digest,
            now=NOW,
        ),
    )
    registry.update(
        state_root, lambda d: registry.register(d, spec=ALPHA, run_key=key, generation=1)
    )
    return root, state_root, key


def _move(git_repo, git, old_rel, new_rel):
    os.makedirs(os.path.dirname(str(git_repo / new_rel)), exist_ok=True)
    git(git_repo, "mv", old_rel, new_rel)


def test_repoint_keeps_the_run_key_and_records_the_path_history(project, git_repo, git):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["run_key"] == key
    assert doc["spec_path"] == MOVED
    assert doc["path_history"] == [ALPHA]
    registry_doc = registry.load(state_root)
    assert registry.current_run_key(registry_doc, MOVED) == key
    assert ALPHA not in registry_doc["specs"]
    assert registry_doc["specs"][MOVED]["path_history"] == [ALPHA]


def test_the_gate_dir_and_integration_branch_are_untouched_by_a_repoint(project, git_repo, git):
    root, state_root, key = project
    before = runstate.load(state_root, key)
    _move(git_repo, git, ALPHA, MOVED)
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["gate_dir"] == before["gate_dir"]
    assert doc["integration_branch"] == before["integration_branch"]


def test_a_digest_match_authorizes_a_repoint_without_a_staged_rename(project, git_repo):
    root, state_root, key = project
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / MOVED).write_bytes((git_repo / ALPHA).read_bytes())
    (git_repo / ALPHA).unlink()
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert doc["spec_path"] == MOVED


def test_an_unrelated_target_is_refused(project, git_repo, git):
    root, state_root, key = project
    (git_repo / "docs" / "specs" / "unrelated.md").write_text("# totally different\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "unrelated")
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/unrelated.md"
        )
    assert "same" in str(excinfo.value).lower()
    assert runstate.load(state_root, key)["spec_path"] == ALPHA


def test_a_mapping_collision_is_refused(project, git_repo, git):
    root, state_root, key = project
    beta_key = runkey.run_key("docs/specs/beta.md")
    runstate.create(
        state_root,
        beta_key,
        schema.new_run_doc(
            run_key=beta_key,
            generation=1,
            spec_path="docs/specs/beta.md",
            workstation_id=WORKSTATION,
            integration_branch=f"conductor/run-{beta_key}",
            gate_dir=f"assertions/{beta_key}",
            spec_digest="b" * 64,
            now=NOW,
        ),
    )
    registry.update(
        state_root,
        lambda d: registry.register(
            d, spec="docs/specs/beta.md", run_key=beta_key, generation=1
        ),
    )
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/beta.md"
        )
    assert beta_key in str(excinfo.value)


def test_a_missing_target_file_is_refused(project):
    root, state_root, key = project
    with pytest.raises(repoint.RepointRefused) as excinfo:
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="docs/specs/ghost.md"
        )
    assert "does not exist" in str(excinfo.value)


def test_a_path_outside_the_repository_is_refused(project):
    root, state_root, key = project
    with pytest.raises(repoint.RepointRefused):
        repoint.repoint(
            state_root, repo_root=root, run_key=key, new_spec_path="../elsewhere/alpha.md"
        )


def test_a_live_owner_blocks_the_repoint(project, git_repo, git):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    holder = os.open(runstate.owner_lock_path(state_root, key), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(repoint.RepointRefused) as excinfo:
            repoint.repoint(
                state_root, repo_root=root, run_key=key, new_spec_path=MOVED, owner_timeout=0.05
            )
        assert "owner" in str(excinfo.value).lower()
    finally:
        os.close(holder)
    assert runstate.load(state_root, key)["spec_path"] == ALPHA


def test_a_crash_after_prepare_reverses_both_files(project, git_repo, git, monkeypatch):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)

    class Crash(RuntimeError):
        pass

    monkeypatch.setattr(
        repoint.transaction, "commit", lambda *_a, **_k: (_ for _ in ()).throw(Crash())
    )
    with pytest.raises(Crash):
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert transaction.recover(state_root) == ["repoint-" + key]
    assert runstate.load(state_root, key)["spec_path"] == ALPHA
    assert registry.current_run_key(registry.load(state_root), ALPHA) == key


def test_a_crash_after_commit_rolls_both_files_forward(project, git_repo, git, monkeypatch):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)

    class Crash(RuntimeError):
        pass

    monkeypatch.setattr(
        repoint.transaction, "apply", lambda *_a, **_k: (_ for _ in ()).throw(Crash())
    )
    with pytest.raises(Crash):
        repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert transaction.recover(state_root) == ["repoint-" + key]
    assert runstate.load(state_root, key)["spec_path"] == MOVED
    assert registry.current_run_key(registry.load(state_root), MOVED) == key


def test_repointing_to_the_same_path_is_a_no_op(project):
    root, state_root, key = project
    doc = repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=ALPHA)
    assert doc["path_history"] == []
    assert doc["revision"] == runstate.load(state_root, key)["revision"]


def test_repoint_takes_the_locks_in_the_documented_order(project, git_repo, git, monkeypatch):
    root, state_root, key = project
    _move(git_repo, git, ALPHA, MOVED)
    order: list[str] = []
    original = locks.hold

    def spy(path, *, kind, **kwargs):
        order.append(kind)
        return original(path, kind=kind, **kwargs)

    monkeypatch.setattr(repoint.locks, "hold", spy)
    repoint.repoint(state_root, repo_root=root, run_key=key, new_spec_path=MOVED)
    assert order == ["project", "owner", "state"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/core/test_repoint.py -q`
Expected: FAIL — `ImportError: cannot import name 'repoint' from 'conductor.core'`

- [ ] **Step 3: Write the implementation**

Create `conductor/core/repoint.py`:

```python
"""``conductor run repoint-spec`` — move a spec inside the repository without minting a new run.

The run key hashes the repository-relative spec path, so renaming a spec would otherwise derive a
different key: a second run, a second gate, a second branch, and a silently abandoned first run.
Repointing keeps the key and rewrites the mapping instead (design §"Project and run identity").

Safety comes from four checks and one journal:

* no live owner — an executing worker holds ``owner.lock``, and rewriting its spec path underneath
  it would desynchronize the goal it already loaded;
* the old and new paths must describe the same content — a staged Git rename, or a digest equal to
  the ``spec_digest`` recorded when the run was created;
* the target must not already be mapped to another run;
* both ``project.json`` and ``run.json`` change through one journalled transaction, so a crash
  completes or reverses both and never leaves a split identity.

Locks are taken here in the global order (project, owner, state) and held across the whole
sequence, so this module writes through ``transaction`` rather than ``registry.commit`` /
``runstate.commit`` — those take the same locks and would be re-entrant acquisitions.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

from conductor.core import atomic, locks, registry, runkey, runstate, schema, transaction

_GIT_TIMEOUT = 30.0


class RepointRefused(RuntimeError):
    """The repoint was refused before any write occurred."""


def _rename_detected(repo_root: str, old_rel: str, new_rel: str) -> bool:
    """Whether git sees the change as a rename from ``old_rel`` to ``new_rel``."""
    out = subprocess.run(
        ["git", "-C", repo_root, "diff", "-M", "--name-status", "HEAD", "--", old_rel, new_rel],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R") and parts[1] == old_rel and parts[2] == new_rel:
            return True
    return False


def _digest_matches(repo_root: str, run: dict, new_rel: str) -> bool:
    """Whether the file at ``new_rel`` still hashes to the run's recorded ``spec_digest``."""
    recorded = run.get("spec_digest")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        with open(os.path.join(repo_root, new_rel), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest() == recorded
    except OSError:
        return False


def content_identity_matches(repo_root: str, run: dict, old_rel: str, new_rel: str) -> bool:
    """Whether the old and new paths describe the same spec: a staged Git rename, or content
    equal to the digest approved when the run was created."""
    return _rename_detected(repo_root, old_rel, new_rel) or _digest_matches(
        repo_root, run, new_rel
    )


def repoint(
    state_root: str,
    *,
    repo_root: str,
    run_key: str,
    new_spec_path: str,
    owner_timeout: float = 5.0,
) -> dict:
    """Repoint ``run_key`` at ``new_spec_path``. Returns the updated run document."""
    try:
        new_rel = runkey.normalize_spec_path(repo_root, new_spec_path)
    except ValueError as exc:
        raise RepointRefused(f"{exc}; no write occurred") from exc
    if not os.path.isfile(os.path.join(repo_root, new_rel)):
        raise RepointRefused(
            f"{new_rel} does not exist in {repo_root}; no write occurred. Move the spec first, "
            f"then re-run: conductor run repoint-spec --run {run_key} {new_rel}"
        )
    with locks.hold(registry.lock_path(state_root), kind="project"):
        transaction.recover(state_root)
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)}; no write occurred"
            )
        found = registry.find_run(project_doc, run_key)
        if found is None:
            raise RepointRefused(
                f"run {run_key!r} is not registered under {state_root}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        old_rel, _generation_entry = found
        try:
            owner_ctx = locks.hold(
                runstate.owner_lock_path(state_root, run_key),
                kind="owner",
                run_key=run_key,
                timeout=owner_timeout,
            )
            owner_ctx.__enter__()
        except locks.LockTimeout as exc:
            raise RepointRefused(
                f"run {run_key!r} has a live owner holding "
                f"{runstate.owner_lock_path(state_root, run_key)}; no write occurred. "
                f"Stop the worker or wait for it to exit, then retry: "
                f"conductor run repoint-spec --run {run_key} {new_rel}"
            ) from exc
        try:
            with locks.hold(
                runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key
            ):
                run_doc = runstate.load(state_root, run_key)
                if run_doc is None:
                    raise RepointRefused(
                        f"no run record at {runstate.run_path(state_root, run_key)}; "
                        "no write occurred"
                    )
                if old_rel == new_rel:
                    return run_doc
                collision = registry.current_run_key(project_doc, new_rel)
                if collision is not None and collision != run_key:
                    raise RepointRefused(
                        f"{new_rel} is already mapped to run {collision!r}; no write occurred. "
                        f"Inspect it with: conductor run show --run {collision}"
                    )
                if not content_identity_matches(repo_root, run_doc, old_rel, new_rel):
                    raise RepointRefused(
                        f"{old_rel} and {new_rel} are not the same spec: git records no rename "
                        f"between them and {new_rel} does not match the digest approved for run "
                        f"{run_key!r}; no write occurred. Stage the rename "
                        f"(git mv {old_rel} {new_rel}) or start a new run for the new spec."
                    )
                new_project = schema.clone(project_doc)
                mapping = new_project["specs"].pop(old_rel)
                mapping.setdefault("path_history", [])
                mapping["path_history"] = [*mapping["path_history"], old_rel]
                new_project["specs"][new_rel] = mapping
                new_project["revision"] = project_doc["revision"] + 1
                schema.validate_project(new_project)

                new_run = schema.clone(run_doc)
                new_run["spec_path"] = new_rel
                new_run["path_history"] = [*new_run.get("path_history", []), old_rel]
                new_run["revision"] = run_doc["revision"] + 1
                schema.validate_run(new_run)

                txn_id = f"repoint-{run_key}"
                transaction.prepare(
                    state_root,
                    txn_id,
                    [
                        {
                            "path": registry.registry_path(state_root),
                            "before": project_doc,
                            "after": new_project,
                        },
                        {
                            "path": runstate.run_path(state_root, run_key),
                            "before": run_doc,
                            "after": new_run,
                        },
                    ],
                )
                transaction.commit(state_root, txn_id)
                transaction.apply(state_root, txn_id)
                return new_run
        finally:
            owner_ctx.__exit__(None, None, None)
```

> **Note for the implementer:** `atomic` is imported above but only used indirectly through
> `transaction`. Drop the import if ruff flags it (`F401`) — the transaction module owns the
> writes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/core/test_repoint.py -q`
Expected: PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check conductor/core tests/conductor/core && ruff format --check conductor/core tests/conductor/core && pyright conductor/core`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add conductor/core/repoint.py tests/conductor/core/test_repoint.py
git commit -m "conductor/core/repoint.py:1-215 — move a spec without minting a second run

- project -> owner -> state lock order; a live owner, a collision or an unrelated target refuses
- rename or approved-digest identity check; project.json and run.json move in one transaction
- run key, gate dir and integration branch survive; both files gain a path-history audit"
```

---

### Task 13: The `conductor run` CLI verb group

**Files:**
- Create: `conductor/run_cmd.py`
- Modify: `bin/conductor` (add the `run` verb, `--run` on `gate-dir`, and the usage text)
- Test: `tests/conductor/test_run_cmd.py`

**Interfaces:**
- Consumes: every `conductor.core` module plus `conductor.paths`.
- Produces the CLI surface:

```
conductor run new <spec.md> [--new-run] [--project <root>]
conductor run list [--all] [--json] [--project <root>]
conductor run show --run <run-key> [--project <root>]
conductor run resolve [--run <run-key>] [--project <root>]
conductor run gate-dir [--run <run-key>] [--project <root>]
conductor run repoint-spec --run <run-key> <new-relative-path> [--project <root>]
```

- Exit codes: `0` success, `1` refusal or failure, `2` ambiguous run (several active, no `--run`), `3` no such run / no active run, `64` usage.

**Scope boundary:** `run new` creates the registry entry, the run directory, and `run.json`. It does **not** create branches or worktrees (Plan 06), install a schedule (Plan 05), or record hosts (Plan 04). `/conductor:start` composes those once those plans land.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/test_run_cmd.py`:

```python
"""The `conductor run` verb group.

Every scheduled or non-interactive invocation carries an explicit run key; a bare command is
allowed only when exactly one active run exists, and otherwise fails with the available keys and
the exact commands (design §"Project and run identity")."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conductor import run_cmd
from conductor.core import registry, resolve, runkey, runstate

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")

# The git_env / git / git_repo fixtures come from tests/conductor/conftest.py (Task 9), which
# covers this directory. Do not add `pytest_plugins` — pytest only honours it in the rootdir
# conftest, and a nested reference is deprecated.


def _run(root, *args):
    return run_cmd.main([*args, "--project", str(root)])


def test_new_creates_the_registry_the_run_dir_and_run_json(git_repo, capsys):
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 0
    key = capsys.readouterr().out.strip()
    assert key == runkey.run_key("docs/specs/alpha.md")
    state_root = resolve.state_root(str(git_repo))
    doc = runstate.load(state_root, key)
    assert doc["spec_path"] == "docs/specs/alpha.md"
    assert doc["status"] == "active"
    assert doc["integration_branch"] == f"conductor/run-{key}"
    assert doc["gate_dir"] == f"assertions/{key}"
    assert doc["spec_digest"] == run_cmd.spec_digest(str(git_repo), "docs/specs/alpha.md")
    assert registry.current_run_key(registry.load(state_root), "docs/specs/alpha.md") == key


def test_new_establishes_the_local_exclude(git_repo):
    _run(git_repo, "new", "docs/specs/alpha.md")
    from conductor.core import hygiene

    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")


def test_new_refuses_when_run_state_is_already_tracked(git_repo, git, capsys):
    (git_repo / ".conductor").mkdir()
    (git_repo / ".conductor" / "goal.md").write_text("goal\n")
    git(git_repo, "add", "-f", ".conductor/goal.md")
    git(git_repo, "commit", "-qm", "oops")
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    assert "rm -r --cached" in capsys.readouterr().err


def test_new_twice_for_the_same_spec_refuses_and_names_the_existing_run(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    err = capsys.readouterr().err
    assert runkey.run_key("docs/specs/alpha.md") in err
    assert "--new-run" in err


def test_new_run_after_a_terminal_generation_creates_generation_two(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    first = runkey.run_key("docs/specs/alpha.md")
    runstate.set_status(state_root, first, "awaiting-team-merge")
    runstate.set_status(state_root, first, "terminal")
    registry.update(state_root, lambda d: registry.mirror_status(d, first, "terminal"))
    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 0
    assert capsys.readouterr().out.strip() == f"{first}-g2"


def test_new_refuses_a_spec_whose_content_already_belongs_to_a_run(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / "docs" / "specs" / "archive" / "alpha.md").write_bytes(
        (git_repo / "docs" / "specs" / "alpha.md").read_bytes()
    )
    assert _run(git_repo, "new", "docs/specs/archive/alpha.md") == 1
    err = capsys.readouterr().err
    assert "conductor run repoint-spec" in err
    assert runkey.run_key("docs/specs/alpha.md") in err


def test_list_shows_active_runs_and_all_shows_history(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    _run(git_repo, "new", "docs/specs/beta.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    beta = runkey.run_key("docs/specs/beta.md")
    runstate.set_status(state_root, beta, "awaiting-team-merge")
    runstate.set_status(state_root, beta, "terminal")
    _run(git_repo, "list")
    active = capsys.readouterr().out
    assert runkey.run_key("docs/specs/alpha.md") in active and beta not in active
    _run(git_repo, "list", "--all")
    assert beta in capsys.readouterr().out


def test_list_json_is_machine_readable(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    _run(git_repo, "list", "--json")
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["run_key"] == runkey.run_key("docs/specs/alpha.md")
    assert payload[0]["status"] == "active"
    assert payload[0]["spec_path"] == "docs/specs/alpha.md"


def test_show_prints_the_run_record(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "show", "--run", key) == 0
    assert json.loads(capsys.readouterr().out)["run_key"] == key


def test_resolve_without_a_key_works_with_one_active_run(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "resolve") == 0
    assert capsys.readouterr().out.strip() == key


def test_resolve_with_two_active_runs_exits_2_and_lists_both_commands(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    _run(git_repo, "new", "docs/specs/beta.md")
    capsys.readouterr()
    assert _run(git_repo, "resolve") == 2
    err = capsys.readouterr().err
    for spec in ("docs/specs/alpha.md", "docs/specs/beta.md"):
        assert f"--run {runkey.run_key(spec)}" in err


def test_resolve_with_no_active_run_exits_3(git_repo, capsys):
    assert _run(git_repo, "resolve") == 3
    assert "conductor run new" in capsys.readouterr().err


def test_gate_dir_prints_the_run_scoped_directory(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "gate-dir", "--run", key) == 0
    assert capsys.readouterr().out.strip() == os.path.join(
        os.path.realpath(str(git_repo)), "assertions", key
    )


def test_gate_dir_fails_closed_when_run_json_disagrees_with_the_key(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    state_root = resolve.state_root(str(git_repo))
    runstate.update(state_root, key, lambda d: {**d, "gate_dir": "assertions/some-other-run"})
    assert _run(git_repo, "gate-dir", "--run", key) == 1
    assert "gate_dir" in capsys.readouterr().err


def test_repoint_spec_moves_the_mapping(git_repo, git, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    git(git_repo, "mv", "docs/specs/alpha.md", "docs/specs/archive/alpha.md")
    assert _run(git_repo, "repoint-spec", "--run", key, "docs/specs/archive/alpha.md") == 0
    state_root = resolve.state_root(str(git_repo))
    assert runstate.load(state_root, key)["spec_path"] == "docs/specs/archive/alpha.md"


def test_unknown_subcommand_is_a_usage_error(git_repo):
    assert _run(git_repo, "frobnicate") == 64


def test_the_bin_wrapper_dispatches_the_run_verb(git_repo):
    out = subprocess.run(
        [CONDUCTOR, "run", "new", "docs/specs/alpha.md"],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env={**os.environ, "CONDUCTOR_HOME": str(git_repo)},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == runkey.run_key("docs/specs/alpha.md")


def test_the_bin_wrapper_routes_gate_dir_run_to_the_resolver(git_repo):
    env = {**os.environ, "CONDUCTOR_HOME": str(git_repo)}
    key = subprocess.run(
        [CONDUCTOR, "run", "new", "docs/specs/alpha.md"],
        capture_output=True, text=True, cwd=str(git_repo), env=env, timeout=60,
    ).stdout.strip()
    out = subprocess.run(
        [CONDUCTOR, "gate-dir", "--run", key],
        capture_output=True, text=True, cwd=str(git_repo), env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith(f"assertions/{key}")


def test_the_bin_wrapper_keeps_the_legacy_gate_dir_form(git_repo):
    out = subprocess.run(
        [CONDUCTOR, "gate-dir", "docs/specs/alpha.md"],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env={**os.environ, "CONDUCTOR_HOME": str(git_repo)},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("/assertions/alpha")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/test_run_cmd.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.run_cmd'`

- [ ] **Step 3: Write the CLI module**

Create `conductor/run_cmd.py`:

```python
"""``conductor run`` — create, list, inspect, resolve and repoint runs.

This is the operator-facing and skill-facing surface over ``conductor.core``. Everything a later
plan needs to name a run goes through here, so the disambiguation rule is stated once: every
scheduled or non-interactive invocation carries an explicit run key, and a bare command is allowed
only when exactly one active run exists.

SCOPE: ``run new`` creates registry state, the run directory and ``run.json``. It does not create
branches or worktrees, install a schedule, or record hosts — those belong to the branch/PR,
heartbeat and adapter plans respectively, and ``/conductor:start`` composes them.

Exit codes: 0 success, 1 refusal/failure, 2 ambiguous run, 3 no such run / no active run,
64 usage.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

from conductor import paths
from conductor.core import (
    hygiene,
    names,
    registry,
    repoint,
    resolve,
    runkey,
    runstate,
    schema,
    workstation,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 2
EXIT_NO_RUN = 3
EXIT_USAGE = 64


def spec_digest(repo_root: str, relative: str) -> str:
    """The sha256 of a spec's bytes — the identity a later repoint checks against."""
    with open(os.path.join(repo_root, relative), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _matching_run(state_root: str, project_doc: dict, digest: str) -> str | None:
    """An existing run whose recorded spec digest equals ``digest``."""
    for key in registry.run_keys(project_doc):
        run = runstate.load(state_root, key)
        if run is not None and run.get("spec_digest") == digest:
            return key
    return None


def cmd_new(args: argparse.Namespace) -> int:
    root = resolve.repo_root(args.project)
    hygiene.assert_state_paths_untracked(root)
    hygiene.ensure_local_exclude(root)
    state_root = os.path.join(root, ".conductor")
    relative = runkey.normalize_spec_path(root, args.spec)
    if not os.path.isfile(os.path.join(root, relative)):
        print(f"{relative} does not exist in {root}; no write occurred", file=sys.stderr)
        return EXIT_FAIL
    registry.init(
        state_root,
        workstation_id=workstation.workstation_id(),
        repo_identity=resolve.repo_identity(root),
    )
    project_doc = registry.load(state_root)
    assert project_doc is not None  # init guarantees it
    existing = registry.current_run_key(project_doc, relative)
    if existing is not None:
        print(
            f"{relative} already has the active run {existing!r}; no write occurred.\n"
            f"  Inspect it:      conductor run show --run {existing}\n"
            f"  Start a new one: finish or fail {existing}, then "
            f"conductor run new {relative} --new-run",
            file=sys.stderr,
        )
        return EXIT_FAIL
    mapped = registry.mapping(project_doc, relative)
    if mapped is not None and not args.new_run:
        print(
            f"{relative} has {len(mapped['generations'])} completed generation(s); no write "
            f"occurred.\n  Start the next one with: conductor run new {relative} --new-run",
            file=sys.stderr,
        )
        return EXIT_FAIL
    digest = spec_digest(root, relative)
    if mapped is None:
        twin = _matching_run(state_root, project_doc, digest)
        if twin is not None:
            print(
                f"{relative} is byte-identical to the spec of run {twin!r}, which is mapped to a "
                f"different path; no write occurred. This is a move, not a new run:\n"
                f"  conductor run repoint-spec --run {twin} {relative}",
                file=sys.stderr,
            )
            return EXIT_FAIL
    generation = registry.next_generation(project_doc, relative)
    key = runkey.run_key(relative, generation)
    derived = names.derived_names(key)  # THE definition of both formats; never inline them here
    runstate.create(
        state_root,
        key,
        schema.new_run_doc(
            run_key=key,
            generation=generation,
            spec_path=relative,
            workstation_id=project_doc["workstation_id"],
            integration_branch=derived.integration_branch,
            gate_dir=derived.gate_dir,
            spec_digest=digest,
            now=_now(),
        ),
    )
    registry.update(
        state_root,
        lambda doc: registry.register(doc, spec=relative, run_key=key, generation=generation),
    )
    print(key)
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    state_root = resolve.state_root(args.project)
    project_doc = registry.load(state_root)
    if project_doc is None:
        if args.json:
            print("[]")
        return EXIT_OK
    active = set(resolve.active_run_keys(state_root))
    rows = []
    for key in registry.run_keys(project_doc):
        run = runstate.load(state_root, key)
        if run is None:
            continue
        if not args.all and key not in active:
            continue
        rows.append(
            {
                "run_key": key,
                "generation": run["generation"],
                "status": run["status"],
                "spec_path": run["spec_path"],
                "integration_branch": run["integration_branch"],
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return EXIT_OK
    if not rows:
        print("no runs" if args.all else "no active runs")
        return EXIT_OK
    width = max(len(r["run_key"]) for r in rows)
    for row in rows:
        print(f"{row['run_key']:<{width}}  {row['status']:<19}  {row['spec_path']}")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    print(json.dumps(resolution.run, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_resolve(args: argparse.Namespace) -> int:
    print(resolve.resolve(run_key=args.run, start=args.project).run_key)
    return EXIT_OK


def cmd_gate_dir(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    gate = resolve.gate_for_run(resolution)
    if gate.fail_closed:
        print(
            f"run {resolution.run_key}: {gate.fail_closed}; no write occurred. "
            f"Inspect it with: conductor run show --run {resolution.run_key}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(gate.directory)
    return EXIT_OK


def cmd_repoint_spec(args: argparse.Namespace) -> int:
    root = resolve.repo_root(args.project)
    doc = repoint.repoint(
        os.path.join(root, ".conductor"),
        repo_root=root,
        run_key=args.run,
        new_spec_path=args.new_path,
    )
    print(f"{doc['run_key']} -> {doc['spec_path']}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor run", description="Create, inspect and repoint conductor runs."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="any path inside the repo (default: $CONDUCTOR_HOME, else the current directory)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="create a run for a spec")
    new.add_argument("spec", help="path to the spec, absolute or repository-relative")
    new.add_argument(
        "--new-run",
        action="store_true",
        help="start the next generation for a spec whose generations have all ended",
    )
    new.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    listing = sub.add_parser("list", help="list runs")
    listing.add_argument("--all", action="store_true", help="include inactive runs")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    show = sub.add_parser("show", help="print a run record")
    show.add_argument("--run", required=True, help="run key")
    show.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    resolving = sub.add_parser("resolve", help="print the run key this invocation means")
    resolving.add_argument("--run", default=None, help="run key (optional)")
    resolving.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    gate = sub.add_parser("gate-dir", help="print a run's done-gate directory")
    gate.add_argument("--run", default=None, help="run key (optional)")
    gate.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    move = sub.add_parser("repoint-spec", help="point a run at a moved spec")
    move.add_argument("--run", required=True, help="run key")
    move.add_argument("new_path", help="the spec's new repository-relative path")
    move.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser
```

> **`argparse.SUPPRESS` is load-bearing, not decoration.** `--project` is accepted both before
> and after the subcommand. With `default=None` on the subparser, argparse writes that `None`
> over a value already parsed by the top-level parser, so `conductor run --project /repo new
> spec.md` would silently fall back to the current directory. `SUPPRESS` makes the subparser set
> the attribute only when the flag is actually present, so both orders work. Verified:
>
> ```
> before-subcmd, sub default=None : None      <-- the bug
> before-subcmd, sub SUPPRESS     : /repo
> after-subcmd,  sub SUPPRESS     : /repo
> neither,       sub SUPPRESS     : None
> ```

Continuing `conductor/run_cmd.py`:

```python
_HANDLERS = {
    "new": cmd_new,
    "list": cmd_list,
    "show": cmd_show,
    "resolve": cmd_resolve,
    "gate-dir": cmd_gate_dir,
    "repoint-spec": cmd_repoint_spec,
}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        return _HANDLERS[args.cmd](args)
    except resolve.RunAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AMBIGUOUS
    except resolve.RunNotFound as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_RUN
    except (
        hygiene.TrackedStateError,
        registry.RegistryMissing,
        registry.RevisionConflict,
        repoint.RepointRefused,
        runstate.RunExists,
        runstate.RunMissing,
        runstate.RevisionConflict,
        schema.SchemaError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except subprocess.CalledProcessError as exc:
        print(
            f"git failed while resolving the project for {args.cmd}: "
            f"{(exc.stderr or '').strip() or exc}",
            file=sys.stderr,
        )
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
```

> **Note:** `paths` is imported for the module docstring's sake only if unused — drop the import
> if ruff flags `F401`; `resolve.gate_for_run` already owns the gate lookup.

- [ ] **Step 4: Wire the `run` verb into `bin/conductor`**

In `bin/conductor`, add a `run)` case immediately before the existing `gate-dir)` case (`bin/conductor:42`):

```bash
  run) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.run_cmd "$@" ;;
```

Then let `gate-dir` accept a run key by inserting this immediately after `gate-dir) shift` and before the comment block:

```bash
    # A run key resolves the gate through conductor.core (run.json is authoritative and ambient
    # run_branch/goal.md/CONDUCTOR_GATE_* are ignored). The legacy spec-path form below stays for
    # un-migrated repos until the migration plan retires it.
    if [ "${1:-}" = "--run" ]; then
      PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.run_cmd gate-dir "$@"
    fi
```

Finally extend the usage text at `bin/conductor:67` by inserting these lines before `  conductor gate {lint|freeze|verify}\n`:

```
  conductor run new <spec.md> [--new-run]\n  conductor run list [--all] [--json]\n  conductor run show --run <run-key>\n  conductor run resolve [--run <run-key>]\n  conductor run gate-dir [--run <run-key>]\n  conductor run repoint-spec --run <run-key> <new-path>\n
```

and change the existing `gate-dir` usage line to:

```
  conductor gate-dir <spec.md> | --run <run-key>    (the run's done-gate dir)\n
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/conductor/test_run_cmd.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole suite and the frozen gate**

Run: `pytest -q && ./bin/conductor gate verify`
Expected: the pre-existing 565 passed / 1 skipped plus this plan's new tests, and a clean gate.

- [ ] **Step 7: Lint and typecheck**

Run: `ruff check conductor tests && ruff format --check conductor/run_cmd.py conductor/core tests/conductor/core tests/conductor/test_run_cmd.py && pyright conductor`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add conductor/run_cmd.py bin/conductor tests/conductor/test_run_cmd.py
git commit -m "conductor/run_cmd.py:1-330, bin/conductor:42-70 — the conductor run verb group

- new/list/show/resolve/gate-dir/repoint-spec; exit 2 ambiguous, 3 no run, 64 usage
- new refuses tracked run state, a live run for the spec, and a byte-identical spec at a new path
- bin/conductor gains 'run' and routes 'gate-dir --run' to the resolver; legacy form unchanged"
```

---

### Task 14: Two concurrent runs in one repository

**Files:**
- Test: `tests/conductor/test_multi_run_isolation.py`

**Interfaces:**
- Consumes: everything above. Produces no new module — this is the end-to-end proof of the plan's central claim.

**Why a separate task:** design line 486 asks for exactly this: *two simultaneous run keys resolving distinct goals, gates, manifests, baselines, and results despite conflicting legacy files or environment variables.* The unit tests each prove one component; this proves they compose.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/test_multi_run_isolation.py`:

```python
"""Two specs conducted in one repository (design §"Unit and contract tests").

Two simultaneous run keys must resolve distinct goals, gates, manifests, baselines and results —
and must keep doing so while conflicting legacy files and ambient gate environment variables are
present, because that is exactly the state a half-migrated repository is in."""

from __future__ import annotations

import json
import os

import pytest

from conductor import run_cmd
from conductor.core import registry, resolve, runkey, runstate

# The git_env / git / git_repo fixtures come from tests/conductor/conftest.py (Task 9), which
# covers this directory. Do not add `pytest_plugins` — pytest only honours it in the rootdir
# conftest, and a nested reference is deprecated.

ALPHA = "docs/specs/alpha.md"
BETA = "docs/specs/beta.md"


@pytest.fixture
def two_runs(git_repo, capsys):
    assert run_cmd.main(["new", ALPHA, "--project", str(git_repo)]) == 0
    alpha = capsys.readouterr().out.strip()
    assert run_cmd.main(["new", BETA, "--project", str(git_repo)]) == 0
    beta = capsys.readouterr().out.strip()
    return str(git_repo), resolve.state_root(str(git_repo)), alpha, beta


def test_the_two_runs_have_distinct_keys_state_dirs_and_locks(two_runs):
    _root, state_root, alpha, beta = two_runs
    assert alpha != beta
    assert runstate.run_dir(state_root, alpha) != runstate.run_dir(state_root, beta)
    assert runstate.state_lock_path(state_root, alpha) != runstate.state_lock_path(state_root, beta)
    assert runstate.owner_lock_path(state_root, alpha) != runstate.owner_lock_path(state_root, beta)


def test_each_run_resolves_its_own_spec_branch_and_gate(two_runs):
    root, _state_root, alpha, beta = two_runs
    first = resolve.resolve(run_key=alpha, start=root)
    second = resolve.resolve(run_key=beta, start=root)
    assert first.run["spec_path"] == ALPHA and second.run["spec_path"] == BETA
    assert first.run["integration_branch"] == f"conductor/run-{alpha}"
    assert second.run["integration_branch"] == f"conductor/run-{beta}"
    gate_a, gate_b = resolve.gate_for_run(first), resolve.gate_for_run(second)
    assert gate_a.directory != gate_b.directory
    assert gate_a.manifest != gate_b.manifest
    assert gate_a.baseline != gate_b.baseline
    assert gate_a.run_dir != gate_b.run_dir
    assert gate_a.fail_closed is None and gate_b.fail_closed is None


def test_conflicting_legacy_files_and_environment_do_not_leak_into_either_run(
    two_runs, monkeypatch
):
    root, _state_root, alpha, beta = two_runs
    legacy = os.path.join(root, ".conductor")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "run_branch"), "w", encoding="utf-8") as fh:
        fh.write("conductor/run-hijacked\n")
    with open(os.path.join(legacy, "goal.md"), "w", encoding="utf-8") as fh:
        fh.write("docs/specs/gamma.md\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked")
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", os.path.join(root, "assertions", "hijacked"))
    monkeypatch.setenv("CONDUCTOR_MANIFEST", os.path.join(root, "assertions", "hijacked", "m.yaml"))
    monkeypatch.setenv("CONDUCTOR_FREEZE_BASELINE", os.path.join(root, "assertions", "hijacked", ".frozen"))
    for key in (alpha, beta):
        gate = resolve.gate_for_run(resolve.resolve(run_key=key, start=root))
        assert gate.source == "run_key"
        assert gate.directory == os.path.join(os.path.realpath(root), "assertions", key)
        assert "hijacked" not in gate.manifest and "hijacked" not in gate.baseline


def test_a_bare_command_refuses_while_both_runs_are_active(two_runs, capsys):
    root, _state_root, alpha, beta = two_runs
    assert run_cmd.main(["resolve", "--project", root]) == run_cmd.EXIT_AMBIGUOUS
    err = capsys.readouterr().err
    assert f"--run {alpha}" in err and f"--run {beta}" in err


def test_finishing_one_run_lets_the_other_resolve_bare(two_runs, capsys):
    root, state_root, alpha, beta = two_runs
    runstate.set_status(state_root, beta, "awaiting-team-merge")
    runstate.set_status(state_root, beta, "terminal")
    registry.update(state_root, lambda d: registry.mirror_status(d, beta, "terminal"))
    assert run_cmd.main(["resolve", "--project", root]) == 0
    assert capsys.readouterr().out.strip() == alpha


def test_a_state_write_to_one_run_does_not_touch_the_other(two_runs):
    _root, state_root, alpha, beta = two_runs
    before = runstate.load(state_root, beta)
    runstate.update(state_root, alpha, lambda d: {**d, "current_phase": "phase-1"})
    assert runstate.load(state_root, beta) == before
    assert runstate.load(state_root, alpha)["current_phase"] == "phase-1"


def test_repointing_one_run_leaves_the_other_mapping_intact(two_runs, git, git_repo):
    root, state_root, alpha, beta = two_runs
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    git(git_repo, "mv", ALPHA, "docs/specs/archive/alpha.md")
    assert (
        run_cmd.main(
            ["repoint-spec", "--run", alpha, "docs/specs/archive/alpha.md", "--project", root]
        )
        == 0
    )
    doc = registry.load(state_root)
    assert registry.current_run_key(doc, "docs/specs/archive/alpha.md") == alpha
    assert registry.current_run_key(doc, BETA) == beta
    assert runstate.load(state_root, beta)["spec_path"] == BETA


def test_the_registry_lists_both_runs_with_their_own_generations(two_runs, capsys):
    root, _state_root, alpha, beta = two_runs
    assert run_cmd.main(["list", "--json", "--project", root]) == 0
    rows = {row["run_key"]: row for row in json.loads(capsys.readouterr().out)}
    assert set(rows) == {alpha, beta}
    assert rows[alpha]["spec_path"] == ALPHA and rows[beta]["spec_path"] == BETA
    assert rows[alpha]["generation"] == 1 and rows[beta]["generation"] == 1


def test_generation_two_of_one_spec_coexists_with_the_other_run(two_runs, capsys):
    root, state_root, alpha, beta = two_runs
    runstate.set_status(state_root, alpha, "awaiting-team-merge")
    runstate.set_status(state_root, alpha, "terminal")
    registry.update(state_root, lambda d: registry.mirror_status(d, alpha, "terminal"))
    assert run_cmd.main(["new", ALPHA, "--new-run", "--project", root]) == 0
    second = capsys.readouterr().out.strip()
    assert second == f"{runkey.run_key(ALPHA)}-g2"
    assert sorted(resolve.active_run_keys(state_root)) == sorted([beta, second])
    gate_first = resolve.gate_for_run(resolve.resolve(run_key=alpha, start=root))
    gate_second = resolve.gate_for_run(resolve.resolve(run_key=second, start=root))
    assert gate_first.directory != gate_second.directory
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/conductor/test_multi_run_isolation.py -q`
Expected: PASS. If anything fails here it is a genuine composition bug in Tasks 1–13 — fix the module, not the test.

- [ ] **Step 3: Run the full gate**

Run: `ruff check . && pytest -q && ./bin/conductor gate verify`
Expected: `ruff check` clean, the full suite green, gate verify clean. `ruff format --check .` will still report the 11 pre-existing files this plan did not touch; confirm none of them are yours with `ruff format --check $(git diff --name-only main...HEAD -- '*.py')`.

- [ ] **Step 4: Commit**

```bash
git add tests/conductor/test_multi_run_isolation.py
git commit -m "tests/conductor/test_multi_run_isolation.py:1-165 — two runs in one repository

- distinct keys, state dirs, locks, branches, gates, manifests, baselines and results
- conflicting .conductor/run_branch, goal.md and CONDUCTOR_GATE_* leak into neither run
- a bare command refuses while both are active and resolves once one ends"
```

---

## Definition of done for this plan

- [ ] `pytest -q` green, with the pre-existing 565 passed / 1 skipped still passing.
- [ ] `./bin/conductor gate verify` clean — assertions A1–A16 unchanged and unweakened.
- [ ] `ruff check .` clean; `ruff format --check` clean on every file this plan created or modified.
- [ ] `pyright .` reports no new errors.
- [ ] `conductor run new`, `list`, `show`, `resolve`, `gate-dir`, and `repoint-spec` all work through `bin/conductor` in a scratch repository.
- [ ] No new module under `conductor/core/` contains a Claude slash command, a Codex dollar invocation, `CLAUDE_PLUGIN_ROOT`, or a host-specific permission flag. Verify: `grep -rn 'CLAUDE_PLUGIN_ROOT\|/conductor:\|\$conductor:' conductor/core/` returns nothing.
- [ ] Legacy flat-state behaviour is unchanged for callers that pass no run key.

---

## Self-review

Run against the design before handing off.

**Spec coverage.** Design §"Project and run identity" maps to tasks as follows: `project.json` contents → Task 7; run key format and generations → Task 3; project-local state layout → Tasks 7, 8; canonical state root from the Git common directory → Task 9; run key as the single source of the branch suffix and gate directory → Task 11; run-key-carrying invocations ignoring ambient state → Tasks 9, 11, 14; per-verb run-key requirement and the ambiguity error → Tasks 9, 13; tracked-path preflight and local exclude → Task 10; `resume-env` mode 0600 → carried in Global Constraints, enforced by the existing assertion A3 and consumed by Plan 05; run status vocabulary and transitions → Task 4; repeated-start reconcile → **Plan 05** (`/conductor:start`), not here; ownership transfer on start from the other host → **Plan 02**; `repoint-spec` → Task 12. §"Failure handling" atomic writes → Task 1; revision guards → Tasks 7, 8; project transactions → Task 6; lock order → Task 2.

**Deliberately deferred, with the owning plan named in-line:** owner-lock lease semantics and takeover (Plan 02), legacy migration and `identity_scheme=legacy-slug-v1` population (Plan 03 — Task 4 and Task 11 accept the scheme so Plan 03 does not have to reopen them), host fields (Plan 04), heartbeat and schedules (Plan 05), branches, worktrees and PRs (Plan 06), reviews and debt (Plan 07).

**Type consistency check.** `run_key` names the string everywhere (`runkey.run_key()` the function, `run_key=` the parameter, `doc["run_key"]` the field). `state_root` is always the `.conductor` directory, never the repository root — the repository root is `repo_root`. `registry.commit` / `runstate.commit` / `transaction.commit` are three different functions on three different modules and are always called module-qualified. `resolve_gate` returns `paths.GateResolution` in both modes; only `source` and `slug` differ.

**Two corrections to the roadmap** to apply when Plan 02 is written: `resolve.resolve` has no `require_active` parameter (an explicit key resolves a run in any status, which is what design line 174 requires), and `runstate` also exports `owner_lock_path`, so Plan 02 must not define a second copy of that path.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-10-plan-01-run-identity-registry.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
