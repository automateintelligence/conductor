# Conductor MVP — Plan 1: Done-Gate Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the conductor plugin's **done-gate foundation** — the two assertion-authoring
skills (`/expectations`, `/executable-assertions`) and the machine-checked assertion runner
(`conductor assert run`) — so a spec gains an objective, fail-closed terminal condition.

**Architecture:** A single installable Claude Code **plugin** (`conductor/`) housing skills +
a small CLI. `/expectations` adds a 3-part Expectations section to a spec; `/executable-assertions`
turns the load-bearing ones into 4-part specs (claim/setup/observation/level);
`assertions-to-tests` turns each spec into a runnable test wired into `assertions/manifest.yaml`;
`conductor assert run` (Python) executes the manifest and is **fail-closed** (anything
unrunnable = NOT done). This plan promotes the Stage-0 E4 prototype (`assertions/run.py`,
`assertions/manifest.yaml`, `bin/conductor`) to production and adds the two skills.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 (stdlib + pytest), `bash`,
YAML manifest (pyyaml optional, built-in fallback parser).

## Global Constraints

- **Plugin install:** one plugin, installable at user or project level; no per-project bootstrap (design §2.1).
- **Fail-closed gate:** missing/unparseable manifest, un-executable command, crash, or timeout = **NOT done**, non-zero exit. Never green-by-default (design §5.2).
- **Assertion-spec contract (verbatim, §5.2):** each spec has `id`, `claim`, `setup`, `observation`/`command`, `timeout`, `level ∈ {spec, phase, task}`. Exactly one runnable test per spec, traceable by `id`.
- **Skills output specs, not code:** `/expectations` and `/executable-assertions` produce prose/specs only — no tests, no implementation (design §5.1). Turning specs into tests is the separate `assertions-to-tests` step.
- **Python gate:** `ruff check . && ruff format --check . && pyright .` and `pytest` must pass before any task is marked complete (user global build commands).
- **Commits:** atomic, per task; message = files changed + 1–2 bullets; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Reuse Stage 0:** `assertions/run.py`, `assertions/manifest.yaml`, `bin/conductor` already exist (E4). Promote/harden them; do not rewrite from scratch.

---

## File Structure

| Path | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (name, version, skills dir) — makes conductor installable. |
| `skills/expectations/SKILL.md` | `/expectations` — add Expectations section to a spec (component 1). |
| `skills/executable-assertions/SKILL.md` | `/executable-assertions` — Expectations → 4-part assertion specs (component 2). |
| `skills/assertions-to-tests/SKILL.md` | Turn each assertion spec into a runnable test + manifest entry via `/test-driven-development` (component 3 bridge, §5.1). |
| `assertions/run.py` | The runner — **modify** (E4 exists): add teardown, isolation, overall timeout, `--level` filter. |
| `assertions/manifest.yaml` | Done-gate manifest (id→command/setup/teardown/timeout/level). Schema source of truth. |
| `bin/conductor` | CLI stub — **modify**: `assert run [--level L]` passes through run.py exit code. |
| `tests/fixtures/sample-spec.md` | Tiny fixture spec with implicit done-gaps, for testing skills 1–3. |
| `tests/test_runner.py` | pytest for runner: red→green, fail-closed (4 modes), teardown, isolation, overall timeout, level filter. |
| `tests/test_skill_outputs.py` | Structural checks that skill runs produce the required sections/parts on the fixture. |

> Frontmatter convention: Claude Code plugin skills use `name:` + `description:` (the
> harness reads these for `/`-invocation). Bodies are executable instructions (matching the
> existing `skills/orchestration/SKILL.md` style).

---

## Task 1: Plugin scaffold

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `tests/__init__.py` (empty)

**Interfaces:**
- Produces: an installable plugin named `conductor` whose `skills/` dir is auto-discovered.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plugin.py
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_plugin_manifest_valid():
    p = os.path.join(ROOT, ".claude-plugin", "plugin.json")
    data = json.load(open(p))
    assert data["name"] == "conductor"
    assert "version" in data
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

- [ ] **Step 5: Commit**

