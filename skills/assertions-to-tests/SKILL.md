---
name: assertions-to-tests
description: Use after /spec-craft:executable-assertions to turn each 4-part assertion spec into one runnable test wired into the run's gate manifest by id, via /superpowers:test-driven-development. Establishes the machine-checked done-gate; does not implement product behavior.
---

# /conductor:assertions-to-tests

Input: the 4-part assertion specs (claim / setup / observation / kind) from
`/spec-craft:executable-assertions`, which persists them to **`<spec>.assertions.md`** (a sibling of
the spec). **Read them from that file** — it is the source of truth and may have been hand-edited
after generation, so use it as-is; do not regenerate. For **each** spec, produce exactly one runnable
test and one manifest entry, traceable by `id`. **Use `/superpowers:test-driven-development`** for the test.

> **The gate is per-spec.** The manifest, tests, and `.frozen` for THIS run live under the run's
> gate dir — `GATE_DIR="$(conductor gate-dir <spec>)"` → `assertions/<slug>/` (single-sourced with
> the run branch; `<slug>` = the run-branch slug). Namespacing keeps sibling specs conducted in
> other worktrees from contending for the one flat `assertions/` slot at the shared merge base.
> Write the manifest at `$GATE_DIR/manifest.yaml` and the tests under `$GATE_DIR/tests/`. `/conductor:start`
> exports `CONDUCTOR_GATE_SLUG` so `conductor gate lint|freeze` and `assert run` resolve the same dir.
> (Invoked standalone with no run set up yet, `conductor gate-dir <spec>` still names the dir.)

For each assertion spec:

1. **Pick a stable `id`** (kebab-case from the claim, e.g. `unknown-code-404`). Keep it stable
   so a red result names the violated claim.
2. **Write the test (it stays RED).** Encode the *claim* as one pass/fail check. Honor the
   *observation*: assert what the result **must contain** AND, explicitly, what it
   **must not contain**. Realize the *setup* as fixtures. Match the test to the spec's **kind**: `example`
   → one concrete case; `property` → assert across generated inputs; `contract` → assert
   pre/postconditions. RED until the system implements the behavior — expected here.
3. **RED-TEAM the test before you keep it — a weak assertion is worse than none.** A frozen test
   that passes while the intent is FALSE certifies fake completion — the exact gate-green-≠-done trap
   this gate exists to prevent, and the one that most often bites the gate's OWN tests (live finding
   2026-07-06: a spec's done-gate shipped six assertions that each passed against a stub). For every
   test, ask: **what trivial or hard-coded implementation would make this pass while the claim is
   actually violated?** Tighten until only a real implementation can pass. Close these holes:
   - **Hard-coded value passes** — checking that output *contains* a fixed string passes a stub that
     always prints it. Anchor to a source of truth (assert the output tracks a declared set / the
     real surface), or assert the input→output mapping across cases — never one constant.
   - **One case passes a `property`** — a `property` asserted on a single input passes while the
     invariant breaks on another. Exercise the range (e.g. group- AND world-writable, not just world;
     every enum value, not one).
   - **Exists-but-unused** — asserting a command/function EXISTS does not prove the caller USES it;
     the prose fragility survives. Assert the caller invokes it (a needle over the skill/recipe).
   - **Tautological / no negative** — a positive-only assertion passes a hollow implementation; every
     observation must also name what MUST be ABSENT (step 2's must-not-contain is the enforcement).
   - **Self-referencing scope** — a grep/presence check must EXCLUDE the files that legitimately
     contain the forbidden token (e.g. the spec that documents a removal), or it can never pass.
   If a hole can't be closed mechanically, say so and mark that assertion for human review rather than
   freezing a check that reads green on a fake.
4. **Wire it into the run's gate manifest (`$GATE_DIR/manifest.yaml`) with a PINNED, STANDALONE
   command** whose test path is repo-relative (so it resolves under the per-spec gate dir):**
   ```yaml
     - id: <id>
       claim: "<one-sentence Boolean claim>"
       command: "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider assertions/<slug>/tests/<test>.py"
       setup: ""
       teardown: ""
       timeout: 30
       level: spec        # GATE TIER: spec (default) | phase | task
       kind: <example|property|contract>   # FORM, carried from the assertion spec
   ```
   **Why pinned (determinism + freeze integrity):** autoload off + `--noconftest` means nothing
   OUTSIDE the frozen test file can influence the check — an autoloaded plugin (e.g. `typeguard`)
   can flip pass/fail across machines, and an *unfrozen* ancestor `conftest.py` is a gate bypass
   (edit it to flip a frozen test without tripping tamper). It's also much faster (no repo-wide
   conftest/plugin loading). Consequence: each test must be self-contained — bootstrap `sys.path`
   itself and define its own fixtures; it cannot rely on any `conftest.py`.
5. **Verify the gate sees it RED:** `conductor assert run --level spec` lists the new id as
   `[FAIL]`. The spec is "done" exactly when every spec-level assertion goes green (§5.1, §7).

**Where it lives:** the manifest is the run's **per-spec** `assertions/<slug>/manifest.yaml` in the
**project** (conductor resolves the project as the git repo of cwd — run from the project root;
`<slug>` from `conductor gate-dir <spec>`), and the tests live under `assertions/<slug>/tests/` in the
project too, referenced by the manifest command. Both are git-committed with the project, never
written into the plugin cache. A run with no slug resolved falls back to the flat legacy
`assertions/manifest.yaml`.

**Scope:** one test per assertion spec; do not implement product behavior. `level` is the gate
tier (assigned here); `kind` is the assertion form (from the spec) — never conflate them.
