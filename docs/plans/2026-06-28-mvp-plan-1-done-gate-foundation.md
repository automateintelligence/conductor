# Conductor MVP — Plan 1: Done-Gate Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the conductor plugin's **done-gate foundation** — the two assertion-authoring
skills (`/conductor:expectations`, `/conductor:executable-assertions`) and the machine-checked
assertion runner (`conductor assert run`) — so a spec gains an objective, fail-closed terminal
condition.

**Architecture:** A single installable Claude Code **plugin** (`conductor/`) housing skills +
a small CLI. `/conductor:expectations` adds a 3-part Expectations section to a spec;
`/conductor:executable-assertions` turns the load-bearing ones into 4-part specs
(claim/setup/observation/kind); `/conductor:assertions-to-tests` turns each spec into a
runnable test wired into `assertions/manifest.yaml`; `conductor assert run` (Python) executes
the manifest and is **fail-closed** (anything unrunnable = NOT done). This plan promotes the
Stage-0 E4 prototype (`assertions/run.py`, `assertions/manifest.yaml`, `bin/conductor`) to
production and adds the skills.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`,
YAML manifest (pyyaml optional, built-in fallback parser).

## Global Constraints

- **Plugin install:** one plugin, installable at user or project level; no per-project bootstrap (design §2.1).
- **Fail-closed gate:** missing/unparseable manifest, un-executable command, crash, per-assertion timeout, or **overall wall-clock** overrun = **NOT done**, non-zero exit. Never green-by-default (design §5.2).
- **Assertion-spec contract (§5.2):** each manifest entry has `id`, `claim`, `command`, `setup`, `teardown`, `timeout`, **`level ∈ {spec, phase, task}`** (gate tier — which hierarchy level the assertion gates; spec-level decides spec-done, §7), and **`kind ∈ {example, property, contract}`** (logical form of the assertion). `level` and `kind` are **distinct fields** (Codex #2). Exactly one runnable test per spec, traceable by `id`.
- **Skills output specs, not code:** `/conductor:expectations` and `/conductor:executable-assertions` produce prose/specs only — no tests, no implementation (§5.1). Turning specs into tests is the separate `/conductor:assertions-to-tests` step.
- **Python gate:** `ruff check . && ruff format --check . && pyright .` and `pytest` must pass before any task is marked complete (user global build commands).
- **Commits:** atomic, per task; message = files changed + 1–2 bullets; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Reuse Stage 0:** `assertions/run.py`, `assertions/manifest.yaml`, `bin/conductor` already exist (E4). Promote/harden; do not rewrite from scratch.

## Conventions (LOCKED — inherited by Plans 2–3)

- **Invocation is namespaced** (verified via claude-code-guide + plugin layout): a plugin
  skill at `skills/<x>/SKILL.md` is invoked **`/conductor:<x>`**. SKILL.md `name:` is display
  metadata only; **there is no bare `/conductor`** and **no aliases**. The supervisor skill is
  therefore `/conductor:conductor`, the worker `/conductor:autodev`. (Design doc's bare `/x`
  are shorthand — see stage0-notes amendment F.)
- **Conducted external skills keep their namespace:** `/superpowers:test-driven-development`,
  `/superpowers:subagent-driven-development`, etc.
- **`level` vs `kind`:** never reuse one word for both. `level` = gate tier {spec, phase, task};
  `kind` = assertion form {example, property, contract}.
- **Plugin validation:** `claude plugin validate ./ [--strict]` (static; no marketplace needed).

---

## File Structure

| Path | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (`name` required; version/description/author optional). Makes conductor installable + namespaced. |
| `skills/expectations/SKILL.md` | `/conductor:expectations` — add Expectations section to a spec (component 1). |
| `skills/executable-assertions/SKILL.md` | `/conductor:executable-assertions` — Expectations → 4-part assertion specs (component 2). |
| `skills/assertions-to-tests/SKILL.md` | `/conductor:assertions-to-tests` — each spec → runnable test + manifest entry via `/superpowers:test-driven-development` (component 3 bridge, §5.1). |
| `assertions/run.py` | The runner — **modify** (E4 exists, 199 lines): add teardown, isolation, **enforced** overall timeout, `--level` filter, `kind` passthrough. |
| `assertions/manifest.yaml` | Done-gate manifest (id/claim/command/setup/teardown/timeout/level/kind). Schema source of truth. |
| `bin/conductor` | CLI stub — **modify**: `assert run [--level L]` passes through run.py exit code. |
| `tests/fixtures/sample-spec.md` | Tiny fixture spec with implicit done-gaps, for testing skills 1–3. |
| `tests/test_runner.py` | pytest for runner: fail-closed (5 modes), teardown, isolation, **enforced** overall timeout, level filter. |
| `tests/test_skill_outputs.py` | Structural checks that each SKILL.md encodes its required contract. |
| `tests/test_plugin.py` | Manifest schema validation (+ Task 6 discovery check). |

---

## Task 1: Plugin scaffold

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `tests/__init__.py` (empty), `tests/test_plugin.py`

**Interfaces:**
- Produces: an installable plugin `conductor` whose `skills/` dir is auto-discovered and whose skills invoke as `/conductor:<skill>`.

- [ ] **Step 1: Write the failing manifest-schema test**

```python
# tests/test_plugin.py
import json, os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_plugin_manifest_schema():
    p = os.path.join(ROOT, ".claude-plugin", "plugin.json")
    data = json.load(open(p))                      # must be valid JSON at canonical path
    assert isinstance(data.get("name"), str) and data["name"] == "conductor"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), "semver version required"
    # only known top-level keys (catch typos that `--strict` would reject)
    assert set(data) <= {"name", "version", "description", "author", "displayName",
                         "homepage", "repository", "license"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugin.py -v`
Expected: FAIL (`FileNotFoundError` — plugin.json absent).

- [ ] **Step 3: Write the plugin manifest**

```json
{
  "name": "conductor",
  "version": "0.1.0",
  "description": "Autonomous spec-completion loop: spec -> plans -> phases -> machine-checked done.",
  "author": "Jeffrey A. Daniels"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plugin.py -v`
Expected: PASS.

- [ ] **Step 5: Static plugin validation (smoke, recorded)**

Run: `claude plugin validate . 2>&1 | tee /tmp/plugin-validate.txt`
Expected: no schema/structure errors (skills are added in Tasks 2–5; full `--strict`
discovery check runs in Task 6). Record the output in the PR. (Run as a recorded smoke step,
not a pytest unit — `claude` may be absent in CI.)

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json tests/test_plugin.py tests/__init__.py
git commit -m "Plan1 T1: conductor plugin manifest + schema test" \
  -m "- .claude-plugin/plugin.json; tests/test_plugin.py (schema + known-keys)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `/conductor:expectations` skill (component 1)

Promote `developing-expectations-in-the-spec.md` to a skill, **generalized** so it works on
any spec (the original's access-control/content-exposure examples are KnowledgeSight-specific;
replace with a domain-neutral set — the only substantive change, made because the conductor
skill must be reusable, not to shorten or restyle).

**Files:**
- Create: `skills/expectations/SKILL.md`
- Create: `tests/fixtures/sample-spec.md`
- Test: `tests/test_skill_outputs.py`

**Interfaces:**
- Consumes: a spec file path (`$ARGUMENTS`).
- Produces: an `## Expectations` section (Success scenarios / Failure scenarios / Must-nots) written into the spec, preceded by a list of definition-of-done gaps.

- [ ] **Step 1: Create the fixture spec**

```markdown
<!-- tests/fixtures/sample-spec.md -->
# Spec: URL Shortener

Build a service that maps a long URL to a short code and redirects.

- `POST /shorten {url}` returns a short code.
- `GET /<code>` redirects (HTTP 302) to the original URL.
- Codes are 7 characters, alphanumeric.
- Unknown codes return 404.
```

- [ ] **Step 2: Write the failing structural test**

```python
# tests/test_skill_outputs.py
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_expectations_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/expectations/SKILL.md")).read().lower()
    for needle in ["success scenarios", "failure scenarios", "must-nots",
                   "definition-of-done gap", "do not write", "expectations section"]:
        assert needle in body, needle
    assert "knowledge" not in body, "must be generalized (no product-specific coupling)"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_skill_outputs.py::test_expectations_skill_contract_present -v`
Expected: FAIL (skill file absent).

- [ ] **Step 4: Write the skill**

```markdown
---
name: expectations
description: Use when a spec has no explicit definition of done. Adds an Expectations section (success scenarios, failure scenarios, must-nots) in domain language, owned by whoever wanted the outcome. Reads a spec path and writes the section into the spec. Pairs with /conductor:executable-assertions.
---

# /conductor:expectations

Read the spec at `$ARGUMENTS`. Add an **Expectations** section to it.

Background (so you make judgment calls, not template-fills): a spec blurs three things —
what the user wants, how it is built, and what counts as done. When "what counts as done"
is implicit, a coding agent fills the gap with its own interpretation. The Expectations
section closes that gap. It is the boundary of the work, owned by the person who wanted the
outcome, written in terms a user or domain expert would recognize — not implementation
language.

An Expectations section has three parts:

1. **Success scenarios.** The concrete conditions under which the result counts as done.
   Not "it works" but specific observable outcomes, stated so that whether each is met is a
   matter of fact, not opinion.
2. **Failure scenarios.** The specific ways this can produce a wrong result that looks
   right — where an agent would generate plausible code that violates intent. Think about
   what "confidently wrong" looks like for this feature.
3. **Must-nots.** Hard constraints the result must never violate, regardless of how done is
   otherwise defined — the load-bearing invariants. Across products these commonly concern
   access control, data exposure, money, data integrity, irreversible actions, and safety:
   anywhere a wrong result that looks right does real or unrecoverable damage.

Method:

- First read the spec and identify every place the definition of done is implicit, assumed,
  or left to interpretation. **List those gaps before writing anything** — they are the raw
  material for the section.
- Apply the **outsider test** to each candidate: would someone not in your head, reading
  only the spec, know whether this condition was met? If not, it is too vague — sharpen it
  until the answer is yes.
- Write in domain/user language, not implementation language. "Restricted results show the
  owner and access path, never the content" is an expectation; "the query returns a non-null
  permission label" is an implementation detail and does not belong here.
- **Surface ambiguities rather than resolving them silently.** Where the spec genuinely does
  not determine what done means, flag it as an open question — do not invent a boundary the
  author did not specify.

**Scope boundary (important):** produce the Expectations section only. **Do not write tests,
executable assertions, or any code**, and do not propose how to verify the expectations.
Encoding expectations as tests is a separate step (`/conductor:executable-assertions` → TDD).
If you reach for verification mechanics, stop and note it for the next step.

**Output / action:**
1. Print the list of definition-of-done gaps you found.
2. Write an `## Expectations` section (the three parts) into the spec file at `$ARGUMENTS`
   (append if absent; update in place if present). Keep it surgical — expectations that
   restate the obvious add noise; keep the ones that close a real gap.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_expectations_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 6: Behavioral smoke check (recorded)**

```bash
cp tests/fixtures/sample-spec.md /tmp/spec.md
claude -p --permission-mode bypassPermissions "/conductor:expectations /tmp/spec.md" </dev/null
grep -c "Success scenarios\|Failure scenarios\|Must-nots" /tmp/spec.md   # expect 3
```
Record the output in the PR.

- [ ] **Step 7: Commit**

```bash
git add skills/expectations/SKILL.md tests/fixtures/sample-spec.md tests/test_skill_outputs.py
git commit -m "Plan1 T2: /conductor:expectations skill (generalized from prompt)" \
  -m "- skills/expectations/SKILL.md; tests/fixtures/sample-spec.md; tests/test_skill_outputs.py" \
  -m "- generalized KnowledgeSight-specific must-not examples to a domain-neutral set" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `/conductor:executable-assertions` skill (component 2)

Promote `developing-executable-assertions-from-spec-expectations.md`, generalized the same
way. **Rename the prompt's 4th part from "Level" to "Kind"** to remove the collision with the
manifest's gate `level` (Codex #2) — Jeff's example/property/contract description is preserved
verbatim; only the field name changes.

**Files:**
- Create: `skills/executable-assertions/SKILL.md`
- Test: `tests/test_skill_outputs.py` (add a function)

**Interfaces:**
- Consumes: a spec path with an Expectations section.
- Produces: three ordered outputs — (1) encoded load-bearing expectations + reason each; (2) deliberately-skipped expectations + reason; (3) per encoded expectation, a 4-part spec: **claim / setup / observation / kind** (`example | property | contract`). No test code. (The manifest gate `level` is assigned later, in `/conductor:assertions-to-tests`.)

- [ ] **Step 1: Write the failing structural test**

```python
# add to tests/test_skill_outputs.py
def test_executable_assertions_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/executable-assertions/SKILL.md")).read().lower()
    for needle in ["claim", "setup", "observation", "kind",
                   "load-bearing", "do not write the test code", "must not contain",
                   "example", "property", "contract"]:
        assert needle in body, needle
    assert "knowledge" not in body and "tier" not in body  # generalized
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_outputs.py::test_executable_assertions_skill_contract_present -v`
Expected: FAIL (file absent).

- [ ] **Step 3: Write the skill**

```markdown
---
name: executable-assertions
description: Use after /conductor:expectations, when a spec's load-bearing expectations need to become machine-checkable. Selects the load-bearing ones and produces 4-part assertion specs (claim, setup, observation, kind). Specs only, no test code. Feeds /conductor:assertions-to-tests.
---

# /conductor:executable-assertions

Read the spec at `$ARGUMENTS`, including its Expectations section. Turn the **load-bearing**
expectations into **executable assertions**. Read this whole framing first — the selection
judgment matters more than the mechanics.

An expectation is prose; prose is re-interpreted slightly differently each run, so the same
expectation can produce different code under different models. An executable assertion
removes the interpretation: a machine check that passes or fails on an exit code, never "it
depends." The expectation says what must be true; the assertion is the runtime proof that it
is true in this build.

**Not every expectation becomes an assertion. Select first:**

- An expectation is **load-bearing** if it would be expensive, dangerous, or silent to get
  wrong. The clearest cases across products are security/access, data exposure, money, data
  integrity, and irreversible actions: anything where a wrong result looks correct but
  leaks data, corrupts state, charges wrongly, or can't be undone. Encode those first.
- A **must-not** is almost always load-bearing. A **failure scenario** usually is. A
  **success scenario** sometimes is — only when "done" is objectively checkable rather than
  a matter of judgment or feel.
- **Skip** expectations whose truth needs a human eye (visual polish, tone, subjective
  quality). Note them out of scope rather than forcing a brittle check.
- When in doubt, prefer **fewer, sharper** assertions over broad coverage.

For each selected expectation, define the assertion in plain terms (the 4-part spec):

- **Claim.** The single Boolean fact this proves, as one true/false sentence — nothing
  softer. If you can't reduce it to one Boolean, it's two assertions or not assertable yet;
  say which.
- **Setup.** What state must exist for the check to be meaningful: inputs, fixtures,
  preconditions. Concrete about conditions, not code.
- **Observation.** What the assertion inspects to decide pass/fail, and what specifically
  counts as fail. For exposure/security rules name what the result **must contain** and,
  just as explicitly, what it **must not contain** — the dangerous failure is usually the
  presence of something that should be absent.
- **Kind.** The logical form: `example` (one concrete input/output case), `property` (must
  hold across all inputs of a kind), or `contract` (a function's pre/postconditions). Prefer
  `property` when the invariant is meant to hold universally — a single case can pass while
  the invariant is broken. (This is the assertion's form; the manifest's separate `level`
  field — spec/phase/task — is assigned when the spec is wired into a test, not here.)

Method/scope:

- Work only from expectations in the spec. Do not invent new ones; if writing an assertion
  exposes a missing expectation, **flag it** rather than quietly adding it.
- Call out expectations that look checkable but aren't, and vague prose that actually
  reduces to a hard Boolean.
- **Do not write the test code** — produce the assertion specs first, so the claims and
  kinds can be confirmed before implementation.

**Output, in order:** (1) expectations judged load-bearing enough to encode, each with a
one-line reason; (2) expectations deliberately not encoded, each with a one-line reason
(unprovable by machine / not load-bearing / subjective); (3) for each encoded one, the 4-part
spec (claim; setup; observation with explicit must-contain and must-not-contain where
exposure is involved; kind). Keep it surgical — specs only.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_executable_assertions_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/executable-assertions/SKILL.md tests/test_skill_outputs.py
git commit -m "Plan1 T3: /conductor:executable-assertions skill (generalized; level->kind)" \
  -m "- skills/executable-assertions/SKILL.md; tests/test_skill_outputs.py" \
  -m "- 4th part renamed Level->Kind (example/property/contract) to free 'level' for the gate tier" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Harden the assertion runner (component 3 — runner)

The E4 prototype (`assertions/run.py`, 199 lines) already does: manifest load (pyyaml +
fallback), per-assertion `setup`+`command`, per-assertion timeout, `results.json`,
fail-closed (missing/unparseable/crash/per-assertion-timeout), exit codes `0/1/2/3`. **Add:**
`teardown`, **isolation** between assertions, an **enforced overall wall-clock** limit
(Codex #3), a `--level` filter (spec-done gate runs `level: spec`), and `kind` passthrough.

**Files:**
- Modify: `assertions/run.py`
- Modify: `bin/conductor`
- Modify: `assertions/manifest.yaml` (document `teardown`, `kind`)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: manifest entries `{id, claim, command, setup, teardown, timeout, level, kind}`.
- Produces: `conductor assert run [--level spec|phase|task]` → stdout per-id `[PASS]/[FAIL]`, aggregate `SUMMARY`, `assertions/run/results.json` (now includes `kind`); exit `0` all-green / `1` red / `2` no-manifest / `3` bad-manifest / `4` overall-timeout.

- [ ] **Step 1: Write failing tests (incl. ENFORCED overall timeout)**

```python
# tests/test_runner.py
import os, subprocess, sys, textwrap, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py")]

def _manifest(tmp_path, body):
    m = tmp_path / "manifest.yaml"; m.write_text(body); return m

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
    # per-assertion timeout 10s but overall budget 1s; the 5s sleep must be cut at ~1s.
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
    assert p.returncode == 0   # red 'phase1' is filtered out

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

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — env override, teardown, `--level`, exit `4`, isolation not yet implemented.

- [ ] **Step 3: Implement the additions in `assertions/run.py`**

Add near the constants (honor env for testability; keep existing constants):
```python
MANIFEST = os.environ.get("CONDUCTOR_MANIFEST", os.path.join(ASSERTIONS_DIR, "manifest.yaml"))
OVERALL_TIMEOUT = int(os.environ.get("CONDUCTOR_OVERALL_TIMEOUT", "0"))   # 0 = none
ISOLATE = os.environ.get("CONDUCTOR_ISOLATE", "") not in ("", "0")
EXIT_OVERALL_TIMEOUT = 4
```

Give `_run` an optional `cwd` (default `REPO_ROOT`) — one-line signature change.

Replace `main()` with (keeps `load_assertions`/parser/`write_results` and the
ManifestMissing/Invalid handling unchanged):
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
            # killed by the OVERALL budget (capped below its own timeout) -> exit 4
            if rc == 124 and eff < timeout:
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
Update the module docstring's exit table to add `4  overall wall-clock timeout`.

- [ ] **Step 4: Update `bin/conductor` to forward `--level`**

```bash
#!/usr/bin/env bash
# conductor CLI — `assert run [--level spec|phase|task]` execs the runner; exit passes through.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in
  assert)
    shift; [ "${1:-}" = "run" ] && shift
    exec python3 "$HERE/assertions/run.py" "$@" ;;
  *) echo "usage: conductor assert run [--level spec|phase|task]" >&2; exit 64 ;;
esac
```

- [ ] **Step 5: Document the full schema in `assertions/manifest.yaml` header**

Per-assertion keys: `id, claim, command, setup, teardown, timeout, level (gate tier), kind (form)`.

- [ ] **Step 6: Run all runner tests + the existing gate**

Run: `pytest tests/test_runner.py -v && ./bin/conductor assert run`
Expected: new tests PASS (incl. enforced-timeout < 3s); existing gate still `2/2 green -> exit 0`.

- [ ] **Step 7: Lint + typecheck + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add assertions/run.py bin/conductor assertions/manifest.yaml tests/test_runner.py
git commit -m "Plan1 T4: harden runner (teardown, isolation, ENFORCED overall timeout, --level, kind)" \
  -m "- assertions/run.py: per-assertion isolated cwd, teardown finally-block, overall budget caps each command timeout + exit 4 when a command is cut by it (Codex #3), --level filter, kind passthrough" \
  -m "- bin/conductor forwards --level; manifest.yaml schema comment (level vs kind)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `/conductor:assertions-to-tests` bridge skill (component 3 — specs → tests)

The §5.1 crux: turn each 4-part assertion spec into ONE runnable test wired into the manifest
by `id`, **using `/superpowers:test-driven-development`**. The test encodes the claim and
stays RED until the system implements the behavior (that implementation is the build loop's
job in Plan 3). This step **assigns the gate `level`** (default `spec`) and **carries the
`kind`** from the spec.

**Files:**
- Create: `skills/assertions-to-tests/SKILL.md`
- Test: `tests/test_skill_outputs.py` (add a function)

**Interfaces:**
- Consumes: the 4-part specs (claim/setup/observation/kind) from `/conductor:executable-assertions`.
- Produces: per spec, a test file + a `manifest.yaml` entry (`id`=spec id, `command` runs that test, `level` gate tier, `kind` carried through). Establishes the done-gate; does not implement product behavior.

- [ ] **Step 1: Write the failing structural test**

```python
# add to tests/test_skill_outputs.py
def test_assertions_to_tests_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/assertions-to-tests/SKILL.md")).read().lower()
    for needle in ["superpowers:test-driven-development", "manifest.yaml", "one test per",
                   "must not contain", "level", "kind", "stays red", "stable"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_outputs.py::test_assertions_to_tests_skill_contract_present -v`
Expected: FAIL.

- [ ] **Step 3: Write the skill**

```markdown
---
name: assertions-to-tests
description: Use after /conductor:executable-assertions to turn each 4-part assertion spec into one runnable test wired into assertions/manifest.yaml by id, via /superpowers:test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /conductor:assertions-to-tests

Input: the assertion specs (claim / setup / observation / kind) produced by
`/conductor:executable-assertions`. For **each** spec, produce exactly one runnable test and
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
loop). Do not collapse two claims into one test. `level` is the gate tier (assigned here);
`kind` is the assertion form (from the spec) — never conflate them.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_assertions_to_tests_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/assertions-to-tests/SKILL.md tests/test_skill_outputs.py
git commit -m "Plan1 T5: /conductor:assertions-to-tests bridge (specs -> manifest-wired tests via TDD)" \
  -m "- skills/assertions-to-tests/SKILL.md; tests/test_skill_outputs.py" \
  -m "- assigns gate level + carries kind; one test per spec by stable id" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Foundation integration test + plugin discovery check

Prove the done-gate foundation end-to-end on the fixture (spec → one manifest-wired test
RED → stub impl → gate GREEN), and validate plugin discovery now that all skills exist
(Codex #4).

**Files:**
- Test: `tests/test_foundation_e2e.py`

**Interfaces:**
- Consumes: all of Tasks 1–5.
- Produces: evidence the gate transitions RED→GREEN driven only by behavior, never by default; and that `claude plugin validate --strict` discovers all three skills.

- [ ] **Step 1: Write the integration test**

```python
# tests/test_foundation_e2e.py
import os, subprocess, sys, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_gate_red_then_green(tmp_path):
    test_file = tmp_path / "test_unknown_code.py"
    test_file.write_text(textwrap.dedent("""\
        from shortener import lookup
        def test_unknown_code_is_404():
            assert lookup("nope") == 404
    """))
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(textwrap.dedent(f"""\
        assertions:
          - id: unknown-code-404
            claim: "An unknown short code returns 404."
            command: "python3 -m pytest -q {test_file}"
            level: spec
            kind: example
    """))
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(manifest), "PYTHONPATH": str(tmp_path)}
    run = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]
    red = subprocess.run(run, env=env, cwd=ROOT)
    assert red.returncode == 1                       # no impl yet -> fail-closed
    (tmp_path / "shortener.py").write_text("def lookup(code):\\n    return 404\\n")
    green = subprocess.run(run, env=env, cwd=ROOT)
    assert green.returncode == 0
```

- [ ] **Step 2: Run it (RED→GREEN)**

Run: `pytest tests/test_foundation_e2e.py -v`
Expected: PASS (the test drives the gate exit 1 → exit 0).

- [ ] **Step 3: Plugin discovery validation (recorded smoke)**

```bash
claude plugin validate . --strict 2>&1 | tee /tmp/plugin-strict.txt
test -f skills/expectations/SKILL.md \
  && test -f skills/executable-assertions/SKILL.md \
  && test -f skills/assertions-to-tests/SKILL.md && echo "ALL SKILLS PRESENT"
```
Expected: no errors; `ALL SKILLS PRESENT`. Confirms the three skills are discoverable and
invoke as `/conductor:expectations`, `/conductor:executable-assertions`,
`/conductor:assertions-to-tests`. Record output in the PR.

- [ ] **Step 4: Full quality gate + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add tests/test_foundation_e2e.py
git commit -m "Plan1 T6: done-gate foundation E2E (RED->GREEN) + plugin discovery check" \
  -m "- tests/test_foundation_e2e.py; recorded claude plugin validate --strict" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Codex review of Plan 1 — all four findings addressed:**
- **#1 invocation convention (Critical):** every conductor skill is `/conductor:<skill>`
  throughout; supervisor=`/conductor:conductor`, worker=`/conductor:autodev` (locked in
  Conventions; recorded as stage0-notes amendment F). Verified via claude-code-guide + plugin
  layout.
- **#2 `level` collision:** split into `level` (gate tier {spec,phase,task}) and `kind`
  (form {example,property,contract}); `/conductor:executable-assertions` renames its 4th part
  to Kind; the runner/manifest/tests use both distinctly.
- **#3 overall timeout:** the runner now caps each command's timeout by remaining overall
  budget AND returns exit 4 when a command is cut by that budget; test asserts enforcement
  (< 3s wall-clock).
- **#4 scaffold proof:** Task 1 validates the manifest schema (+ known-keys) and runs
  `claude plugin validate`; Task 6 adds `claude plugin validate --strict` + skill-presence
  once skills exist. (Used `validate` rather than `install/list` — static, no marketplace
  dependency, more reliable in CI.)

**Spec coverage (§11 comps 1–3, §5.1–5.2):** component 1 → T2; component 2 → T3; component 3
(runner + tests-via-TDD) → T4 (full §5.2: setup/teardown, isolation, per-assertion + enforced
overall timeout, fail-closed, level) + T5 (specs→tests) + T6 (RED→GREEN). Plugin install
(§2.1) → T1/T6.

**Placeholder scan:** none; runner edits reference the real 199-line file and show exact
additions.

**Type/name consistency:** manifest keys `{id, claim, command, setup, teardown, timeout,
level, kind}` identical across T4/T5/T6; exit codes `0/1/2/3/4`; CLI `conductor assert run
[--level]`; env overrides identical in tests and run.py; `/conductor:<skill>` namespacing
uniform.

**One deliberate change to Jeff's prompts:** generalized the KnowledgeSight-specific
must-not/load-bearing examples to a domain-neutral set; renamed the assertion-form field
Level→Kind to avoid the gate-`level` collision. Structure/method/wording otherwise preserved.
Flagged in T2/T3 commit messages.

---

## Open follow-ups (not this plan)
- Plan 2 (ledger + claim model, components 4–5) consumes this manifest/runner as the
  "assertions decide" half of §7; uses the same `/conductor:issue-sync` namespacing.
- Plan 3 (`/conductor:autodev` + `/conductor:conductor`, components 6–7) drives spec-level
  assertions RED→GREEN via the build loop; `/conductor:conductor` calls
  `/conductor:expectations`+`/conductor:executable-assertions` as the precondition (or points
  the user at them, §5).
