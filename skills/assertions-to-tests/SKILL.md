---
name: assertions-to-tests
description: Use after /spec-craft:executable-assertions to turn each 4-part assertion spec into one runnable test wired into assertions/manifest.yaml by id, via /superpowers:test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /conductor:assertions-to-tests

Input: the 4-part assertion specs (claim / setup / observation / kind) from
`/spec-craft:executable-assertions`, which persists them to **`<spec>.assertions.md`** (a sibling of
the spec). **Read them from that file** — it is the source of truth and may have been hand-edited
after generation, so use it as-is; do not regenerate. For **each** spec, produce exactly one runnable
test and one manifest entry, traceable by `id`. **Use `/superpowers:test-driven-development`** for the test.

For each assertion spec:

1. **Pick a stable `id`** (kebab-case from the claim, e.g. `unknown-code-404`). Keep it stable
   so a red result names the violated claim.
2. **Write the test (it stays RED).** Encode the *claim* as one pass/fail check. Honor the
   *observation*: assert what the result **must contain** AND, explicitly, what it
   **must not contain**. Realize the *setup* as fixtures. Match the test to the spec's **kind**: `example`
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

**Where it lives:** the manifest is the **project's** `assertions/manifest.yaml` at the project root
(conductor resolves the project as the git repo of cwd — run from the project root), and the tests
live in the project too, referenced by the manifest command. Both are git-committed with the
project, never written into the plugin cache.

**Scope:** one test per assertion spec; do not implement product behavior. `level` is the gate
tier (assigned here); `kind` is the assertion form (from the spec) — never conflate them.