```bash
git add .claude-plugin/plugin.json tests/test_plugin.py tests/__init__.py
git commit -m "Plan1 T1: conductor plugin manifest" \
  -m "- .claude-plugin/plugin.json; tests/test_plugin.py" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `/expectations` skill (component 1)

Promote `developing-expectations-in-the-spec.md` to a skill, **generalized** so it works on
any spec (the original's access-control/content-exposure examples are KnowledgeSight-specific;
replace with domain-neutral examples — this is the only substantive change, made because the
conductor skill must be reusable, not to shorten or restyle).

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
import os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_expectations_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/expectations/SKILL.md")).read()
    # Skill must instruct the three Expectations parts and the gaps-first output,
    # and must forbid writing tests/code (scope boundary).
    for needle in ["Success scenarios", "Failure scenarios", "Must-nots",
                   "definition-of-done gap", "do not write", "Expectations section"]:
        assert needle.lower() in body.lower(), needle
    # Must be generalized: no product-specific 'knowledge'/'tier' coupling.
    assert "knowledge" not in body.lower()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_skill_outputs.py::test_expectations_skill_contract_present -v`
Expected: FAIL (skill file absent).

- [ ] **Step 4: Write the skill**

```markdown
---
name: expectations
description: Use when a spec has no explicit definition of done. Adds an Expectations section (success scenarios, failure scenarios, must-nots) in domain language, owned by whoever wanted the outcome. Reads a spec path and writes the section into the spec. Pairs with /executable-assertions.
---

# /expectations

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
Encoding expectations as tests is a separate step (`/executable-assertions` → TDD). If you
reach for verification mechanics, stop and note it for the next step.

**Output / action:**
1. Print the list of definition-of-done gaps you found.
2. Write an `## Expectations` section (the three parts) into the spec file at `$ARGUMENTS`
   (append if absent; update in place if present). Keep it surgical — expectations that
   restate the obvious add noise; keep the ones that close a real gap.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_expectations_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 6: Behavioral smoke check (manual, recorded)**

Run the skill against the fixture and confirm an `## Expectations` section with the three
parts is written and a gaps list is printed:
```bash
cp tests/fixtures/sample-spec.md /tmp/spec.md
claude -p --permission-mode bypassPermissions "/expectations /tmp/spec.md" </dev/null
grep -c "Success scenarios\|Failure scenarios\|Must-nots" /tmp/spec.md   # expect 3
```
Record the output under the task's PR description.

- [ ] **Step 7: Commit**

```bash
git add skills/expectations/SKILL.md tests/fixtures/sample-spec.md tests/test_skill_outputs.py
git commit -m "Plan1 T2: /expectations skill (generalized from prompt)" \
  -m "- skills/expectations/SKILL.md; tests/fixtures/sample-spec.md; tests/test_skill_outputs.py" \
  -m "- generalized KnowledgeSight-specific must-not examples to domain-neutral set" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `/executable-assertions` skill (component 2)

Promote `developing-executable-assertions-from-spec-expectations.md`, generalized the same way.

**Files:**
- Create: `skills/executable-assertions/SKILL.md`
- Test: `tests/test_skill_outputs.py` (add a function)

**Interfaces:**
- Consumes: a spec path with an Expectations section.
- Produces: three ordered outputs — (1) encoded load-bearing expectations + reason each; (2) deliberately-skipped expectations + reason; (3) per encoded expectation, a 4-part spec: **claim / setup / observation / level**. No test code.

- [ ] **Step 1: Write the failing structural test**

```python
# add to tests/test_skill_outputs.py
def test_executable_assertions_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/executable-assertions/SKILL.md")).read().lower()
    for needle in ["claim", "setup", "observation", "level",
                   "load-bearing", "do not write the test code", "must not contain"]:
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
description: Use after /expectations, when a spec's load-bearing expectations need to become machine-checkable. Selects the load-bearing ones and produces 4-part assertion specs (claim, setup, observation, level) — specs only, no test code. Feeds the assertion runner.
---

# /executable-assertions

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
- **Level.** `spec` (one concrete case), a **property** that must hold across all inputs of
  a kind, or a **contract** on a function (pre/post). Prefer a property when the invariant
  is meant to hold universally — a single case can pass while the invariant is broken.

Method/scope:

