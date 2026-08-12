# Host Adapter Layer and Preflight Floors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `conductor/hosts/{base,claude,codex}.py` — the one place Conductor knows a host by name — so that executable discovery, version floors, argv construction, launch prompts, permission postures, process liveness, hook installation, and isolated implementation dispatch each have a per-host implementation behind a single interface, verified by contract tests that run identically against fake `claude` and `codex` executables.

**Architecture:** A new `conductor/hosts/` package. `base.py` owns the `HostAdapter` protocol, the closed host and posture vocabularies, the error taxonomy, the `DispatchResult` record, the version-probe mechanics, and `load`/`opposite`. `proc.py` owns host-neutral `/proc` mechanics (start-tick minting, cmdline, cwd, pid enumeration); the *predicate* that decides "is this one of my processes" stays per-host. `claude.py` and `codex.py` each build their own argv, their own launch prompt, and their own permission projection — nothing about argument vectors is shared, ever. `cli.py` exposes a read-only `conductor host` verb group so an owner can inspect and preflight a host without a run.

**Tech Stack:** Python 3.12 standard library only (`dataclasses`, `glob`, `json`, `os`, `re`, `shutil`, `subprocess`, `time`, `typing.Protocol`), pytest, ruff, pyright. Linux `/proc` for process inspection.

**Source design:** `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md` §"System architecture" (lines 61–103), the host-floor paragraph of §"Packaging and installation" (line 365), the fake-executable prescription in §"Integration tests" (line 518), and the adapter/permission/dispatch bullets of §"Unit and contract tests" (lines 495–499).

**Verified host facts:** `docs/reviews/2026-08-12-codex-host-ground-truth.md` — Codex CLI vocabulary established by running codex-cli `0.147.0` on 2026-08-12. This plan cites that document rather than restating it. Where this plan diverges from the design, the divergence is named in §"Where this plan corrects the roadmap and the design".

**Roadmap:** `docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md` — this is Plan 04 of 11, reserved at roadmap line 296.

---

## Dependency status — read this before believing the numbering

Plan 04 depends on **Plan 01 only** (roadmap dependency table, lines 61–73). Plan 01 is **merged**: PR #84, merge commit `f3b0858`, and its residuals are recorded in `docs/reviews/2026-08-10-plan-01-residuals.md`. Every interface Plan 04 needs already exists on `main`.

The numbering misleads. Plans 02 and 03 are unwritten, and Plan 04 does **not** wait for them. The coupling runs the other way: **Plan 02's `ownership.prove_exited(record)` will call this plan's `process_alive(identity)`**, so the identity format defined in Task 7 is an interface Plan 02 consumes. Plan 02 must not invent a second identity string.

Plan 04 in fact consumes almost nothing from Plan 01 — only `conductor.core.resolve.repo_root` for the CLI's project resolution. The adapters are deliberately stateless: they take paths and return argv, prompts, environments, and booleans. That is what makes them testable without a run.

---

## The central constraint: land it unwired

**Plan 04 ships `conductor/hosts/` and a read-only `conductor host` CLI verb. It does not change a single launch path.** `conductor/driver.py` and `conductor/resume_script.py` are byte-identical after this plan. `conductor/preflight.py` is byte-identical after this plan. Plan 05 does the wiring.

The reason is not caution for its own sake. **Plan 02 (ownership and leases) is unwritten.** A Codex run launched through this adapter today would have no lease, no `owner.lock` semantics, and no takeover. Two heartbeats twenty minutes apart could launch two workers on the same run branch, and the only thing standing between them is the guard at `conductor/resume_script.py:214` — which, as Task 8 proves, does not exist at all on a Codex host. Landing the adapter unwired means nothing runs through it, so nothing can strand.

The mechanical statement of this, checked in the definition of done:

```bash
grep -n 'conductor\.hosts\|conductor/hosts' conductor/driver.py conductor/resume_script.py conductor/preflight.py
# must return nothing
```

Do not add a permanent test asserting this. It is true for exactly one plan and Plan 05 would have to delete it; a test whose correct future is deletion is a trap. The grep lives in the definition of done and in the PR description.

---

## Explicit non-goal: Codex session continuation

`codex exec resume`, `codex resume`, and `codex fork` all exist (ground truth §"Session resume — corrects the design"). The design assumed they did not, and assumed resume is purely durable-state reconciliation. That assumption was false as a statement of fact about the CLI.

**Decision: Plan 04 does not use any of them, and that is deliberate, not an oversight.** Every fire stays a cold start reconciled from durable evidence, exactly as the Claude path does. The reason is that Conductor's entire correctness story is that durable state is authoritative and the worker is disposable — Git heads, GitHub state, `results.json`, `run.json`, `handoff.md`, in that precedence (design line 290). A Codex session that remembers something the ledger does not is a second source of truth, and the class of bug where the two disagree is not one this system can detect.

No adapter method takes a session id. No adapter method returns one. `codex exec review` likewise exists but its argument contract was not verified, so Task 4 does not use it; Plan 07 may revisit it with its own ground-truth pass.

Revisiting this is a Plan 05 or later decision and requires re-deciding the reconciliation model, not just adding a flag.

---

## Scope boundary — what this plan does not touch

Named explicitly so the plan does not sprawl:

