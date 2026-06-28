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
`assertions/manifest.yaml`, `bin/conductor`) to production and adds the bridge skill. The two
authoring skills live in spec-craft, NOT here.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`,
YAML manifest (pyyaml optional, built-in fallback parser).

## Global Constraints

- **Plugin + dependency:** one `conductor` plugin, installable at user/project level (§2.1),
  declaring `dependencies: ["spec-craft"]`.
- **Fail-closed gate:** missing/unparseable manifest, un-executable command, crash,
  per-assertion timeout, or **overall wall-clock** overrun = **NOT done**, non-zero exit.
  Never green-by-default (design §5.2).
- **Manifest contract (§5.2):** `id`, `claim`, `command`, `setup`, `teardown`, `timeout`,
  **`level ∈ {spec, phase, task}`** (gate tier; spec-level decides spec-done, §7),
  **`kind ∈ {example, property, contract}`** (assertion form, carried from spec-craft). `level`
  and `kind` are distinct fields.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task is complete.
- **Commits:** atomic; message = files changed + 1–2 bullets; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Reuse Stage 0:** `assertions/run.py` (199 lines), `assertions/manifest.yaml`, `bin/conductor` already exist (E4). Promote/harden; do not rewrite from scratch.

## Conventions (LOCKED — inherited by Plans 3–4)

- **Namespaced invocation:** conductor skills are `/conductor:<skill>` (supervisor
  `/conductor:conductor`, worker `/conductor:autodev`); conducted skills keep their namespace
  (`/spec-craft:executable-assertions`, `/superpowers:test-driven-development`). No bare names,
  no aliases (stage0-notes amendment F).
- **`level` (gate tier) ≠ `kind` (form).**
- **Validate with** `claude plugin validate ./ [--strict]`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Conductor manifest with `dependencies: ["spec-craft"]`. |
| `skills/assertions-to-tests/SKILL.md` | `/conductor:assertions-to-tests` — spec-craft's 4-part specs → manifest-wired tests via `/superpowers:test-driven-development`. |
| `assertions/run.py` | The runner — **modify** (E4 exists): teardown, isolation, **enforced** overall timeout, `--level`, `kind` passthrough. |
| `assertions/manifest.yaml` | Done-gate manifest (id/claim/command/setup/teardown/timeout/level/kind). |
| `bin/conductor` | CLI — **modify**: `assert run [--level L]` passes through run.py exit code. |
| `tests/test_runner.py` | Runner pytest: fail-closed (5 modes), teardown, isolation, enforced overall timeout, level filter. |
| `tests/test_skill_outputs.py` | Structural check for the bridge skill. |
| `tests/test_plugin.py` | Manifest schema + `dependencies: ["spec-craft"]`. |
| `tests/test_foundation_e2e.py` | RED→GREEN gate end-to-end. |

---

## Task 1: Conductor plugin scaffold (with spec-craft dependency)

**Files:**
- Create: `.claude-plugin/plugin.json`, `tests/__init__.py`, `tests/test_plugin.py`

**Interfaces:**
- Produces: installable `conductor` plugin that auto-installs `spec-craft`; skills invoke as `/conductor:<skill>`.

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

- [ ] **Step 2: Run test to verify it fails** → FAIL (manifest absent).

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

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Static validation (recorded smoke):** `claude plugin validate .` (no marketplace needed). Record output.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json tests/test_plugin.py tests/__init__.py
git commit -m "Plan2 T1: conductor plugin manifest (dependencies: spec-craft)" \
  -m "- .claude-plugin/plugin.json; tests/test_plugin.py (schema + dependency)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Harden the assertion runner

The E4 prototype (`assertions/run.py`, 199 lines) already does: manifest load (pyyaml +
fallback), per-assertion `setup`+`command`, per-assertion timeout, `results.json`,
fail-closed (missing/unparseable/crash/per-assertion-timeout), exit codes `0/1/2/3`. **Add:**
`teardown`, **isolation**, an **enforced overall wall-clock** limit (Codex #3), a `--level`
filter, and `kind` passthrough.

**Files:**
- Modify: `assertions/run.py`, `bin/conductor`, `assertions/manifest.yaml`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: manifest entries `{id, claim, command, setup, teardown, timeout, level, kind}`.
- Produces: `conductor assert run [--level spec|phase|task]` → per-id `[PASS]/[FAIL]`, aggregate `SUMMARY`, `assertions/run/results.json` (incl. `kind`); exit `0`/`1`/`2`/`3`/`4` (4 = overall-timeout).

- [ ] **Step 1: Write failing tests (incl. ENFORCED overall timeout)**

```python
# tests/test_runner.py
import os, subprocess, sys, textwrap, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py")]

def _manifest(tmp_path, body):
    (tmp_path / "manifest.yaml").write_text(body)

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

def test_overall_timeout_is_enforced_exit_4(tmp_path):
    _manifest(tmp_path, textwrap.dedent("""\
        assertions:
          - id: slow
            command: "sleep 5"
            timeout: 10
            level: spec
    """))
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    elapsed = time.monotonic() - start
    assert p.returncode == 4
    assert elapsed < 3, f"overall timeout not enforced (took {elapsed:.1f}s)"

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
    p = subprocess.run(RUN + ["--level", "spec"], env=_env(tmp_path), cwd=ROOT)
    assert p.returncode == 0

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
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_ISOLATE="1"), cwd=ROOT)
    assert p.returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail** → FAIL.

