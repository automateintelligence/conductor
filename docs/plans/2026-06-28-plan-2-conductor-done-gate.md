# Conductor MVP — Plan 2: Done-Gate Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build conductor's **done-gate execution** layer — the fail-closed assertion runner
(`conductor assert run`) and the `/conductor:assertions-to-tests` bridge that turns
`spec-craft`'s assertion specs into runnable, manifest-wired tests — so the loop has an
objective, machine-checked terminal condition.

**Architecture:** The `conductor` plugin **declares `dependencies: ["spec-craft"]`** (Plan 1),
so installing conductor auto-installs spec-craft and `/spec-craft:executable-assertions` is
available. This plan promotes the Stage-0 E4 prototype (`assertions/run.py`,
`assertions/manifest.yaml`, `bin/conductor`) to production and adds the bridge skill.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`,
YAML manifest (pyyaml optional, built-in fallback parser).

## Global Constraints

- **Plugin + dependency:** one `conductor` plugin (§2.1), declaring `dependencies: ["spec-craft"]`.
- **Fail-closed gate (§5.2):** missing/unparseable manifest, **a level-filter that matches zero
  assertions**, un-executable command, crash, per-assertion timeout, or **overall wall-clock**
  overrun = **NOT done**, non-zero exit. Never green-by-default.
- **Exit codes:** `0` all green · `1` ≥1 red · `2` manifest missing · `3` manifest unparseable
  · `4` overall wall-clock timeout · `5` **no assertions match the requested level** (Codex #1).
- **Manifest contract (§5.2):** `id, claim, command, setup, teardown, timeout,
  level ∈ {spec,phase,task}` (gate tier; spec-level decides spec-done, §7),
  `kind ∈ {example,property,contract}` (form). `level` and `kind` are distinct fields.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task complete.
- **Commits:** atomic; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Reuse Stage 0:** `assertions/run.py` (199 lines), `assertions/manifest.yaml`, `bin/conductor` exist (E4). Promote/harden.

## Conventions (LOCKED — inherited by Plans 3–4)
Namespaced invocation `/conductor:<skill>` (supervisor `/conductor:conductor`, worker
`/conductor:autodev`); conducted skills keep their namespace (`/spec-craft:executable-assertions`,
`/superpowers:test-driven-development`); no bare names/aliases (amendment F). `level`≠`kind`.
Validate with `claude plugin validate ./ [--strict]`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Conductor manifest with `dependencies: ["spec-craft"]`. |
| `skills/assertions-to-tests/SKILL.md` | `/conductor:assertions-to-tests` — spec-craft's specs → manifest-wired tests via `/superpowers:test-driven-development`. |
| `assertions/run.py` | The runner — **modify** (E4 exists): teardown, isolation, **strictly-enforced** overall timeout, `--level` (empty = fail-closed), `kind`. |
| `assertions/manifest.yaml` | Done-gate manifest. |
| `bin/conductor` | CLI — `assert run [--level L]`. |
| `tests/test_runner.py` | Runner pytest: fail-closed (6 modes), teardown, isolation, strict overall timeout, level filter (incl. empty). |
| `tests/test_skill_outputs.py` | Bridge skill structural check. |
| `tests/test_plugin.py` | Manifest schema + `dependencies: ["spec-craft"]`. |
| `tests/test_foundation_e2e.py` | RED→GREEN gate end-to-end. |

---

## Task 1: Conductor plugin scaffold (with spec-craft dependency)

**Files:** Create `.claude-plugin/plugin.json`, `tests/__init__.py`, `tests/test_plugin.py`

**Interfaces:** installable `conductor` plugin that auto-installs `spec-craft`; skills `/conductor:<skill>`.

- [ ] **Step 1: Write the failing manifest test**

```python
# tests/test_plugin.py
import json, os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_plugin_manifest_schema_and_dependency():
    data = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    assert data.get("name") == "conductor"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), "semver version required"
    assert "spec-craft" in data.get("dependencies", []), "must depend on spec-craft"
    assert set(data) <= {"name", "version", "description", "author", "dependencies",
                         "displayName", "homepage", "repository", "license"}
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write the manifest**

