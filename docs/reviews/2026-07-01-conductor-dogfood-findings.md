# Conductor dogfood findings — 2026-07-01

**Context.** First real-project run of conductor as an *installed plugin* (0.2.0 → 0.3.0), driving
the `2026-06-29-model-extraction-evaluation-spec` on the `~/programming/ai` monorepo. Goal of the
run is **conductor working optimally** (the eval spec is the test vehicle). This doc captures
conductor improvements surfaced so they survive session restarts. **Living doc — append as the
dogfood continues.**

---

## Shipped this session (merged + installed)

- **0.3.0 — project-root integration** (PR #25). Split `REPO_ROOT` → `PLUGIN_ROOT` (tool code) +
  `PROJECT` (`$CONDUCTOR_HOME`, else git-repo-of-cwd, else cwd) in one shared `conductor/paths.py`.
  Run state + gate (`.conductor/` goal/handoff, `assertions/manifest.yaml`, `.frozen`,
  `results.json`, test cwd) now anchor to the **project**, not the plugin cache; `handoff.write`
  anchors too; skills state the model. Verified: pytest 101, live E2E, no plugin-dir leak.
- **0.2.0 — assertions-file handoff** (conductor #24 + spec-craft #3). `/spec-craft:executable-assertions`
  persists specs to `<spec>.assertions.md`; conductor `start` + `assertions-to-tests` read it,
  idempotently (never regenerate over hand edits).
- **0.2.0 — cron driver defaults** (#23): `CronCreate "*/7 * * * *"`, `durable:true`; docs for
  idle-only firing, 7-day expiry, Tier-B terminal survival.
- **0.2.0 — source-aware install** (#22) + published `automateintelligence/marketplace` catalog.

---

## Open findings (prioritized)

### HIGH — Freeze integrity hole: a check's execution dependencies aren't captured
`conductor/freeze.py::_referenced_files` hashes only files **named** in `command`/`setup`/`teardown`.
A bare `pytest <test>` also loads, *unnamed*: ancestor `conftest.py` files and autoloaded pytest
plugins. Neither is hashed → neither is frozen. So a worker can leave the frozen **test file**
untouched and instead edit an ancestor `conftest.py` (add a fixture, monkeypatch, alter collection)
to flip a frozen test's result **without tripping tamper** — bypassing the central "worker can't
cheat the gate" property. Swapped/added plugins do the same from the environment side.
- *Evidence:* dogfood run; the agent independently chose `--noconftest` + self-bootstrap, noting it's
  "immune to future conftest churn" — the benign version of the same bypass.
- *Fix (primary):* have `assertions-to-tests` emit **standalone/pinned** commands (`--noconftest`,
  autoload off, explicit `-p`) so nothing outside the frozen file can influence the check.
- *Fix (thorough):* `freeze.py` also walks + hashes the ancestor `conftest.py` chain a command would
  load (heavier, but covers non-standalone tests).

### HIGH — `assertions-to-tests` should generate pinned/standalone pytest commands
Motivated by **determinism** (autoloaded plugins — e.g. `typeguard` — can flip pass/fail across
machines → a reproducibility hole in a "machine-checked definition of done") *and* speed. Concrete
form the agent hand-crafted for all 19 entries:
```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider <test>
```
Feeds the freeze fix above. The runner stays command-agnostic; the skill (which knows it's emitting
pytest) is the right place to pin the environment. Also note where assertion tests are placed so they
don't inherit a heavy ancestor `conftest.py`.

### HIGH — Setup never codex-reviews the plan, and the generated plan drops the recipe's review/merge steps
Two related gaps, same root: the plan comes from conductor-unaware `/superpowers:writing-plans`.
1. **The plan artifact is never codex-reviewed.** `/conductor:start` step 4 generates `plan.md` and
   goes straight to issue-sync — no review gate. The plan dictates every phase yet is the *least*-reviewed
   setup artifact, and it contradicts the standing "codex-review before proceeding" order. *(Operator
   caught this live: "/conductor did not require a codex review for the plan.")*
2. **The generated plan's per-phase workflow omits conductor's recipe.** The dogfood plan's per-phase
   flow is `RED → implement → GREEN → ruff/pyright → commit → gate-verify`. Missing vs the autodev
   recipe: `/code-review` per task, **`/codex review`**, receiving-code-review, `conductor merge-gate`,
   `/document-release`. Full-auto still runs the recipe (the autodev skill applies it regardless), but a
   **supervised** build that follows the plan literally silently skips codex-per-PR and the merge-gate.
- *Fix:* (a) `/conductor:start` runs a **codex review of the plan** (step 4→5), surfaces findings,
  revises before the ledger/build; (b) the plan template / writing-plans invocation carries conductor's
  per-phase recipe steps (codex, merge-gate, document-release), or the plan explicitly states "each
  phase executes via the autodev recipe" so the discipline can't be dropped by reading it literally.

### MED–HIGH — autodev recipe (TDD) vs the frozen-gate model: the phase cycle is ambiguous
The autodev recipe runs each phase through `/superpowers:subagent-driven-development`, which is
**TDD-first** ("write a failing test → implement"). But conductor's done-gate tests are **pre-written
and frozen at setup**, and the worker must never edit a frozen test. So the TDD "write the test" step
is wrong/redundant for a conductor phase — the correct cycle is **"confirm the phase's frozen
assertions are RED → implement the product → confirm GREEN → review → commit."** The dogfood agent had
to *reverse-engineer this inversion on its own* (same class of problem as the integration-model
reverse-engineering).
- *Evidence:* dogfood run, agent's own words: *"each task's cycle isn't 'write failing test →
  implement' — it's 'confirm the frozen assertion is RED → implement the stub → confirm GREEN.'"*
- *Fix:* the `autodev` skill should state the conductor phase cycle explicitly — implement **to** the
  frozen gate; frozen tests are never written or touched; task-level tests may be *added* (freeze
  allows adding, never weakening) but aren't required when the frozen assertions already specify the
  unit. So no run re-derives the inversion.

### MEDIUM — User coding principles (CLAUDE.md) must propagate to subagents + the codex review
Conductor is (correctly) **principle-agnostic** — it should inherit each user's project/global
`CLAUDE.md` rather than hardcode standards ("use libraries, don't reinvent" is Jeff's; other users
bring their own). The general-plugin answer is "rely on CLAUDE.md for all users." The design work is
**propagation**, not encoding:
- **Enforcement lives in the reviews, not the gate.** The done-gate checks *behavior*, not *style* — it
  can't tell "used a library" from "hand-rolled it." So qualitative principles are a `/code-review` +
  `/codex` + receiving-code-review concern.
- **Implementation subagents** (fresh context per phase): verify they inherit the project `CLAUDE.md`;
  if not, the autodev recipe should anchor them to it explicitly (prevention as the code is written).
- **codex is the leak:** `/code-review` + receiving-code-review are Claude in-project (get `CLAUDE.md`
  automatically), but `/codex` is an external CLI that won't auto-load it — the codex review invocation
  should point it at the project `CLAUDE.md` so it enforces the user's principles too.
- *Nuance:* a few principles ARE gateable (coverage thresholds, "no TODOs") and could become assertions;
  qualitative dogma stays in review — don't assert the un-assertable.
- *Fix:* (a) verify/ensure `CLAUDE.md` reaches fresh subagents; (b) the codex review references the
  project `CLAUDE.md`.

### MEDIUM — Gate runtime is O(N × cold-pytest-startup), run serially
`assertions/run.py` runs assertions **sequentially**, one cold subprocess each. On this monorepo:
~2 min before the pinned-command fix (heavy plugin autoload ×19), ~57s after (≈3s/assertion cold
pytest startup ×19). Grows with N, and the spec gate runs on every autodev fire.
- *Levers:* (a) **parallelize** `run.py` (bounded thread pool; pair with `CONDUCTOR_ISOLATE` for cwd
  safety; must reconcile with the wall-clock deadline logic). (b) `--level` scoping so autodev runs a
  narrower `task`/`phase` level during phase work and the full `spec` gate only at the done-check.
- *Note:* demoted from urgent after the pinned-command fix; log for when N grows.

### MEDIUM — Configurable gate location for monorepos
conductor forces `assertions/` + `.conductor/` at the **project root** (git-repo-of-cwd). A monorepo
with a "no new top-level dirs" convention can't cleanly scope the gate to a subdir without
`CONDUCTOR_HOME=<subdir>` on **every** call — fragile: the subdir isn't its own git repo (so
auto-resolution won't reach it), the cron / Tier-B autostart can't easily carry the env var, and a
single forgotten one silently points the gate at the wrong place.
- *Fix:* a **project-committed config** (e.g. `.conductor/config` with a gate-dir field) so a monorepo
  can point the gate at a subdir with no env var and no per-call plumbing.

### LOW–MED — Supervised setup has no auto-recovery from transient interruptions
The **autonomous** loop is self-healing: an API error / dropped connection mid-phase just means the
next cron fire reconciles and continues. But the **supervised setup** phase (build gate → plan →
ledger → goal) is interactive, with no cron re-firing — so a "connection closed mid-response" simply
**stalls until a human types "continue."** During the dogfood run the operator hit this (API error
while writing the plan) and had to hand-roll resilience with a recovery `/loop` (fire a "resume if
stalled, but stop at the supervised checkpoint" prompt every ~15m).
- *Fix options:* make `/conductor:start`'s setup resumable-by-default under a light self-heartbeat, or
  simply **document the recovery `/loop`** for supervised setup so operators don't invent it. The loop
  prompt must scope to "resume setup only; do NOT arm the cron / start phases" or it can push past the
  supervised checkpoint.

### LOW–MED — `$CLAUDE_PLUGIN_ROOT` unreliability
The run saw `$CLAUDE_PLUGIN_ROOT=.claude` (a bogus relative path); the agent fell back to the absolute
0.3.0 `bin/conductor`. The `start` skill leans on the variable — it should validate it (absolute dir
containing `bin/conductor`) and fall back gracefully rather than trust it.

---

## Design decisions (need a human call, not just a fix)

### Auto-merge to `main` vs. a PR-review flow
autodev auto-merges each gated phase PR to `main` (behind merge-gate + codex + per-task review). This
conflicts with a PR-review + smoke-gate flow. Options: keep walk-away auto-merge; switch to
open-PR-and-wait-for-approval; make it **configurable** (default to gated). *Jeff leans gated but is
running walk-away to dogfood the loop.*

### Top-level gate dirs at the monorepo root
Decided for this run: **allow** `assertions/` (committed contract — the only visible one) +
`.conductor/` (gitignored scratch) at the repo root. Ties directly to the "configurable gate
location" finding — the proper long-term answer is the committed config.

---

## Deferred (Phase 2 / known)

- **Single-flight guard / dispatcher** for *controlled* parallelism. Single-loop is already safe
  (`CronCreate` fires only while idle → ticks can't overlap a running fire); the dispatcher is the
  Phase-2 piece that caps and assigns parallel workers.
- **Same-login claim "small race window"** — known audit item.
- **E5 full live-loop self-stop** is gated (`RUN_CONDUCTOR_E2E=1`), manual, shares the live remote;
  the Tier-B `@reboot` autostart snippet is documented but not reboot-tested.

---

## Process learnings

- **codex-review every PR** before recommending merge (standing order).
- **Run the full `pytest` suite on skill-only PRs**, not just `claude plugin validate` — a stale
  `test_start_skill_contract` needle slipped through PR #23 (validate-only) and only surfaced when the
  full suite ran during the 0.3.0 work.
- **`plugin update` no-ops without a `plugin.json` version bump.** To ship: merge → bump version →
  `marketplace update` → `plugin update <plugin>@<marketplace>` → **restart the session**.