- Work only from expectations in the spec. Do not invent new ones; if writing an assertion
  exposes a missing expectation, **flag it** rather than quietly adding it.
- Call out expectations that look checkable but aren't, and vague prose that actually
  reduces to a hard Boolean.
- **Do not write the test code** — produce the assertion specs first, so the claims and
  levels can be confirmed before implementation. Getting the claim and observation right on
  paper is what protects against a test that passes while the real invariant is violated.

**Output, in order:** (1) expectations judged load-bearing enough to encode, each with a
one-line reason it made the cut; (2) expectations deliberately not encoded, each with a
one-line reason (unprovable by machine / not load-bearing / subjective); (3) for each
encoded one, the 4-part spec (claim; setup; observation with explicit must-contain and
must-not-contain where exposure is involved; level). Keep it surgical — specs only.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_executable_assertions_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/executable-assertions/SKILL.md tests/test_skill_outputs.py
git commit -m "Plan1 T3: /executable-assertions skill (generalized from prompt)" \
  -m "- skills/executable-assertions/SKILL.md; tests/test_skill_outputs.py" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Harden the assertion runner (component 3 — runner)

The E4 prototype (`assertions/run.py`, current) already does: manifest load (pyyaml +
fallback), per-assertion `setup`+`command`, per-assertion timeout, `results.json`,
fail-closed (missing/unparseable/crash/timeout), exit codes `0/1/2/3`. **Add the four §5.2
gaps:** `teardown`, **isolation** between assertions, an **overall wall-clock** limit, and a
`--level` filter (the spec-done gate runs `level: spec`).

**Files:**
- Modify: `assertions/run.py` (currently 199 lines)
- Modify: `bin/conductor`
- Modify: `assertions/manifest.yaml` (add `teardown`, document `level` use)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `assertions/manifest.yaml` entries `{id, claim, command, setup, teardown, timeout, level}`.
- Produces: `conductor assert run [--level spec|phase|task]` → stdout per-id `[PASS]/[FAIL]`, aggregate `SUMMARY`, `assertions/run/results.json`; exit `0` all-green / `1` red / `2` no-manifest / `3` bad-manifest / `4` overall-timeout.

- [ ] **Step 1: Write failing tests for the four new behaviors**

```python
# tests/test_runner.py
import json, os, subprocess, sys, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py")]

def _manifest(tmp_path, body):
    m = tmp_path / "manifest.yaml"; m.write_text(body); return m

def test_teardown_runs_after_command(tmp_path):
    # teardown writes a marker; assert it exists after the run
    marker = tmp_path / "torn_down"
    body = textwrap.dedent(f"""\
        assertions:
          - id: t1
            command: "true"
            teardown: "touch {marker}"
            timeout: 10
            level: spec
    """)
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(_manifest(tmp_path, body))}
    subprocess.run(RUN, env=env, cwd=ROOT)
    assert marker.exists()

def test_overall_timeout_exit_4(tmp_path):
    body = textwrap.dedent("""\
        assertions:
          - id: slow
            command: "sleep 5"
            timeout: 10
            level: spec
    """)
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(_manifest(tmp_path, body)),
           "CONDUCTOR_OVERALL_TIMEOUT": "1"}
    p = subprocess.run(RUN, env=env, cwd=ROOT)
    assert p.returncode == 4

def test_level_filter_runs_only_spec(tmp_path, capfd=None):
    body = textwrap.dedent("""\
        assertions:
          - id: spec1
            command: "true"
            level: spec
          - id: phase1
            command: "false"
            level: phase
    """)
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(_manifest(tmp_path, body))}
    p = subprocess.run(RUN + ["--level", "spec"], env=env, cwd=ROOT)
    assert p.returncode == 0   # the red 'phase1' is filtered out

def test_isolation_each_assertion_gets_clean_cwd(tmp_path):
    # assertion A creates a file in its cwd; assertion B must not see it
    body = textwrap.dedent("""\
        assertions:
          - id: a
            command: "touch leaked"
            level: spec
          - id: b
            command: "test ! -e leaked"
            level: spec
    """)
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(_manifest(tmp_path, body)),
           "CONDUCTOR_ISOLATE": "1"}
    p = subprocess.run(RUN, env=env, cwd=ROOT)
    assert p.returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — `CONDUCTOR_MANIFEST` env override, `teardown`, `--level`, exit `4`, and
isolation are not yet implemented.

- [ ] **Step 3: Implement the four additions in `assertions/run.py`**

Make these edits (keep the existing fail-closed structure):

```python
# near the constants (after line 40): honor env overrides for testability
MANIFEST = os.environ.get("CONDUCTOR_MANIFEST", os.path.join(ASSERTIONS_DIR, "manifest.yaml"))
OVERALL_TIMEOUT = int(os.environ.get("CONDUCTOR_OVERALL_TIMEOUT", "0"))  # 0 = none
ISOLATE = os.environ.get("CONDUCTOR_ISOLATE", "") not in ("", "0")
EXIT_OVERALL_TIMEOUT = 4