```json
{
  "name": "conductor",
  "version": "0.1.0",
  "description": "Autonomous spec-completion loop: spec -> plans -> phases -> machine-checked done.",
  "author": "Jeffrey A. Daniels",
  "dependencies": ["spec-craft"]
}
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Static validation (recorded smoke):** `claude plugin validate .`.
- [ ] **Step 6: Commit** (`Plan2 T1: conductor manifest (dependencies: spec-craft)` … + trailer).

---

## Task 2: Harden the assertion runner

E4's `assertions/run.py` (199 lines) already does manifest load (pyyaml + fallback),
per-assertion `setup`+`command`, per-assertion timeout, `results.json`, fail-closed
(missing/unparseable/crash/per-assertion-timeout), exit `0/1/2/3`. **Add:** `teardown`,
**isolation**, a **strictly-enforced overall wall-clock** limit (Codex #2), `--level` with
**empty-match = fail-closed** (Codex #1), and `kind` passthrough.

**Files:** Modify `assertions/run.py`, `bin/conductor`, `assertions/manifest.yaml`; test `tests/test_runner.py`

**Interfaces:** `conductor assert run [--level spec|phase|task]` → per-id `[PASS]/[FAIL]`,
`SUMMARY`, `assertions/run/results.json` (incl. `kind`); exit `0/1/2/3/4/5`.

- [ ] **Step 1: Write failing tests (incl. empty-filter + sub-second budget)**

```python
# tests/test_runner.py
import os, subprocess, sys, textwrap, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py")]

def _manifest(tmp_path, body): (tmp_path / "manifest.yaml").write_text(body)
def _env(tmp_path, **extra):
    return {**os.environ, "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"), **extra}

def test_teardown_runs_after_command(tmp_path):
    marker = tmp_path / "torn_down"
    _manifest(tmp_path, textwrap.dedent(f"""\
        assertions:
          - id: t1
            command: "true"
            teardown: "touch {marker}"
            timeout: 10
            level: spec
    """))
    subprocess.run(RUN, env=_env(tmp_path), cwd=ROOT)
    assert marker.exists()

def test_isolation_clean_cwd_per_assertion(tmp_path):
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: a
            command: "touch leaked"
            level: spec
          - id: b
            command: "test ! -e leaked"
            level: spec
    """))
    assert subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_ISOLATE="1"), cwd=ROOT).returncode == 0

def test_level_filter_runs_only_spec(tmp_path):
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: spec1
            command: "true"
            level: spec
          - id: phase1
            command: "false"
            level: phase
    """))
    assert subprocess.run(RUN + ["--level", "spec"], env=_env(tmp_path), cwd=ROOT).returncode == 0

def test_empty_level_filter_is_fail_closed(tmp_path):                 # Codex #1
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: only-phase
            command: "true"
            level: phase
    """))
    # spec-done gate with ZERO spec-level assertions must NOT be green-by-default
    assert subprocess.run(RUN + ["--level", "spec"], env=_env(tmp_path), cwd=ROOT).returncode == 5

def test_overall_timeout_enforced_exit_4(tmp_path):
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: slow
            command: "sleep 5"
            timeout: 10
            level: spec
    """))
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    assert p.returncode == 4 and (time.monotonic() - start) < 3

