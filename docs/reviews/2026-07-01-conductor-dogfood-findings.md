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