| Out of scope | Owner |
| --- | --- |
| Retiring `~/.claude/scheduled_tasks.json`; the heartbeat/cron replacement; `heartbeat.sh`; the orchestrator context contract and its two verbatim reminders; the per-fire context budget; `compaction.marker` | **Plan 05** |
| `owner.lock` lease semantics, takeover, prune, rebind, `prove_exited` | **Plan 02** |
| Legacy flat-state migration and `identity_scheme=legacy-slug-v1` | **Plan 03** |
| Branch/worktree/PR model, merge gates, sync phases, `default_branch()` fail-closed inversion | **Plan 06** |
| Reviewer routing, verdict schema, review debt, review freshness | **Plan 07** |
| `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, catalog `policy` blocks, `--sparse` bundling | **Plan 09** |

Two notes for whoever executes this:

- **`.agents/` and `.codex/` exist at the repo root, are empty, and are invisible to git.** Git does not track empty directories, so `git status --porcelain .agents/` is *also* empty — they are not reported as untracked, they simply do not appear. A step that expects to see them in `git status` will find nothing and must not conclude they are missing. Verified 2026-08-12 (ground truth §"Verification status"). Plan 04 creates no file in either directory.
- **Codex skills use the same `SKILL.md` format as Claude** (verified at `~/.codex/skills/code-review/SKILL.md`: `name` + `description` YAML frontmatter). Conductor's existing `skills/*/SKILL.md` files are reusable across both hosts with no rewrite. The Codex delta is launch, argv, result capture, and packaging — not skill content. This plan therefore adds no skill file and edits none.

---

## Global Constraints

- **Host floor:** Claude Code `2.1.224`, Codex CLI `0.147.0`. Manifests, preflight diagnostics, and installation documentation publish these minimums. *(This plan implements the preflight half.)*
- **Canonical editable checkout:** `~/programming/conductor`; the old `~/.claude/conductor` is retired. *(The mechanism changed after the roadmap was written — see §"Where this plan corrects the roadmap and the design". Plan 04 is unaffected: it never reads, writes, moves, or names the checkout root, and resolves its own source root from `__file__` or an environment override.)*
- **Plugin identity:** one public name, `conductor`. No second Codex-specific product identity.
- **Run key format:** `<spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]`. Generation 1 omits `-g<N>`.
- **Run integration branch:** `conductor/run-<run-key>`. **Phase branch:** `conductor/<run-key>/phase-<phase-id>`.
- **Run status vocabulary (exactly these six):** `active`, `checkpointed`, `blocked`, `awaiting-team-merge`, `terminal`, `failed`.
- **Review policy vocabulary (exactly these three):** `opposite-required`, `same-host-fallback-allowed`, `blocked-pending-opposite-host`.
- **Global lock order:** `migration.lock`, then `project.lock`, then `owner.lock`, then `state.lock`; multi-run operations take run locks in sorted run-key order. *(Plan 04 acquires no lock. It is stateless.)*
- **Lease defaults:** 120 second lease, renewed at least every 30 seconds. *(Plan 02.)*
- **Review freshness:** default maximum review age 24 hours. **Per-fire context budget:** default `0.60`. *(Plans 07 and 05.)*
- **Conductor never merges to the repository default branch.**
- **Every state write** uses a sibling temporary file, flush, fsync, and atomic replace. *(Plan 04's only state write is `install_hooks`, which uses `conductor.core.atomic.write_json_atomic` rather than a second implementation.)*
- **Every actionable failure reports:** run key and current state where one exists, the failed invariant or operation, the affected path, whether any write occurred, and the exact inspect/retry/recovery command.
- **Adapters launch argument vectors, never interpolated shell strings.**
- **Tooling gates:** `ruff check . && ruff format --check .`, `pyright .`, `pytest -q`. Python 3.12.

---

## Working agreements for this plan

- **Do not reformat files this plan does not touch.** `ruff format --check .` fails on 11 pre-existing files today (verified 2026-08-12: 11 would be reformatted, 101 already formatted). Run `ruff format` on **only** the files you create or modify, and verify with `ruff format --check <those files>`.
- **The done-gate is frozen.** `assertions/manifest.yaml` and `assertions/self_enforcement/` hold A1–A16 under `conductor/freeze.py`. This plan touches none of the files those assertions observe. `A12` observes `skills/start/SKILL.md` and `skills/autodev/SKILL.md`; Plan 04 edits neither. Run `./bin/conductor gate verify` at the end anyway and treat any failure as a real regression, never a baseline to refresh.
- **Linux only, and say so.** Process liveness reads `/proc`. The existing driver already does this (`conductor/resume_script.py:215` reads `/proc/$pid/cwd`), so this is not a new platform constraint — but Tasks 7 and 8 make it explicit in code and record the macOS gap as a residual rather than shipping a silent no-op.
- **Commit granularity.** One commit per task; message style is files modified with line numbers plus one or two bullets on what was done.
- **The concrete adapters must match the Protocol signature for signature.** `load()` returns `HostAdapter`; pyright checks structural conformance at each `return`. A parameter renamed in `claude.py` but not in `base.py` fails `pyright .`, not a test.

---

## The real surface — what is Claude-specific today

Plan 04's true size is larger than its roadmap entry suggests, because "no core module contains a host-specific string" is a claim about production code that is currently false in three modules. Plan 04 does not move these; it builds the destination. **Every line below was re-read and verified on 2026-08-12 at commit `9971573`.**

### `conductor/resume_script.py` — the launch path

| Line | What is there | Why it is host-specific |
| --- | --- | --- |
| `175` | `CLAUDE_BIN="$(command -v claude \|\| true)"` | resolves one host's executable by name |
| `176` | fallback to `$HOME/.local/bin/claude` | Claude 2.x standalone launcher path |
| `178` | `ls -d "$HOME"/.claude/plugins/cache/*/conductor/*/bin/conductor` | Claude's plugin cache layout; Codex has none at this path |
| `181-182` | `[ ! -x "$CLAUDE_BIN" ]` → `driver-unresolved claude=%s` | the fail-loud message names one host |
| `214` | `for pid in $(pgrep -f 'claude' ...)` | **the double-drive guard — see Task 8** |
| `234` | comment prescribing `--dangerously-skip-permissions` | Claude's bypass flag |
| `253` | `--dangerously-skip-permissions) POSTURE="full-bypass"` | Claude's bypass flag, in the posture parser |
| `334` | the same flag, in the operator nudge text | Claude's bypass flag |
| `356` | the same flag, in `_posture_of` | Claude's bypass flag |
| `261` | `"$CLAUDE_BIN" -p "/conductor:autodev" "$@"` | **the launch — one executable, one flag, one invocation syntax** |

### `conductor/driver.py` — durability evidence

| Line | What is there |
| --- | --- |
| `56` | `os.environ.get("CLAUDE_CONFIG_DIR")` |
| `57-58` | default `~/.claude` |
| `59` | `scheduled_tasks.json` — a Claude harness file; whether Codex has an analogue is *not determined* (ground truth §"Things NOT determined" item 3) |
| `67` | `_scheduled_tasks_file()` call site |
| `79` | `("tasks", "scheduled_tasks", "schedules")` — the Claude harness JSON shapes |
| `98` | `!= "/conductor:autodev"` — a hardcoded Claude slash command used as a *match predicate* |
| `169` | the same slash command inside the operator-facing failure message |

### `conductor/preflight.py` — availability, with no version floor at all

| Line | What is there |
| --- | --- |
| `15-26` | `REQUIRED_COMMANDS` — ten hardcoded `/`-prefixed Claude slash-command names |
| `33` | `.claude-plugin/plugin.json` as the only manifest shape a plugin root may have |
| `51` | `~/.claude` as the only home |
| `55-62` | `~/.claude/plugins/cache/*/*/*/skills/*/SKILL.md` and `.../commands/*.md` — Claude cache globs |
| `70-71` | `CLAUDE_PLUGIN_ROOT` |
| **absent** | **there is no host version check anywhere in this file, or anywhere in the repository.** `grep -rn 'claude --version\|codex --version' conductor/ bin/` returns nothing. The floor in design line 365 is currently unenforced. |

**Correction to the task framing:** the roadmap entry and the briefing cite `conductor/preflight.py:14-23` for the hardcoded command list; the list literal is actually `:15-26` (line 13–14 is its comment). `driver.py:67` and `:79` are call-site and JSON-shape lines rather than the `scheduled_tasks.json` path construction, which is `:55-59`. The lines are real; the ranges above are the verified ones.

**What that inventory means for effort.** Thirty-one production lines carry a host assumption. They are concentrated, not diffuse, and none of them is skill content — which is why the SKILL.md format compatibility finding matters: the delta really is ~31 lines plus the adapter that replaces them, not a port of `skills/`.

---

## File Structure

**New package — `conductor/hosts/`:**

| File | Responsibility |
| --- | --- |
| `conductor/hosts/__init__.py` | package marker; re-exports `load`, `opposite`, `HOST_IDS` |
| `conductor/hosts/base.py` | the `HostAdapter` protocol, `HOST_IDS`/`POSTURES`, the error taxonomy, `DispatchResult`, `probe_version`, `assert_minimum_version`, `reject_flaglike_prompt`, `load`, `opposite` |
| `conductor/hosts/proc.py` | host-neutral `/proc` mechanics: `pids()`, `start_ticks(pid)`, `cmdline(pid)`, `cwd(pid)`. Knows no host name |
| `conductor/hosts/claude.py` | `ClaudeAdapter` — every Claude-specific string in the repository ends up here |
| `conductor/hosts/codex.py` | `CodexAdapter` — every Codex-specific string lives here and nowhere else |
| `conductor/hosts/cli.py` | `conductor host list\|show\|preflight` — read-only diagnostics |

**Modified:**

| File | Change |
| --- | --- |
| `bin/conductor` | add the `host` verb; extend the usage text. Nothing else. |

**Tests — `tests/conductor/hosts/`:** (`tests/conductor/conftest.py` already covers this directory; do not add `pytest_plugins`.)

| File | Covers |
| --- | --- |
| `tests/conductor/hosts/__init__.py` | package marker |
| `tests/conductor/hosts/conftest.py` | the `fake_host` fixture — real executables on `PATH` (Task 2) |
| `tests/conductor/hosts/test_registry.py` | Task 1 |
| `tests/conductor/hosts/test_discovery.py` | Task 2 |
| `tests/conductor/hosts/test_version.py` | Task 3 |
| `tests/conductor/hosts/test_argv.py` | Task 4 |
| `tests/conductor/hosts/test_invocation.py` | Task 5 |
| `tests/conductor/hosts/test_permissions.py` | Task 6 |
| `tests/conductor/hosts/test_liveness.py` | Task 7 |
| `tests/conductor/hosts/test_double_drive.py` | Task 8 |
| `tests/conductor/hosts/test_dispatch.py` | Task 9 |
| `tests/conductor/hosts/test_hooks.py` | Task 10 |
| `tests/conductor/hosts/test_host_cli.py` | Task 11 |

---

## How to read the test steps

Every test step carries a **Falsifier** line: the exact edit you would make to the implementation to prove the test can fail. This repository has shipped tests that passed with their own fix deleted — three of nineteen on a recent branch, and Plan 01's residuals record four more found the same way (`docs/reviews/2026-08-10-plan-01-residuals.md` §"Method notes"). The falsifier is not ceremony. **Before marking any task done, apply its falsifier, watch the test fail, and revert.**

Contract tests are parametrized over `("claude", "codex")` wherever the contract is shared, so the same assertion runs against both hosts. Where a contract is genuinely asymmetric — `install_hooks` is the only one — the shared assertion is stated at the level where symmetry holds ("installs or refuses loudly; never silently no-ops") and the branch is inside the test.

---

### Task 1: The package, the vocabularies, the error taxonomy, and the registry

**Files:**
- Create: `conductor/hosts/__init__.py`
- Create: `conductor/hosts/base.py`
- Create: `tests/conductor/hosts/__init__.py`
- Test: `tests/conductor/hosts/test_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `HOST_IDS: tuple[str, ...]` — `("claude", "codex")`
  - `POSTURES: tuple[str, ...]` — `("supervised", "scoped", "full-bypass")`
  - `class UnknownHost(ValueError)`, `class HostUnavailable(RuntimeError)`, `class HostVersionTooOld(RuntimeError)`, `class PermissionProfileError(ValueError)`, `class HookContractUnverified(RuntimeError)`, `class DispatchTimeout(RuntimeError)`
  - `@dataclass(frozen=True) class DispatchResult` with fields `host: str`, `argv: tuple[str, ...]`, `returncode: int`, `result_path: str`, `result_text: str`, `truncated: bool`, `duration_s: float`
  - `class HostAdapter(Protocol)` — the full member list, filled in across Tasks 2–10
  - `load(host_id: str) -> HostAdapter`, `opposite(host_id: str) -> str`

**Design note for the implementer.** The roadmap's protocol block (roadmap lines 309–326) lists twelve members and the design calls them "eleven adapter capabilities" (design lines 85–95). Neither count survives contact with the ground truth. The final surface is nineteen members; §"Where this plan corrects the roadmap and the design" justifies each addition. Write the Protocol with every member now, as `...` bodies, so later tasks fill in implementations against a fixed interface rather than growing the interface underneath each other.

`id` is not merely a label: **`adapter.id` is also the basename of the host's executable** (`claude`, `codex`). Tasks 7 and 8 rely on that, and Task 2 asserts it, so it cannot drift.

- [ ] **Step 1: Create the package markers**

```bash
mkdir -p conductor/hosts tests/conductor/hosts
touch conductor/hosts/__init__.py tests/conductor/hosts/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/conductor/hosts/test_registry.py`:

```python
"""The host registry and the closed vocabularies (design §"System architecture").

Two hosts, two postures projections, one interface. The vocabularies are closed sets for the
same reason Plan 01's status vocabulary is: a typo'd host id must fail at the boundary, not
resolve to a silently different launch.
"""

from __future__ import annotations

import dataclasses

import pytest

from conductor.hosts import base


def test_host_ids_are_exactly_the_two_supported_hosts():
    assert base.HOST_IDS == ("claude", "codex")


def test_posture_vocabulary_is_closed_and_ordered_least_to_most_privileged():
    assert base.POSTURES == ("supervised", "scoped", "full-bypass")


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_load_returns_an_adapter_whose_id_matches_the_request(host_id):
    adapter = base.load(host_id)
    assert adapter.id == host_id


def test_load_refuses_an_unknown_host_and_names_the_supported_set():
    with pytest.raises(base.UnknownHost) as excinfo:
        base.load("gemini")
    assert "gemini" in str(excinfo.value)
    assert "claude" in str(excinfo.value) and "codex" in str(excinfo.value)


def test_opposite_is_an_involution_over_the_host_set():
    for host_id in base.HOST_IDS:
        assert base.opposite(base.opposite(host_id)) == host_id
        assert base.opposite(host_id) != host_id


def test_opposite_refuses_an_unknown_host():
    with pytest.raises(base.UnknownHost):
        base.opposite("gemini")


def test_dispatch_result_is_frozen_and_carries_the_named_result_file():
    result = base.DispatchResult(
        host="codex",
        argv=("codex", "exec"),
        returncode=0,
        result_path="/tmp/out.txt",
        result_text="done",
        truncated=False,
        duration_s=1.5,
    )
    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]


def test_every_error_is_distinguishable_and_none_is_a_bare_exception():
    for exc in (
        base.UnknownHost,
        base.HostUnavailable,
        base.HostVersionTooOld,
        base.PermissionProfileError,
        base.HookContractUnverified,
        base.DispatchTimeout,
    ):
        assert issubclass(exc, Exception)
        assert exc is not Exception
    # UnknownHost is a ValueError so a caller validating input can catch it with the rest of
    # its argument validation; the runtime failures are RuntimeErrors and must not be caught
    # by that same handler.
    assert issubclass(base.UnknownHost, ValueError)
    assert issubclass(base.PermissionProfileError, ValueError)
    assert not issubclass(base.HostUnavailable, ValueError)
    assert not issubclass(base.HookContractUnverified, ValueError)


def test_the_protocol_declares_every_member_the_adapters_must_implement():
    """The interface is fixed in Task 1 so later tasks fill it in rather than grow it."""
    expected = {
        "id",
        "executable",
        "source_root",
        "version",
        "minimum_version",
        "upgrade_hint",
        "native_invocation",
        "launch_prompt",
        "worker_argv",
        "worker_env",
        "reviewer_argv",
        "permission_profile",
        "validate_permissions",
        "process_identity",
        "process_alive",
        "processes_under",
        "install_hooks",
        "hook_installed",
        "dispatch_implementation",
    }
    declared = {n for n in base.HostAdapter.__annotations__} | {
        n for n in vars(base.HostAdapter) if not n.startswith("_")
    }
    assert expected <= declared, expected - declared
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.hosts.base'`

**Falsifier for this task's tests:** change `HOST_IDS` to `("claude",)` — `test_host_ids_are_exactly_the_two_supported_hosts`, both parametrized `load` cases, and `test_opposite_is_an_involution_over_the_host_set` all fail. Change `load` to return a `ClaudeAdapter` for any unknown id and `test_load_refuses_an_unknown_host_and_names_the_supported_set` fails.

- [ ] **Step 4: Write the implementation**

Create `conductor/hosts/base.py`:

```python
"""Host adapters — the only place Conductor knows a host by name.

Design §"System architecture" forbids the core from containing a Claude slash command, a Codex
dollar invocation, ``CLAUDE_PLUGIN_ROOT``, a host-specific permission flag, or an assumption
about one installation directory. This package is where all of those live instead.

The single most important rule in this package: **no argument vector is ever built by shared
code.** ``-p`` means ``--print`` to Claude and ``--profile`` to Codex (ground truth §"Model and
configuration"), so a shared argv template is wrong exactly once, silently, and presents as a
model-selection bug. Each adapter builds its own argv in its own module, and
``tests/conductor/hosts/test_argv.py`` enforces that structurally.

Sharing *validation* and *process-table mechanics* is fine and is done here and in ``proc``.
Sharing *argv construction* is not.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

HOST_IDS = ("claude", "codex")

# Least privileged first. The projection onto each host is the adapter's job: Claude's posture
# is a mode plus a settings file, Codex's is a graded sandbox axis, and they do not map one to
# one (ground truth §"Sandbox and approvals"). The shared vocabulary is the posture NAME only.
POSTURES = ("supervised", "scoped", "full-bypass")

# `codex --version` and `codex exec --help` HANG when stdin is an open pipe or a terminal
# (ground truth §"Codex help hangs without stdin redirection"). Under cron the symptom is a
# stuck worker, not a failed one, so every probe in this package redirects stdin from /dev/null
# and bounds itself with a timeout.
VERSION_PROBE_TIMEOUT = 20.0

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


class UnknownHost(ValueError):
    """A host id outside ``HOST_IDS``."""


class HostUnavailable(RuntimeError):
    """The host's executable, source root, or version could not be resolved."""


class HostVersionTooOld(RuntimeError):
    """The installed host is below the supported floor (design line 365)."""


class PermissionProfileError(ValueError):
    """A permission profile is malformed, uses an unknown posture, or belongs to another host."""


class HookContractUnverified(RuntimeError):
    """The host's PreCompact hook contract has not been verified on this host.

    Design line 306: a missing, untrusted, disabled, or ineffective required hook blocks
    unattended mode rather than allowing an unbounded session. Raising is that rule, not a stub.
    """


class DispatchTimeout(RuntimeError):
    """An implementation dispatch exceeded its timeout; the child was killed."""


@dataclass(frozen=True)
class DispatchResult:
    """The bounded result of one isolated implementation dispatch.

    ``result_path`` is load-bearing. Codex can write its final message to a file the caller
    names (``-o/--output-last-message``); Claude cannot, and its adapter captures stdout and
    writes that same file itself. Defining the contract the other way round — "the adapter
    returns captured stdout" — would generalise the Claude-side compromise and throw away the
    better surface (ground truth §"Output").
    """

    host: str
    argv: tuple[str, ...]
    returncode: int
    result_path: str
    result_text: str
    truncated: bool
    duration_s: float


class HostAdapter(Protocol):
    """Everything that genuinely differs between Claude Code and Codex.

    ``id`` is also the basename of the host's executable. Tasks 7 and 8 depend on that.
    """

    id: str

    def executable(self) -> str: ...
    def source_root(self) -> str: ...
    def version(self) -> tuple[int, ...]: ...
    def minimum_version(self) -> tuple[int, ...]: ...
    def upgrade_hint(self) -> str: ...
    def native_invocation(self, skill: str) -> str: ...
    def launch_prompt(self, skill: str, *, run_key: str | None = None) -> str: ...
    def worker_argv(
        self,
        *,
        state_root: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]: ...
    def worker_env(
        self, *, state_root: str, run_key: str, project_root: str
    ) -> dict[str, str]: ...
    def reviewer_argv(
        self,
        *,
        pr: int,
        head_sha: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]: ...
    def permission_profile(self, posture: str = "supervised") -> dict: ...
    def validate_permissions(self, profile: dict) -> None: ...
    def process_identity(self, pid: int) -> str: ...
    def process_alive(self, identity: str) -> bool: ...
    def processes_under(self, roots: list[str]) -> list[int]: ...
    def install_hooks(
        self, state_root: str, run_key: str, *, command: list[str]
    ) -> str: ...
    def hook_installed(self, state_root: str, run_key: str) -> bool: ...
    def dispatch_implementation(
        self,
        prompt: str,
        *,
        timeout: float,
        result_path: str | None = None,
        posture: str = "scoped",
    ) -> DispatchResult: ...


def opposite(host_id: str) -> str:
    """The default reviewer host for a run owned by ``host_id`` (design line 25)."""
    if host_id not in HOST_IDS:
        raise UnknownHost(f"unknown host {host_id!r}; supported hosts are {HOST_IDS}")
    return "codex" if host_id == "claude" else "claude"


def load(host_id: str) -> HostAdapter:
    """The adapter for ``host_id``. Imports are local so ``base`` stays leaf-level."""
    if host_id == "claude":
        from conductor.hosts.claude import ClaudeAdapter

        return ClaudeAdapter()
    if host_id == "codex":
        from conductor.hosts.codex import CodexAdapter

        return CodexAdapter()
    raise UnknownHost(f"unknown host {host_id!r}; supported hosts are {HOST_IDS}")


def reject_flaglike_prompt(prompt: str) -> str:
    """Refuse a prompt that would be parsed as an option.

    Both hosts take the prompt as a trailing positional argument. Neither adapter emits a
    ``--`` separator, because whether ``codex exec`` honours one was not verified and guessing
    at an unverified CLI contract is how the ``-p`` collision happened in the first place.
    Refusing the input instead is verifiable today.
    """
    if prompt.startswith("-"):
        raise ValueError(
            f"prompt would be parsed as an option: {prompt[:40]!r}. Prompts are passed as a "
            "trailing positional argument and must not start with '-'."
        )
    return prompt


def probe_version(
    executable: str, *, timeout: float = VERSION_PROBE_TIMEOUT
) -> tuple[int, ...]:
    """``<executable> --version`` parsed into a comparable tuple.

    ``stdin=DEVNULL`` is the whole point of this helper existing rather than each adapter
    calling ``subprocess.run``: without it ``codex --version`` hangs forever instead of
    answering (ground truth §"Codex help hangs without stdin redirection").
    """
    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HostUnavailable(f"{executable!r} is not executable") from exc
    except subprocess.TimeoutExpired as exc:
        raise HostUnavailable(
            f"{executable} --version did not answer within {timeout}s (no write occurred); "
            f"check the install with: command -v {executable}"
        ) from exc
    if proc.returncode != 0:
        raise HostUnavailable(
            f"{executable} --version exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
        )
    match = _VERSION_RE.search(proc.stdout) or _VERSION_RE.search(proc.stderr)
    if not match:
        raise HostUnavailable(
            f"{executable} --version printed no dotted version number: "
            f"{(proc.stdout or proc.stderr).strip()[:200]!r}"
        )
    return tuple(int(part) for part in match.group(1).split("."))


def assert_minimum_version(adapter: HostAdapter) -> tuple[int, ...]:
    """The installed version, or ``HostVersionTooOld`` naming the floor and the exact check."""
    found = adapter.version()
    floor = adapter.minimum_version()
    if found < floor:
        raise HostVersionTooOld(
            f"{adapter.id} {'.'.join(map(str, found))} is below the supported floor "
            f"{'.'.join(map(str, floor))} (no write occurred). {adapter.upgrade_hint()}"
        )
    return found
```

Create `conductor/hosts/__init__.py`:

```python
"""Claude Code and Codex host adapters."""

from conductor.hosts.base import HOST_IDS, HostAdapter, load, opposite

__all__ = ["HOST_IDS", "HostAdapter", "load", "opposite"]
```

- [ ] **Step 5: Run the test to verify it fails differently**

Run: `pytest tests/conductor/hosts/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.hosts.claude'` on the two `load` cases. Everything not calling `load` passes. This is expected: Task 2 creates the adapters.

- [ ] **Step 6: Create minimal adapter shells so the registry resolves**

Create `conductor/hosts/claude.py`:

```python
"""The Claude Code adapter. Every Claude-specific string in Conductor belongs here."""

from __future__ import annotations


class ClaudeAdapter:
    id: str = "claude"
```

Create `conductor/hosts/codex.py`:

```python
"""The Codex CLI adapter. Every Codex-specific string in Conductor belongs here.

Verified against codex-cli 0.147.0 on 2026-08-12; see
``docs/reviews/2026-08-12-codex-host-ground-truth.md``.
"""

from __future__ import annotations


class CodexAdapter:
    id: str = "codex"
```

These are not stubs of adapter behaviour — they are the class declarations Tasks 2–10 fill in, and no method is declared until it is implemented. `load()` returns them; pyright will report them as not conforming to `HostAdapter` until Task 10 completes, which is the honest signal. Add `# type: ignore[return-value]` on both `return` statements in `load`, with the comment `# conforming from Task 10`, and **delete both ignores in Task 10**.

- [ ] **Step 7: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_registry.py -q`
Expected: PASS (10 passed)

- [ ] **Step 8: Lint and typecheck**

Run: `ruff check conductor/hosts tests/conductor/hosts && ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add conductor/hosts/__init__.py conductor/hosts/base.py conductor/hosts/claude.py \
        conductor/hosts/codex.py tests/conductor/hosts/__init__.py \
        tests/conductor/hosts/test_registry.py
git commit -m "conductor/hosts/base.py:1-250 — host adapter protocol, vocabularies, registry

- HOST_IDS/POSTURES closed sets, six named errors, frozen DispatchResult, load/opposite
- probe_version redirects stdin from /dev/null: codex --version hangs on an open stdin"
```

---

### Task 2: Executable and source-root discovery

**Files:**
- Create: `tests/conductor/hosts/conftest.py`
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_discovery.py`

**Interfaces:**
- Consumes: `base.HostUnavailable`.
- Produces:
  - `ClaudeAdapter.executable() -> str`, `CodexAdapter.executable() -> str`
  - `ClaudeAdapter.source_root() -> str`, `CodexAdapter.source_root() -> str`
  - the `fake_host` pytest fixture: writes a real executable named `claude` or `codex` into a temp directory, prepends it to `PATH`, and returns its path. Every later task uses it.

**Design note.** `source_root()` has one contract on both hosts: **the directory whose `skills/<name>/SKILL.md` this host will read.** Defining it that way, rather than as "the plugin directory", is what lets the Codex side work today — this repository has no `.codex-plugin/plugin.json` yet (Plan 09 adds it), and validating on a manifest that does not exist would make every Codex path fail for a packaging reason rather than an adapter reason.

Claude's resolution order transcribes what production already does: `CLAUDE_PLUGIN_ROOT`, then the newest installed plugin cache (the same glob shape as `conductor/resume_script.py:178`), then this repository via `__file__` (the same walk as `conductor/preflight.py:65`). Codex's order is `CODEX_PLUGIN_ROOT`, then this repository. **Codex's installed plugin-cache layout is deliberately not hardcoded**: the only observed root was `/home/danie906/.codex/.tmp/plugins`, a `.tmp` path that no documentation makes contractual (ground truth §"Codex plugin system"). Guessing it would be a rot pattern of exactly the kind `conductor/resume_script.py:40-49` exists to detect. It is recorded as a residual instead.

- [ ] **Step 1: Write the fixture**

Create `tests/conductor/hosts/conftest.py`:

```python
"""Fake host executables on PATH.

Design §"Integration tests" (line 518) prescribes this shape: "Temporary repositories,
worktrees, bare remotes, and fake Claude, Codex, and gh executables verify exact arguments,
environment, prompts, Git transitions, scheduler entries, review routing, and crash recovery."

These are real executable files, not mocks. A mock would let a wrong argv pass, and argv is
precisely what this plan is about. The repository already uses this pattern for `crontab`
(`tests/conductor/test_resume_script.py:750-774`) and for `claude` (`:463-486`).
"""

from __future__ import annotations

import os
import stat

import pytest

from conductor.hosts import base

#: A fake that records its argv and its cwd, then exits 0.
RECORDER = """#!/bin/sh
: > "$REC.argv"
for a in "$@"; do printf '%s\\n' "$a" >> "$REC.argv"; done
pwd > "$REC.cwd"
exit 0
"""


def _install(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """Install a fake executable for a host and put it first on PATH.

    Usage: ``exe = fake_host("codex", body)``. ``body`` defaults to a recorder that writes
    ``<tmp>/rec-<host>.argv`` (one argument per line) and ``<tmp>/rec-<host>.cwd``.
    """
    bindir = tmp_path / "fake-bin"
    bindir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    def _make(host_id: str, body: str | None = None):
        assert host_id in base.HOST_IDS
        rec = tmp_path / f"rec-{host_id}"
        script = body if body is not None else RECORDER
        return _install(bindir, host_id, script.replace("$REC", str(rec)))

    return _make


@pytest.fixture
def skill_root(tmp_path):
    """A directory that looks like an installed Conductor source root to an adapter."""
    root = tmp_path / "source-root"
    (root / "skills" / "autodev").mkdir(parents=True)
    (root / "skills" / "autodev" / "SKILL.md").write_text(
        "---\nname: autodev\ndescription: the conductor worker\n---\n"
    )
    return root
```

- [ ] **Step 2: Write the failing test**

Create `tests/conductor/hosts/test_discovery.py`:

```python
"""Executable and source-root discovery — the shared contract, run against both hosts.

`source_root()` answers one question on both hosts: which directory's
`skills/<name>/SKILL.md` will this host read? Not "where is the plugin manifest" — this
repository has no `.codex-plugin/plugin.json` until Plan 09, and validating on a manifest that
does not exist yet would fail the Codex path for a packaging reason, not an adapter reason.
"""

from __future__ import annotations

import os

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS
_ROOT_ENV = {"claude": "CLAUDE_PLUGIN_ROOT", "codex": "CODEX_PLUGIN_ROOT"}


@pytest.mark.parametrize("host_id", HOSTS)
def test_id_is_the_executable_basename(host_id, fake_host):
    """Tasks 7 and 8 match live processes by this basename. If it drifts, both break."""
    fake_host(host_id)
    adapter = base.load(host_id)
    assert os.path.basename(adapter.executable()) == adapter.id


@pytest.mark.parametrize("host_id", HOSTS)
def test_executable_resolves_from_path(host_id, fake_host):
    exe = fake_host(host_id)
    assert base.load(host_id).executable() == str(exe)


@pytest.mark.parametrize("host_id", HOSTS)
def test_missing_executable_raises_host_unavailable_naming_the_check(
    host_id, monkeypatch, tmp_path
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    with pytest.raises(base.HostUnavailable) as excinfo:
        base.load(host_id).executable()
    message = str(excinfo.value)
    assert host_id in message
    assert f"command -v {host_id}" in message


def test_claude_falls_back_to_the_stable_unversioned_launcher(
    monkeypatch, tmp_path
):
    """Transcribed from conductor/resume_script.py:176 — the claude 2.x standalone launcher
    is the one path that does not rot on a node upgrade."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    launcher = home / ".local" / "bin" / "claude"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert base.load("claude").executable() == str(launcher)


@pytest.mark.parametrize("host_id", HOSTS)
def test_source_root_env_override_wins_and_must_contain_the_skill(
    host_id, monkeypatch, skill_root
):
    monkeypatch.setenv(_ROOT_ENV[host_id], str(skill_root))
    assert base.load(host_id).source_root() == str(skill_root)


@pytest.mark.parametrize("host_id", HOSTS)
def test_source_root_refuses_an_override_without_the_skill_tree(
    host_id, monkeypatch, tmp_path
):
    empty = tmp_path / "not-a-source-root"
    empty.mkdir()
    monkeypatch.setenv(_ROOT_ENV[host_id], str(empty))
    with pytest.raises(base.HostUnavailable) as excinfo:
        base.load(host_id).source_root()
    assert "skills/autodev/SKILL.md" in str(excinfo.value)
    assert str(empty) in str(excinfo.value)


@pytest.mark.parametrize("host_id", HOSTS)
def test_source_root_falls_back_to_this_repository(host_id, monkeypatch):
    """With no override and no installed package, an adapter resolves its own checkout —
    which is what makes the whole suite runnable from a worktree."""
    monkeypatch.delenv(_ROOT_ENV[host_id], raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-home-for-this-test")
    root = base.load(host_id).source_root()
    assert os.path.isfile(os.path.join(root, "skills", "autodev", "SKILL.md"))


def test_the_two_adapters_do_not_read_each_others_environment(monkeypatch, skill_root):
    """A Codex machine has no CLAUDE_PLUGIN_ROOT; a stray one must not steer Codex."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-home-for-this-test")
    assert base.load("codex").source_root() != str(skill_root)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_discovery.py -q`
Expected: FAIL — `AttributeError: 'ClaudeAdapter' object has no attribute 'executable'`

**Falsifier:** after the implementation is in, delete the `~/.local/bin/claude` fallback branch from `ClaudeAdapter.executable` and `test_claude_falls_back_to_the_stable_unversioned_launcher` fails. Make `CodexAdapter.source_root` read `CLAUDE_PLUGIN_ROOT` and `test_the_two_adapters_do_not_read_each_others_environment` fails. Drop the `skills/autodev/SKILL.md` validation and `test_source_root_refuses_an_override_without_the_skill_tree` fails.

- [ ] **Step 4: Write the shared validation helper**

Append to `conductor/hosts/base.py`:

```python
def validated_source_root(root: str, *, host_id: str) -> str:
    """``root`` if it is a Conductor source root for ``host_id``, else ``HostUnavailable``.

    The test is the skill tree, not the plugin manifest: Codex skills use the same SKILL.md
    format as Claude (ground truth §"Skill file format is compatible across hosts"), so the
    skill tree is the one artifact both hosts genuinely need. The manifests differ
    (``.claude-plugin`` vs ``.codex-plugin``) and Plan 09 owns them.
    """
    probe = os.path.join(root, "skills", "autodev", "SKILL.md")
    if not os.path.isfile(probe):
        raise HostUnavailable(
            f"{root!r} is not a Conductor source root for host {host_id!r}: no "
            f"skills/autodev/SKILL.md (no write occurred). Install the plugin, or set "
            f"{'CLAUDE_PLUGIN_ROOT' if host_id == 'claude' else 'CODEX_PLUGIN_ROOT'} to a "
            f"checkout that has one."
        )
    return os.path.realpath(root)
```

`os` is already in `base.py`'s import block from Task 1.

- [ ] **Step 5: Write the Claude implementation**

Replace the body of `conductor/hosts/claude.py`:

```python
"""The Claude Code adapter. Every Claude-specific string in Conductor belongs here."""

from __future__ import annotations

import glob
import os
import shutil

from conductor.hosts import base


class ClaudeAdapter:
    id: str = "claude"

    def executable(self) -> str:
        """`claude` from PATH, else the 2.x standalone launcher.

        Transcribed from conductor/resume_script.py:175-176. The launcher path is stable and
        unversioned on purpose: a node-version-pinned path rots on the next upgrade and every
        headless fire then dies silently — the 2026-07-05 failure this repository is built
        around.
        """
        found = shutil.which("claude")
        if found:
            return found
        launcher = os.path.expanduser("~/.local/bin/claude")
        if os.access(launcher, os.X_OK):
            return launcher
        raise base.HostUnavailable(
            "claude is not on PATH and ~/.local/bin/claude is not executable "
            "(no write occurred). Check the install with: command -v claude"
        )

    def source_root(self) -> str:
        """CLAUDE_PLUGIN_ROOT, then the newest installed plugin cache, then this checkout.

        The cache glob is the same shape conductor/resume_script.py:178 uses to find the
        conductor bin, sorted by version so an upgrade wins without regeneration.
        """
        override = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if override:
            return base.validated_source_root(override, host_id=self.id)
        home = os.path.expanduser("~/.claude")
        cached = sorted(glob.glob(f"{home}/plugins/cache/*/conductor/*"))
        for candidate in reversed(cached):
            if os.path.isfile(
                os.path.join(candidate, "skills", "autodev", "SKILL.md")
            ):
                return os.path.realpath(candidate)
        return base.validated_source_root(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            host_id=self.id,
        )
```

`os.path.dirname(os.path.dirname(...))` from `conductor/hosts/claude.py` yields the repository root — the same two-level walk `conductor/preflight.py:65` performs from `conductor/preflight.py`. Do not change the nesting depth of this file without changing that expression.

- [ ] **Step 6: Write the Codex implementation**

Replace the body of `conductor/hosts/codex.py`:

```python
"""The Codex CLI adapter. Every Codex-specific string in Conductor belongs here.

Verified against codex-cli 0.147.0 on 2026-08-12; see
``docs/reviews/2026-08-12-codex-host-ground-truth.md``. Flags used by this module all come
from ``codex exec --help`` at that version.
"""

from __future__ import annotations

import os
import shutil

from conductor.hosts import base


class CodexAdapter:
    id: str = "codex"

    def executable(self) -> str:
        found = shutil.which("codex")
        if found:
            return found
        raise base.HostUnavailable(
            "codex is not on PATH (no write occurred). Check the install with: "
            "command -v codex"
        )

    def source_root(self) -> str:
        """CODEX_PLUGIN_ROOT, then this checkout.

        The installed Codex plugin cache is deliberately NOT globbed. The only root observed
        on any machine was `$CODEX_HOME/.tmp/plugins` (ground truth §"Codex plugin system"),
        and a `.tmp` path that no documentation makes contractual is exactly the kind of
        generation-time guess conductor/resume_script.py:40-49 exists to detect as rot. Plan 09
        establishes the real layout when it publishes the catalog; until then an operator with
        an installed package sets CODEX_PLUGIN_ROOT.
        """
        override = os.environ.get("CODEX_PLUGIN_ROOT")
        if override:
            return base.validated_source_root(override, host_id=self.id)
        return base.validated_source_root(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            host_id=self.id,
        )
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_discovery.py -q`
Expected: PASS (14 passed)

- [ ] **Step 8: Apply each falsifier, watch it fail, revert**

Run the three edits named in the Falsifier note under Step 3, one at a time. Each must produce a failing test naming the behaviour it removed. Revert after each.

- [ ] **Step 9: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/base.py conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/conftest.py tests/conductor/hosts/test_discovery.py
git commit -m "conductor/hosts/{claude,codex}.py:1-70 — executable + source-root discovery

- claude: PATH then the unversioned 2.x launcher then the plugin cache, per resume_script.py:175-178
- codex: PATH then CODEX_PLUGIN_ROOT; the .tmp plugin cache is not globbed (unverified layout)
- source_root validates on skills/autodev/SKILL.md, not on a manifest Plan 09 has not added"
```

---

### Task 3: Version probes and the supported floor

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_version.py`

**Interfaces:**
- Consumes: `base.probe_version`, `base.assert_minimum_version`, `base.HostVersionTooOld`.
- Produces:
  - `version() -> tuple[int, ...]` on both adapters
  - `minimum_version() -> tuple[int, ...]` — Claude `(2, 1, 224)`, Codex `(0, 147, 0)`
  - `upgrade_hint() -> str`

**Design note — the hazard this task exists for.** `codex --version` and `codex exec --help` **hang** unless stdin is redirected from `/dev/null` (ground truth §"Codex help hangs without stdin redirection"). In a cron-driven fire the symptom is a stuck worker holding a lock, not a failed one — the single worst failure mode this system has, and the exact class the 2026-07-05 silent stall belonged to.

Testing that honestly is harder than it looks. `subprocess.run` without `stdin=` inherits the parent's fd 0, and under pytest fd 0 is often already `/dev/null`, so a naive test would pass with the fix deleted. Step 2 therefore runs the adapter call inside a child Python process whose stdin is a pipe the test holds open and never writes to. With the redirect, the child answers; without it, the fake blocks and the test's own `communicate(timeout=...)` raises. That is deterministic, needs no fd surgery inside pytest, and genuinely fails when the fix is removed.

**`upgrade_hint()` does not invent an installer command.** No install or upgrade command was verified for either host, and a preflight that prints a wrong command is worse than one that prints none. The hint names the floor and the check (`claude --version` / `codex --version`), which is verified. Design line 559 asks preflight to fail "with the documented minimum-version command"; this is the honest subset.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_version.py`:

```python
"""Host version probes and the supported floor (design line 365).

Floors: Claude Code 2.1.224, Codex CLI 0.147.0 — the versions against which plugin discovery,
marketplace policy, non-interactive launch, native subagents, and PreCompact contracts were
verified.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS
_FLOORS = {"claude": (2, 1, 224), "codex": (0, 147, 0)}
_REAL_OUTPUT = {"claude": "2.1.227 (Claude Code)", "codex": "codex-cli 0.147.0"}


def _printer(line: str) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' {line!r}\nexit 0\n"


@pytest.mark.parametrize("host_id", HOSTS)
def test_minimum_version_is_the_published_floor(host_id):
    assert base.load(host_id).minimum_version() == _FLOORS[host_id]


@pytest.mark.parametrize("host_id", HOSTS)
def test_version_parses_the_real_output_shape_of_that_host(host_id, fake_host):
    """Verbatim output shapes captured on 2026-08-12 from the installed binaries."""
    fake_host(host_id, _printer(_REAL_OUTPUT[host_id]))
    assert base.load(host_id).version() >= _FLOORS[host_id]


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_version_below_the_floor_is_refused_naming_floor_and_check(
    host_id, fake_host
):
    fake_host(host_id, _printer("0.0.1"))
    adapter = base.load(host_id)
    with pytest.raises(base.HostVersionTooOld) as excinfo:
        base.assert_minimum_version(adapter)
    message = str(excinfo.value)
    assert ".".join(map(str, _FLOORS[host_id])) in message
    assert f"{host_id} --version" in message
    assert "no write occurred" in message


@pytest.mark.parametrize("host_id", HOSTS)
def test_exactly_the_floor_is_accepted(host_id, fake_host):
    fake_host(host_id, _printer(".".join(map(str, _FLOORS[host_id]))))
    adapter = base.load(host_id)
    assert base.assert_minimum_version(adapter) == _FLOORS[host_id]


@pytest.mark.parametrize("host_id", HOSTS)
def test_unparseable_version_output_fails_closed(host_id, fake_host):
    fake_host(host_id, _printer("something went wrong"))
    with pytest.raises(base.HostUnavailable):
        base.load(host_id).version()


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_nonzero_version_exit_fails_closed(host_id, fake_host):
    fake_host(host_id, "#!/bin/sh\necho boom >&2\nexit 7\n")
    with pytest.raises(base.HostUnavailable) as excinfo:
        base.load(host_id).version()
    assert "7" in str(excinfo.value)


# --- the stdin hazard ---------------------------------------------------------------------
#
# `codex --version` HANGS when stdin is an open pipe or a terminal. Inheriting the parent's
# fd 0 is therefore a stuck worker under cron, not a failed one. Under pytest fd 0 is usually
# already /dev/null, which would let this test pass with the fix deleted — so the adapter call
# runs in a child whose stdin is a pipe THIS test holds open and never writes to.

_CHILD = textwrap.dedent(
    """
    import sys
    from conductor.hosts import base
    print(base.load(sys.argv[1]).version())
    """
)


@pytest.mark.parametrize("host_id", HOSTS)
def test_version_probe_redirects_stdin_and_cannot_hang_on_an_open_pipe(host_id, tmp_path):
    bindir = tmp_path / "hang-bin"
    bindir.mkdir()
    fake = bindir / host_id
    # `cat` returns immediately when stdin is at EOF (/dev/null) and blocks forever when it is
    # an open pipe with no writer closing it. This is the real binary's behaviour, reproduced.
    fake.write_text(
        "#!/bin/sh\ncat > /dev/null\nprintf '%s\\n' "
        + repr(_REAL_OUTPUT[host_id])
        + "\nexit 0\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        # conductor/hosts/base.py -> conductor/hosts -> conductor -> repo root
        "PYTHONPATH": str(pathlib.Path(base.__file__).resolve().parents[2]),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, host_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        out, err = child.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail(
            f"{host_id} version probe inherited an open stdin and hung — it must pass "
            "stdin=subprocess.DEVNULL"
        )
    assert child.returncode == 0, err
    assert str(_FLOORS[host_id][0]) in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_version.py -q`
Expected: FAIL — `AttributeError: 'ClaudeAdapter' object has no attribute 'minimum_version'`

**Falsifier:** delete `stdin=subprocess.DEVNULL` from `base.probe_version` and
`test_version_probe_redirects_stdin_and_cannot_hang_on_an_open_pipe` fails with the explicit
`pytest.fail` message, for both hosts. Change Claude's floor to `(2, 1, 0)` and
`test_minimum_version_is_the_published_floor` fails. Remove the `"no write occurred"` clause
from `assert_minimum_version` and `test_a_version_below_the_floor_is_refused_naming_floor_and_check` fails.

- [ ] **Step 3: Write the implementation**

Append to `ClaudeAdapter` in `conductor/hosts/claude.py`:

```python
    # Design line 365: the version against which plugin discovery, marketplace policy,
    # non-interactive launch, native subagents, and the PreCompact contract were verified.
    # Lowering this requires re-running the same contract suite.
    MINIMUM_VERSION = (2, 1, 224)

    def version(self) -> tuple[int, ...]:
        return base.probe_version(self.executable())

    def minimum_version(self) -> tuple[int, ...]:
        return self.MINIMUM_VERSION

    def upgrade_hint(self) -> str:
        """No installer command is named here on purpose: none was verified, and a preflight
        that prints a wrong upgrade command is worse than one that prints none."""
        return (
            "Upgrade Claude Code to "
            f"{'.'.join(map(str, self.MINIMUM_VERSION))} or newer, then confirm with: "
            "claude --version"
        )
```

Append to `CodexAdapter` in `conductor/hosts/codex.py`:

```python
    MINIMUM_VERSION = (0, 147, 0)

    def version(self) -> tuple[int, ...]:
        return base.probe_version(self.executable())

    def minimum_version(self) -> tuple[int, ...]:
        return self.MINIMUM_VERSION

    def upgrade_hint(self) -> str:
        return (
            "Upgrade the Codex CLI to "
            f"{'.'.join(map(str, self.MINIMUM_VERSION))} or newer, then confirm with: "
            "codex --version"
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_version.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Apply the stdin falsifier, watch it fail, revert**

Run: delete `stdin=subprocess.DEVNULL` from `base.probe_version`, then
`pytest tests/conductor/hosts/test_version.py -k stdin -q`
Expected: FAIL for both hosts with "inherited an open stdin and hung". Restore the line and re-run to PASS.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/claude.py conductor/hosts/codex.py tests/conductor/hosts/test_version.py
git commit -m "conductor/hosts/{claude,codex}.py — version probe + supported floor

- floors 2.1.224 / 0.147.0; below-floor refusal names the floor and the --version check
- a child-with-open-stdin test proves the probe cannot hang, which is what codex does without DEVNULL"
```

---

### Task 4: Argument vectors — and the `-p` collision

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_argv.py`

**Interfaces:**
- Consumes: `base.reject_flaglike_prompt`, `base.POSTURES`, Task 5's `launch_prompt` (write Task 5 first if you prefer; the two tasks are commutative, but Task 4's tests reference `launch_prompt`, so implement its Claude and Codex bodies here if Task 5 has not run yet and delete the duplicate then).
- Produces:
  - `worker_argv(*, state_root, run_key, project_root, posture="supervised") -> list[str]`
  - `worker_env(*, state_root, run_key, project_root) -> dict[str, str]`
  - `reviewer_argv(*, pr, head_sha, run_key, project_root, posture="scoped") -> list[str]`

**Execution note.** To keep Task 4 self-contained, implement `launch_prompt` here in its final form (the code is given in Task 5 Step 3) and let Task 5 add only its own tests and the `native_invocation` split. Do not write a temporary `launch_prompt` and rewrite it in Task 5.

**Hazard 1 — the whole reason this task is separate.**

> `-p` means `--profile` in Codex and `--print` in Claude (ground truth §"Model and configuration"). An adapter that builds argv by string templating across hosts gets this wrong exactly once, silently, and in a way that presents as a model-selection bug: Codex would treat the prompt as the name of a config profile, fail to find `$CODEX_HOME/<prompt>.config.toml`, and either error obscurely or run with the base configuration and no prompt.

**The rule this plan encodes: cross-host argv templating is forbidden.** Not discouraged — forbidden, and enforced three ways:

1. Each adapter's `worker_argv` / `reviewer_argv` / `launch_prompt` / `permission_profile` is *defined in its own module*. A structural test reads `__module__` off the unbound function and fails if it is `conductor.hosts.base`. Moving one onto a shared mixin to "remove duplication" fails the suite.
2. A source-level tripwire: `conductor/hosts/codex.py` must not contain the token `"-p"` at all.
3. A behavioural assertion: Claude's worker argv has the prompt immediately after `-p`; Codex's contains neither `-p` nor `--profile`.

The duplication between the two `worker_argv` bodies is deliberate and must not be refactored away. They are not two instances of one thing; they are two different command-line grammars that happen to be similar in length.

**Design decisions the implementer must not re-derive.**

- **Claude's worker prompt stays byte-identical to production.** `conductor/resume_script.py:261` fires `"$CLAUDE_BIN" -p "/conductor:autodev"`. The adapter emits exactly `["<exe>", "-p", "/conductor:autodev", *posture_flags]`. Appending run scoping to that string would change a dispatch path proven over many live fires, for no benefit.
- **The run key therefore travels in the environment, not in argv**, which is why `worker_env` exists (see §"Where this plan corrects the roadmap and the design"). Codex's prompt is free text and *does* name the run key inline, because it can without changing any host dispatch behaviour.
- **Codex gets `--cd <project_root>`.** Codex resolves its workspace from cwd; naming it explicitly is the documented way to be unambiguous (ground truth §"Filesystem scope"). Claude has no equivalent flag, so the wrapper sets cwd. This asymmetry is real and is why argv cannot be shared.
- **No `--` separator is emitted.** Whether `codex exec` honours one was not verified, and this plan does not guess at unverified CLI contracts — that is precisely the mistake `-p` represents. `base.reject_flaglike_prompt` refuses a prompt starting with `-` instead, which is testable today.
- **The reviewer posture defaults to `supervised`, the least privileged one.** A reviewer reads a diff; it does not write. Defaulting to `scoped` would make the Claude reviewer depend on a `CONDUCTOR_CLAUDE_SETTINGS` file an operator may not have configured, and anything higher hands a read-only job write access. Task 9's *implementation* dispatch defaults to `scoped`, because implementation does write — that asymmetry is intentional.
- **`reviewer_argv` renders a review request, not a review workflow.** Plan 07 owns reviewer routing, verdict schemas, and debt. Task 4 produces only "ask host H to review PR #N at head SHA S for run K" in each host's grammar. Claude uses `/code-review <pr>`, which `conductor/preflight.py:23` already requires to be installed. Codex uses the explicit-instruction form. **`codex exec review` exists but its argument contract was not verified** (ground truth §"Non-interactive invocation" records only that the subcommand exists), so it is not used; Plan 07 may adopt it after its own ground-truth pass.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_argv.py`:

```python
"""Argument vectors, per host, never shared.

`-p` is `--print` to Claude and `--profile` to Codex. A shared argv builder is wrong exactly
once, silently, and presents as a model-selection bug. These tests make that structurally
impossible rather than merely discouraged.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS
STATE_ROOT = "/proj/.conductor"
RUN_KEY = "alpha-1a2b3c4d"
PROJECT = "/proj"


# --- the structural rule -------------------------------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize(
    "member", ["worker_argv", "reviewer_argv", "launch_prompt", "permission_profile"]
)
def test_each_adapter_defines_its_own_command_line_grammar(host_id, member):
    """No shared base class, no mixin, no template. If someone "removes the duplication"
    between the two worker_argv bodies, this fails."""
    fn = getattr(type(base.load(host_id)), member)
    assert inspect.isfunction(fn), member
    assert fn.__module__ == f"conductor.hosts.{host_id}", (member, fn.__module__)


def test_the_codex_module_contains_no_dash_p_token_at_all():
    """A tripwire, not a style rule: `-p` in codex.py means `--profile`, and the only reason
    to write it there is to have copied a Claude argv."""
    from conductor.hosts import codex

    text = pathlib.Path(codex.__file__).read_text()
    assert '"-p"' not in text and "'-p'" not in text


# --- worker argv ---------------------------------------------------------------------------


def test_claude_worker_argv_is_the_proven_production_invocation(fake_host):
    """conductor/resume_script.py:261 — `"$CLAUDE_BIN" -p "/conductor:autodev"`. Byte-identical."""
    exe = fake_host("claude")
    argv = base.load("claude").worker_argv(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    assert argv[:3] == [str(exe), "-p", "/conductor:autodev"]


def test_codex_worker_argv_uses_exec_and_never_dash_p(fake_host, monkeypatch, skill_root):
    exe = fake_host("codex")
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    argv = base.load("codex").worker_argv(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    assert argv[0] == str(exe)
    assert argv[1] == "exec"
    assert "-p" not in argv, "-p is --profile to codex; the prompt is not a profile name"
    assert "--profile" not in argv
    assert argv[argv.index("--cd") + 1] == PROJECT


@pytest.mark.parametrize("host_id", HOSTS)
def test_worker_argv_ends_with_the_prompt_as_a_trailing_positional(
    host_id, fake_host, monkeypatch, skill_root
):
    fake_host(host_id)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    adapter = base.load(host_id)
    argv = adapter.worker_argv(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    assert adapter.launch_prompt("autodev", run_key=RUN_KEY) in argv


@pytest.mark.parametrize("host_id", HOSTS)
def test_worker_argv_is_a_list_of_plain_strings_never_a_shell_string(
    host_id, fake_host, monkeypatch, skill_root
):
    """Design line 97: adapters launch argument vectors, never interpolated shell commands."""
    fake_host(host_id)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    argv = base.load(host_id).worker_argv(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    assert isinstance(argv, list) and len(argv) >= 2
    assert all(isinstance(token, str) for token in argv)
    assert not any(token.strip() in ("&&", "||", ";", "|") for token in argv)


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_unknown_posture_is_refused(host_id, fake_host, monkeypatch, skill_root):
    fake_host(host_id)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    with pytest.raises(base.PermissionProfileError):
        base.load(host_id).worker_argv(
            state_root=STATE_ROOT,
            run_key=RUN_KEY,
            project_root=PROJECT,
            posture="yolo",
        )


# --- worker env ----------------------------------------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
def test_worker_env_scopes_the_worker_to_the_run(host_id):
    env = base.load(host_id).worker_env(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    assert env["CONDUCTOR_HOME"] == PROJECT
    assert env["CONDUCTOR_STATE_ROOT"] == STATE_ROOT
    assert env["CONDUCTOR_RUN_KEY"] == RUN_KEY


@pytest.mark.parametrize("host_id", HOSTS)
def test_worker_env_never_carries_the_other_hosts_variables(host_id):
    env = base.load(host_id).worker_env(
        state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
    )
    foreign = "CODEX_HOME" if host_id == "claude" else "CLAUDE_PLUGIN_ROOT"
    assert foreign not in env


def test_worker_env_does_not_export_the_run_branch():
    """conductor/resume_script.py:18-20 — CONDUCTOR_RUN_BRANCH is deliberately not exported;
    a stale literal would override the file that is the single source of truth."""
    for host_id in HOSTS:
        env = base.load(host_id).worker_env(
            state_root=STATE_ROOT, run_key=RUN_KEY, project_root=PROJECT
        )
        assert "CONDUCTOR_RUN_BRANCH" not in env


# --- reviewer argv -------------------------------------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
def test_reviewer_argv_names_the_pull_request_and_the_exact_head_sha(
    host_id, fake_host, monkeypatch, skill_root
):
    """A review is tied to a head SHA (design line 590). The SHA must reach the reviewer."""
    fake_host(host_id)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    argv = base.load(host_id).reviewer_argv(
        pr=91, head_sha="deadbeef" * 5, run_key=RUN_KEY, project_root=PROJECT
    )
    joined = " ".join(argv)
    assert "91" in joined
    assert "deadbeef" * 5 in joined


def test_claude_reviewer_argv_uses_the_slash_command_preflight_already_requires(fake_host):
    """conductor/preflight.py:23 requires /code-review to be installed. Use it rather than
    inventing a reviewer skill Plan 07 has not written."""
    fake_host("claude")
    argv = base.load("claude").reviewer_argv(
        pr=91, head_sha="a" * 40, run_key=RUN_KEY, project_root=PROJECT
    )
    assert "/code-review" in argv[argv.index("-p") + 1]


def test_codex_reviewer_argv_does_not_use_the_unverified_review_subcommand(fake_host):
    """`codex exec review` exists but its argument contract was not verified. Guessing at an
    unverified CLI contract is the mistake `-p` represents."""
    fake_host("codex")
    argv = base.load("codex").reviewer_argv(
        pr=91, head_sha="a" * 40, run_key=RUN_KEY, project_root=PROJECT
    )
    assert "review" not in argv[:3]


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_reviewer_posture_defaults_to_the_least_privileged_one(
    host_id, fake_host, monkeypatch, skill_root
):
    """A reviewer reads a diff; it does not write. Defaulting to 'scoped' would make the
    Claude reviewer depend on a settings file an operator may not have configured, and
    defaulting to anything higher would hand a read-only job write access."""
    fake_host(host_id)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    monkeypatch.delenv("CONDUCTOR_CLAUDE_SETTINGS", raising=False)
    adapter = base.load(host_id)
    argv = adapter.reviewer_argv(
        pr=91, head_sha="a" * 40, run_key=RUN_KEY, project_root=PROJECT
    )
    for token in adapter.permission_profile("supervised")["argv"]:
        assert token in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_flaglike_prompt_is_refused_rather_than_smuggled_into_argv(host_id):
    with pytest.raises(ValueError) as excinfo:
        base.reject_flaglike_prompt("--dangerously-bypass-approvals-and-sandbox")
    assert "must not start with '-'" in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_argv.py -q`
Expected: FAIL — `AttributeError: type object 'ClaudeAdapter' has no attribute 'worker_argv'`

**Falsifier (this is the task's whole point — run all four):**
1. Move `worker_argv` onto a shared base class in `base.py` and have both adapters inherit it → `test_each_adapter_defines_its_own_command_line_grammar` fails for both hosts on that member.
2. Make Codex's worker argv `[exe, "exec", "-p", prompt]` → `test_codex_worker_argv_uses_exec_and_never_dash_p` fails **and** `test_the_codex_module_contains_no_dash_p_token_at_all` fails.
3. Drop the run key from `worker_env` → `test_worker_env_scopes_the_worker_to_the_run` fails for both hosts.
4. Drop `head_sha` from `reviewer_argv` → `test_reviewer_argv_names_the_pull_request_and_the_exact_head_sha` fails for both hosts.

- [ ] **Step 3: Write the Claude implementation**

Append to `ClaudeAdapter`:

```python
    def worker_argv(
        self,
        *,
        state_root: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]:
        """`claude -p "/conductor:autodev" <posture flags>`.

        Byte-identical to conductor/resume_script.py:261, which is the invocation many live
        fires have proven. The run key does NOT appear here: appending text after a slash
        command would change a working dispatch path for no benefit, so it travels in
        ``worker_env`` instead.
        """
        profile = self.permission_profile(posture)
        return [
            self.executable(),
            "-p",
            base.reject_flaglike_prompt(self.launch_prompt("autodev", run_key=run_key)),
            *profile["argv"],
        ]

    def worker_env(
        self, *, state_root: str, run_key: str, project_root: str
    ) -> dict[str, str]:
        """The environment that scopes a Claude worker to one run.

        CONDUCTOR_RUN_BRANCH is deliberately absent — conductor/resume_script.py:18-20: a
        stale literal would override .conductor/run_branch, which is the single source of
        truth for the branch.
        """
        return {
            "CONDUCTOR_HOME": project_root,
            "CONDUCTOR_STATE_ROOT": state_root,
            "CONDUCTOR_RUN_KEY": run_key,
        }

    def reviewer_argv(
        self,
        *,
        pr: int,
        head_sha: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]:
        """Ask Claude to review one pull request at one head SHA.

        `/code-review` is already a required command in conductor/preflight.py:23, so this
        invokes what the preflight already guarantees rather than a reviewer skill Plan 07 has
        not written. Plan 07 replaces the prompt body; the argv grammar stays.
        """
        prompt = (
            f"/code-review {pr}\n\n"
            f"Review pull request #{pr} at head SHA {head_sha} for Conductor run {run_key}."
        )
        profile = self.permission_profile(posture)
        return [
            self.executable(),
            "-p",
            base.reject_flaglike_prompt(prompt),
            *profile["argv"],
        ]
```

- [ ] **Step 4: Write the Codex implementation**

Append to `CodexAdapter`:

```python
    def worker_argv(
        self,
        *,
        state_root: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]:
        """`codex exec --cd <project> <sandbox flags> <prompt>`.

        Written from scratch against `codex exec --help` at codex-cli 0.147.0, NOT adapted
        from the Claude vector. `-p` is `--profile` here: passing the prompt after `-p` would
        make Codex look for `$CODEX_HOME/<prompt>.config.toml`, fail to find it, and present
        as a model-selection bug. `--cd` names the workspace explicitly because Codex
        otherwise infers it from cwd (ground truth §"Filesystem scope").

        No `--` separator is emitted: whether `codex exec` honours one was not verified, and
        guessing at an unverified CLI contract is the mistake this docstring is about.
        ``reject_flaglike_prompt`` covers the case a separator would have covered.
        """
        profile = self.permission_profile(posture)
        return [
            self.executable(),
            "exec",
            "--cd",
            project_root,
            *profile["argv"],
            base.reject_flaglike_prompt(self.launch_prompt("autodev", run_key=run_key)),
        ]

    def worker_env(
        self, *, state_root: str, run_key: str, project_root: str
    ) -> dict[str, str]:
        """Codex carries the run key in its prompt as well, but the environment is what the
        `conductor` CLI inside the session reads, so it is set identically on both hosts."""
        return {
            "CONDUCTOR_HOME": project_root,
            "CONDUCTOR_STATE_ROOT": state_root,
            "CONDUCTOR_RUN_KEY": run_key,
        }

    def reviewer_argv(
        self,
        *,
        pr: int,
        head_sha: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]:
        """Ask Codex to review one pull request at one head SHA.

        `codex exec review` exists (ground truth §"Non-interactive invocation") but its
        argument contract was not verified, so it is not used. Plan 07 may adopt it after its
        own ground-truth pass; until then this is an ordinary `codex exec` with an explicit
        instruction, which is verified to work.
        """
        prompt = (
            f"Review pull request #{pr} at head SHA {head_sha} for Conductor run {run_key}. "
            f"Report findings by severity. Do not push, merge, or modify the branch."
        )
        profile = self.permission_profile(posture)
        return [
            self.executable(),
            "exec",
            "--cd",
            project_root,
            *profile["argv"],
            base.reject_flaglike_prompt(prompt),
        ]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_argv.py -q`
Expected: PASS (30 passed). Task 6 supplies `permission_profile`; if it has not run yet, implement its Claude and Codex bodies from Task 6 Step 3 now and let Task 6 add only its tests.

- [ ] **Step 6: Apply all four falsifiers, watch each fail, revert**

- [ ] **Step 7: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/claude.py conductor/hosts/codex.py tests/conductor/hosts/test_argv.py
git commit -m "conductor/hosts/{claude,codex}.py — per-host argv, worker env, reviewer argv

- forbids cross-host argv templating structurally (__module__ check) and by source tripwire
- claude keeps the proven -p /conductor:autodev vector; codex uses exec --cd and never -p
- the run key travels in worker_env because claude's argv cannot carry it without changing dispatch"
```

---

### Task 5: Launch prompt versus native invocation — the `$conductor:*` decision

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_invocation.py`

**Interfaces:**
- Consumes: `source_root()` from Task 2.
- Produces:
  - `native_invocation(skill: str) -> str` — **diagnostics only**: what a human types
  - `launch_prompt(skill: str, *, run_key: str | None = None) -> str` — **what actually goes into argv**

**The decision, and why it diverges from the design's literal wording.**

The design says Codex exposes `$conductor:*` skills (design line 101) and warns that the dollar invocation "is passed as literal prompt text and cannot be expanded as an environment variable" (design line 97). The ground truth establishes that this warning, while true, is not the important part:

- `$name` is **not a Codex host dispatch primitive**. Under Claude, `claude -p "/conductor:autodev"` is dispatched by the host. Under Codex, `$name` is literal prompt text that the *model* interprets by consulting a convention table.
- That table lives in `~/.codex/AGENTS.md`, which on the verified machine is **oh-my-codex's file** — a third-party install, not a documented Codex-native feature (the installed skills carry an `[OMX]` description prefix).
- Every entry in that table expands to the same thing: *"Read `./.codex/skills/<name>/SKILL.md`, do X."*
- Therefore, on a machine where conductor has not established that convention, `$conductor:autodev` **resolves to nothing at all**. Fixing the quoting does not make the launch work; it only makes it fail differently.

**Decision: the Codex adapter's launch prompt emits an explicit instruction naming the absolute `SKILL.md` path.** That is exactly what every `AGENTS.md` dispatch entry expands to anyway, so nothing is lost. What is gained: the launch is deterministic on a machine with no `AGENTS.md`, no oh-my-codex, and no convention table; and no `$` token survives, so the shell-expansion hazard the design warns about is removed by construction rather than by careful quoting.

`native_invocation` keeps returning `$conductor:autodev` because that *is* what a Codex user types when the convention is present, and it is what diagnostics and documentation should show. **The two are now different strings on Codex and identical on Claude, and that difference is the point.** Conflating them — which the roadmap's single `native_invocation` method does — is what would ship a launch that depends on a third-party file.

This is a divergence from the design's literal wording, not from its intent: design line 101 is a statement about the user-facing surface, which `native_invocation` preserves.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_invocation.py`:

```python
"""What a human types (`native_invocation`) versus what goes into argv (`launch_prompt`).

On Claude these coincide: `/conductor:autodev` is dispatched by the host itself. On Codex they
must not. `$conductor:autodev` is a prompting convention interpreted by the model via
~/.codex/AGENTS.md — a third-party file — and on a machine without it the token resolves to
nothing at all. See docs/reviews/2026-08-12-codex-host-ground-truth.md §"Skill invocation under
Codex".
"""

from __future__ import annotations

import os

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS
RUN_KEY = "alpha-1a2b3c4d"


@pytest.fixture(autouse=True)
def _roots(monkeypatch, skill_root):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    return skill_root


def test_native_invocation_is_the_user_facing_surface_each_host_documents():
    assert base.load("claude").native_invocation("autodev") == "/conductor:autodev"
    assert base.load("codex").native_invocation("autodev") == "$conductor:autodev"


def test_claude_launch_prompt_is_the_native_invocation_because_the_host_dispatches_it():
    adapter = base.load("claude")
    assert adapter.launch_prompt("autodev") == adapter.native_invocation("autodev")


def test_codex_launch_prompt_names_the_skill_file_and_never_the_dollar_token(_roots):
    """The launch must not depend on ~/.codex/AGENTS.md existing."""
    prompt = base.load("codex").launch_prompt("autodev")
    assert "$" not in prompt
    assert "conductor:autodev" not in prompt
    expected = os.path.join(str(_roots), "skills", "autodev", "SKILL.md")
    assert expected in prompt


def test_codex_launch_prompt_names_an_absolute_existing_path(_roots):
    prompt = base.load("codex").launch_prompt("autodev")
    path = next(t for t in prompt.split() if t.endswith("SKILL.md"))
    assert os.path.isabs(path)
    assert os.path.isfile(path)


@pytest.mark.parametrize("host_id", HOSTS)
def test_launch_prompt_is_never_flaglike(host_id):
    """It goes in as a trailing positional; a leading '-' would be parsed as an option."""
    assert base.reject_flaglike_prompt(base.load(host_id).launch_prompt("autodev"))


@pytest.mark.parametrize("host_id", HOSTS)
def test_launch_prompt_carries_the_run_key_only_where_the_host_grammar_allows_it(host_id):
    prompt = base.load(host_id).launch_prompt("autodev", run_key=RUN_KEY)
    if host_id == "codex":
        assert RUN_KEY in prompt
    else:
        # Claude's is a bare slash command; the key travels in worker_env (Task 4).
        assert prompt == "/conductor:autodev"


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_unknown_skill_is_refused_rather_than_rendered(host_id):
    with pytest.raises(ValueError) as excinfo:
        base.load(host_id).launch_prompt("no-such-skill")
    assert "no-such-skill" in str(excinfo.value)


def test_codex_launch_prompt_refuses_when_the_skill_file_is_absent(monkeypatch, tmp_path):
    """Fail closed and name the path: a prompt pointing at a nonexistent SKILL.md would send
    the model looking for a file it cannot find, and the fire would burn a whole context
    discovering that."""
    empty = tmp_path / "no-skills"
    (empty / "skills" / "autodev").mkdir(parents=True)
    (empty / "skills" / "autodev" / "SKILL.md").write_text("---\nname: autodev\n---\n")
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(empty))
    (empty / "skills" / "autodev" / "SKILL.md").unlink()
    with pytest.raises(base.HostUnavailable):
        base.load("codex").launch_prompt("autodev")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_invocation.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'native_invocation'`

**Falsifier:** make `CodexAdapter.launch_prompt` return `f"$conductor:{skill}"` — `test_codex_launch_prompt_names_the_skill_file_and_never_the_dollar_token` and `test_codex_launch_prompt_names_an_absolute_existing_path` both fail. Make `native_invocation` return the launch prompt on Codex and `test_native_invocation_is_the_user_facing_surface_each_host_documents` fails. Drop the `KNOWN_SKILLS` check and `test_an_unknown_skill_is_refused_rather_than_rendered` fails for both hosts.

- [ ] **Step 3: Write the implementations**

Add to `conductor/hosts/base.py`:

```python
#: The skills Conductor ships. Rendering an invocation for anything else is a typo, and a
#: typo'd skill name costs a whole fire to discover. Kept here rather than globbed so the
#: adapters stay testable against a fixture source root.
KNOWN_SKILLS = (
    "assertions-to-tests",
    "autodev",
    "issue-sync",
    "prepare",
    "start",
)


def assert_known_skill(skill: str) -> str:
    if skill not in KNOWN_SKILLS:
        raise ValueError(
            f"unknown Conductor skill {skill!r}; shipped skills are {KNOWN_SKILLS}"
        )
    return skill
```

Append to `ClaudeAdapter`:

```python
    def native_invocation(self, skill: str) -> str:
        """What a Claude user types. The host dispatches this itself."""
        return f"/conductor:{base.assert_known_skill(skill)}"

    def launch_prompt(self, skill: str, *, run_key: str | None = None) -> str:
        """Identical to ``native_invocation``: on Claude the host IS the dispatcher.

        ``run_key`` is accepted for interface symmetry and deliberately unused — see
        ``worker_env``. Appending it would change a dispatch path proven over many live fires.
        """
        return self.native_invocation(skill)
```

Append to `CodexAdapter`:

```python
    def native_invocation(self, skill: str) -> str:
        """What a Codex user types WHEN the ~/.codex/AGENTS.md convention is installed.

        Diagnostics and documentation only. This is NOT what the adapter launches — see
        ``launch_prompt``.
        """
        return f"$conductor:{base.assert_known_skill(skill)}"

    def launch_prompt(self, skill: str, *, run_key: str | None = None) -> str:
        """An explicit instruction naming the absolute SKILL.md path.

        `$name` is not a Codex host primitive. It is a prompting convention the MODEL
        interprets by reading a dispatch table in ~/.codex/AGENTS.md, which on the verified
        machine is oh-my-codex's third-party file, and every entry in that table expands to
        exactly the instruction below. Emitting the expansion directly costs nothing, works on
        a machine with no AGENTS.md, and leaves no `$` token to be shell-expanded into an
        empty string.
        """
        base.assert_known_skill(skill)
        path = os.path.join(self.source_root(), "skills", skill, "SKILL.md")
        if not os.path.isfile(path):
            raise base.HostUnavailable(
                f"skill file missing: {path} (no write occurred). Reinstall the conductor "
                "plugin or set CODEX_PLUGIN_ROOT to a checkout that has it."
            )
        scope = f" The Conductor run key is {run_key}." if run_key else ""
        return f"Read {path} and execute it.{scope}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_invocation.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Apply the falsifiers, watch them fail, revert**

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/base.py conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/test_invocation.py
git commit -m "conductor/hosts/{base,claude,codex}.py — launch prompt split from native invocation

- codex launches by naming the absolute SKILL.md path, not \$conductor:autodev
- \$name is a ~/.codex/AGENTS.md convention (third-party), not a host dispatch primitive
- native_invocation keeps the documented user-facing surface for diagnostics"
```

---

### Task 6: Permission postures and bypass non-transfer

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_permissions.py`

**Interfaces:**
- Consumes: `base.POSTURES`, `base.PermissionProfileError`.
- Produces:
  - `permission_profile(posture: str = "supervised") -> dict` — `{"host": str, "posture": str, "argv": list[str]}`
  - `validate_permissions(profile: dict) -> None`

**Design note.** Claude's permission posture is a mode plus a settings file; Codex's sandbox is a graded axis `read-only` / `workspace-write` / `danger-full-access` (ground truth §"Sandbox and approvals"). **These do not map one to one.** The shared vocabulary is the posture *name* only; the projection is per-host and lives in each adapter.

The posture names are transcribed from `conductor/resume_script.py:249-259`, which already derives exactly `supervised` / `scoped` / `full-bypass` from the owner's configured flags. Reusing those names means the adapter and the shipped driver label the same thing the same way, which matters the day Plan 05 replaces the driver and an operator compares two logs.

Claude's `scoped` posture needs a settings path, which the adapter does not have. It renders `--settings` only when `CONDUCTOR_CLAUDE_SETTINGS` names an existing file, and otherwise refuses — because a `scoped` posture that silently renders no flags is a `supervised` fire mislabelled as least-privilege, which is precisely the mislabelling `conductor/resume_script.py:241-248` was written to prevent.

**Bypass non-transfer** (design line 499) is the invariant that a profile minted by one host cannot authorise the other. A Claude `full-bypass` profile handed to `CodexAdapter.validate_permissions` must raise, not be quietly accepted — the flags mean nothing to Codex and accepting it would report a bypass posture that was never actually applied.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_permissions.py`:

```python
"""Permission profiles and bypass non-transfer (design line 499).

Claude's posture is a mode plus a settings file; Codex's is a graded sandbox axis. The shared
vocabulary is the posture NAME. The flags are not shared, and a profile minted by one host must
never authorise the other.
"""

from __future__ import annotations

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize("posture", base.POSTURES)
def test_every_posture_round_trips_through_its_own_hosts_validator(
    host_id, posture, monkeypatch, tmp_path
):
    settings = tmp_path / "scoped-settings.json"
    settings.write_text("{}")
    monkeypatch.setenv("CONDUCTOR_CLAUDE_SETTINGS", str(settings))
    adapter = base.load(host_id)
    profile = adapter.permission_profile(posture)
    assert profile["host"] == host_id
    assert profile["posture"] == posture
    adapter.validate_permissions(profile)  # must not raise


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_unknown_posture_is_refused_naming_the_vocabulary(host_id):
    with pytest.raises(base.PermissionProfileError) as excinfo:
        base.load(host_id).permission_profile("yolo")
    assert "yolo" in str(excinfo.value)
    for name in base.POSTURES:
        assert name in str(excinfo.value)


@pytest.mark.parametrize("host_id", HOSTS)
def test_supervised_renders_no_privilege_escalating_flag(host_id):
    """The default posture is the least privileged one on both hosts. A full-access agent
    firing every heartbeat is a standing security posture the owner opts into explicitly —
    conductor/resume_script.py:232-236."""
    profile = base.load(host_id).permission_profile()
    assert profile["posture"] == "supervised"
    joined = " ".join(profile["argv"])
    assert "dangerously" not in joined
    assert "danger-full-access" not in joined


def test_claude_full_bypass_uses_claudes_flag():
    profile = base.load("claude").permission_profile("full-bypass")
    assert "--dangerously-skip-permissions" in profile["argv"]


def test_codex_full_bypass_uses_codexs_flag():
    """Ground truth §"Sandbox and approvals": the analogue of Claude's bypass flag."""
    profile = base.load("codex").permission_profile("full-bypass")
    assert "--dangerously-bypass-approvals-and-sandbox" in profile["argv"]


def test_codex_postures_use_the_graded_sandbox_axis():
    codex = base.load("codex")
    assert codex.permission_profile("supervised")["argv"] == ["--sandbox", "read-only"]
    assert codex.permission_profile("scoped")["argv"] == [
        "--sandbox",
        "workspace-write",
    ]


def test_claude_scoped_refuses_without_a_settings_file_rather_than_rendering_nothing(
    monkeypatch,
):
    """A scoped posture that silently renders no flags is a supervised fire mislabelled as
    least-privilege — the mislabelling conductor/resume_script.py:241-248 exists to prevent."""
    monkeypatch.delenv("CONDUCTOR_CLAUDE_SETTINGS", raising=False)
    with pytest.raises(base.PermissionProfileError) as excinfo:
        base.load("claude").permission_profile("scoped")
    assert "CONDUCTOR_CLAUDE_SETTINGS" in str(excinfo.value)


def test_claude_scoped_refuses_a_settings_path_that_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTOR_CLAUDE_SETTINGS", str(tmp_path / "absent.json"))
    with pytest.raises(base.PermissionProfileError):
        base.load("claude").permission_profile("scoped")


# --- bypass non-transfer -------------------------------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_profile_minted_by_the_other_host_is_refused(host_id, monkeypatch, tmp_path):
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    monkeypatch.setenv("CONDUCTOR_CLAUDE_SETTINGS", str(settings))
    other = base.opposite(host_id)
    foreign = base.load(other).permission_profile("full-bypass")
    with pytest.raises(base.PermissionProfileError) as excinfo:
        base.load(host_id).validate_permissions(foreign)
    message = str(excinfo.value)
    assert host_id in message and other in message


@pytest.mark.parametrize("host_id", HOSTS)
def test_smuggled_extra_flags_are_refused(host_id):
    """A profile is not a free-form argv carrier. If the argv is not this host's canonical
    rendering for that posture, the profile is refused — otherwise a caller could label a
    bypass fire 'supervised' and the log would say so."""
    adapter = base.load(host_id)
    profile = adapter.permission_profile("supervised")
    profile["argv"] = [*profile["argv"], "--dangerously-skip-permissions"]
    with pytest.raises(base.PermissionProfileError):
        adapter.validate_permissions(profile)


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize("missing", ["host", "posture", "argv"])
def test_a_malformed_profile_is_refused(host_id, missing):
    adapter = base.load(host_id)
    profile = adapter.permission_profile("supervised")
    del profile[missing]
    with pytest.raises(base.PermissionProfileError) as excinfo:
        adapter.validate_permissions(profile)
    assert missing in str(excinfo.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_permissions.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'permission_profile'`

**Falsifier:** remove the `profile["host"] != self.id` check from `validate_permissions` and `test_a_profile_minted_by_the_other_host_is_refused` fails for both hosts. Make `validate_permissions` skip the canonical-argv comparison and `test_smuggled_extra_flags_are_refused` fails for both hosts. Make Claude's `scoped` posture fall back to `[]` when `CONDUCTOR_CLAUDE_SETTINGS` is unset and `test_claude_scoped_refuses_without_a_settings_file_rather_than_rendering_nothing` fails.

- [ ] **Step 3: Write the implementations**

Add to `conductor/hosts/base.py`:

```python
def validate_profile_shape(profile: dict, *, host_id: str, canonical) -> None:
    """The half of ``validate_permissions`` that is genuinely identical on both hosts.

    ``canonical`` renders this host's argv for a posture. Comparing against it is what stops a
    profile being used as a free-form argv carrier: a caller could otherwise append a bypass
    flag to a profile labelled ``supervised`` and every log line would report the wrong
    posture.
    """
    for field in ("host", "posture", "argv"):
        if field not in profile:
            raise PermissionProfileError(
                f"permission profile is missing {field!r}: {sorted(profile)}"
            )
    if profile["host"] != host_id:
        raise PermissionProfileError(
            f"permission profile was minted for host {profile['host']!r} and cannot "
            f"authorise host {host_id!r}; permissions do not transfer between hosts"
        )
    if profile["posture"] not in POSTURES:
        raise PermissionProfileError(
            f"unknown posture {profile['posture']!r}; expected one of {POSTURES}"
        )
    expected = canonical(profile["posture"])
    if list(profile["argv"]) != list(expected):
        raise PermissionProfileError(
            f"permission profile argv for posture {profile['posture']!r} on host "
            f"{host_id!r} is not this host's canonical rendering: expected {expected}, "
            f"got {list(profile['argv'])}"
        )
```

Append to `ClaudeAdapter`:

```python
    _SETTINGS_ENV = "CONDUCTOR_CLAUDE_SETTINGS"

    def _posture_argv(self, posture: str) -> list[str]:
        """Claude's projection: a mode, or a settings file. Names transcribed from
        conductor/resume_script.py:249-259 so the adapter and the shipped driver label the
        same fire the same way."""
        if posture not in base.POSTURES:
            raise base.PermissionProfileError(
                f"unknown posture {posture!r}; expected one of {base.POSTURES}"
            )
        if posture == "supervised":
            return []
        if posture == "full-bypass":
            return ["--dangerously-skip-permissions"]
        settings = os.environ.get(self._SETTINGS_ENV, "")
        if not settings or not os.path.isfile(settings):
            raise base.PermissionProfileError(
                f"posture 'scoped' needs a least-privilege settings file: set "
                f"{self._SETTINGS_ENV} to an existing settings.json (no write occurred). "
                f"Refusing rather than rendering no flags — that would be a supervised fire "
                f"mislabelled as least-privilege."
            )
        return ["--settings", settings]

    def permission_profile(self, posture: str = "supervised") -> dict:
        return {"host": self.id, "posture": posture, "argv": self._posture_argv(posture)}

    def validate_permissions(self, profile: dict) -> None:
        base.validate_profile_shape(
            profile, host_id=self.id, canonical=self._posture_argv
        )
```

Append to `CodexAdapter`:

```python
    def _posture_argv(self, posture: str) -> list[str]:
        """Codex's projection onto its graded sandbox axis (ground truth §"Sandbox and
        approvals"). Written independently of Claude's: `--sandbox` has no Claude analogue and
        `--dangerously-skip-permissions` means nothing here."""
        if posture not in base.POSTURES:
            raise base.PermissionProfileError(
                f"unknown posture {posture!r}; expected one of {base.POSTURES}"
            )
        if posture == "supervised":
            return ["--sandbox", "read-only"]
        if posture == "scoped":
            return ["--sandbox", "workspace-write"]
        return ["--dangerously-bypass-approvals-and-sandbox"]

    def permission_profile(self, posture: str = "supervised") -> dict:
        return {"host": self.id, "posture": posture, "argv": self._posture_argv(posture)}

    def validate_permissions(self, profile: dict) -> None:
        base.validate_profile_shape(
            profile, host_id=self.id, canonical=self._posture_argv
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_permissions.py -q`
Expected: PASS (25 passed)

- [ ] **Step 5: Apply the three falsifiers, watch each fail, revert**

- [ ] **Step 6: Run Task 4's suite too**

Run: `pytest tests/conductor/hosts/test_argv.py tests/conductor/hosts/test_permissions.py -q`
Expected: PASS. Task 4's argv builders consume `permission_profile`; this is the first point where both halves exist.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/base.py conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/test_permissions.py
git commit -m "conductor/hosts/{base,claude,codex}.py — permission postures + bypass non-transfer

- one posture vocabulary (supervised/scoped/full-bypass), two projections that share no flag
- a profile minted by one host is refused by the other; smuggled extra argv is refused
- claude 'scoped' refuses without a settings file rather than silently rendering nothing"
```

---

### Task 7: Process identity and liveness

**Files:**
- Create: `conductor/hosts/proc.py`
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_liveness.py`

**Interfaces:**
- Consumes: nothing outside the package.
- Produces:
  - `conductor/hosts/proc.py`: `pids() -> list[int]`, `start_ticks(pid: int) -> int`, `cmdline(pid: int) -> list[str]`, `cwd(pid: int) -> str | None`, `class ProcessGone(LookupError)`
  - `process_identity(pid: int) -> str` on both adapters — format `"<host-id>:<pid>:<start-ticks>"`
  - `process_alive(identity: str) -> bool` on both adapters

**This is the interface Plan 02 consumes.** `ownership.prove_exited(record)` calls `process_alive`. Plan 02 must not mint a second identity string; `OwnerRecord.wrapper_identity` and `OwnerRecord.host_identity` are strings in this format.

**Why the identity carries start ticks.** A bare PID is not an identity. Between one heartbeat and the next, the kernel can hand the same PID to an unrelated process, and a lease check that trusts a bare PID reports a dead worker as alive — which is exactly the case Plan 02's residual list calls "expiry is necessary but never sufficient". Field 22 of `/proc/<pid>/stat` is the process start time in clock ticks since boot; a PID plus its start time is unique for the machine's uptime. Parsing that field requires care: field 2 (`comm`) is parenthesised and may itself contain spaces and parentheses, so the parse splits on the **last** `)` and indexes from there.

**Why liveness is per-host and not shared.** `proc.py` knows no host name — it is process-table mechanics, and sharing mechanics is fine. The *predicate* "is this one of my processes" is per-host: each adapter matches a live process by its own executable basename, which is `adapter.id` (asserted in Task 2). That is what makes `ClaudeAdapter.process_alive` and `CodexAdapter.process_alive` genuinely different functions rather than one function with a parameter.

Handing a Codex identity to the Claude adapter **raises**, it does not return `False`. Returning `False` would be a second fail-open: a caller asking the wrong adapter would conclude the worker had exited and take over a live run.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_liveness.py`:

```python
"""Process identity and liveness — the interface Plan 02's ownership.prove_exited consumes.

A bare PID is not an identity: the kernel reuses PIDs, and a lease check that trusts one
reports a dead worker as alive. Identity is `<host>:<pid>:<start-ticks>`, where start-ticks is
field 22 of /proc/<pid>/stat — unique per PID for the machine's uptime.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from conductor.hosts import base, proc

HOSTS = base.HOST_IDS

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="process liveness reads /proc; the shipped driver already requires it "
    "(conductor/resume_script.py:215)",
)

_LONG_RUNNING = "#!/bin/sh\nsleep 120\n"


@pytest.fixture
def live_host_process(fake_host):
    """A real, running process that looks like the named host to the process table."""
    started = []

    def _start(host_id, cwd=None):
        exe = fake_host(host_id, _LONG_RUNNING)
        child = subprocess.Popen([str(exe)], cwd=cwd)
        started.append(child)
        # The kernel rewrites a #! script to `/bin/sh <script>`, so /proc/<pid>/cmdline
        # carries a token whose basename is the host id — the same shape a real host has.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(
                os.path.basename(t) == host_id for t in proc.cmdline(child.pid)
            ):
                return child
            time.sleep(0.02)
        raise AssertionError(f"fake {host_id} never appeared in the process table")

    yield _start
    for child in started:
        child.kill()
        child.wait(timeout=10)


# --- proc mechanics ------------------------------------------------------------------------


def test_start_ticks_survives_a_command_name_containing_spaces_and_parentheses():
    """/proc/<pid>/stat field 2 is parenthesised and may contain both. A naive split() puts
    field 22 in the wrong place and every identity silently mismatches.

    Layout: pid, (comm), then fields 3..21 (nineteen of them), then field 22 = start ticks.
    Everything after the LAST ')' is fields 3 onward, so field 22 is index 19.
    """
    fields_3_to_21 = " ".join(str(i) for i in range(3, 22))
    assert len(fields_3_to_21.split()) == 19
    line = f"1234 (weird )name( here) {fields_3_to_21} 987654"
    assert proc.parse_start_ticks(line) == 987654


def test_start_ticks_refuses_a_truncated_stat_line():
    with pytest.raises(ValueError):
        proc.parse_start_ticks("1234 (sh) S 1 2 3")


def test_start_ticks_of_a_missing_pid_raises_process_gone():
    with pytest.raises(proc.ProcessGone):
        proc.start_ticks(4_000_000)


def test_cmdline_of_a_missing_pid_is_empty_not_an_error():
    assert proc.cmdline(4_000_000) == []


# --- identity ------------------------------------------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
def test_identity_is_host_pid_and_start_ticks(host_id, live_host_process):
    child = live_host_process(host_id)
    identity = base.load(host_id).process_identity(child.pid)
    tag, pid, ticks = identity.split(":")
    assert tag == host_id
    assert int(pid) == child.pid
    assert int(ticks) == proc.start_ticks(child.pid)


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_live_host_process_reads_as_alive(host_id, live_host_process):
    """The load-bearing assertion for Codex: this must not be vacuously False."""
    adapter = base.load(host_id)
    child = live_host_process(host_id)
    assert adapter.process_alive(adapter.process_identity(child.pid)) is True


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_exited_process_reads_as_dead(host_id, live_host_process):
    adapter = base.load(host_id)
    child = live_host_process(host_id)
    identity = adapter.process_identity(child.pid)
    child.kill()
    child.wait(timeout=10)
    assert adapter.process_alive(identity) is False


@pytest.mark.parametrize("host_id", HOSTS)
def test_pid_reuse_does_not_read_as_alive(host_id, live_host_process):
    """Same PID, different start time: a recycled PID must not resurrect a dead lease."""
    adapter = base.load(host_id)
    child = live_host_process(host_id)
    real = adapter.process_identity(child.pid)
    tag, pid, ticks = real.split(":")
    forged = f"{tag}:{pid}:{int(ticks) + 1}"
    assert adapter.process_alive(forged) is False


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_live_process_that_is_not_this_host_reads_as_dead(host_id, live_host_process):
    """A recycled PID now held by some unrelated program is not our worker."""
    adapter = base.load(host_id)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        forged = f"{host_id}:{other.pid}:{proc.start_ticks(other.pid)}"
        assert adapter.process_alive(forged) is False
    finally:
        other.kill()
        other.wait(timeout=10)


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_identity_from_the_other_host_raises_rather_than_returning_false(
    host_id, live_host_process
):
    """Returning False would be a second fail-open: the caller would conclude the worker
    exited and take over a live run."""
    adapter = base.load(host_id)
    child = live_host_process(base.opposite(host_id))
    foreign = base.load(base.opposite(host_id)).process_identity(child.pid)
    with pytest.raises(ValueError) as excinfo:
        adapter.process_alive(foreign)
    assert host_id in str(excinfo.value)
    assert base.opposite(host_id) in str(excinfo.value)


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize("bad", ["", "claude", "claude:notanint:1", "a:b:c:d"])
def test_a_malformed_identity_raises_rather_than_reading_as_dead(host_id, bad):
    with pytest.raises(ValueError):
        base.load(host_id).process_alive(bad)


@pytest.mark.parametrize("host_id", HOSTS)
def test_process_identity_of_a_missing_pid_raises(host_id):
    with pytest.raises(proc.ProcessGone):
        base.load(host_id).process_identity(4_000_000)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_liveness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conductor.hosts.proc'`

**Falsifier:** make `CodexAdapter.process_alive` `return False` unconditionally — `test_a_live_host_process_reads_as_alive[codex]` fails. Drop the start-ticks comparison and `test_pid_reuse_does_not_read_as_alive` fails for both hosts. Drop the executable-basename check and `test_a_live_process_that_is_not_this_host_reads_as_dead` fails for both hosts. Change `parse_start_ticks` to `line.split()[21]` and `test_start_ticks_survives_a_command_name_containing_spaces_and_parentheses` fails.

- [ ] **Step 3: Write the process-table mechanics**

Create `conductor/hosts/proc.py`:

```python
"""Process-table mechanics. Knows no host name.

Sharing this is fine and sharing argv is not, and the difference is worth stating: reading
/proc is one mechanism with one correct implementation, while an argument vector is a grammar
and the two hosts have different ones.

Linux only. The shipped driver already requires /proc (conductor/resume_script.py:215 reads
/proc/$pid/cwd), so this is not a new platform constraint — but it is now explicit, and the
macOS gap is recorded as a residual rather than shipped as a silent no-op.
"""

from __future__ import annotations

import os


class ProcessGone(LookupError):
    """The pid does not exist, or vanished between two reads."""


def pids() -> list[int]:
    """Every pid currently in /proc, ascending."""
    try:
        return sorted(int(name) for name in os.listdir("/proc") if name.isdigit())
    except FileNotFoundError:  # no /proc: not Linux
        return []


def parse_start_ticks(stat_line: str) -> int:
    """Field 22 of a /proc/<pid>/stat line: start time in clock ticks since boot.

    Field 2 (``comm``) is parenthesised and may contain spaces AND parentheses, so a naive
    ``split()[21]`` lands on the wrong field and every identity silently mismatches. Split on
    the LAST ``)`` instead: everything after it is fields 3 onward, so field 22 is index 19.
    """
    _, _, rest = stat_line.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:
        raise ValueError(f"unparseable /proc stat line: {stat_line[:120]!r}")
    return int(fields[19])


def start_ticks(pid: int) -> int:
    """The process's start time in clock ticks. A pid plus this is unique for the uptime."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            return parse_start_ticks(handle.read())
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise ProcessGone(f"no such process: {pid}") from exc
    except PermissionError as exc:  # another user's process
        raise ProcessGone(f"process {pid} is not inspectable by this user") from exc


def cmdline(pid: int) -> list[str]:
    """The process's argv, or an empty list when it is gone or not inspectable.

    Empty rather than raising: callers scanning the whole table race against exits constantly,
    and a scan that raises on every exiting process is a scan that never completes.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def cwd(pid: int) -> str | None:
    """The process's working directory, or ``None`` when it is gone or not inspectable.

    ``None`` for another user's process is a real limitation and it is the same limitation the
    shipped ``pgrep``-based guard already has: neither can see across users.
    """
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
```

- [ ] **Step 4: Write the shared identity parse and the per-host predicate**

Add to `conductor/hosts/base.py`:

```python
def parse_identity(identity: str, *, host_id: str) -> tuple[int, int]:
    """``(pid, start_ticks)`` from ``"<host>:<pid>:<ticks>"``, for ``host_id`` only.

    A foreign identity RAISES. Returning False would be a fail-open: the caller would conclude
    the worker had exited and take over a live run.
    """
    parts = identity.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"malformed process identity {identity!r}; expected '<host>:<pid>:<start-ticks>'"
        )
    tag, pid_text, ticks_text = parts
    if tag != host_id:
        raise ValueError(
            f"process identity {identity!r} belongs to host {tag!r}; the {host_id!r} adapter "
            f"cannot decide its liveness — ask the {tag!r} adapter"
        )
    try:
        return int(pid_text), int(ticks_text)
    except ValueError as exc:
        raise ValueError(f"malformed process identity {identity!r}") from exc
```

Append to **both** `ClaudeAdapter` and `CodexAdapter` — written out in each module, not shared, because the predicate is the host-specific part:

```python
    def _is_own_process(self, pid: int) -> bool:
        """Whether ``pid`` is one of THIS host's processes.

        Matches on the executable basename, which equals ``self.id``. Deliberately narrower
        than the shipped guard: ``pgrep -f 'claude'`` (conductor/resume_script.py:214) matches
        the whole command line, so any process whose argv merely CONTAINS the string — a
        python process running out of ~/.claude/conductor, for instance — counts as a live
        Claude. Token-basename matching does not.
        """
        return any(os.path.basename(token) == self.id for token in proc.cmdline(pid))

    def process_identity(self, pid: int) -> str:
        """``"<host>:<pid>:<start-ticks>"``. Plan 02 stores this verbatim in owner.lock."""
        return f"{self.id}:{pid}:{proc.start_ticks(pid)}"

    def process_alive(self, identity: str) -> bool:
        pid, ticks = base.parse_identity(identity, host_id=self.id)
        try:
            if proc.start_ticks(pid) != ticks:
                return False  # the pid was recycled
        except proc.ProcessGone:
            return False
        return self._is_own_process(pid)
```

Add `from conductor.hosts import base, proc` to both adapter modules' imports.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_liveness.py -q`
Expected: PASS (26 passed)

- [ ] **Step 6: Apply all four falsifiers, watch each fail, revert**

The first one — `CodexAdapter.process_alive` returning `False` — is the one this task exists for. Confirm it fails on `test_a_live_host_process_reads_as_alive[codex]` specifically, not merely somewhere.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/proc.py conductor/hosts/base.py conductor/hosts/claude.py \
        conductor/hosts/codex.py tests/conductor/hosts/test_liveness.py
git commit -m "conductor/hosts/proc.py:1-90 — per-host process identity and liveness

- identity is <host>:<pid>:<start-ticks>; start ticks defeat pid reuse (Plan 02 consumes this)
- parse_start_ticks splits on the last ')' so a comm with spaces cannot shift field 22
- a foreign-host identity raises rather than reading as dead"
```

---

### Task 8: The double-drive guard — the one that silently does not exist on Codex

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_double_drive.py`

**Interfaces:**
- Consumes: `proc.pids`, `proc.cwd`, `_is_own_process` from Task 7.
- Produces: `processes_under(roots: list[str]) -> list[int]` on both adapters.

**Hazard 2 — stated exactly.**

> `conductor/resume_script.py:214` is `for pid in $(pgrep -f 'claude' 2>/dev/null); do`, and lines 215–219 exit the fire if any matched process has its cwd under the project or the run worktree. That is the "never double-drive" guard, and it is the only thing preventing two heartbeats from launching two workers on the same run branch.
>
> **On a Codex host it fails open.** `pgrep -f 'claude'` never matches a Codex process, the loop body never executes, and the guard exits normally having protected nothing. It does not warn. It does not log. There is no failure to investigate, because from the script's point of view nothing went wrong. Two Codex workers can drive the same run, produce conflicting commits on the same branch, and the first evidence would be a merge conflict in a branch that has only one author.

This is why `processes_under` exists and why `process_alive` alone cannot replace line 214. **The two answer different questions.** `process_alive(identity)` asks "is the process I recorded still running?" — it needs a prior recording. The double-drive guard asks "is *any* process of my host already driving this directory?" — there is no prior recording, because the whole point is to detect a driver this fire does not know about. A protocol with only `process_alive` cannot express it. See §"Where this plan corrects the roadmap and the design".

**The test that matters** is `test_a_live_codex_process_in_the_project_is_detected`. It launches a real process that looks like Codex, with its cwd inside a temp project, and asserts the Codex adapter finds it. A `pgrep -f 'claude'` transliteration returns `[]` and the test fails. That is the difference between a guard and the appearance of one.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_double_drive.py`:

```python
"""The double-drive guard, per host.

conductor/resume_script.py:214 guards with `pgrep -f 'claude'`. On a Codex host that never
matches, so the loop body never runs and the guard silently protects nothing — two Codex
workers could drive the same run branch. These tests prove each adapter detects a live process
of ITS OWN host under a given root, and does not detect the other host's.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from conductor.hosts import base, proc

HOSTS = base.HOST_IDS

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the process scan reads /proc; the shipped driver already requires it",
)

_LONG_RUNNING = "#!/bin/sh\nsleep 120\n"


@pytest.fixture
def driver_in(fake_host):
    """Start a fake host process whose cwd is a directory under `root`."""
    started = []

    def _start(host_id, root):
        os.makedirs(root, exist_ok=True)
        exe = fake_host(host_id, _LONG_RUNNING)
        child = subprocess.Popen([str(exe)], cwd=str(root))
        started.append(child)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.cwd(child.pid) == os.path.realpath(str(root)):
                return child
            time.sleep(0.02)
        raise AssertionError(f"fake {host_id} never reported cwd {root}")

    yield _start
    for child in started:
        child.kill()
        child.wait(timeout=10)


def test_a_live_codex_process_in_the_project_is_detected(driver_in, tmp_path):
    """THE test for this task. A `pgrep -f 'claude'` transliteration returns [] here."""
    project = tmp_path / "project"
    child = driver_in("codex", project)
    assert child.pid in base.load("codex").processes_under([str(project)])


def test_a_live_claude_process_in_the_project_is_detected(driver_in, tmp_path):
    project = tmp_path / "project"
    child = driver_in("claude", project)
    assert child.pid in base.load("claude").processes_under([str(project)])


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_process_of_the_other_host_is_not_reported(host_id, driver_in, tmp_path):
    """Each adapter answers only for its own host. Plan 05 asks both when it needs both."""
    project = tmp_path / "project"
    driver_in(base.opposite(host_id), project)
    assert base.load(host_id).processes_under([str(project)]) == []


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_process_in_a_subdirectory_of_a_root_is_reported(host_id, driver_in, tmp_path):
    """The shipped guard matches `"$PROJECT"|"$PROJECT"/*` — a worker deep in the tree still
    counts as driving it."""
    project = tmp_path / "project"
    child = driver_in(host_id, project / "src" / "deep")
    assert child.pid in base.load(host_id).processes_under([str(project)])


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_sibling_directory_with_a_shared_prefix_is_not_reported(
    host_id, driver_in, tmp_path
):
    """`/proj` must not match `/project-other`. A prefix compare without the separator would."""
    project = tmp_path / "proj"
    project.mkdir()
    child = driver_in(host_id, tmp_path / "proj-other")
    assert child.pid not in base.load(host_id).processes_under([str(project)])


@pytest.mark.parametrize("host_id", HOSTS)
def test_multiple_roots_are_all_checked(host_id, driver_in, tmp_path):
    """The shipped guard checks the project AND the run worktree."""
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    child = driver_in(host_id, worktree)
    found = base.load(host_id).processes_under([str(project), str(worktree)])
    assert child.pid in found


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_unrelated_program_in_the_project_is_not_reported(host_id, tmp_path):
    """Narrower than the shipped guard: `pgrep -f 'claude'` matches any process whose command
    line merely contains the string, including a python run out of ~/.claude/anything."""
    project = tmp_path / "claude-shaped-path"
    project.mkdir()
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(project)
    )
    try:
        assert other.pid not in base.load(host_id).processes_under([str(project)])
    finally:
        other.kill()
        other.wait(timeout=10)


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_empty_root_list_reports_nothing_rather_than_everything(host_id, driver_in, tmp_path):
    driver_in(host_id, tmp_path / "project")
    assert base.load(host_id).processes_under([]) == []


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_result_is_sorted_and_deduplicated(host_id, driver_in, tmp_path):
    project = tmp_path / "project"
    driver_in(host_id, project)
    driver_in(host_id, project / "sub")
    found = base.load(host_id).processes_under([str(project), str(project)])
    assert found == sorted(set(found))
    assert len(found) >= 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_double_drive.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'processes_under'`

**Falsifier — run this one and look at it.** Implement `CodexAdapter.processes_under` as the literal transliteration of the shipped guard:

```python
    def processes_under(self, roots: list[str]) -> list[int]:
        return [
            pid
            for pid in proc.pids()
            if any("claude" in t for t in proc.cmdline(pid))
            and (proc.cwd(pid) or "") .startswith(tuple(roots))
        ]
```

`test_a_live_codex_process_in_the_project_is_detected` fails, and it fails by returning `[]` — which is precisely how the shipped guard fails in production, except that in production nothing is asserting. Revert.

Two more: drop the `os.sep` from the prefix comparison and `test_a_sibling_directory_with_a_shared_prefix_is_not_reported` fails. Match `self.id in token` instead of `os.path.basename(token) == self.id` and `test_an_unrelated_program_in_the_project_is_not_reported` fails.

- [ ] **Step 3: Write the implementation**

Append to **both** `ClaudeAdapter` and `CodexAdapter`, written out in each module:

```python
    def processes_under(self, roots: list[str]) -> list[int]:
        """Pids of THIS host's processes whose working directory is at or under a root.

        This is the double-drive guard. conductor/resume_script.py:214 implements it as
        ``pgrep -f 'claude'``, which on a Codex host matches nothing: the loop body never runs,
        the guard exits normally, and two workers can drive one run branch with no log line and
        no failure to investigate. A per-host scan is the fix, and it cannot be expressed as
        ``process_alive`` — that needs an identity recorded earlier, and the whole point here is
        to find a driver this fire does not know about.

        Same-user only, like the shipped guard: /proc/<pid>/cwd is unreadable across users.
        """
        resolved = [os.path.realpath(root) for root in roots]
        if not resolved:
            return []
        found = set()
        for pid in proc.pids():
            if not self._is_own_process(pid):
                continue
            where = proc.cwd(pid)
            if where is None:
                continue
            if any(
                where == root or where.startswith(root + os.sep) for root in resolved
            ):
                found.add(pid)
        return sorted(found)
```

The `where.startswith(root + os.sep)` is not a stylistic detail: `startswith(root)` alone reports a process in `/tmp/proj-other` as driving `/tmp/proj`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_double_drive.py -q`
Expected: PASS (16 passed)

- [ ] **Step 5: Apply the three falsifiers, watch each fail, revert**

- [ ] **Step 6: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/test_double_drive.py
git commit -m "conductor/hosts/{claude,codex}.py — per-host double-drive scan

- processes_under() replaces the pgrep -f 'claude' guard, which never matches on a codex host
- a live fake codex under the project is asserted found; the shipped guard's shape returns []
- prefix compare includes the separator so /proj-other does not count as /proj"
```

---

### Task 9: Isolated implementation dispatch and bounded result collection

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`, `conductor/hosts/base.py`
- Test: `tests/conductor/hosts/test_dispatch.py`

**Interfaces:**
- Consumes: `base.DispatchResult`, `base.DispatchTimeout`, `permission_profile`.
- Produces: `dispatch_implementation(prompt: str, *, timeout: float, result_path: str | None = None, posture: str = "scoped") -> DispatchResult` on both adapters.

**The posture default is `scoped`, unlike the reviewer's.** An implementation subagent writes files. Codex projects that onto `--sandbox workspace-write`. Claude has no equivalent without a settings file, so on a machine with no `CONDUCTOR_CLAUDE_SETTINGS` the Claude dispatch **refuses** rather than launching a `supervised` headless session that would stall on the first permission prompt — the silent-stall class `conductor/resume_script.py:229-236` documents at length. Refusing names the variable to set; stalling names nothing.

**Design note — what this method is, and what it is not.**

Design line 99 says "Claude uses its native subagent primitive. Codex uses a native subagent when available and enabled, otherwise a fresh non-interactive Codex child process." That sentence describes two different mechanisms at two different layers, and only one of them is reachable from Python. A cron-launched wrapper has no in-session subagent API; the Task-tool dispatch happens *inside* the host session, driven by skill prose. What a Python adapter can do is spawn a fresh child process with an isolated context.

**Decision: `dispatch_implementation` is the out-of-session child-process form, and that is stated in its docstring so nobody reads it as the in-session path.** It is what the preflight capability probe uses and what any Python-side dispatch would use. The in-session native-subagent path stays in `skills/autodev/SKILL.md` and is Plan 05's orchestrator contract. Whether Codex has a native subagent primitive at all is still open (ground truth §"Things NOT determined" item 1) — which is another reason not to encode an answer here.

**The result contract exploits the asymmetry rather than levelling it down.** Codex writes its final message to a file the caller names (`-o/--output-last-message`); Claude cannot, so its adapter captures stdout and writes that same file itself. Defining the contract the other way round — "the adapter returns captured stdout" — would generalise the Claude-side compromise and throw away the better surface (ground truth §"Output").

**Bounded means byte-capped, not schema-validated.** Codex has `--output-schema`; Claude has no equivalent, so a structured-result contract cannot be symmetric today. Plan 04 caps at 64 KiB and sets `truncated`. Structured verdicts are Plan 07's problem and Plan 07 may use `--output-schema` on the Codex side only, as a Codex-only affordance. Recorded in the residuals.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_dispatch.py`:

```python
"""Isolated implementation dispatch — the same contract against both hosts.

The result contract is "the host's final message lands in a file the caller names". Codex does
that natively with -o; Claude's adapter captures stdout and writes the file itself. Defining it
the other way round would generalise the Claude-side compromise.
"""

from __future__ import annotations

import os

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS

#: Writes the marker where its host natively puts a final message. The Codex fake honours -o;
#: the Claude fake prints to stdout. Neither knows about the other.
_CODEX_FAKE = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--output-last-message) out="$2"; shift 2 ;;
    *) last="$1"; shift ;;
  esac
done
printf 'MARKER:%s' "$last" > "$out"
exit 0
"""

_CLAUDE_FAKE = """#!/bin/sh
last=""
while [ $# -gt 0 ]; do last="$1"; shift; done
printf 'MARKER:%s' "$last"
exit 0
"""

_FAKES = {"claude": _CLAUDE_FAKE, "codex": _CODEX_FAKE}


@pytest.fixture(autouse=True)
def _roots(monkeypatch, skill_root, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    # The implementation posture is 'scoped' on both hosts; Claude's needs a settings file.
    settings = tmp_path / "scoped-settings.json"
    settings.write_text("{}")
    monkeypatch.setenv("CONDUCTOR_CLAUDE_SETTINGS", str(settings))


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_final_message_lands_in_the_named_result_file(host_id, fake_host, tmp_path):
    fake_host(host_id, _FAKES[host_id])
    target = tmp_path / "result.txt"
    result = base.load(host_id).dispatch_implementation(
        "implement the widget", timeout=30, result_path=str(target)
    )
    assert result.result_path == str(target)
    assert os.path.isfile(target)
    assert result.result_text == "MARKER:implement the widget"
    assert target.read_text() == result.result_text


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_result_records_the_host_the_argv_and_the_exit_code(
    host_id, fake_host, tmp_path
):
    fake_host(host_id, _FAKES[host_id])
    result = base.load(host_id).dispatch_implementation(
        "do the thing", timeout=30, result_path=str(tmp_path / "r.txt")
    )
    assert result.host == host_id
    assert result.returncode == 0
    assert isinstance(result.argv, tuple)
    assert os.path.basename(result.argv[0]) == host_id
    assert result.duration_s >= 0


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_result_path_is_allocated_when_the_caller_names_none(
    host_id, fake_host, tmp_path
):
    fake_host(host_id, _FAKES[host_id])
    result = base.load(host_id).dispatch_implementation("do it", timeout=30)
    assert os.path.isfile(result.result_path)
    assert result.result_text.startswith("MARKER:")


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_oversized_result_is_truncated_and_flagged(host_id, fake_host, tmp_path):
    body = {
        "codex": "#!/bin/sh\nout=''\nwhile [ $# -gt 0 ]; do case \"$1\" in -o|--output-last-message) out=\"$2\"; shift 2;; *) shift;; esac; done\nyes X | head -c 200000 > \"$out\"\nexit 0\n",
        "claude": "#!/bin/sh\nyes X | head -c 200000\nexit 0\n",
    }[host_id]
    fake_host(host_id, body)
    result = base.load(host_id).dispatch_implementation(
        "big", timeout=60, result_path=str(tmp_path / "big.txt")
    )
    assert result.truncated is True
    assert len(result.result_text.encode()) <= base.RESULT_BYTE_CAP


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_nonzero_exit_is_reported_not_raised(host_id, fake_host, tmp_path):
    """A dispatched subagent that fails is data for the orchestrator, not an exception."""
    fake_host(host_id, "#!/bin/sh\necho nope >&2\nexit 3\n")
    result = base.load(host_id).dispatch_implementation(
        "fail please", timeout=30, result_path=str(tmp_path / "r.txt")
    )
    assert result.returncode == 3


@pytest.mark.parametrize("host_id", HOSTS)
def test_a_timeout_kills_the_child_and_raises_naming_the_host_and_budget(
    host_id, fake_host, tmp_path
):
    fake_host(host_id, "#!/bin/sh\nsleep 60\n")
    with pytest.raises(base.DispatchTimeout) as excinfo:
        base.load(host_id).dispatch_implementation(
            "slow", timeout=1.0, result_path=str(tmp_path / "r.txt")
        )
    message = str(excinfo.value)
    assert host_id in message
    assert "1.0" in message


@pytest.mark.parametrize("host_id", HOSTS)
def test_dispatch_refuses_a_flaglike_prompt(host_id, fake_host, tmp_path):
    fake_host(host_id, _FAKES[host_id])
    with pytest.raises(ValueError):
        base.load(host_id).dispatch_implementation(
            "--sandbox", timeout=30, result_path=str(tmp_path / "r.txt")
        )


def test_codex_dispatch_uses_output_last_message_rather_than_parsing_stdout(
    fake_host, tmp_path
):
    """Ground truth §"Output": -o is the better surface and the contract is shaped around it."""
    fake_host("codex", _CODEX_FAKE)
    target = tmp_path / "r.txt"
    result = base.load("codex").dispatch_implementation(
        "x", timeout=30, result_path=str(target)
    )
    assert "-o" in result.argv or "--output-last-message" in result.argv
    assert str(target) in result.argv


def test_claude_dispatch_writes_the_result_file_itself(fake_host, tmp_path):
    """Claude has no -o. The adapter absorbs that difference so the contract stays symmetric."""
    fake_host("claude", _CLAUDE_FAKE)
    target = tmp_path / "r.txt"
    result = base.load("claude").dispatch_implementation(
        "x", timeout=30, result_path=str(target)
    )
    assert str(target) not in result.argv
    assert target.read_text() == result.result_text


def test_claude_dispatch_refuses_rather_than_stalling_without_a_scoped_settings_file(
    fake_host, tmp_path, monkeypatch
):
    """An implementation dispatch writes files. A headless `claude -p` with no pre-authorised
    permissions STALLS on the first prompt instead of failing — the exact silent-stall class
    conductor/resume_script.py:229-236 documents. Refusing names the variable to set."""
    fake_host("claude", _CLAUDE_FAKE)
    monkeypatch.delenv("CONDUCTOR_CLAUDE_SETTINGS", raising=False)
    with pytest.raises(base.PermissionProfileError) as excinfo:
        base.load("claude").dispatch_implementation(
            "x", timeout=30, result_path=str(tmp_path / "r.txt")
        )
    assert "CONDUCTOR_CLAUDE_SETTINGS" in str(excinfo.value)


def test_codex_dispatch_gets_workspace_write_not_read_only(fake_host, tmp_path):
    """The scoped projection differs per host: Codex's is a sandbox level, Claude's a file."""
    fake_host("codex", _CODEX_FAKE)
    result = base.load("codex").dispatch_implementation(
        "x", timeout=30, result_path=str(tmp_path / "r.txt")
    )
    assert "workspace-write" in result.argv
    assert "read-only" not in result.argv
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_dispatch.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'dispatch_implementation'`

**Falsifier:** make Claude's implementation return `DispatchResult(..., result_text=proc.stdout, ...)` without writing `result_path` — `test_the_final_message_lands_in_the_named_result_file[claude]` and `test_claude_dispatch_writes_the_result_file_itself` both fail. Drop `-o` from Codex's argv and `test_codex_dispatch_uses_output_last_message_rather_than_parsing_stdout` fails **and** `test_the_final_message_lands_in_the_named_result_file[codex]` fails. Remove the byte cap and `test_an_oversized_result_is_truncated_and_flagged` fails for both hosts. Let `subprocess.TimeoutExpired` propagate instead of converting it and `test_a_timeout_kills_the_child_and_raises_naming_the_host_and_budget` fails for both.

- [ ] **Step 3: Write the shared result plumbing**

Add to `conductor/hosts/base.py`:

```python
#: Bounded means byte-capped. Codex has --output-schema and Claude has no equivalent, so a
#: SCHEMA-validated result contract cannot be symmetric today; Plan 07 may use --output-schema
#: as a Codex-only affordance for reviewer verdicts. 64 KiB is roughly the size at which a
#: subagent summary stops being a summary.
RESULT_BYTE_CAP = 64 * 1024


def read_bounded(path: str) -> tuple[str, bool]:
    """``(text, truncated)`` for a result file, capped at ``RESULT_BYTE_CAP`` bytes."""
    if not os.path.isfile(path):
        return "", False
    with open(path, "rb") as handle:
        raw = handle.read(RESULT_BYTE_CAP + 1)
    truncated = len(raw) > RESULT_BYTE_CAP
    return raw[:RESULT_BYTE_CAP].decode("utf-8", "replace"), truncated


def allocate_result_path(host_id: str) -> str:
    """A caller-nameable result file when the caller named none."""
    fd, path = tempfile.mkstemp(prefix=f"conductor-{host_id}-result-", suffix=".txt")
    os.close(fd)
    return path
```

`os` and `tempfile` are already in `base.py`'s import block from Task 2.

- [ ] **Step 4: Write the Claude implementation**

Append to `ClaudeAdapter`:

```python
    def dispatch_implementation(
        self,
        prompt: str,
        *,
        timeout: float,
        result_path: str | None = None,
        posture: str = "scoped",
    ) -> base.DispatchResult:
        """Dispatch product work to a fresh, isolated Claude process.

        This is the OUT-OF-SESSION form. Design line 99's "Claude uses its native subagent
        primitive" describes the in-session path, which lives in skills/autodev/SKILL.md and is
        reachable only from inside a host session — a Python adapter has no Task-tool API. This
        method is what the preflight capability probe and any Python-side dispatch use.

        Claude has no --output-last-message, so the adapter captures stdout and writes the
        result file itself, keeping the cross-host contract symmetric.

        The default posture is 'scoped' because implementation writes files. On a machine with
        no CONDUCTOR_CLAUDE_SETTINGS this REFUSES rather than launching a supervised headless
        session that would stall on the first permission prompt.
        """
        base.reject_flaglike_prompt(prompt)
        profile = self.permission_profile(posture)
        target = result_path or base.allocate_result_path(self.id)
        argv = [self.executable(), "-p", prompt, *profile["argv"]]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise base.DispatchTimeout(
                f"claude implementation dispatch exceeded {timeout}s and was killed "
                f"(result file {target} may be absent or partial); shorten the dispatch or "
                f"raise the budget"
            ) from exc
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(completed.stdout)
        text, truncated = base.read_bounded(target)
        return base.DispatchResult(
            host=self.id,
            argv=tuple(argv),
            returncode=completed.returncode,
            result_path=target,
            result_text=text,
            truncated=truncated,
            duration_s=time.monotonic() - started,
        )
```

Add `import subprocess` and `import time` to `conductor/hosts/claude.py`.

- [ ] **Step 5: Write the Codex implementation**

Append to `CodexAdapter`:

```python
    def dispatch_implementation(
        self,
        prompt: str,
        *,
        timeout: float,
        result_path: str | None = None,
        posture: str = "scoped",
    ) -> base.DispatchResult:
        """Dispatch product work to a fresh, isolated `codex exec` child.

        Whether Codex has a native subagent primitive is still open (ground truth §"Things NOT
        determined" item 1), so this is the child-process form design line 99 names as the
        fallback. It is also the only form a Python adapter can reach.

        `-o/--output-last-message` writes the final message to the file the caller named, which
        is why the cross-host result contract is shaped around a named file rather than around
        captured stdout: stdout parsing is the Claude-side compromise and should not be
        generalised into the interface.
        """
        base.reject_flaglike_prompt(prompt)
        profile = self.permission_profile(posture)
        target = result_path or base.allocate_result_path(self.id)
        argv = [
            self.executable(),
            "exec",
            "-o",
            target,
            *profile["argv"],
            prompt,
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise base.DispatchTimeout(
                f"codex implementation dispatch exceeded {timeout}s and was killed "
                f"(result file {target} may be absent or partial); shorten the dispatch or "
                f"raise the budget"
            ) from exc
        text, truncated = base.read_bounded(target)
        return base.DispatchResult(
            host=self.id,
            argv=tuple(argv),
            returncode=completed.returncode,
            result_path=target,
            result_text=text,
            truncated=truncated,
            duration_s=time.monotonic() - started,
        )
```

Add `import subprocess` and `import time` to `conductor/hosts/codex.py`.

`subprocess.run(..., timeout=)` kills the child before raising `TimeoutExpired`, so the "kills the child" half of the contract is the standard library's, not something to reimplement.

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_dispatch.py -q`
Expected: PASS (18 passed)

- [ ] **Step 7: Apply all four falsifiers, watch each fail, revert**

- [ ] **Step 8: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/base.py conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/test_dispatch.py
git commit -m "conductor/hosts/{base,claude,codex}.py — isolated dispatch, bounded named result

- the contract is 'final message lands in a file the caller names': codex uses -o, claude writes it
- 64 KiB cap with a truncated flag; --output-schema is a codex-only affordance left to Plan 07
- timeout converts to DispatchTimeout naming host and budget; the child is already killed"
```

---

### Task 10: Hook installation, and the Codex PreCompact contract that is not verified

**Files:**
- Modify: `conductor/hosts/claude.py`, `conductor/hosts/codex.py`
- Test: `tests/conductor/hosts/test_hooks.py`

**Interfaces:**
- Consumes: `conductor.core.atomic.write_json_atomic` (Plan 01), `base.HookContractUnverified`.
- Produces:
  - `install_hooks(state_root: str, run_key: str, *, command: list[str]) -> str` — returns the path written
  - `hook_installed(state_root: str, run_key: str) -> bool`

**The `command` parameter is the adapter boundary, and it is a divergence from the roadmap signature.** The roadmap has `install_hooks(self, state_root, run_key) -> None`, which implies the adapter knows what the hook should *do*. It does not and must not: the PreCompact hook runs Plan 05's checkpoint sequence, and Plan 05 is unwritten. Inventing a command name here would either hardcode a verb that does not exist or block Task 10 on Plan 05. **The adapter owns host-native placement and format; the caller owns the command.** That is the correct boundary and it also makes the method testable today.

**Codex's implementation refuses, and that is the specified behaviour, not a stub.** Design line 306: "Preflight verifies the minimum host version, installs the host-native hook response, and executes a contract probe proving that it requests a checkpoint and blocks normal continuation. A missing, untrusted, disabled, or ineffective required hook blocks unattended mode rather than allowing an unbounded session."

The Codex `PreCompact` hook contract was **not verified** (ground truth §"Things NOT determined" item 2 — the design cites `https://learn.chatgpt.com/docs/hooks` and nothing local confirms it). Two further facts make silence unsafe: `~/.codex/hooks.json` on the verified machine registers `SessionStart` and `PreToolUse` handlers from a third-party install, and `--ignore-user-config` does **not** disable hooks — it only skips `config.toml`, so the scratch-`CODEX_HOME` workaround remains necessary for a clean fire (ground truth §"Hooks are not disabled by `--ignore-user-config`"). An adapter that wrote a `PreCompact` entry into that file on the strength of unverified documentation would produce exactly the "installed but ineffective" state the design says must block.

So `CodexAdapter.install_hooks` raises `HookContractUnverified` with the probe that must be run. Codex unattended mode is therefore blocked at the end of Plan 04. **That is fine, and it is the point of landing unwired:** nothing launches through this adapter yet, so the block costs nothing and prevents shipping a hook that silently does not fire.

**The shared contract, stated where symmetry actually holds:** `install_hooks` either produces a hook `hook_installed` can confirm, or refuses loudly with an actionable message. It never silently no-ops. That contract is testable identically on both hosts; the branch is inside the test.

**Claude's hook payload shape is written from the documented hooks reference and is not verified by a live probe here.** The test asserts a round trip through the adapter's own reader, not conformance to Claude's schema, because this plan cannot verify that schema without launching a real session. Design line 306's contract probe is Plan 05's, and this is recorded as a residual.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_hooks.py`:

```python
"""PreCompact hook installation.

The shared contract is "installs, or refuses loudly — never silently no-ops". Codex's hook
contract was not verified (ground truth §"Things NOT determined" item 2), and ~/.codex/hooks.json
already carries third-party handlers that --ignore-user-config does NOT disable, so writing a
PreCompact entry on the strength of documentation alone would produce exactly the
"installed but ineffective" state design line 306 says must block unattended mode.
"""

from __future__ import annotations

import json

import pytest

from conductor.hosts import base

HOSTS = base.HOST_IDS
RUN_KEY = "alpha-1a2b3c4d"
COMMAND = ["conductor", "heartbeat", "checkpoint", "--run", RUN_KEY]


@pytest.fixture
def isolated_host_config(monkeypatch, tmp_path):
    """Point both hosts' configuration at scratch directories. No test touches a real one."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return tmp_path


@pytest.mark.parametrize("host_id", HOSTS)
def test_install_hooks_either_installs_or_refuses_loudly_never_silently(
    host_id, isolated_host_config, tmp_path
):
    """The one contract that holds on both hosts."""
    adapter = base.load(host_id)
    state_root = str(tmp_path / "state")
    try:
        written = adapter.install_hooks(state_root, RUN_KEY, command=COMMAND)
    except base.HookContractUnverified as exc:
        assert adapter.hook_installed(state_root, RUN_KEY) is False
        message = str(exc)
        assert "PreCompact" in message
        assert "unattended" in message
        assert "learn.chatgpt.com/docs/hooks" in message  # the contract to verify
        return
    assert isinstance(written, str) and written
    assert adapter.hook_installed(state_root, RUN_KEY) is True


def test_claude_writes_a_precompact_hook_carrying_the_callers_command(
    isolated_host_config, tmp_path
):
    """The adapter owns placement and format; the CALLER owns the command. Plan 05 supplies
    the checkpoint verb, which does not exist yet — hardcoding one here would block Task 10
    on an unwritten plan."""
    state_root = str(tmp_path / "state")
    written = base.load("claude").install_hooks(state_root, RUN_KEY, command=COMMAND)
    payload = json.loads(open(written).read())
    assert "PreCompact" in json.dumps(payload)
    assert " ".join(COMMAND) in json.dumps(payload)


def test_claude_hook_installation_is_idempotent(isolated_host_config, tmp_path):
    """A reconcile regenerates hooks; a second install must not duplicate the entry."""
    adapter = base.load("claude")
    state_root = str(tmp_path / "state")
    first = adapter.install_hooks(state_root, RUN_KEY, command=COMMAND)
    before = open(first).read()
    second = adapter.install_hooks(state_root, RUN_KEY, command=COMMAND)
    assert second == first
    assert open(second).read() == before


def test_claude_hooks_for_two_runs_do_not_collide(isolated_host_config, tmp_path):
    """Each run owns its schedule, marker, log, and lock (design line 268). Hooks too."""
    adapter = base.load("claude")
    state_root = str(tmp_path / "state")
    adapter.install_hooks(state_root, RUN_KEY, command=COMMAND)
    adapter.install_hooks(state_root, "beta-99887766", command=["conductor", "x"])
    assert adapter.hook_installed(state_root, RUN_KEY) is True
    assert adapter.hook_installed(state_root, "beta-99887766") is True


def test_codex_refuses_and_names_the_probe_that_would_unblock_it(
    isolated_host_config, tmp_path
):
    with pytest.raises(base.HookContractUnverified) as excinfo:
        base.load("codex").install_hooks(
            str(tmp_path / "state"), RUN_KEY, command=COMMAND
        )
    message = str(excinfo.value)
    assert "codex" in message
    assert "not verified" in message
    assert "no write occurred" in message


def test_codex_refusal_writes_nothing(isolated_host_config, tmp_path):
    """A refusal that left a partial hook behind would be worse than no refusal."""
    codex_home = isolated_host_config / "codex-home"
    state_root = tmp_path / "state"
    with pytest.raises(base.HookContractUnverified):
        base.load("codex").install_hooks(str(state_root), RUN_KEY, command=COMMAND)
    assert not codex_home.exists() or not any(codex_home.rglob("*"))
    assert not state_root.exists() or not any(state_root.rglob("*"))


@pytest.mark.parametrize("host_id", HOSTS)
def test_hook_installed_is_false_before_any_install(host_id, isolated_host_config, tmp_path):
    assert base.load(host_id).hook_installed(str(tmp_path / "state"), RUN_KEY) is False


@pytest.mark.parametrize("host_id", HOSTS)
def test_an_empty_command_is_refused(host_id, isolated_host_config, tmp_path):
    """A hook registered with no command is a hook that silently does nothing — the exact
    failure mode design line 306 calls 'ineffective'."""
    with pytest.raises(ValueError):
        base.load(host_id).install_hooks(str(tmp_path / "state"), RUN_KEY, command=[])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_hooks.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'install_hooks'`

**Falsifier:** make `CodexAdapter.install_hooks` `return ""` silently — `test_install_hooks_either_installs_or_refuses_loudly_never_silently[codex]` fails on the `isinstance(written, str) and written` assertion, and `test_codex_refuses_and_names_the_probe_that_would_unblock_it` fails. Make Claude's install append a second identical entry on re-run and `test_claude_hook_installation_is_idempotent` fails. Drop the empty-command check and `test_an_empty_command_is_refused` fails for both hosts.

- [ ] **Step 3: Write the Claude implementation**

Append to `ClaudeAdapter`:

```python
    def _hook_path(self, state_root: str, run_key: str) -> str:
        """Per-run, under the run's own state directory. Each run owns its hook the way it
        owns its schedule, marker, log, and lock (design line 268), so completing one run
        cannot disturb another's."""
        return os.path.join(state_root, "runs", run_key, "hooks.json")

    def install_hooks(
        self, state_root: str, run_key: str, *, command: list[str]
    ) -> str:
        """Write this run's PreCompact hook and return the path.

        The CALLER supplies the command. The adapter owns only host-native placement and
        format: Plan 05 owns the checkpoint sequence the hook triggers, and hardcoding a verb
        that does not exist yet would block this task on an unwritten plan.

        The payload shape follows Claude Code's documented hooks reference. It is NOT verified
        by a live probe here — design line 306 requires a contract probe proving the hook
        requests a checkpoint and blocks continuation, and that probe is Plan 05's, because it
        needs a real session.
        """
        if not command:
            raise ValueError(
                "install_hooks needs a non-empty command: a hook registered with no command "
                "is the 'ineffective hook' design line 306 says must block unattended mode"
            )
        path = self._hook_path(state_root, run_key)
        write_json_atomic(
            path,
            {
                "hooks": {
                    "PreCompact": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": " ".join(command)}
                            ],
                        }
                    ]
                }
            },
        )
        return path

    def hook_installed(self, state_root: str, run_key: str) -> bool:
        doc = read_json(self._hook_path(state_root, run_key))
        return bool(doc and doc.get("hooks", {}).get("PreCompact"))
```

Add `from conductor.core.atomic import read_json, write_json_atomic` to `conductor/hosts/claude.py`. This is the only place Plan 04 writes state, and it reuses Plan 01's durable writer rather than adding a second implementation of temp-write-fsync-replace.

- [ ] **Step 4: Write the Codex implementation**

Append to `CodexAdapter`:

```python
    def _hook_path(self, state_root: str, run_key: str) -> str:
        return os.path.join(state_root, "runs", run_key, "hooks.json")

    def install_hooks(
        self, state_root: str, run_key: str, *, command: list[str]
    ) -> str:
        """Refuse: Codex's PreCompact hook contract has not been verified on this host.

        This is design line 306's rule, not a stub. Three facts make writing a hook here
        unsafe:

        1. The contract is documented at https://learn.chatgpt.com/docs/hooks and was NOT
           verified locally (ground truth §"Things NOT determined" item 2).
        2. ~/.codex/hooks.json already registers third-party SessionStart and PreToolUse
           handlers, so this adapter would be merging into a file it does not own.
        3. `--ignore-user-config` does NOT disable hooks — it only skips config.toml — so an
           "isolated" fire still runs them (ground truth §"Hooks are not disabled by
           --ignore-user-config").

        Writing an unverified entry would produce exactly the "installed but ineffective"
        state the design says must block unattended mode, except silently.
        """
        if not command:
            raise ValueError(
                "install_hooks needs a non-empty command: a hook registered with no command "
                "is the 'ineffective hook' design line 306 says must block unattended mode"
            )
        raise base.HookContractUnverified(
            "codex PreCompact hook contract is not verified on this host, so unattended mode "
            "is blocked and no hook was installed (no write occurred). To unblock: verify the "
            "contract at https://learn.chatgpt.com/docs/hooks against codex-cli "
            f"{'.'.join(map(str, self.MINIMUM_VERSION))} or newer with a probe proving the "
            "hook requests a checkpoint and blocks normal continuation, then implement "
            "CodexAdapter.install_hooks. Supervised (attended) runs are unaffected."
        )

    def hook_installed(self, state_root: str, run_key: str) -> bool:
        """Always False until ``install_hooks`` is implemented. Honest, not optimistic."""
        return False
```

Add `import os` to `conductor/hosts/codex.py` if Task 5 has not already.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_hooks.py -q`
Expected: PASS (11 passed)

- [ ] **Step 6: Remove the Task 1 type ignores**

Both adapters now satisfy the full `HostAdapter` protocol. Delete the two `# type: ignore[return-value]  # conforming from Task 10` comments in `base.load` and confirm `pyright conductor/hosts` is clean without them. **If pyright reports non-conformance, a signature drifted** — fix the adapter to match `base.HostAdapter`, not the protocol to match the adapter.

- [ ] **Step 7: Apply the three falsifiers, watch each fail, revert**

- [ ] **Step 8: Lint, typecheck, commit**

```bash
ruff check conductor/hosts tests/conductor/hosts && \
  ruff format --check conductor/hosts tests/conductor/hosts && pyright conductor/hosts
git add conductor/hosts/base.py conductor/hosts/claude.py conductor/hosts/codex.py \
        tests/conductor/hosts/test_hooks.py
git commit -m "conductor/hosts/{claude,codex}.py — PreCompact hook installation

- the caller supplies the command; the adapter owns host-native placement and format only
- codex refuses with HookContractUnverified: the contract is undocumented locally and
  --ignore-user-config does not disable hooks, so an unverified write would be silently ineffective
- both adapters now satisfy HostAdapter; the load() type ignores are removed"
```

---

### Task 11: `conductor host` — preflight floors and read-only diagnostics

**Files:**
- Create: `conductor/hosts/cli.py`
- Modify: `bin/conductor`
- Test: `tests/conductor/hosts/test_host_cli.py`

**Interfaces:**
- Consumes: the whole adapter surface, `base.assert_minimum_version`.
- Produces:
  - `conductor host list` — the supported hosts and their floors
  - `conductor host show <id>` — resolved executable, source root, version, native invocation, postures
  - `conductor host preflight [--host <id>] [--unattended] [--json]` — exit 0 iff every check passes

**Why a new verb rather than extending `conductor preflight`.** `conductor preflight` is invoked by `skills/start/SKILL.md:26` on every start, and changing it changes live behaviour for the run currently executing out of this checkout. Plan 04 lands unwired. `conductor/preflight.py` is not modified; a new read-only verb is added that nothing calls automatically. **Plan 05 folds the floor check into the start path.**

**`--unattended` is the design's own distinction, not an invention.** Design line 306 blocks *unattended* mode on a missing or ineffective required hook. Attended use is unaffected. So the default preflight passes for both hosts today; `--unattended` requires an installable PreCompact hook and therefore fails on Codex, correctly and loudly, until the contract is verified.

**The dispatch check does not dispatch by default.** Design line 99 says an adapter that cannot dispatch isolated implementation work fails preflight. A live probe costs a model call, so the default renders the dispatch argv and reports it as unprobed; `--probe` actually dispatches a trivial prompt. Reporting "dispatch: ok" without having dispatched would be the sort of claim this repository's residuals file exists to prevent.

- [ ] **Step 1: Write the failing test**

Create `tests/conductor/hosts/test_host_cli.py`:

```python
"""`conductor host` — read-only diagnostics and the preflight floor.

Deliberately a NEW verb. `conductor preflight` is invoked by skills/start/SKILL.md:26 on every
start; changing it would change live behaviour, and Plan 04 lands unwired.
"""

from __future__ import annotations

import json

import pytest

from conductor.hosts import base, cli

HOSTS = base.HOST_IDS
_REAL_OUTPUT = {"claude": "2.1.227 (Claude Code)", "codex": "codex-cli 0.147.0"}
_PARSED_VERSION = {"claude": "2.1.227", "codex": "0.147.0"}


def _printer(line: str) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' {line!r}\nexit 0\n"


@pytest.fixture(autouse=True)
def _roots(monkeypatch, skill_root, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(skill_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_CLAUDE_SETTINGS", raising=False)


def test_list_names_both_hosts_and_their_floors(capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    for host_id in HOSTS:
        assert host_id in out
    assert "2.1.224" in out
    assert "0.147.0" in out


@pytest.mark.parametrize("host_id", HOSTS)
def test_show_reports_the_resolved_surface(host_id, fake_host, skill_root, capsys):
    fake_host(host_id, _printer(_REAL_OUTPUT[host_id]))
    assert cli.main(["show", host_id]) == 0
    out = capsys.readouterr().out
    assert host_id in out
    assert str(skill_root) in out  # the resolved source root
    assert _PARSED_VERSION[host_id] in out
    assert base.load(host_id).native_invocation("autodev") in out


def test_show_refuses_an_unknown_host(capsys):
    assert cli.main(["show", "gemini"]) != 0
    assert "gemini" in capsys.readouterr().err


@pytest.mark.parametrize("host_id", HOSTS)
def test_preflight_passes_a_current_host_in_the_default_attended_mode(
    host_id, fake_host, capsys
):
    fake_host(host_id, _printer(_REAL_OUTPUT[host_id]))
    assert cli.main(["preflight", "--host", host_id]) == 0


@pytest.mark.parametrize("host_id", HOSTS)
def test_preflight_fails_below_the_floor_and_prints_the_minimum_version_command(
    host_id, fake_host, capsys
):
    """Design line 559: older host versions fail preflight with the documented
    minimum-version command."""
    fake_host(host_id, _printer("0.0.1"))
    assert cli.main(["preflight", "--host", host_id]) != 0
    err = capsys.readouterr().err
    assert f"{host_id} --version" in err


@pytest.mark.parametrize("host_id", HOSTS)
def test_preflight_fails_when_the_executable_is_absent(host_id, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    assert cli.main(["preflight", "--host", host_id]) != 0
    assert f"command -v {host_id}" in capsys.readouterr().err


def test_unattended_preflight_fails_on_codex_because_the_hook_contract_is_unverified(
    fake_host, capsys
):
    """Design line 306: a missing or ineffective required hook blocks UNATTENDED mode."""
    fake_host("codex", _printer(_REAL_OUTPUT["codex"]))
    assert cli.main(["preflight", "--host", "codex"]) == 0
    assert cli.main(["preflight", "--host", "codex", "--unattended"]) != 0
    assert "PreCompact" in capsys.readouterr().err


def test_unattended_preflight_passes_on_claude(fake_host):
    fake_host("claude", _printer(_REAL_OUTPUT["claude"]))
    assert cli.main(["preflight", "--host", "claude", "--unattended"]) == 0


@pytest.mark.parametrize("host_id", HOSTS)
def test_the_dispatch_check_does_not_claim_success_it_did_not_observe(
    host_id, fake_host, capsys
):
    """Reporting 'dispatch: ok' without dispatching is the claim this repository's residuals
    file exists to prevent."""
    fake_host(host_id, _printer(_REAL_OUTPUT[host_id]))
    assert cli.main(["preflight", "--host", host_id, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["dispatch"]["status"] == "unprobed"
    assert report["checks"]["dispatch"]["argv"]


@pytest.mark.parametrize("host_id", HOSTS)
def test_json_output_names_every_check_and_the_overall_verdict(host_id, fake_host, capsys):
    fake_host(host_id, _printer(_REAL_OUTPUT[host_id]))
    cli.main(["preflight", "--host", host_id, "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["host"] == host_id
    assert report["ok"] is True
    assert set(report["checks"]) == {
        "executable",
        "source_root",
        "version",
        "permissions",
        "dispatch",
        "hooks",
    }


def test_preflight_with_no_host_checks_both(fake_host, capsys):
    fake_host("claude", _printer(_REAL_OUTPUT["claude"]))
    fake_host("codex", _printer(_REAL_OUTPUT["codex"]))
    assert cli.main(["preflight"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "codex" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/conductor/hosts/test_host_cli.py -q`
Expected: FAIL — `ImportError: cannot import name 'cli' from 'conductor.hosts'`

**Falsifier:** make the `dispatch` check report `"status": "ok"` without probing and `test_the_dispatch_check_does_not_claim_success_it_did_not_observe` fails for both hosts. Make `--unattended` ignore the hook check and `test_unattended_preflight_fails_on_codex_because_the_hook_contract_is_unverified` fails. Swallow `HostVersionTooOld` and exit 0 and `test_preflight_fails_below_the_floor_and_prints_the_minimum_version_command` fails for both hosts.

- [ ] **Step 3: Write the implementation**

Create `conductor/hosts/cli.py`:

```python
"""`conductor host` — read-only host diagnostics and the supported-version floor.

A NEW verb rather than an extension of `conductor preflight`, deliberately.
`skills/start/SKILL.md:26` invokes `conductor preflight` on every start; changing that module
would change live behaviour, and this plan lands the adapter unwired. Plan 05 folds the floor
check into the start path.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile

from conductor.hosts import base


def _check(fn):
    """Run one check; return ``(status, detail)`` without letting it abort the report."""
    try:
        return "ok", fn()
    except (base.HostUnavailable, base.HostVersionTooOld, base.PermissionProfileError) as exc:
        return "fail", str(exc)
    except base.HookContractUnverified as exc:
        return "unverified", str(exc)


def report(host_id: str, *, unattended: bool = False, probe: bool = False) -> dict:
    """Every check, with its own verdict, and one overall ``ok``."""
    adapter = base.load(host_id)
    checks: dict[str, dict] = {}

    status, detail = _check(adapter.executable)
    checks["executable"] = {"status": status, "detail": detail}
    status, detail = _check(adapter.source_root)
    checks["source_root"] = {"status": status, "detail": detail}
    status, detail = _check(lambda: ".".join(map(str, base.assert_minimum_version(adapter))))
    checks["version"] = {
        "status": status,
        "detail": detail,
        "floor": ".".join(map(str, adapter.minimum_version())),
    }

    def _permissions():
        """Every posture must validate, except that 'scoped' may be unconfigured.

        A missing least-privilege settings file is an operator choice, not a broken adapter —
        conductor/resume_script.py:322-337 nudges rather than refuses. A failure in
        'supervised' or 'full-bypass' is a broken adapter and does refuse.
        """
        notes = []
        for posture in base.POSTURES:
            try:
                adapter.validate_permissions(adapter.permission_profile(posture))
                notes.append(f"{posture}: ok")
            except base.PermissionProfileError:
                if posture != "scoped":
                    raise
                notes.append("scoped: unconfigured (set CONDUCTOR_CLAUDE_SETTINGS to enable)")
        return "; ".join(notes)

    status, detail = _check(_permissions)
    checks["permissions"] = {"status": status, "detail": detail}

    # Design line 99: an adapter that cannot dispatch isolated implementation work fails
    # preflight. Rendering the vector proves the adapter CAN construct a dispatch; only
    # --probe proves the host answers. Never report "ok" for something not observed.
    dispatch: dict = {}
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            if probe:
                result = adapter.dispatch_implementation(
                    "Reply with the single word: ready.",
                    timeout=120,
                    result_path=handle.name,
                )
                dispatch = {
                    "status": "ok" if result.returncode == 0 else "fail",
                    "argv": list(result.argv),
                    "detail": result.result_text[:200],
                }
            else:
                argv = adapter.worker_argv(
                    state_root="/dev/null",
                    run_key="preflight",
                    project_root=".",
                )
                dispatch = {
                    "status": "unprobed",
                    "argv": argv,
                    "detail": "argv rendered; re-run with --probe to execute a live dispatch",
                }
    except (base.HostUnavailable, base.DispatchTimeout, ValueError) as exc:
        dispatch = {"status": "fail", "argv": [], "detail": str(exc)}
    checks["dispatch"] = dispatch

    if unattended:
        status, detail = _check(
            lambda: adapter.install_hooks(
                tempfile.mkdtemp(prefix="conductor-preflight-"),
                "preflight",
                command=["true"],
            )
        )
    else:
        status, detail = "skipped", "hooks are required only for unattended mode"
    checks["hooks"] = {"status": status, "detail": detail}

    acceptable = {"ok", "skipped"} | ({"unprobed"} if not probe else set())
    return {
        "host": host_id,
        "unattended": unattended,
        "ok": all(c["status"] in acceptable for c in checks.values()),
        "checks": checks,
    }


def _print_human(doc: dict) -> None:
    print(f"host {doc['host']}: {'OK' if doc['ok'] else 'FAILED'}")
    for name, check in doc["checks"].items():
        print(f"  {name}: {check['status']} — {check.get('detail', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conductor host",
        description="Inspect and preflight the Claude Code and Codex hosts. Read-only.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="supported hosts and their version floors")
    show = sub.add_parser("show", help="one host's resolved surface")
    show.add_argument("host")
    pre = sub.add_parser("preflight", help="floor + capability checks; exit 0 iff all pass")
    pre.add_argument("--host", default=None, help="default: every supported host")
    pre.add_argument(
        "--unattended",
        action="store_true",
        help="also require an installable PreCompact hook (design line 306)",
    )
    pre.add_argument(
        "--probe", action="store_true", help="execute a live implementation dispatch"
    )
    pre.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        for host_id in base.HOST_IDS:
            adapter = base.load(host_id)
            print(
                f"{host_id}: floor {'.'.join(map(str, adapter.minimum_version()))}, "
                f"reviewer {base.opposite(host_id)}, "
                f"native invocation {adapter.native_invocation('autodev')}"
            )
        return 0

    if args.cmd == "show":
        try:
            adapter = base.load(args.host)
        except base.UnknownHost as exc:
            print(str(exc), file=sys.stderr)
            return 2
        doc = report(args.host)
        _print_human(doc)
        print(f"  native invocation: {adapter.native_invocation('autodev')}")
        return 0 if doc["ok"] else 1

    hosts = [args.host] if args.host else list(base.HOST_IDS)
    try:
        docs = [
            report(h, unattended=args.unattended, probe=args.probe) for h in hosts
        ]
    except base.UnknownHost as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(docs[0] if args.host else docs, indent=2, sort_keys=True))
    else:
        for doc in docs:
            _print_human(doc)
    failures = [
        f"{doc['host']}: {name}: {check.get('detail', '')}"
        for doc in docs
        for name, check in doc["checks"].items()
        if not doc["ok"] and check["status"] not in ("ok", "skipped", "unprobed")
    ]
    for line in failures:
        print(line, file=sys.stderr)
    return 0 if all(doc["ok"] for doc in docs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add the verb to `bin/conductor`**

Insert next to the existing `preflight` line (`bin/conductor:32`), in the same style:

```bash
  host) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.hosts.cli "$@" ;;
```

And add to the usage text at `bin/conductor:74`, after the `conductor preflight` line:

```
  conductor host {list|show <id>|preflight [--host <id>] [--unattended] [--probe] [--json]}\n
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/conductor/hosts/test_host_cli.py -q`
Expected: PASS (17 passed)

- [ ] **Step 6: Run the verb for real**

Run: `./bin/conductor host list && ./bin/conductor host preflight`
Expected: exit 0 on this workstation. Claude Code `2.1.227` ≥ `2.1.224` and Codex CLI `0.147.0` = the floor, both verified 2026-08-12. `./bin/conductor host preflight --unattended` exits 1 naming the Codex PreCompact contract — that is the correct answer today, not a failure of this step.

- [ ] **Step 7: Apply the three falsifiers, watch each fail, revert**

- [ ] **Step 8: Full suite, gate, lint, typecheck, commit**

```bash
ruff check . && ruff format --check conductor/hosts tests/conductor/hosts conductor/hosts/cli.py
pyright .
pytest -q
./bin/conductor gate verify
git add conductor/hosts/cli.py bin/conductor tests/conductor/hosts/test_host_cli.py
git commit -m "conductor/hosts/cli.py:1-190, bin/conductor:32,74 — conductor host verb

- list/show/preflight; exit 0 iff every check passes, per-check verdicts in --json
- a new verb, not an extension of conductor preflight, which start invokes on every run
- dispatch reports 'unprobed' unless --probe: no check claims a success it did not observe"
```

---

## Where this plan corrects the roadmap and the design

Recorded here rather than papered over, because a later reader comparing the plan to the roadmap will otherwise assume the plan drifted.

**1. The adapter surface is nineteen members, not eleven or twelve.** The design lists eleven capabilities (lines 85–95); the roadmap's Protocol block (lines 309–326) lists twelve methods. Neither is sufficient. Each addition and each split:

| Member | Why it is not in the roadmap's twelve |
| --- | --- |
| `upgrade_hint()` | design line 559 requires preflight to fail "with the documented minimum-version command"; nothing in the twelve renders one |
| `launch_prompt()` split from `native_invocation()` | `$conductor:autodev` is a prompting convention, not a host primitive — see item 3. A single method cannot be both "what a user types" and "what is deterministic to launch" |
| `worker_env()` | Claude's proven worker argv is a bare slash command (`conductor/resume_script.py:261`). The run key cannot travel in argv without changing a dispatch path many live fires have proven, so it travels in the environment |
| `process_identity()` | `process_alive(identity)` needs an identity to have been minted. Nothing in the twelve mints one, and Plan 02 storing a bare PID would reintroduce PID reuse |
| `processes_under()` | **`process_alive` cannot express the double-drive guard.** It asks "is the process I recorded alive?"; the guard asks "is any process of my host already driving this directory?", with no prior recording — that is the whole point. See Task 8 |
| `hook_installed()` | `install_hooks -> None` gives a caller no way to confirm the hook exists, which makes design line 306's "missing, untrusted, disabled, or ineffective" undetectable |
| `DispatchResult` | referenced in the roadmap's `dispatch_implementation` signature and **defined nowhere**. Task 1 defines it |

**2. `install_hooks` takes the command.** The roadmap's `install_hooks(state_root, run_key)` implies the adapter knows what the hook should do. It runs Plan 05's checkpoint sequence, which does not exist. The caller supplies the command; the adapter owns host-native placement and format.

**3. The design's `$conductor:*` launch is not implementable as written.** Design line 101 says Codex exposes `$conductor:*` skills and line 97 warns the token must not be shell-expanded. Both statements are true and neither is the problem. `$name` is resolved by the *model* reading a dispatch table in `~/.codex/AGENTS.md` — on the verified machine, oh-my-codex's third-party file. On a machine without it, `$conductor:autodev` resolves to nothing. Fixing the quoting does not make the launch work; it makes it fail differently. Task 5 emits the expansion (an explicit `SKILL.md` path) instead. The user-facing surface design line 101 describes is preserved by `native_invocation`.

**4. The design's session-resume assumption is factually wrong, and this plan declares the correct behaviour anyway.** `codex exec resume`, `codex resume`, and `codex fork` exist. See §"Explicit non-goal".

**5. `dispatch_implementation` cannot be the native subagent primitive.** Design line 99 describes an in-session mechanism. A Python adapter spawned from cron has no Task-tool API. The method is the out-of-session child-process form, and its docstring says so, so that nobody reads it as the in-session path. Whether Codex has a native subagent primitive at all is still open (ground truth §"Things NOT determined" item 1).

**6. "Bounded structured result collection" (design capability 11) cannot be symmetric.** Codex has `--output-schema`; Claude has no equivalent. Plan 04 delivers bounded *text* with a byte cap. Structured verdicts are Plan 07's, and Plan 07 may use `--output-schema` as a Codex-only affordance.

**7. Roadmap Plan 00 is superseded.** The Global Constraints block still describes relocating `~/.claude/conductor` to `~/programming/conductor` with a quarantine rename. `docs/superpowers/specs/2026-08-12-conductor-source-decommission-design.md` replaces that with a fresh clone and a decommission checklist. **Plan 04 is unaffected either way** — it never reads, writes, moves, or names the checkout root; `source_root()` resolves from an environment override or `__file__`. Noted only so a reader does not treat the constraint block as current.

**8. Two line references in the roadmap's own framing are off by a few lines.** Corrected in §"The real surface": the `REQUIRED_COMMANDS` literal is `conductor/preflight.py:15-26`, not `:14-23`, and the `scheduled_tasks.json` path is built at `conductor/driver.py:55-59`. Every other cited line was verified exactly as claimed at commit `9971573`.

**9. The Codex plugin system is absent from the design entirely.** It exists (`codex plugin {add, list, marketplace, remove}`), with `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, a `policy` block Claude has no counterpart for, and `--sparse` file scoping Claude has no mechanism for at all. That is Plan 09's problem, not this one's — but note the ordering warning in the ground-truth doc: **packaging conductor for Codex before this adapter lands produces a plugin that installs cleanly and then tries to spawn `claude`**, failing at first fire on a machine that may not have Claude installed. Plan 09 must not start before Plan 04 merges.

---

## Residuals this plan knowingly leaves

Written now, in the plan, so the implementer does not have to rediscover them and the Plan 04 residuals file has a starting point.

- **Codex `PreCompact` is unimplemented and blocks unattended Codex runs.** By design (Task 10). Unblocking requires verifying `https://learn.chatgpt.com/docs/hooks` against codex-cli `0.147.0` with a probe proving the hook requests a checkpoint and blocks continuation. Owner: Plan 05, which needs the probe anyway for design line 306.
- **Claude's hook payload shape is unprobed.** Task 10's tests assert a round trip through the adapter's own reader, not conformance to Claude's schema. A live-session contract probe is Plan 05's.
- **`processes_under` and `process_alive` are Linux-only and same-user-only.** `/proc` is required; another user's `cwd` is unreadable. Both limitations are identical to the shipped `pgrep` guard, so nothing regresses — but on macOS the adapter would need `ps`/`libproc`, and no test covers that.
- **Codex's installed plugin-cache layout is not encoded.** The only observed root was `$CODEX_HOME/.tmp/plugins`, which no documentation makes contractual. Operators with an installed package set `CODEX_PLUGIN_ROOT`. Plan 09 establishes the real layout.
- **`codex exec review` is unused.** It exists; its argument contract was not verified. Plan 07 owns the decision.
- **No adapter consults `~/.claude/scheduled_tasks.json` or any Codex analogue.** Whether Codex has a native scheduler was *not observed*, not *confirmed absent* (ground truth §"Things NOT determined" item 3). Plan 05 must resolve that before assuming OS cron on both hosts.
- **`worker_argv`'s `state_root` parameter is unused on both hosts.** It is in the roadmap signature and Plan 05 may need it; it is carried rather than dropped so Plan 05 does not have to widen the signature. If Plan 05 finds no use, delete it there.
- **Nothing calls any of this.** That is the plan's central constraint, not an oversight. Plan 05's wiring is where the adapter first carries load, and that is when the argv, the environment, and the hook payload get their first live exercise.

---

## Definition of done for this plan

- [ ] `pytest -q` green, with the new tests counted. Record the before/after counts in the PR, as Plan 01 did.
- [ ] `./bin/conductor gate verify` clean — assertions A1–A16 unchanged and unweakened.
- [ ] `ruff check .` clean; `ruff format --check` clean on **every file this plan created or modified** (not repo-wide: 11 pre-existing files fail).
- [ ] `pyright .` reports no new errors, and both adapters satisfy `base.HostAdapter` with the Task 1 type ignores deleted.
- [ ] `./bin/conductor host list`, `./bin/conductor host show claude`, `./bin/conductor host show codex`, and `./bin/conductor host preflight` all work from a clean checkout. `./bin/conductor host preflight --unattended` exits 1 naming the Codex PreCompact contract.
- [ ] **The adapter is unwired.** `grep -n 'conductor\.hosts\|conductor/hosts' conductor/driver.py conductor/resume_script.py conductor/preflight.py` returns nothing.
- [ ] **No host string escaped the package.** `grep -rn 'dangerously-skip-permissions\|dangerously-bypass-approvals\|CLAUDE_PLUGIN_ROOT\|CODEX_PLUGIN_ROOT\|codex exec' conductor/core/` returns nothing.
- [ ] **Every falsifier was run.** For each task, the named edit was applied, the named test observed failing, and the edit reverted. State this explicitly in the PR — three of nineteen tests on a recent branch passed with their own fix deleted, and Plan 01 found four more the same way.
- [ ] `conductor/driver.py`, `conductor/resume_script.py`, and `conductor/preflight.py` are byte-identical to `9971573`: `git diff 9971573 -- conductor/driver.py conductor/resume_script.py conductor/preflight.py` is empty.

---

## Self-review

Run against the design and the ground truth before handing off.

**Spec coverage.** Design §"System architecture" adapter capabilities (lines 85–95) map to tasks: host identifier and executable discovery → Tasks 1, 2; plugin/source-root discovery → Task 2; worker prompt construction and launch → Tasks 4, 5; reviewer prompt construction and launch → Task 4; preflight and version checks → Tasks 3, 11; least-privilege permission profile validation → Task 6; process identity and liveness → Tasks 7, 8; hook installation → Task 10; native invocation rendering → Task 5; implementation-subagent dispatch into an isolated context → Task 9; bounded structured result collection → Task 9, **partially** (byte-bounded, not schema-structured; see correction 6). §"Packaging and installation" host floor (line 365) → Tasks 3, 11. §"Unit and contract tests" bullets: "Claude and Codex adapter contracts" → every task; "isolated implementation-subagent dispatch" → Task 9; "permission profiles and bypass non-transfer" → Task 6. "dispatch-to-commit attribution" and "worker/reviewer inversion" are **Plans 05 and 07**; Plan 04 supplies only `opposite()` and the argv.

**Deliberately deferred, with the owning plan named in-line:** hook contract probe and the wiring of every adapter method into a launch path (Plan 05), lease and takeover semantics over `process_alive` (Plan 02), reviewer prompt content and verdict schema (Plan 07), Codex packaging (Plan 09).

**Placeholder scan.** No step says "TBD", "add appropriate error handling", "write tests for the above", or "similar to Task N". Every code step carries the code. Two forward references exist and both name the exact step to copy from: Task 4 references Task 5's `launch_prompt` body and Task 6's `permission_profile` body, with the instruction to implement them once rather than write a temporary version. Task 10 Step 6 deletes the two type ignores Task 1 Step 6 introduces — the only deliberately temporary artifact in the plan, and both ends are written down.

**Type consistency check.** `host_id` names the string everywhere (`base.load(host_id)`, `adapter.id`, `profile["host"]`). `identity` is always `"<host>:<pid>:<start-ticks>"` and is minted only by `process_identity`. `posture` is always one of `base.POSTURES` and is never a host flag. `profile` is always `{"host", "posture", "argv"}`. `result_path` is the same parameter name in `dispatch_implementation` and the same field name in `DispatchResult`. `source_root()` returns a directory containing `skills/<name>/SKILL.md` in both adapters and in `validated_source_root`. `state_root` is the `.conductor` directory, never the repository root — the same convention Plan 01 fixed. `proc.ProcessGone` is raised by `start_ticks` and `process_identity` only; `cmdline` and `cwd` return empty/`None` because whole-table scans race against exits constantly.

**One thing a reviewer should push on.** Task 4 and Task 6 are mutually referential: `worker_argv` calls `permission_profile`, and Task 4's tests need Task 6's implementation. They are written as separate tasks because a reviewer could reject the permission projection while accepting the argv grammar, or the reverse. If the executing agent finds the ordering awkward, implementing Task 6 before Task 4 is correct and changes nothing else.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-10-plan-04-host-adapters.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration. Carry each task's falsifier into the reviewer's dispatch and have the reviewer build its own revert-proof rather than trusting the implementer's report; Plan 01's residuals record three implementer reports on one branch that claimed coverage which did not exist, all three caught this way.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