def test_overall_budget_not_rounded_up(tmp_path):                    # Codex #2 (sub-second)
    # a1 eats most of the 1s budget; a2 must be cut by the <1s remainder, not a false pass
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: a1
            command: "sleep 0.7"
            timeout: 30
            level: spec
          - id: a2
            command: "sleep 0.5"
            timeout: 30
            level: spec
    """))
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    assert p.returncode == 4 and (time.monotonic() - start) < 2.0     # not a 1.2s false pass
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement in `assertions/run.py`**

Constants (after line 40):
```python
MANIFEST = os.environ.get("CONDUCTOR_MANIFEST", os.path.join(ASSERTIONS_DIR, "manifest.yaml"))
OVERALL_TIMEOUT = float(os.environ.get("CONDUCTOR_OVERALL_TIMEOUT", "0"))   # 0 = none
ISOLATE = os.environ.get("CONDUCTOR_ISOLATE", "") not in ("", "0")
EXIT_OVERALL_TIMEOUT = 4
EXIT_NO_MATCH = 5
```

Give `_run` an optional `cwd=REPO_ROOT`. Replace `main()` (keep `load_assertions`,
`_parse_flat_yaml`, `write_results`, ManifestMissing/Invalid handling):
```python
import argparse, tempfile, shutil

def _overall_fail(results):
    write_results(results)
    print("SUMMARY: overall wall-clock exceeded -> gate NOT done (exit 4)")
    return EXIT_OVERALL_TIMEOUT

def _remaining(deadline):
    return None if deadline is None else (deadline - time.monotonic())

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=["spec", "phase", "task"], default=None)
    args, _ = ap.parse_known_args()
    try:
        assertions = load_assertions(MANIFEST)
    except ManifestMissing:
        write_results({}); print(f"[GATE] FAIL: manifest missing at {MANIFEST}")
        print("SUMMARY: gate NOT done (manifest missing) -> exit 2"); return EXIT_NO_MANIFEST
    except ManifestInvalid as exc:
        write_results({}); print(f"[GATE] FAIL: manifest unparseable: {exc}")
        print("SUMMARY: gate NOT done (manifest unparseable) -> exit 3"); return EXIT_BAD_MANIFEST
    if args.level:
        assertions = [a for a in assertions if str(a.get("level", "spec")) == args.level]
        if not assertions:                                       # Codex #1: empty != done
            write_results({}); print(f"[GATE] FAIL: no assertions at level '{args.level}'")
            print("SUMMARY: gate NOT done (no matching assertions) -> exit 5")
            return EXIT_NO_MATCH

    deadline = time.monotonic() + OVERALL_TIMEOUT if OVERALL_TIMEOUT else None
    results, passed = {}, 0
    for a in assertions:
        aid, command = str(a["id"]), str(a["command"])
        setup = str(a.get("setup", "") or ""); teardown = str(a.get("teardown", "") or "")
        kind = str(a.get("kind", "example")); timeout = float(a.get("timeout", DEFAULT_TIMEOUT))
        workdir = tempfile.mkdtemp(prefix=f"assert-{aid}-") if ISOLATE else REPO_ROOT
        start = time.monotonic()
        try:
            # setup (if any) then command — each capped by the REAL remaining budget (no round-up),
            # with a deadline re-check AFTER every return (Codex #2: strict wall-clock).
            failed_reason = None
            for is_setup, cmd in ([(True, setup)] if setup else []) + [(False, command)]:
                rem = _remaining(deadline)
                if rem is not None and rem <= 0:
                    return _overall_fail(results)
                eff = timeout if rem is None else min(timeout, rem)   # float; NOT max(1, ...)
                rc, reason = _run(cmd, eff, workdir)
                if deadline is not None and time.monotonic() > deadline:
                    results[aid] = {"pass": False, "rc": 124, "kind": kind,
                                    "duration": round(time.monotonic()-start, 3),
                                    "reason": "overall-timeout"}
                    print(f"[FAIL] {aid} (reason=overall-timeout)")
                    return _overall_fail(results)
                if rc != 0:
                    failed_reason = (f"setup-failed({reason})" if is_setup else reason)
                    results[aid] = {"pass": False, "rc": rc, "kind": kind,
                                    "duration": round(time.monotonic()-start, 3),
                                    "reason": failed_reason}
                    print(f"[FAIL] {aid} (rc={rc}, reason={failed_reason})")
                    break
            if failed_reason is None:
                results[aid] = {"pass": True, "rc": 0, "kind": kind,
                                "duration": round(time.monotonic()-start, 3), "reason": "ok"}
                print(f"[PASS] {aid}"); passed += 1
        finally:
            if teardown:
                _run(teardown, 5, workdir)                       # best-effort cleanup
            if ISOLATE and workdir != REPO_ROOT:
                shutil.rmtree(workdir, ignore_errors=True)

    write_results(results)
    total = len(assertions); failed = total - passed
    if failed == 0:
        print(f"SUMMARY: {passed}/{total} green -> gate DONE (exit 0)"); return EXIT_OK
    print(f"SUMMARY: {passed}/{total} green, {failed} red -> gate NOT done (exit 1)")
    return EXIT_RED
```
Update the docstring exit table to add `4 overall timeout` and `5 no matching assertions`.

- [ ] **Step 4: Update `bin/conductor`**

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in
  assert) shift; [ "${1:-}" = "run" ] && shift; exec python3 "$HERE/assertions/run.py" "$@" ;;
  *) echo "usage: conductor assert run [--level spec|phase|task]" >&2; exit 64 ;;
esac
```

- [ ] **Step 5: Document the schema** in `assertions/manifest.yaml` header: `id, claim, command, setup, teardown, timeout, level (gate tier), kind (form)`.

- [ ] **Step 6: Run runner tests + existing gate**

Run: `pytest tests/test_runner.py -v && ./bin/conductor assert run`
Expected: all pass (incl. empty-filter exit 5, sub-second budget exit 4 < 2s); existing gate green.

- [ ] **Step 7: Lint + typecheck + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add assertions/run.py bin/conductor assertions/manifest.yaml tests/test_runner.py
git commit -m "Plan2 T2: harden runner (teardown, isolation, strict overall timeout, --level empty=fail-closed, kind)" \
  -m "- assertions/run.py: setup+command capped by REAL remaining budget + post-return deadline check (Codex #2); empty level filter -> exit 5 (Codex #1); isolated cwd; teardown; kind passthrough" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `/conductor:assertions-to-tests` bridge skill

Turn each 4-part assertion spec from **`/spec-craft:executable-assertions`** into ONE runnable
test wired into the manifest by `id`, via `/superpowers:test-driven-development`. Assigns the
gate `level` (default `spec`) and carries the `kind`. The test stays RED until behavior exists
(Plan 4).

**Files:** Create `skills/assertions-to-tests/SKILL.md`; test `tests/test_skill_outputs.py`

- [ ] **Step 1: Structural test**

```python
# tests/test_skill_outputs.py
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_assertions_to_tests_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/assertions-to-tests/SKILL.md")).read().lower()
    for needle in ["superpowers:test-driven-development", "spec-craft:executable-assertions",
                   "manifest.yaml", "one test per", "must not contain", "level", "kind",
                   "stays red", "stable"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write the skill** (`skills/assertions-to-tests/SKILL.md`, frontmatter `name: assertions-to-tests`):

```markdown
---
name: assertions-to-tests
description: Use after /spec-craft:executable-assertions to turn each 4-part assertion spec into one runnable test wired into assertions/manifest.yaml by id, via /superpowers:test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /conductor:assertions-to-tests

Input: the assertion specs (claim / setup / observation / kind) produced by
`/spec-craft:executable-assertions`. For **each** spec, produce exactly one runnable test and
one manifest entry, traceable by `id`. **Use `/superpowers:test-driven-development`** for the test.

For each assertion spec:

1. **Pick a stable `id`** (kebab-case from the claim, e.g. `unknown-code-404`). Keep it stable
   so a red result names the violated claim.
2. **Write the test (it stays RED).** Encode the *claim* as one pass/fail check. Honor the
   *observation*: assert what the result **must contain** AND, explicitly, what it **must not
   contain**. Realize the *setup* as fixtures. Match the test to the spec's **kind**: `example`
   → one concrete case; `property` → assert across generated inputs; `contract` → assert
   pre/postconditions. RED until the system implements the behavior — expected here.
3. **Wire it into `assertions/manifest.yaml`:**
   ```yaml
     - id: <id>
       claim: "<one-sentence Boolean claim>"
       command: "python3 -m pytest -q <path/to/test>"
       setup: ""
       teardown: ""
       timeout: 30
       level: spec        # GATE TIER: spec (default) | phase | task
       kind: <example|property|contract>   # FORM, carried from the assertion spec
   ```
4. **Verify the gate sees it RED:** `conductor assert run --level spec` lists the new id as
   `[FAIL]`. The spec is "done" exactly when every spec-level assertion goes green (§5.1, §7).

**Scope:** one test per assertion spec; do not implement product behavior. `level` is the gate
tier (assigned here); `kind` is the assertion form (from the spec) — never conflate them.
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`Plan2 T3: /conductor:assertions-to-tests bridge` … + trailer).

---

## Task 4: Done-gate integration test + plugin discovery

**Files:** Test `tests/test_foundation_e2e.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_foundation_e2e.py
import os, subprocess, sys, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_gate_red_then_green(tmp_path):
    (tmp_path / "test_unknown_code.py").write_text(textwrap.dedent("""\
        from shortener import lookup
        def test_unknown_code_is_404():
            assert lookup("nope") == 404
    """))
    (tmp_path / "manifest.yaml").write_text(textwrap.dedent(f"""\
        assertions:
          - id: unknown-code-404
            claim: "An unknown short code returns 404."
            command: "python3 -m pytest -q {tmp_path / 'test_unknown_code.py'}"
            level: spec
            kind: example
    """))
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"),
           "PYTHONPATH": str(tmp_path)}
    run = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]
    assert subprocess.run(run, env=env, cwd=ROOT).returncode == 1     # no impl -> fail-closed
    (tmp_path / "shortener.py").write_text("def lookup(code):\\n    return 404\\n")
    assert subprocess.run(run, env=env, cwd=ROOT).returncode == 0     # behavior -> green
```

- [ ] **Step 2: Run → PASS.**

- [ ] **Step 3: Plugin discovery validation (recorded smoke)**

```bash
claude plugin validate . --strict 2>&1 | tee /tmp/conductor-strict.txt
test -f skills/assertions-to-tests/SKILL.md && echo "BRIDGE SKILL PRESENT"
```

- [ ] **Step 4: Full quality gate + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add tests/test_foundation_e2e.py
git commit -m "Plan2 T4: done-gate E2E (RED->GREEN) + plugin discovery check" \
  -m "- tests/test_foundation_e2e.py; recorded claude plugin validate --strict" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Codex review (Plans 2–3) — Plan 2 items addressed:**
- **#1 empty level filter:** `--level` matching zero assertions returns **exit 5** ("no matching
  assertions"), never green-by-default; tested (`test_empty_level_filter_is_fail_closed`). ✓
- **#2 strict overall timeout:** each setup/command is capped by the REAL remaining budget
  (float, no `max(1,…)` round-up) with a deadline re-check after every return; a command that
  finishes past the deadline cannot pass; tested (`test_overall_timeout_enforced_exit_4`,
  `test_overall_budget_not_rounded_up`). ✓
- Prior Codex (Plan 1 v2): `/conductor:` namespacing, `level`≠`kind`, `claude plugin validate`. ✓

**Coverage:** component 3 (runner + tests-via-TDD) → T2 (full §5.2) + T3 (bridge) + T4
(RED→GREEN). Plugin + dependency (§2.1) → T1. Components 1–2 are Plan 1 (spec-craft).

**Consistency:** manifest keys identical T2/T3/T4; exit codes `0/1/2/3/4/5`; CLI
`conductor assert run [--level]`; `dependencies: ["spec-craft"]`; namespacing uniform.

---

## Open follow-ups
- Plan 3 (ledger + claim model) consumes this manifest/runner as the "assertions decide" half of §7.
- Plan 4 (`/conductor:autodev` + `/conductor:conductor`) drives spec-level assertions RED→GREEN;
  `/conductor:conductor` invokes `/spec-craft:expectations` + `/spec-craft:executable-assertions`
  as the precondition, then `/conductor:assertions-to-tests`.
