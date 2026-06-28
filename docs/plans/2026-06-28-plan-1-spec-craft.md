# spec-craft Plugin Implementation Plan (MVP Plan 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build **`spec-craft`** — a standalone Claude Code plugin with two skills that make a
spec's definition of done explicit and machine-checkable: `/spec-craft:expectations` (adds an
Expectations section) and `/spec-craft:executable-assertions` (derives 4-part assertion specs).

**Architecture:** An independent plugin (`spec-craft/`, its own repo
`automateintelligence/spec-craft`) with **no dependency on conductor** — usable in any project
on its own. The skills are promotions of Jeff's existing prompts
(`developing-expectations-in-the-spec.md`, `developing-executable-assertions-from-spec-expectations.md`),
generalized to be product-agnostic. They output **prose/specs only** (no tests, no code);
downstream tooling (e.g. the conductor done-gate, Plan 2) turns specs into runnable tests.

**Tech Stack:** Markdown skills (Claude Code plugin), Python 3 + pytest (structural tests only).

## Global Constraints

- **Standalone, conductor-agnostic:** spec-craft must not reference or require conductor. It is
  consumed *by* conductor (via a declared `dependencies: ["spec-craft"]` in conductor's
  manifest), never the reverse.
- **Invocation is namespaced:** plugin skill `skills/<x>/SKILL.md` → **`/spec-craft:<x>`**.
  SKILL.md `name:` is display metadata only; no bare names, no aliases (verified via
  claude-code-guide + plugin layout; stage0-notes amendment F).
- **Skills output specs, not code:** both skills produce prose/specs only — no tests, no
  implementation (design §5.1).
- **`level` vs `kind`:** the assertion's logical form is **`kind ∈ {example, property, contract}`**.
  Do NOT call it "level" — `level` is a downstream gate-tier concept (conductor) and must not
  collide here.
- **Python gate:** `ruff check . && ruff format --check . && pyright . && pytest` before any task is complete.
- **Commits:** atomic; message = files changed + 1–2 bullets; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure (spec-craft repo)

| Path | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (`name: spec-craft`; no dependencies). |
| `skills/expectations/SKILL.md` | `/spec-craft:expectations` — add Expectations section to a spec. |
| `skills/executable-assertions/SKILL.md` | `/spec-craft:executable-assertions` — Expectations → 4-part specs (claim/setup/observation/kind). |
| `tests/fixtures/sample-spec.md` | Tiny fixture spec with implicit done-gaps. |
| `tests/test_plugin.py` | Manifest schema validation. |
| `tests/test_skill_outputs.py` | Structural checks that each SKILL.md encodes its contract. |

---

## Task 1: spec-craft plugin scaffold

**Files:**
- Create: `.claude-plugin/plugin.json`, `tests/__init__.py`, `tests/test_plugin.py`

**Interfaces:**
- Produces: installable plugin `spec-craft` whose skills invoke as `/spec-craft:<skill>`.

- [ ] **Step 1: Write the failing manifest-schema test**

```python
# tests/test_plugin.py
import json, os, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_plugin_manifest_schema():
    data = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    assert data.get("name") == "spec-craft"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), "semver version required"
    assert "dependencies" not in data, "spec-craft must be conductor-agnostic (no deps)"
    assert set(data) <= {"name", "version", "description", "author", "displayName",
                         "homepage", "repository", "license"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugin.py -v` → FAIL (manifest absent).

- [ ] **Step 3: Write the manifest**

```json
{
  "name": "spec-craft",
  "version": "0.1.0",
  "description": "Make a spec's definition of done explicit and machine-checkable: add Expectations, then derive executable-assertion specs. Standalone.",
  "author": "Jeffrey A. Daniels"
}
```

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Static validation (recorded smoke):** `claude plugin validate .` (full `--strict` after skills exist, Task 3). Record output.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json tests/test_plugin.py tests/__init__.py
git commit -m "spec-craft T1: plugin manifest + schema test" \
  -m "- .claude-plugin/plugin.json (name=spec-craft, no deps); tests/test_plugin.py" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `/spec-craft:expectations` skill

Promote `developing-expectations-in-the-spec.md`, **generalized** so it works on any spec (the
original's access-control/content-exposure examples are KnowledgeSight-specific; replace with a
domain-neutral set — the only substantive change, made for reusability, not to shorten/restyle).

**Files:**
- Create: `skills/expectations/SKILL.md`, `tests/fixtures/sample-spec.md`
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
    assert "conductor" not in body, "spec-craft must be conductor-agnostic"
```

- [ ] **Step 3: Run test to verify it fails** → FAIL (file absent).

- [ ] **Step 4: Write the skill**

```markdown
---
name: expectations
description: Use when a spec has no explicit definition of done. Adds an Expectations section (success scenarios, failure scenarios, must-nots) in domain language, owned by whoever wanted the outcome. Reads a spec path and writes the section into the spec. Pairs with /spec-craft:executable-assertions.
---