- [ ] **Step 3: Implement the additions in `assertions/run.py`**

Add near the constants:
```python
MANIFEST = os.environ.get("CONDUCTOR_MANIFEST", os.path.join(ASSERTIONS_DIR, "manifest.yaml"))
OVERALL_TIMEOUT = int(os.environ.get("CONDUCTOR_OVERALL_TIMEOUT", "0"))   # 0 = none
ISOLATE = os.environ.get("CONDUCTOR_ISOLATE", "") not in ("", "0")
EXIT_OVERALL_TIMEOUT = 4
```

Give `_run` an optional `cwd=REPO_ROOT`. Replace `main()` (keep `load_assertions`,
`_parse_flat_yaml`, `write_results`, and the ManifestMissing/Invalid handling unchanged):
```python
import argparse, tempfile, shutil

def _overall_fail(results):
    write_results(results)
    print("SUMMARY: overall wall-clock exceeded -> gate NOT done (exit 4)")
    return EXIT_OVERALL_TIMEOUT

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

    deadline = time.monotonic() + OVERALL_TIMEOUT if OVERALL_TIMEOUT else None
    results, passed = {}, 0
    for a in assertions:
        if deadline is not None and time.monotonic() >= deadline:
            return _overall_fail(results)                       # budget spent, work remains
        aid, command = str(a["id"]), str(a["command"])
        setup = str(a.get("setup", "") or ""); teardown = str(a.get("teardown", "") or "")
        kind = str(a.get("kind", "example")); timeout = int(a.get("timeout", DEFAULT_TIMEOUT))
        workdir = tempfile.mkdtemp(prefix=f"assert-{aid}-") if ISOLATE else REPO_ROOT

        def cap(t):                                             # cap by remaining overall time
            if deadline is None: return t
            return max(1, min(t, int(deadline - time.monotonic())))

        start = time.monotonic()
        try:
            if setup:
                rc, reason = _run(setup, cap(timeout), workdir)
                if rc != 0:
                    results[aid] = {"pass": False, "rc": rc, "kind": kind,
                                    "duration": round(time.monotonic()-start, 3),
                                    "reason": f"setup-failed({reason})"}
                    print(f"[FAIL] {aid} (rc={rc}, reason=setup-failed({reason}))"); continue
            eff = cap(timeout)
            rc, reason = _run(command, eff, workdir)
            if rc == 124 and eff < timeout:                     # cut by OVERALL budget -> exit 4
                results[aid] = {"pass": False, "rc": rc, "kind": kind,
                                "duration": round(time.monotonic()-start, 3),
                                "reason": "overall-timeout"}
                print(f"[FAIL] {aid} (rc={rc}, reason=overall-timeout)")
                return _overall_fail(results)
            ok = rc == 0
            results[aid] = {"pass": ok, "rc": rc, "kind": kind,
                            "duration": round(time.monotonic()-start, 3), "reason": reason}
            print(f"[PASS] {aid}" if ok else f"[FAIL] {aid} (rc={rc}, reason={reason})")
            passed += 1 if ok else 0
        finally:
            if teardown:
                _run(teardown, 5, workdir)                      # best-effort, always runs
            if ISOLATE and workdir != REPO_ROOT:
                shutil.rmtree(workdir, ignore_errors=True)

    write_results(results)
    total = len(assertions); failed = total - passed
    if failed == 0:
        print(f"SUMMARY: {passed}/{total} green -> gate DONE (exit 0)"); return EXIT_OK
    print(f"SUMMARY: {passed}/{total} green, {failed} red -> gate NOT done (exit 1)")
    return EXIT_RED
```
Update the docstring exit table to add `4  overall wall-clock timeout`.

- [ ] **Step 4: Update `bin/conductor`**

```bash
#!/usr/bin/env bash
# conductor CLI — `assert run [--level spec|phase|task]` execs the runner; exit passes through.
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
Expected: new tests PASS (enforced-timeout < 3s); existing gate still green.

- [ ] **Step 7: Lint + typecheck + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add assertions/run.py bin/conductor assertions/manifest.yaml tests/test_runner.py
git commit -m "Plan2 T2: harden runner (teardown, isolation, ENFORCED overall timeout, --level, kind)" \
  -m "- assertions/run.py: isolated cwd, teardown finally-block, overall budget caps each command + exit 4 when cut (Codex #3), --level, kind passthrough" \
  -m "- bin/conductor forwards --level; manifest schema comment (level vs kind)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `/conductor:assertions-to-tests` bridge skill

Turn each 4-part assertion spec from **`/spec-craft:executable-assertions`** into ONE runnable
test wired into the manifest by `id`, via `/superpowers:test-driven-development`. Assigns the
gate `level` (default `spec`) and carries the `kind`. The test stays RED until behavior exists
(implemented by the build loop, Plan 4).

**Files:**
- Create: `skills/assertions-to-tests/SKILL.md`
- Test: `tests/test_skill_outputs.py`

**Interfaces:**
- Consumes: the 4-part specs (claim/setup/observation/kind) emitted by `/spec-craft:executable-assertions`.
- Produces: per spec, a test file + a `manifest.yaml` entry (`id`, `command`, `level`, `kind`).

- [ ] **Step 1: Write the failing structural test**

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

- [ ] **Step 2: Run test to verify it fails** → FAIL.

- [ ] **Step 3: Write the skill**

```markdown
---
name: assertions-to-tests
description: Use after /spec-craft:executable-assertions to turn each 4-part assertion spec into one runnable test wired into assertions/manifest.yaml by id, via /superpowers:test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /conductor:assertions-to-tests