# _run(): add an optional cwd so each assertion can run in an isolated dir
def _run(cmd: str, timeout: int, cwd: str = REPO_ROOT):
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.returncode, ("ok" if proc.returncode == 0 else "nonzero-exit")
    except subprocess.TimeoutExpired:
        return 124, f"timeout>{timeout}s"
    except Exception as exc:
        return 127, f"exec-error: {exc}"

# main(): add --level arg, overall deadline, per-assertion isolation dir, and teardown
import argparse, tempfile, shutil
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=["spec", "phase", "task"], default=None)
    args, _ = ap.parse_known_args()
    deadline = time.monotonic() + OVERALL_TIMEOUT if OVERALL_TIMEOUT else None
    # ... load_assertions(MANIFEST) with the same try/except as today ...
    if args.level:
        assertions = [a for a in assertions if str(a.get("level", "spec")) == args.level]
    results, passed = {}, 0
    for a in assertions:
        if deadline and time.monotonic() > deadline:
            print("SUMMARY: overall wall-clock exceeded -> gate NOT done (exit 4)")
            write_results(results); return EXIT_OVERALL_TIMEOUT
        aid, command = str(a["id"]), str(a["command"])
        setup = str(a.get("setup", "") or ""); teardown = str(a.get("teardown", "") or "")
        timeout = int(a.get("timeout", DEFAULT_TIMEOUT))
        workdir = tempfile.mkdtemp(prefix=f"assert-{aid}-") if ISOLATE else REPO_ROOT
        try:
            start = time.monotonic()
            if setup:
                rc, reason = _run(setup, timeout, workdir)
                if rc != 0:
                    results[aid] = {"pass": False, "rc": rc,
                                    "duration": round(time.monotonic()-start, 3),
                                    "reason": f"setup-failed({reason})"}
                    print(f"[FAIL] {aid} (rc={rc}, reason=setup-failed({reason}))"); continue
            rc, reason = _run(command, timeout, workdir)
            ok = rc == 0
            results[aid] = {"pass": ok, "rc": rc,
                            "duration": round(time.monotonic()-start, 3), "reason": reason}
            print(f"[PASS] {aid}" if ok else f"[FAIL] {aid} (rc={rc}, reason={reason})")
            passed += 1 if ok else 0
        finally:
            if teardown:
                _run(teardown, timeout, workdir)            # best-effort, always runs
            if ISOLATE and workdir != REPO_ROOT:
                shutil.rmtree(workdir, ignore_errors=True)  # isolation cleanup
    write_results(results)
    total, failed = len(assertions), len(assertions) - passed
    if failed == 0:
        print(f"SUMMARY: {passed}/{total} green -> gate DONE (exit 0)"); return EXIT_OK
    print(f"SUMMARY: {passed}/{total} green, {failed} red -> gate NOT done (exit 1)")
    return EXIT_RED