# /spec-craft:expectations

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
Encoding expectations as tests is a separate step (`/spec-craft:executable-assertions` → TDD).
If you reach for verification mechanics, stop and note it for the next step.

**Output / action:**
1. Print the list of definition-of-done gaps you found.
2. Write an `## Expectations` section (the three parts) into the spec file at `$ARGUMENTS`
   (append if absent; update in place if present). Keep it surgical — expectations that
   restate the obvious add noise; keep the ones that close a real gap.
```

- [ ] **Step 5: Run test to verify it passes** → PASS.

- [ ] **Step 6: Behavioral smoke check (recorded)**

```bash
cp tests/fixtures/sample-spec.md /tmp/spec.md
claude -p --permission-mode bypassPermissions "/spec-craft:expectations /tmp/spec.md" </dev/null
grep -c "Success scenarios\|Failure scenarios\|Must-nots" /tmp/spec.md   # expect 3
```

- [ ] **Step 7: Commit**

```bash
git add skills/expectations/SKILL.md tests/fixtures/sample-spec.md tests/test_skill_outputs.py
git commit -m "spec-craft T2: /spec-craft:expectations skill (generalized from prompt)" \
  -m "- skills/expectations/SKILL.md; tests/fixtures/sample-spec.md; tests/test_skill_outputs.py" \
  -m "- generalized KnowledgeSight-specific must-not examples to a domain-neutral set" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `/spec-craft:executable-assertions` skill

Promote `developing-executable-assertions-from-spec-expectations.md`, generalized the same way.
**The prompt's 4th part is named "Kind"** (example/property/contract) — Jeff's description is
preserved verbatim; the field is `kind` (not "level", which is a downstream gate-tier concept).

**Files:**
- Create: `skills/executable-assertions/SKILL.md`
- Test: `tests/test_skill_outputs.py` (add a function)

**Interfaces:**
- Consumes: a spec path with an Expectations section.
- Produces: three ordered outputs — (1) encoded load-bearing expectations + reason each; (2) deliberately-skipped + reason; (3) per encoded expectation, a 4-part spec: **claim / setup / observation / kind** (`example | property | contract`). No test code.

- [ ] **Step 1: Write the failing structural test**

```python
# add to tests/test_skill_outputs.py
def test_executable_assertions_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/executable-assertions/SKILL.md")).read().lower()
    for needle in ["claim", "setup", "observation", "kind",
                   "load-bearing", "do not write the test code", "must not contain",
                   "example", "property", "contract"]:
        assert needle in body, needle
    assert "knowledge" not in body and "tier" not in body          # generalized
    assert "conductor" not in body                                 # conductor-agnostic
```

- [ ] **Step 2: Run test to verify it fails** → FAIL.

- [ ] **Step 3: Write the skill**

```markdown
---
name: executable-assertions
description: Use after /spec-craft:expectations, when a spec's load-bearing expectations need to become machine-checkable. Selects the load-bearing ones and produces 4-part assertion specs (claim, setup, observation, kind). Specs only, no test code — feeds any downstream test runner.
---

# /spec-craft:executable-assertions

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
  the invariant is broken.

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

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Plugin discovery validation (recorded smoke)**

```bash
claude plugin validate . --strict 2>&1 | tee /tmp/spec-craft-strict.txt
test -f skills/expectations/SKILL.md && test -f skills/executable-assertions/SKILL.md \
  && echo "BOTH SKILLS PRESENT"
```
Expected: no errors; confirms `/spec-craft:expectations` + `/spec-craft:executable-assertions` discoverable.

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && ruff format --check . && pyright . && pytest -q
git add skills/executable-assertions/SKILL.md tests/test_skill_outputs.py
git commit -m "spec-craft T3: /spec-craft:executable-assertions skill (generalized; kind field)" \
  -m "- skills/executable-assertions/SKILL.md; tests/test_skill_outputs.py" \
  -m "- 4th part = Kind (example/property/contract); conductor-agnostic" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Coverage:** design components 1 (expectations) → T2; component 2 (executable-assertions) → T3;
plugin install (§2.1) → T1/T3.

**Standalone guarantee:** tests assert no `conductor` reference in either skill and no
`dependencies` in the manifest — spec-craft is independently installable and usable.

**Placeholder scan:** none; full SKILL.md bodies given.

**Consistency:** `kind ∈ {example, property, contract}` used (never "level"); `/spec-craft:<skill>`
namespacing uniform; generalized examples (no "knowledge"/"tier").

**Deliberate prompt changes:** generalized KnowledgeSight-specific examples; field named `kind`.
Structure/method/wording otherwise preserved verbatim.

---

## Hand-off to Plan 2 (conductor done-gate)
Conductor's manifest declares `dependencies: ["spec-craft"]`; conductor's recipe invokes
`/spec-craft:executable-assertions` and feeds its 4-part specs into `/conductor:assertions-to-tests`
+ the runner. spec-craft itself knows nothing about conductor.