Input: the assertion specs (claim / setup / observation / kind) produced by
`/spec-craft:executable-assertions`. For **each** spec, produce exactly one runnable test and
one manifest entry, traceable by `id`. **Use `/superpowers:test-driven-development`** for the
test itself.

For each assertion spec:

1. **Pick a stable `id`** (kebab-case from the claim, e.g. `unknown-code-404`). Keep it
   stable so a red result names the violated claim.
2. **Write the test (it stays RED).** Encode the *claim* as a single pass/fail check. Honor
   the *observation*: assert what the result **must contain** AND, explicitly, what it **must
   not contain** (the dangerous failure is usually a forbidden value being present). Realize
   the *setup* as fixtures. Match the test to the spec's **kind**: `example` → one concrete
   case; `property` → assert across a generated set of inputs, not a single case; `contract`
   → assert the pre/postconditions. The test stays RED until the system implements the
   behavior — that is expected here.
3. **Wire it into `assertions/manifest.yaml`:**
   ```yaml
     - id: <id>
       claim: "<the one-sentence Boolean claim>"
       command: "python3 -m pytest -q <path/to/test>"
       setup: ""          # optional shell setup
       teardown: ""       # optional cleanup
       timeout: 30
       level: spec        # GATE TIER: spec (default) | phase | task — which level this gates
       kind: <example|property|contract>   # FORM, carried from the assertion spec
   ```
4. **Verify the gate sees it RED:** `conductor assert run --level spec` lists the new id as
   `[FAIL]`. The spec is "done" exactly when every spec-level assertion goes green — the
   loop's terminal condition (design §5.1, §7).

**Scope:** one test per assertion spec; do not implement product behavior (that is the build
loop). `level` is the gate tier (assigned here); `kind` is the assertion form (from the spec)
— never conflate them.
```

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/assertions-to-tests/SKILL.md tests/test_skill_outputs.py
git commit -m "Plan2 T3: /conductor:assertions-to-tests bridge (spec-craft specs -> manifest-wired tests)" \
  -m "- skills/assertions-to-tests/SKILL.md; tests/test_skill_outputs.py" \
  -m "- consumes /spec-craft:executable-assertions; assigns gate level + carries kind" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Done-gate integration test + plugin discovery

Prove the gate transitions RED→GREEN driven only by behavior, and validate plugin discovery.

**Files:**
- Test: `tests/test_foundation_e2e.py`

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

- [ ] **Step 2: Run it (RED→GREEN)** → PASS.

- [ ] **Step 3: Plugin discovery validation (recorded smoke)**

```bash
claude plugin validate . --strict 2>&1 | tee /tmp/conductor-strict.txt
test -f skills/assertions-to-tests/SKILL.md && echo "BRIDGE SKILL PRESENT"
```
Confirms `/conductor:assertions-to-tests` discoverable and the `spec-craft` dependency declared.

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

**Codex review (applied to the runner/bridge here):**
- **#2 level/kind:** manifest carries both distinctly; bridge assigns `level`, carries `kind`. ✓
- **#3 overall timeout:** runner caps each command by remaining budget + exit 4 when cut; test asserts < 3s. ✓
- **#1 invocation:** `/conductor:assertions-to-tests`, consumes `/spec-craft:executable-assertions`, uses `/superpowers:test-driven-development`. ✓
- **#4 scaffold proof:** T1 manifest schema + dependency test + `claude plugin validate`; T4 `--strict` discovery. ✓

**Coverage:** design component 3 (runner + tests-via-TDD) → T2 (full §5.2) + T3 (bridge) + T4
(RED→GREEN). Plugin + dependency (§2.1) → T1. Components 1–2 are Plan 1 (spec-craft).

**Consistency:** manifest keys identical across T2/T3/T4; exit codes `0/1/2/3/4`; CLI
`conductor assert run [--level]`; `dependencies: ["spec-craft"]`; namespacing uniform.

---

## Open follow-ups (not this plan)
- Plan 3 (ledger + claim model, components 4–5) consumes this manifest/runner as the
  "assertions decide" half of §7.
- Plan 4 (`/conductor:autodev` + `/conductor:conductor`, components 6–7) drives spec-level
  assertions RED→GREEN; `/conductor:conductor` invokes `/spec-craft:expectations` +
  `/spec-craft:executable-assertions` as the precondition, then `/conductor:assertions-to-tests`.