```

(Keep the existing `load_assertions`, `_parse_flat_yaml`, `write_results`, and the
`ManifestMissing`/`ManifestInvalid` handling unchanged. Update the module docstring's exit
table to add `4  overall wall-clock timeout`.)

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

- [ ] **Step 5: Add `teardown` to the manifest schema comment**

In `assertions/manifest.yaml`, document the full per-assertion schema in the header comment:
`id, claim, command, setup, teardown, timeout, level`.

- [ ] **Step 6: Run all runner tests + the existing gate**

Run: `pytest tests/test_runner.py -v && ./bin/conductor assert run`
Expected: new tests PASS; existing gate still `2/2 green -> exit 0`.

- [ ] **Step 7: Lint + typecheck + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add assertions/run.py bin/conductor assertions/manifest.yaml tests/test_runner.py
git commit -m "Plan1 T4: harden assertion runner (teardown, isolation, overall timeout, --level)" \
  -m "- assertions/run.py: env-overridable manifest, per-assertion isolated cwd, teardown finally-block, overall deadline exit 4, --level filter" \
  -m "- bin/conductor forwards --level; manifest.yaml schema comment" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `assertions-to-tests` bridge skill (component 3 — specs → tests)

The §5.1 crux: turn each 4-part assertion spec into ONE runnable test wired into the manifest
by `id`, **using `/test-driven-development`**. The test encodes the claim and stays RED until
the system implements the behavior (that implementation is the build loop's job in Plan 3).

**Files:**
- Create: `skills/assertions-to-tests/SKILL.md`
- Test: `tests/test_skill_outputs.py` (add a function)

**Interfaces:**
- Consumes: the 4-part specs from `/executable-assertions`.
- Produces: for each spec, a test file + a `manifest.yaml` entry (`id` = the spec's id, `command` runs that test, `level` carried through). Establishes the done-gate; does not implement product behavior.

- [ ] **Step 1: Write the failing structural test**

```python
# add to tests/test_skill_outputs.py
def test_assertions_to_tests_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/assertions-to-tests/SKILL.md")).read().lower()
    for needle in ["test-driven-development", "manifest.yaml", "one test per",
                   "id", "red", "must not contain"]:
        assert needle in body, needle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_outputs.py::test_assertions_to_tests_skill_contract_present -v`
Expected: FAIL.

- [ ] **Step 3: Write the skill**

```markdown
---
name: assertions-to-tests
description: Use after /executable-assertions to turn each 4-part assertion spec into one runnable test wired into assertions/manifest.yaml by id, via /test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /assertions-to-tests

Input: the assertion specs (claim / setup / observation / level) produced by
`/executable-assertions`. For **each** spec, produce exactly one runnable test and one
manifest entry, traceable by `id`. **Use `superpowers:test-driven-development`** for the test
itself.

For each assertion spec:

1. **Pick a stable `id`** (kebab-case, derived from the claim, e.g. `unknown-code-404`).
2. **Write the test (RED).** Encode the *claim* as a single pass/fail check. Honor the
   *observation*: assert what the result **must contain** AND, explicitly, what it **must not
   contain** (the dangerous failure is usually a forbidden value being present). Realize the
   *setup* as fixtures. For `level: property`, assert across a generated set of inputs, not a
   single case; for `level: contract`, assert the pre/postconditions. The test stays RED
   until the system implements the behavior — that is expected here.
3. **Wire it into `assertions/manifest.yaml`:**
   ```yaml
     - id: <id>
       claim: "<the one-sentence Boolean claim>"
       command: "python3 -m pytest -q <path/to/test>"
       setup: ""        # optional shell setup, if fixtures need it
       teardown: ""     # optional cleanup
       timeout: 30
       level: <spec|phase|task carried from the assertion spec>
   ```
4. **Verify the gate sees it RED:** `conductor assert run --level spec` lists the new id as
   `[FAIL]` (behavior not yet built). The spec is "done" exactly when every spec-level
   assertion goes green — that is the loop's terminal condition (design §5.1, §7).

**Scope:** one test per assertion spec; do not implement the product behavior (that is the
build loop). Do not collapse two claims into one test. Keep the id stable so a red result
names the violated claim.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_skill_outputs.py::test_assertions_to_tests_skill_contract_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/assertions-to-tests/SKILL.md tests/test_skill_outputs.py
git commit -m "Plan1 T5: assertions-to-tests bridge skill (specs -> manifest-wired tests via TDD)" \
  -m "- skills/assertions-to-tests/SKILL.md; tests/test_skill_outputs.py" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Foundation integration test (the whole chain)

