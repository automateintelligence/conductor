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

**CONFIRMED LIVE (Phase 1, 2026-07-01).** The agent built + committed Phase 1 per the plan and
**stopped — no PR, no `/codex`** — presenting it as "complete for your review." Only the operator's
catch ("shouldn't there be a /codex-reviewed PR?") triggered the correction. Crucially the agent
*knew* the recipe (it recited it and self-corrected instantly), so the plan didn't merely omit the
steps — **it overrode the recipe in execution**: when the operative plan and the skill's recipe
disagreed, the plan won. So the fix must make the recipe **authoritative** over the plan's per-phase
shorthand, not just add steps to one plan. Also surfaced: it committed setup + Phase 1 onto **one
branch** instead of per-phase PRs off `main` — the branching model ("one PR per phase off `main`;
setup its own PR") needs to be explicit too.

**Autonomous 3–6 (2026-07-01).** After two clean cycles the operator let phases 3–6 run unsupervised.
The codex step is *earning its keep* — it caught **2 real edge-case bugs** in Phase 2's `axis_verdict`
(a float delta-boundary suppressing a legit BEATS; a zero-width-band containment mislabeled) that the
subagent introduced and the frozen fixtures didn't cover — exactly what skipping codex would ship. But
the discipline is living in the **agent's working context** (it applies the full cycle from memory, not
from the plan), so a mid-run context loss that resumes from the plan could silently skip codex on the
remaining phases — now *unsupervised, merging to `main`*. Reinforces: the recipe must be **enforced**
(skill/plan), not context-dependent, before autonomous runs are trustworthy. *(Positive: merge-gate
worked well when scoped via `CONDUCTOR_MERGE_VERIFY` rather than bypassed — kept its mergeability/thread
checks and verified the phase's assertions green on the merged ref.)*
- *Root cause (verified 2026-07-01):* `start` step 4 is a bare `/superpowers:writing-plans` invocation
  — it passes **nothing** about the workflow. The recipe is defined only in **autodev step 6** and is
  never handed to the plan-writer, so the generated plan can't encode it.
- *Fix — defense in depth (the "add it vs check it" binary collapses to both):*
  1. **Input:** step 4 hands `writing-plans` the conductor per-phase recipe so the plan encodes it.
  2. **Check:** a post-plan verification that the plan actually carries the recipe — `writing-plans` is
     a general LLM skill, so "asked" ≠ "included." Natural home = the **codex-review-of-the-plan** step
     `start` also lacks (one step does both: catch a bad plan + confirm the recipe is present).
  3. **Execution:** make the recipe **authoritative** so autodev + the supervised path apply it
     regardless of the plan (Phase 1 proved a plan can *override* the recipe, so 1+2 alone aren't safe).

### HIGH — Frozen-RED gate collides with every "run-all-tests-green" mechanism (CI *and* merge-gate)
Conductor's done-gate is tests that are **frozen RED and go green phase-by-phase**, living in the
project's test tree (`tests/model_eval/`). Any "run the whole suite" step collects them and fails until
the gate is fully green (Phase 6):
- **Project CI** runs `pytest tests/` → collects the 16 not-yet-implemented gate tests → **red CI on
  every phase PR**.
- **conductor's own merge-gate**: `CONDUCTOR_MERGE_VERIFY` defaults to **`pytest -q`**
  (`merge_gate.py:171`), re-run on the merged ref → same full suite → `merge-ref-verify-failed`
  **blocks every per-phase merge**.
So both the project CI *and conductor's merge-gate* bake in "all-green-when-done" (autodev's
merge-when-the-whole-gate-is-green), fighting the **per-phase merge** workflow where the gate is
intentionally incremental. *(Dogfood Phase 1→2 boundary, 2026-07-01, confirmed by code.)*
- *Fix (this run):* exclude the gate from full-suite runs until Phase 6 in **both** places — CI
  (`--ignore=tests/model_eval`) and merge-gate (`CONDUCTOR_MERGE_VERIFY="pytest -q --ignore=tests/model_eval"`,
  which *keeps* merge-gate's mergeability/thread/draft/behind checks rather than bypassing the gate).
  Re-include at Phase 6 — **track as a checkbox**; if forgotten, the gate never runs in CI/merge-gate again.
- *Fix (conductor):* (a) make gate tests a recognizable, **auto-excludable class** — a pytest marker
  (`conductor_gate` + `-m "not conductor_gate"`) or dir convention — so full-suite runs skip the
  not-yet-green gate with no per-project `--ignore` to remember/remove; (b) scope merge-gate's verify to
  a **phase-level** criterion (this phase's assertions), not blanket `pytest`; (c) document the gate ↔
  CI/merge-gate relationship. Same "per-phase merge vs all-green-when-done" theme as the recipe finding.

### MED — Interactive/supervised runs operate *outside* conductor's resilient loop (compaction + ledger) — a usage consequence, not a core design flaw
Conductor is *designed* to be compaction-proof: the cron fires `/conductor:autodev`, and **each fire is
fresh context that reconciles from durable state and reloads the recipe from the skill.** But the
dogfood ran all phases in **one long interactive session** (agent dispatches a subagent per phase and
applies the cycle from its own accumulating context). That forfeits the immunity — the session hit
auto-compact mid-build (≈33%→20%).
- **What survives compaction:** the *place* — reconcile-first re-derives progress from git + plan +
  gate. Solid.
- **What's at risk:** the *how* — the codex/merge-gate discipline lives in the accumulating context,
  not reloaded per phase, so a compact summary can drop "always codex" and leave the agent following
  the plan (stops at commit) → skips codex, now unsupervised + merging to `main`. Auto-compact is the
  most likely trigger of the recipe-authoritative risk above.
- *Cause (2026-07-01) — NOT a conductor design flaw (operator corrected an earlier over-escalation):*
  the ledger is `/conductor:start` **step 5** (issue-sync *creates* it; autodev only *reconciles* an
  existing one). In the operator's **supervised** choice, the agent **reasonably deferred step 5** —
  holding outward-facing GH issues until the plan is reviewed is what supervised mode *should* do, and
  an autonomous-at-setup run would have created the ledger there. So the missing ledger is a
  *consequence of the supervised path + a defensible deferral*, not a hole in the design (verified: no
  milestone/issues on the repo).
- *The real (soft) gap — UX/robustness, shared by the advisor:* at the **autonomy transition** (operator
  later chose "autonomous 3–6"), neither the agent nor the advisor flagged that autodev / resilient mode
  *requires* the ledger that supervised deferred. `/conductor:autodev` reconciles + **claims from the
  ledger**, so with none present "autonomous" silently meant *interactive-continuous* (accumulating
  session, recipe-in-memory, compaction-fragile), not the designed *autodev-fired* loop (ledger, fresh
  context per fire, recipe-from-skill). The ledger is the linchpin between the two.
- *Fix (operator):* to get resilient autonomy, create the ledger (`/conductor:issue-sync` → milestone +
  phase issues; reconcile marks 1–2 done, 3–6 ready), then drive phases via `/conductor:autodev` fires
  (fresh context + recipe-from-skill each fire). Or, staying interactive, restart into a fresh session
  at each phase boundary and re-anchor to the recipe; don't let auto-compact summarize mid-phase.
- *Fix (conductor):* don't offer/accept "go autonomous" without the ledger its autonomy requires —
  either create the ledger as part of choosing autonomy, or clearly state that ledger-less "autonomous"
  is interactive-continuous (compaction-fragile), not the autodev-fired loop.
- *Fix (conductor):* the design's compaction-immunity only holds when phases run as discrete
  `/conductor:autodev` fires — so the skills/docs should steer supervised runs to *also* execute per
  fresh fire (or checkpoint+restart at phase boundaries), not one accumulating interactive session.
  Same root as recipe-not-authoritative: the recipe must come from the skill each fire, never memory.

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
  `marketplace update` → `plugin update <plugin>@<marketplace>` → **`/reload-plugins` in running
  sessions** (no restart needed — operator correction 2026-07-02; a restart also works).

---

## 2026-07-02 — Restart resilience: clerical state decays to zero (live, Phases 1–3)

Operator question: *"What is the restart resiliency if a phase is interrupted mid-execution? Do we
rely on the model figuring that out before it starts building something that is already done?"*
Honest answer: **at task granularity, yes** — and the live run quantifies how bad the reliance on
model-performed bookkeeping is.

**Live evidence (3 phases complete).** What the model maintained vs dropped:

| State surface | Design says | Live run after 3 phases |
|---|---|---|
| Per-phase branch + PR + codex round + merge | recipe steps 3–7 | ✅ kept (after Phase-1 prodding) |
| Per-task commits | recipe step 3 | ✅ kept — pinned messages, recoverable |
| Plan checkboxes | step 8 "update plan.md index" | ❌ **0 of 27 ever ticked** |
| Ledger status labels | step 8 | ❌ painted once by issue-sync (baked into the dict), never maintained |
| Handoff (`.conductor/handoff.md`) | step 9, every fire | ❌ **never written** (only `goal.md` exists) |
| Lease / claim / reconcile | steps 2, 5 | ❌ never invoked (interactive mode never enters the loop) |

**The pattern:** a step survives iff its output is load-bearing for the model's *immediate next
action* (can't open a PR without a commit). Pure bookkeeping whose value is deferred to a future
context — checkboxes, labels, handoffs — decays to zero. Same root as the Phase-1 skipped-PR
finding: prompt-listed steps are not a durable mechanism.

### HIGH — State transitions must be mechanical side effects, not prompt steps
Phase completion currently asks the model to separately: flip the label, tick plan boxes, write the
handoff, release the lease (autodev steps 8–9). All four decayed live. But the model *reliably* runs
`conductor merge-gate` (it blocks the merge it wants). Fix: one `conductor phase-done <issue>`
(or merge-gate side effect) that atomically flips the label, ticks the plan's phase boxes, writes
the handoff, releases the lease. One load-bearing command it must run > four clerical steps it
will drop.

### HIGH — reconcile trusts caller-supplied truth instead of deriving it
`ledger/reconcile.py:11-12` takes `tests_red` / `pr_merged` as **flags from the caller** — the
repair loop that exists to fix model bookkeeping drift is itself fed by model-reported state. Fix:
reconcile (or a wrapper) derives `tests_red` from `conductor assert run` output directly. Needs the
phase→assertion-ID mapping to be machine-readable (assertion `id`s in the phase-issue body or a
`phase:` field in manifest entries); today it lives only in prose phase titles. Then labels become
derived state, repaired from ground truth with no model honesty in the loop.

### MED — No intra-phase resume procedure (task-grain recovery = model inference)
On stale-lease reclaim a fresh worker gets the plan + git. The recipe's per-task commits with pinned
messages ARE machine-readable breadcrumbs — but nothing in autodev step 6 mandates "diff the plan's
tasks against `git log` on the phase branch and the gate's per-assertion RED/GREEN **before**
implementing", and an uncommitted dirty tree at crash time is entirely unspecified (stash? commit
WIP? reset?). Add both to the recipe: reconcile-within-phase first, and a dirty-tree policy.

### MED — `ledger convert` can't parse real plans → hand-built dicts → task layer silently dropped
Why the ledger is titles-only: `sync.py:5` `_H2` requires the conductor dialect `## Title [status]`,
but `/superpowers:writing-plans` emits `## Phase N — Title (A3, A4, A5)` — zero phases parse, so the
agent hand-built the `generate` dict and dropped every phase's `tasks[]` (hence: empty bodies —
by design — but also **zero task sub-issues/checklists**, which is the real loss). Milestone
description empty is by design (`sync.py:62-64` passes title only). Fix: tolerant `_H2` (optional
trailing `[status]`, default `ready`) so `convert <plan.md>` harvests the `- [ ]` task lines real
plans already contain.

**Also strengthens** the existing MED "interactive/supervised runs operate outside the resilient
loop": it's not just that cron/reconcile don't fire — a supervised run accrues **no durable run
state at all beyond git**. Restart resilience today = git history + the frozen gate + the model
re-deriving everything else.

---

## 2026-07-02 (later) — Operator findings + Fix pass 1 scope

### HIGH — Plans carry no spec binding: the implementer cooks from the ingredients list
Operator finding (his analogy: *"like following a recipe with the ingredients list only and no
instructions on how to cook the meal"*). Verified: the dogfood plan **never names
`2026-06-29-model-extraction-evaluation-spec.md` and never instructs reading it** (0 grep hits for
the filename). It cites `spec §5/§7/§9/§10` shorthand in scattered implementation notes — references
a fresh-context worker cannot resolve to a file. The assertions are the ingredients; the spec
(architecture §4, metric definitions §6, decision rule §7…) is the cooking instructions, and no
worker ever reads them. Root cause = the recipe-drop root cause: start step 4 invokes
conductor-unaware `/superpowers:writing-plans` with no required inputs and nothing lints the
artifact. Fix: step 4 passes spec+expectations+assertions paths and requires a `Normative spec:`
header + a per-phase `Spec:` section pointer; `conductor plan-lint` enforces both mechanically;
autodev's phase subagent prompt includes the spec path ("read the phase's spec sections first").

### HIGH — Independent process-compliance checks (operator directive)
*"If conductor is going to truly be robust, there needs to be independent checks of all the little
things that we expect the model to do."* Corroborated cross-model: checkbox decay is universal
("pretty much every model forgets until told explicitly") — so no prompt line is a mechanism. Each
expectation gets a mechanical enforcer:

| Expectation | Enforcer |
|---|---|
| ledger statuses truthful | `reconcile --from-gate` derives `tests_red` from `results.json` via assertion-ID markers in phase bodies |
| plan contains spec binding + recipe + tasks | `conductor plan-lint` at setup + convert time |
| every PR codex-reviewed **≥2×**, final review postdates last commit | merge-gate process legs — the one command a worker can't skip (it blocks the merge it wants) |
| checkboxes ticked, label flipped, lease released, task issues closed | `conductor ledger phase-done` (atomic) |

Operator also corrected the resilience picture: the git layer (per-phase branches, pinned task
commits) IS a restart backstop he'd initially discounted — the fix pass builds enforcement around
that working layer instead of adding prompt steps.

### Fix pass 1 (2026-07-02) — agreed scope
Dogfood paused before Phases 4–6; **they become the validation run for these fixes.**
- **PR-1 ledger core:** tolerant `convert` dialect (`## Phase N — Title` w/ optional `[status]`,
  H2-of-any-kind section bounds), assertion-ID extraction from phase-title parens →
  `<!-- conductor-assertions: … -->` body marker, `reconcile --from-gate`, `ledger phase-done`.
- **PR-2 merge-gate process legs:** `Closes #` required; ≥`CONDUCTOR_MIN_REVIEWS` (default 2)
  "Codex review" marker comments; last review must postdate last commit (review-of-final-state).
- **PR-3 spec-bound plans:** `conductor plan-lint`; start step 4 passes spec paths + plan codex
  review; autodev spec-read subagent prompt + intra-phase resume procedure (plan-vs-git-log diff,
  dirty-tree policy) + phase-done wiring + post-codex-reviews-as-PR-comments; assertions-to-tests
  pinned standalone pytest commands.
- **Live-plan amendment (ai repo, docs):** normative-spec header + per-phase Spec pointers for
  Phases 4–6; tick Phase 1–3 boxes (verifiably done: merged PRs + green gate).
- Ship: merge → bump 0.4.0 → marketplace update → plugin update → restart worker session → resume.
- **Deferred unchanged:** freeze conftest-chain walk (pinned commands close most of it), gate
  runtime, configurable gate location, dispatcher/parallelism.

---

## Fix pass 1 — SHIPPED as 0.4.0 (2026-07-02)

All three PRs merged to main, each after codex ×2-or-3 with the final review postdating the last
commit (the new standard, applied to ourselves):

- **#27 ledger core** (3 rounds): tolerant `convert` dialect; `conductor-assertions` markers;
  `reconcile --from-gate` (derived truth, fail-closed on unresolved/ambiguous tokens);
  `ledger phase-done` (gate-verified atomic bookkeeping, done-label-last, gh-error re-runnable,
  best-effort plan tick). Codex caught 5 real issues across rounds — all fixed.
- **#26 merge-gate process legs** (3 rounds, built by a subagent): `Closes #` required; ≥2
  `CONDUCTOR_MIN_REVIEWS` "Codex review" comments; review-of-final-state via
  `max(committedDate, pushedDate)` from GraphQL `commits(last:1)`; optional
  `CONDUCTOR_REVIEW_AUTHOR` provenance filter; negative/malformed env fails closed. Trust model
  documented: defends against negligent workers, not adversarial ones. Caveat verified live:
  github.com returns null `pushedDate` (deprecated) so the committedDate fallback is operative.
- **#29 spec-bound plans** (2 rounds; supersedes #28, auto-closed by a base-branch deletion):
  `conductor plan-lint` (presence floor; substance = plan codex review); start step 4/4b
  (spec+Expectations+assertions passed into plan-writing; plan lint + codex review before
  issue-sync); autodev spec-first briefs + reconcile-within-phase + dirty-tree policy +
  phase-done wiring; assertions-to-tests pinned standalone commands; reconcile rule 5 requires
  `tests_red` (codex caught that a routine `--from-gate` reconcile would have REOPENED every
  completed phase).

Live-repo work: ai-platform plan carries `Normative spec:`/`Assertion specs:` anchors + Spec
pointers on all 7 phases (worker's own 856ca61 did 4–6 + the 13 ticks; we added 1–3 + 7 with
`gate: none`); issues #127–132 backfilled with assertion markers. plan-lint learned the live
run's organic "Spec intent — REQUIRED READING (…):" dialect instead of forcing a rewrite.

Suite: 101 → 199 passed. **Phases 4–6 of the dogfood are the validation run for all of this.**

Owner decision still open: backfill A1's concrete secret-pattern list (assertions-doc-only)
into spec §12 MN1.