Prove the done-gate foundation end-to-end on the fixture: spec → Expectations → assertion
specs → one manifest-wired test (RED) → stub implementation → gate GREEN. This is the
component-3 deliverable: a machine-checked terminal condition that is RED until behavior
exists and GREEN exactly when it does.

**Files:**
- Test: `tests/test_foundation_e2e.py`
- Create (fixture impl target): `tests/fixtures/shortener/` (stub implemented during the test)

**Interfaces:**
- Consumes: all of Tasks 2–5.
- Produces: evidence that the gate transitions RED→GREEN driven only by behavior, never by default.

- [ ] **Step 1: Write the integration test**

```python
# tests/test_foundation_e2e.py
import os, subprocess, sys, textwrap, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_gate_red_then_green(tmp_path):
    # one assertion: unknown code -> 404. Test encodes the claim.
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
    """))
    env = {**os.environ, "CONDUCTOR_MANIFEST": str(manifest),
           "PYTHONPATH": str(tmp_path)}
    run = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]
    # RED: no shortener module yet -> fail-closed, exit 1
    red = subprocess.run(run, env=env, cwd=ROOT)
    assert red.returncode == 1
    # implement minimal behavior
    (tmp_path / "shortener.py").write_text("def lookup(code):\\n    return 404\\n")
    green = subprocess.run(run, env=env, cwd=ROOT)
    assert green.returncode == 0
```

- [ ] **Step 2: Run it to verify RED→GREEN transition**

Run: `pytest tests/test_foundation_e2e.py -v`
Expected: PASS (the test internally drives the gate from exit 1 to exit 0).

- [ ] **Step 3: Full quality gate + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add tests/test_foundation_e2e.py
git commit -m "Plan1 T6: done-gate foundation integration test (RED->GREEN by behavior only)" \
  -m "- tests/test_foundation_e2e.py: assertion test red without impl, green with minimal impl" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (design §11 components 1–3, §5.1–5.2):**
- Component 1 `/expectations` → Task 2 (skill, generalized, 3-part output, scope boundary). ✓
- Component 2 `/executable-assertions` → Task 3 (4-part specs, selection-first, specs-not-code). ✓
- Component 3 runner + tests-via-TDD → Task 4 (runner hardened to full §5.2: setup/teardown, isolation, per-assertion + overall timeout, fail-closed, level) + Task 5 (specs→tests bridge) + Task 6 (RED→GREEN gate). ✓
- §5.2 runner contract items: location/manifest ✓ (T4/T5); single invocation + per-id + aggregate ✓ (T4); exit semantics ✓ (T4); setup/teardown ✓ (T4); per-assertion + overall timeout ✓ (T4); one test per spec by id ✓ (T5); fail-closed ✓ (existing + T4 tests). 
- Plugin install (§2.1) → Task 1. ✓

**Placeholder scan:** no "TBD/TODO/handle edge cases"; every code step shows code; the runner edits reference the real current file (199 lines) and show the exact additions.

**Type/name consistency:** manifest keys `{id, claim, command, setup, teardown, timeout, level}` used identically across T4 (run.py), T5 (skill), T6 (test). Exit codes `0/1/2/3/4` consistent. CLI `conductor assert run [--level]` consistent T4↔T5↔T6. Env overrides (`CONDUCTOR_MANIFEST`, `CONDUCTOR_OVERALL_TIMEOUT`, `CONDUCTOR_ISOLATE`) used identically in tests and run.py.

**One deliberate change to Jeff's prompts:** generalized the KnowledgeSight-specific
must-not / load-bearing examples (access/exposure/tier) to a domain-neutral set
(access, data exposure, money, integrity, irreversible actions, safety). Structure, method,
and wording otherwise preserved verbatim. Flagged in Tasks 2–3 commit messages.

---

## Open follow-ups (not this plan)
- Plan 2 (ledger + claim model, components 4–5) consumes the manifest/runner here as the
  `assertions decide` half of §7.
- Plan 3 (`/autodev` + `/conductor`, components 6–7) drives spec-level assertions RED→GREEN
  via the build loop; `/conductor` calls `/expectations`+`/executable-assertions` as the
  precondition (or points the user at them, §5).
