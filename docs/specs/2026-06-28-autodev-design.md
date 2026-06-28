# Design: `/conductor` + `/autodev` — Autonomous Spec-Completion Loop

**Created**: 2026-06-28
**Status**: Draft v6 (design — incorporates Codex review; experiments in CLI next, then planning)
**Topic**: An orchestration layer that drives a project from a spec to completion
across multiple plans and phases, unattended, resumably, bound to machine-checked
executable assertions as the terminal condition.

> **Review notes (v6):**
> - **Locked:** unit = phase-level (§6.1); repo = new `conductor/`; phase-instruction
>   home = issue body; loop cadence = cron interval.
> - **Incorporated Codex review (REQUEST CHANGES):** Stage-0 build gate (§11, with the
>   correction that experiments gate the *build*, not `/conductor` runtime); assertion
>   runner contract (§5.2); tiered durability goal (§2); merge safety gate (§6.2);
>   ledger state-model + precedence + lease/retry (§7.x); conductor idle-vs-dispatcher
>   fix (§3); phase size/retry bounds (§6.1).
> - **Outstanding:** experiment results (§12); planning/implementation runs in CLI.

> **▶ STAGE 0 COMPLETE (2026-06-28) — READ BEFORE PLANNING.** E0–E5 all passed. Results +
> evidence: [`docs/stage0-results.md`](../stage0-results.md). Design amendments **A–E** to
> fold into the plan: [`docs/stage0-notes.md`](../stage0-notes.md). **`/writing-plans` MUST
> read both** before generating the MVP plan (components 1–7, §11). Headline changes:
> composition = Option 1 in-session `/loop` (primary) + local **Tier-B autostart**; cloud
> `/schedule` is **blocked** on skills-in-cloud (amendment E); `/conductor` is
> reconcile-first/idempotent (B); fresh context via `claude -p`, never `/clear`/`/compact`
> (A); issue-sync via `gh api` (D).

---

## 1. Problem

The goal is **unattended spec-completion**: state what you want, walk away, and come
back to either finished work or a clear, recoverable note about why it stopped — not
to a babysitting session and not to a pile of half-finished worktrees.

Today's agents (Claude Code, Codex) plus existing skills (superpowers, spec-kit) can
plan and execute *a single plan or feature*, but they cannot autonomously:

1. **Loop across the hierarchy** — spec → multiple plans → multiple phases → multiple
   tasks → done. Large scope needs several `plan.md` files; finishing one plan does
   not trigger authoring the next, and finishing a phase does not advance to the next.
2. **Recover cleanly across days** — when a plan glosses over something hard, the
   work tangents into the next day; the thread is lost; worktrees end up partial and
   duplicative.
3. **Survive context limits** — compaction degrades a long run; a model-authored
   handoff into a fresh session is better but manual.
4. **Know when it is actually done** — "done" is decided by the model's judgment
   rather than by an objective, machine-checked condition.

### Prior-art verdict

A web survey (2026-06-28) confirmed: **no off-the-shelf OSS tool does the full
`spec → multi-plan → multi-phase → executable-done`, unattended, resumable loop.**
The pieces exist and we **borrow** them; the missing ~200 lines is the **plan→plan
outer loop + spec-assertion terminal gate**.

| Source | Borrowed pattern | Why not adopt wholesale |
|---|---|---|
| **Ralph Wiggum loop** | Fresh context every iteration; state in files; git tags as checkpoints | DIY pattern; single evolving plan file; no plan→plan progression |
| **Magentic-One** | Per-iteration **progress self-check** ("satisfied? looping? progressing?") | Generalist; not git/spec/code-bound |
| **spec-kit Loop Engineering** (#2977) | Externalized ledger split (`debt`, `verdicts`, `memory`) | Single-feature; **mandates human sign-off** |
| **FramLoop** | "Review the finished PR, not each phase"; carry state across phases | Off-stack; single-plan; won't author plans |
| **OpenHands** | Resume substrate (persist state, replay) | Task-horizon; no plan hierarchy |
| **bmad-dev-auto** | The **worker protocol** (§6) — status state machine, `baseline..final` bracketing, blocked-as-routing | BMAD-specific; would replace the user's recipe |

---

## 2. Goals / Non-goals

### Goals
- Drive `spec → plan(s) → phase(s) → task(s) → done` with no human in the
  phase- or plan-transition loop.
- **A slight extension to planning methodology** — planning stays in superpowers /
  spec-kit, prompted to write plan with (a) specific guidance for constructing plans as a GitHub-issue
  hierarchy and (b) in-loop DevOps hygiene (commit-per-task, PR-per-phase, issue
  linkage), enforced automatically.
- Make the terminal condition **objective**: all of the spec's executable
  assertions pass.
- **Survive long runs by construction, tiered:** **MVP** survives `/clear` +
  compaction (in-session); **Phase 2** adds session-death + multi-day survival
  (cross-session — §4, §11). *(The headline "walk away for days" is the Phase-2
  deliverable; MVP is its in-session foundation.)*
- Produce a clean DevOps record and a recoverable trail of excavated follow-up work.
- Reuse existing skills (superpowers, spec-kit, Codex) — orchestrate, don't reimplement.
- Support **two** spec sources (superpowers, spec-kit).
  (https://github.com/obra/superpowers, https://github.com/github/spec-kit)

### Non-goals
- Not a replacement for `/subagent-driven-development`, `/verification-before-completion`,
  `/test-driven-development`, `/using-git-worktrees`, `/dispatching-parallel-agents`,
  `/codex` — the conductor *conducts* these.
- Not a re-implementation of `check_git_progress()` / tmux orchestration (retired).

### 2.1 Implementation constraints
- **Design for easy installation** as a single plugin, installed **at the user or project level**. No per-project bootstrap.
- *Unlocks (v5):* hooks, small scripts, and standard CLIs (`git`, `gh`) + subagents
  are all permissible **if** they serve easy install or robustness (e.g., a
  `PreCompact`/`Stop` hook as a handoff/commit backstop — §10). Dynamic `.js`
  Workflows remain unnecessary (the scheduler+subagent path covers control flow).

---

## 3. Mental model — a supervisor over three orthogonal roles

Three independent roles that **compose, they do not nest**:

- **`/autodev` = the worker.** One invocation = **one phase** of progress (§6.1):
  claim it, run the phase recipe, leave durable state, exit.
- **`/goal` = the target.** The human-stated objective ("complete the spec"); the
  *machine* proof of it is the executable-assertion gate (§5). `/autodev` re-reads the
  goal every fire.
- **the schedule = the clock.** Re-fires the worker (cron `/loop` in-session, or
  `/schedule` cloud cross-session — §4).

### `/conductor` = the supervisor you invoke once
1. **Validate preconditions** — spec has Expectations + Executable Assertions (§5).
2. **Bootstrap a plan if none exists** — `/writing-plans` (superpowers) or spec-kit
   `/plan`+`/tasks`, with gh-issue-construction guidance.
3. **Implement assertions as runnable tests** (§5.1–5.2) — the done-gate must be machine-runnable.
4. **Run issue-sync** — generate the gh-issue hierarchy (or convert existing) (§7).
5. **Record the `/goal`** and **start the driver** (cron `/loop /autodev`, or
   `/schedule` cloud).
6. **Start the dispatcher** when more than one loop runs (§7).

**After setup:**
- **Single-loop (default):** `/conductor` is idle; the schedule drives `/autodev`.
- **Multi-loop (Phase 2):** dispatch is **not** idle — a persistent **dispatcher loop**
  (a dedicated lightweight scheduled process owned by `/conductor`) is the only entity
  that hands out claims (§7). Its full process is specified in Phase 2.

### Candidate compositions (to be decided by experiment — §12)
- **Option 1 (in-session):** `/conductor` → starts `/loop` → fires `/autodev`,
  done-gate = assertions.
- **Option 2 (watchdog over 1):** cloud `/schedule 6h "ensure /conductor is running
  for <spec>"` *wraps* Option 1 to survive session/container death — and **is the
  loop-crash recovery** (§10).

Hypothesis: these are **complementary, not alternatives** — Option 2 is the
cross-session tier around Option 1. **E5 (§12) decides.**

### Why the loop can't quit early (the anti-laziness guarantee)
1. **The driver is external** — the schedule runs `/autodev` regardless of the agent's
   inclination. The agent cannot end the run.
2. **The goal is re-loaded every fire** — fresh context, so the done-condition is
   re-asserted, never "remembered."
3. **Done is a machine check** — only green executable assertions (exit codes) stop
   the loop. The only other terminal state is an explicit **escalation-halt** (§9).

### Parallelism — designed in from day one
The state/claim model is **parallel-correct from the start** (§7); a single loop is
just the **N=1 degenerate case**, so we never retrofit. Multiple loops run one per
independent plan/worktree, coordinating through the shared ledger. **The dependency
graph lives in the plans/ledger, not the loop structure.** Execution uses
`/superpowers:dispatching-parallel-agents` (fan-out) and `/subagent-driven-development`
(per-unit) — conducted, not reinvented. Default *runtime* = single loop; multi-loop is
opt-in (Phase 2).

---

## 4. Durability & compaction

### Durability tiers
| Tier | Mechanism | Survives | Tier |
|---|---|---|---|
| In-session | cron `/loop /autodev` | `/clear`, compaction | **MVP** |
| Cross-session | **`/schedule` (cloud)** | session death, container reclaim | **Phase 2** |

Verified (2026-06-27): cron `/loop` **survives `/clear`**. It does **not** survive
session/container death — use cloud `/schedule` for that. (`/ralph-loop` loops via a
Stop hook inside the session → survives neither.)

**Non-negotiable for cross-session durability:** every iteration **commits + pushes
git** and **writes GitHub issues**, so a fresh container resumes from server-side
state with zero local context.

### Loop prompt pattern
Cron checking in does no harm — if a unit is progressing it observes and re-fires:
> *"Ensure the `/autodev` task `<name>` is progressing. Stop the loop when it is fully
> completed."*

### Resuming after usage/rate limits
Codex and Claude Code can hit **5-hour token-usage windows** (info in the error or
`/status`). The loop must detect this and **auto-schedule a resume** when the window
resets — a first-class requirement, not an edge case. (Phase 2.)

### Thin-session architecture
> Keep the main session **thin**; do heavy work in a **fresh subagent** each iteration:
> read ledger → claim unit → dispatch subagent (fresh context) → distill result to
> ledger + git → write handoff → push → exit.

### Handoff (every iteration)
A **model-authored handoff is written and pushed every iteration**. Its prompt is
Jeff's base line **plus** the framework-required payload:

> *Base:* "write a handoff for a new session in a different terminal, so include all
> necessary context and reference docs."
>
> *Required payload:* goal/done-condition; paths to spec + expectations + assertions +
> plan-index + ADR dir; active plan/milestone; current phase issue # and its status
> label; `baseline..final` SHAs of the last unit + what it did; the next unit; open
> `debt`/`feature`/`blocked` issues; branch/worktree; the **exact resume command**.

A `PreCompact`/`Stop` **hook backstop** (now permissible, §2.1) may guarantee the
handoff + commit exist even on an abrupt stop — decide via E1/E9 (§12).

---

## 5. Spec sources & preconditions

`/conductor` consumes whichever format is present; it imposes none.
- **superpowers**: `/brainstorming` → spec → `/writing-plans`. Phases + tasks in `plan.md`.
- **spec-kit**: `CONSTITUTION` + `spec.md` → `plan.md` → `tasks.md` (phases/tasks in plan.md and tasks.md).

### Precondition: executable assertions (the done-condition)
- **Safe default** — if the spec lacks Expectations / Executable Assertions,
  `/conductor` **stops and points the user** at `/expectations` and
  `/executable-assertions`.
- **Opt-in autonomy** — `--auto-assert` lets `/conductor` launch those skills itself.
  (Off by default; available for testing.)

### 5.1 Assertion specs must be made runnable (the crux)
`/executable-assertions` deliberately outputs **specs** (claim / setup / observation /
level), **not test code**. The terminal gate needs **runnable** checks (exit 0/1).
Therefore the first build work the loop does is **implement the assertion specs as
executable tests via `/test-driven-development`**: the assertions become the
acceptance tests (red), and the spec is "done" exactly when they go green.
**executable-assertions → TDD → done-gate.** Without this, "machine-checked done"
does not exist.

### 5.2 Assertion runner contract (the done-gate interface)
The gate must be a single, deterministic, machine-readable interface:
- **Location / manifest** — assertions live at a known path with a manifest mapping
  each assertion-spec **id** → its test (file/command). Suggested: `assertions/`
  (tests) + `assertions/manifest.yaml` (id → command, setup, timeout, level).
- **Invocation** — one command runs the whole gate (e.g. `conductor assert run`,
  internally the project's test runner filtered to the assertion suite) and emits an
  aggregate result + **per-assertion** pass/fail keyed by id.
- **Exit semantics** — `0` = all assertions green; non-zero = ≥1 red. Per-assertion
  detail captured to a results artifact for the ledger/handoff.
- **Setup/teardown** — each assertion declares its setup/fixtures and teardown (from
  the spec's "setup"); the runner guarantees isolation between assertions.
- **Timeout** — per-assertion and overall wall-clock limits; a timeout = **fail**.
- **Mapping** — exactly one runnable test per encoded assertion spec, traceable by id
  (so a red result names the violated claim).
- **Fail-closed (critical)** — if the runner cannot execute (missing dep, crash,
  timeout, no manifest), the gate is treated as **NOT done**. The loop **never** stops
  on an unrunnable or indeterminate gate.

### Precondition: gh-issue hierarchy (automated, not prompted)
Because deepen-in-place (§9) cannot stop to ask, issue construction is **fully
automated** via issue-sync (§7).

---

## 6. The worker contract

`/autodev` **runs the user's recipe** but borrows **bmad-dev-auto's protocol** — its
*way of reporting state*, not its engine. This is **not** us running BMAD; it solves
one problem: *how does a fresh-context iteration know what the previous one did,
without trusting a chat message?*

- **Two distinct axes on every phase issue — do not conflate them:**
  - **Status label = lifecycle / entry-point** (where the unit is). NOT a lock.
  - **Assignee (+ lease) = ownership / claim** (who is working it now). The lock (§7).
- **Status label → action `/autodev` takes:**

  | Label (state) | → Action |
  |---|---|
  | `status:draft` | plan it |
  | `status:ready` | implement |
  | `status:in-progress` | resume implementing **(by the owner only)** |
  | `status:in-review` | review |
  | `status:done` / closed | re-review / skip |
  | `status:blocked` | halt → route (§9) |

- **Monitor the artifact, not the chat** — read status label + assignee + git commits
  + PR state, never "✅ done!". *(Anti-laziness.)*
- **`baseline_revision..final_revision`** — git HEAD before/after the unit; the
  commits between are exactly what it produced (equal = it did nothing).
- **`blocked` is a routing signal, not a failure** — route to deepen-in-place or the
  user (§9).
- **One coherent intent (one phase) per iteration**; subagent-based execution.

### 6.1 Unit-of-work granularity — phase-level (LOCKED), with bounds
One fire = **one whole phase** (the recipe below, dispatching subagents for the
phase's tasks). Rationale: in practice this holds without compaction the large
majority of the time, and keeps control flow simple (one PR per fire). **Bounds
(required so a phase can't run away):**
- **Pre-execution split check** — before running, estimate phase size (task count /
  expected diff / token budget). If it exceeds a configured threshold, **split it via
  deepen-in-place (§9) before executing** rather than starting an oversized fire.
- **Per-phase retry cap** `R` — after `R` failed attempts on the same phase, stop
  retrying and route to escalation (§9) / recovery (§10).
- **Per-fire runtime/token budget** — if exceeded mid-phase, **checkpoint (commit) +
  handoff** and let the next fire resume the owned unit (don't blow context).
E5/E6 (§12) stress-test the phase-level choice and these bounds.

### The user's recipe (one phase per fire)
Stored **in `plan.md` / the phase issue**, semi-hardcoded, so plans can carry their
own rules if needed:

1. `/subagent-driven-development` to implement the phase's tasks
2. Use `/code-review` for self-review of each task completion
3. **commit after every task** (`<files-changed> — <description>`)
4. **one PR per phase**
5. **Codex review of the PR** —
   `/codex $superpowers:requesting-code-review Provide read-only, pre-merge review of PR#<pr-num>`
6. Process it in Claude Code with —
   `/receiving-code-review Consider the Codex review, apply fixes, commit and leave comment in PR when completed.`
7. **merge the PR with `--merge`** (no squash) — only through the safety gate (§6.2)
8. Update documentation with `/document-release`

### 6.2 Merge safety gate (autonomous merge preconditions)
An autonomous `--merge` proceeds **only if all** hold; otherwise block → resolve or
escalate (§9/§10), never force-merge:
- **CI / required checks green** on the PR head.
- **Branch current** with base (else update/rebase; re-run checks).
- **No unresolved "changes requested"** and no open required-review threads.
- **Local verification re-run green** on the merge ref (tests + lint), not trusting CI alone.
- **No merge conflicts** (else auto-resolve if trivial, else escalate).
- **Branch-protection / merge-queue rules satisfied** (use the queue if configured).

---

## 7. The ledger — GitHub issues canonical, with a git seam

Work state lives in **GitHub issues** (server-side, conflict-free across worktrees,
provenance-native). The **contract + ground truth** stay in **git**.

| Layer | Home | Rationale |
|---|---|---|
| Spec, Expectations, **Executable-Assertions** + tests + manifest (§5.2), CONSTITUTION | **git** | The contract; the gate runs these. |
| Code, commits, tags | **git** | Ground truth (`baseline..final`). |
| Plan/Phase **instructions + status(label) + claim(assignee) + progress** | **GitHub issues** | Atomic units; server state; provenance. |
| Excavated follow-up (`debt` / `feature`) | **GitHub issues** (labeled) | Native triage. |

### Hierarchy → native primitives
- **Plan → Milestone**; **Phase → Issue**; **Tasks → sub-issues / checklist**
- **PR per phase →** `Closes #<phase-issue>`
- **Spec → Project board** (or top tracking issue); **`plan.md` → thin index** (LOCKED)

### Issue-sync (automated)
Used by `/conductor` and `/autodev` (next-plan + deepen-in-place). Never prompts.
- **Generate** — plan → milestone/issues/sub-issues/labels/links.
- **Convert (backward compat)** — existing `plan.md`/`tasks.md` → same hierarchy.
- **Reconcile** — each iteration (see state model below).

### Concurrency control (parallel-correct from day one)
The **claim is the assignee, not the status label** (§6). Designed for N loops; N=1 is
the degenerate case.
- **Eligibility to pick a unit** = **unassigned** AND not `blocked`/`done`/dependency-blocked.
- **Dispatcher (default when >1 loop):** the persistent dispatcher loop (§3) is the
  only entity that assigns units → no race possible.
- **Assignee-claim (single pool fallback):** a worker self-assigns, re-reads to
  confirm sole assignee, backs off and re-picks if it lost. Small race window.
- **Lease + stale-claim recovery (required before multi-loop):** a claim is a *lease*
  (heartbeat timestamp on the issue, TTL `L`). A worker that dies leaves `in-progress`
  + assigned; if the lease goes stale past `L`, the dispatcher **reclaims**.
- **Single-loop runtime needs no claiming** — but the model is built in so enabling
  multi-loop is a config flip, not a rewrite.

### State model & reconciliation
Each phase issue has axes: **status-label** ∈ {draft, ready, in-progress, in-review,
done, blocked}; **assignee** ∈ {none, worker-id}; **lease** ∈ {fresh, stale, none};
corroborated by **git** (commits since baseline), **PR** (none/open/checks/merged),
**tests** (red/green).

- **Ground-truth precedence (conflict resolution):**
  **git commits + assertion/test results > PR state > issue status-label.**
  On conflict, the higher source wins and the lower is repaired.
- **Forward transitions** follow the §6 table. **Backward transitions** are legal on
  evidence: `in-review → in-progress` (changes requested); `done/closed → in-progress`
  (assertions regressed); `in-progress → ready` (unassigned/abandoned).
- **Invalid combinations → repair:**
  - `status:done`/closed **but** tests red → reopen → `in-progress`.
  - `in-progress` **but** no assignee → reset → `ready`.
  - `in-progress` **but** stale lease → dispatcher reclaims.
  - closed issue **but** PR not merged → reopen.
- **Retry caps:** per-unit retry limit `R` (§6.1); exceeded → `blocked` + escalate.
- **Lease rules:** renew on each fire that makes progress; stale beyond `L` → reclaim.

> The *exhaustive* reconciliation matrix (full cross-product of the axes) is produced
> during `/writing-plans` and **validated by E3/E8** (§12); this section fixes the
> precedence, transition, lease, and retry rules it must satisfy.

### Invariant: **issues track, assertions decide**
Done = "all (spec-level) executable assertions pass," run from git — not "all issues
closed." Closing the issue hierarchy is a **consequence** of green assertions.
*(Spec-level assertions are expected RED mid-run — a phase is incomplete — so they
gate the WHOLE spec, not a single fire. Per-fire "unit done" uses local
verification, §8.)*

---

## 8. Algorithms

**Iteration contract (every fire):** *reconcile-first, idempotent, bounded-retry.* A
fire reads durable ground truth before acting, can be safely re-run, and retries a
failing unit only `R` times before routing it (§9) or surfacing it (§10).
**Note:** the runtime algorithm has **no experiment step** — experiments gate the
*build* of conductor, not its runtime (§11 Stage 0, §12).

### `/conductor` (once, at launch)
```
1. Detect spec source; load spec + Expectations + Executable Assertions.
2. PRECONDITION: assertions present?  no → stop & point user (or --auto-assert).
3. Implement assertion specs as runnable tests (TDD) + manifest → the done-gate (§5.1–5.2).
4. Plan exists?  no → /writing-plans (or spec-kit) to author plan 1.
5. issue-sync: generate (or convert existing).
6. Record /goal; start driver (cron /loop /autodev, or /schedule cloud).
7. If multi-loop: start the dispatcher loop (§3, §7).
```

### `/autodev` (one phase per fire, then exit)
```
1. RE-LOAD GOAL (fresh context): "not done until all executable-assertions pass".
2. RECONCILE (precedence: git/tests > PR > label): issues/labels/assignees/leases, git, PR, tests.
   PROGRESS SELF-CHECK: did the last unit advance the spec? looping? ballooning past plan?
3. SPEC-DONE GATE: run the assertion runner (§5.2). Fail-closed if unrunnable.
   all green AND no plans left → mark spec done, STOP the loop.
4. PICK next eligible phase (unassigned & not blocked/done; climb the ladder):
   phase available?                   → SPLIT-CHECK (§6.1) → run the phase recipe (§6)
   plan done (phases all green)?      → /writing-plans next plan → issue-sync
   no plans left but assertions red?  → /writing-plans to close the gap → issue-sync
5. CLAIM (assign self + lease; label status:in-progress). [dispatcher assigns if multi-loop]
6. EXECUTE the phase in a FRESH SUBAGENT via the recipe (§6); merge via gate (§6.2);
   local verification = unit-done. Respect per-fire budget (§6.1).
7. On design friction → ESCALATION GATE (§9).  On process failure → §10.
8. RECORD: update label/progress, commit, update plan.md index, renew/release lease.
9. WRITE HANDOFF (§4) + push.  EXIT.
```

---

## 9. The escalation gate — the agent decides at the moment of *design* friction

When a unit reveals complexity beyond the plan, `/autodev` does **not** improvise
deeper silently. It answers one question:

> **"Is this something that can sensibly be returned to and patched later, or is it a
> structural issue that must be addressed and built now to make forward progress?"**

- **Patch-later** → IF the work can still be completed as designed but there's a
  potential impact or a better way, file a `debt` or `feature` GitHub issue capturing
  the excavation/innovation (what's hard, what was tried, links to branch/commits);
  add a comment on the in-progress issue linking the new one; **continue** the current unit.
- **Build-now** → IF the work cannot continue without a design change, use one
  **bounded deepen-in-place**: re-invoke `/writing-plans` scoped to just that
  Phase/Task → issue-sync the sub-plan; **enqueue it into the ledger and mark the
  original unit `blocked-on-subplan`**; the **same loop** works the sub-plan's units;
  the original unblocks when the sub-plan goes green. (Same loop + a dependency edge —
  not a nested loop.) Upon **COMPLETION** of a deepen-in-place plan, generate a
  `docs/ADR/*.md` — only upon completion, so the ADR reflects what was *ultimately
  implemented*, not what was planned.
- **Build-now AND needs human judgment** (ambiguous spec, architectural decision not
  covered, secrets/infra/external config) → **halt** with a handoff + issue. **The
  only branch that pages the user.**

This is the fix for the "tangent into the next day → lost thread → duplicative
worktrees" failure: bounded self-resolution, otherwise parked with context, otherwise paged.

---

## 10. Failure & recovery (process/infra failures)

§9 handles *design* friction; this section handles *process* failure (crashes,
outages, limits, death). **The recovery mechanism is the scheduler re-firing into a
reconcile-first iteration; durable state (pushed git + issues + handoff) survives,
session-local state is disposable.**

| Failure | Detection | Recovery |
|---|---|---|
| Subagent dies mid-phase | `baseline..final` shows no/partial commits; label still `in-progress`; self-check flags no advance | Next fire resumes the owned unit; commit-if-coherent or reset-to-last-commit; **bounded retries** → then `blocked`/escalate |
| Recipe step errors (build/test/lint) | non-zero exit | Normal feedback → `/code-review` + `/receiving-code-review` + TDD converge; non-convergent after `R` → escalation gate (§9) |
| Done-gate stays red after a plan | gate red, no plans left | Author next plan to close the gap (§8); repeatedly red w/ no progress → escalate (spec may be wrong) |
| GitHub/network outage; push fails | API error / push retry exhausted | **Commit locally always**; retry push w/ backoff; if issues unwritable, *pause* (avoid ledger drift); re-fire reconciles when GitHub returns (git = ground truth) |
| 5-hr usage / rate limit | error or `/status` | Schedule **resume after window reset** (§4); in-flight unit left `in-progress`, resumed next window |
| Session/container death or compaction | n/a | Cloud `/schedule` re-spawns → fresh container reads pushed git+issues+handoff, reconciles, resumes |
| Loop/conductor itself dies | watchdog sees it not running | **Option 2 cloud `/schedule` watchdog restarts it** (§3) |
| Ledger inconsistency / double-work | reconcile mismatch | git + tests win (precedence, §7); repair labels; double-work prevented by assignee + lease; stale-claim recovery |
| Deepen-in-place sub-plan won't converge | sub-plan attempts exhausted | escalate the **parent** unit → page user |

**Throughline:** the only failures *not* auto-recovered are the ones that reach the
escalation gate (§9) — and those page you with a handoff + issue, by design.
Everything else is "re-fire and reconcile." Validated by **E9** (§12).

---

## 11. Components & build order

> **Stage 0 — Framework validation gate (one-time, build-time):** run **E0–E5**
> (§12) and **record results** before any `/writing-plans` or MVP build. This gates
> *building conductor itself*; it is **not** a step `/conductor` runs per user-spec
> (the runtime algorithm in §8 has no experiment step). E5's result **locks the
> composition (Option 1 vs Option-2-watchdog)** before MVP is planned.
>
> **STATUS: Stage 0 COMPLETE (2026-06-28) — E0–E5 all green.** Results + evidence:
> [`docs/stage0-results.md`](../stage0-results.md); design amendments A–E:
> [`docs/stage0-notes.md`](../stage0-notes.md). Composition locked to **Option 1
> (in-session `/loop`) + local Tier-B autostart**; cloud `/schedule` (Tier A) is
> feasibility-gated on skills-in-cloud (amendment E).

| # | Component | Tier | Notes |
|---|---|---|---|
| 0 | **E0–E5 experiments** (§12) | **Stage 0 gate** | must pass + be recorded first |
| 1 | `/expectations` (skill) | MVP | promote existing prompt |
| 2 | `/executable-assertions` (skill) | MVP | promote existing prompt; outputs specs |
| 3 | **assertion runner + tests via `/test-driven-development`** (§5.1–5.2) | MVP | specs → runnable gate; fail-closed |
| 4 | `issue-sync` (skill) | MVP | generate / convert / reconcile (§7 state model) |
| 5 | **ledger + claim model** (assignee + lease + labels, §7) | MVP-design | parallel-correct; single-loop runtime first |
| 6 | `/autodev` (skill) | MVP | §6 contract + §6.2 merge gate + §8 algo + §9 escalation + §10 recovery; single-loop, in-session |
| 7 | `/conductor` (skill) | MVP | setup + start single loop |
| 8 | cloud `/schedule` + watchdog + 5-hr-resume | Phase 2 | §4 / §10 cross-session + loop-crash recovery |
| 9 | multi-loop + dispatcher loop | Phase 2 | §3 / §7 (model already built in MVP) |
| 10 | optional `PreCompact`/`Stop` hook backstop | Phase 2 | only if E1/E9 show state can be dropped |

**Default runtime = single sequential loop, in-session.** Packaged as **one plugin**,
easy install at user/project level (§2.1). Lives in the new **`conductor/` repo**.

**Sequencing:** Stage 0 → assertion skills + runner (load-bearing wall) → issue-sync +
claim model → `/autodev` → `/conductor`.

---

## 12. Validation experiments — STAGE 0 GATE (run in CLI)

These validate the **framework architecture once, at build time** — they are **not**
steps `/conductor` runs per user-spec. They gate *building/planning conductor itself*.
Run as **tiny stubs** in the **CLI** (where the real skills live). Each: question →
minimal setup → pass criteria.

- **E0 — Loop fires a skill repeatedly & self-stops on a machine gate.** Stub
  `/autodev`: increment a counter; run a one-line "assertion" (grep `DONE`); when
  green, delete its own cron. `/loop 1m /autodev-stub`; write `DONE` after 3 fires.
  *Pass:* fires on interval, stops itself when green, never asks.
- **E1 — Survives `/clear`.** `/clear` after fire 2. *Pass:* fires 3+ continue; state
  from disk; counter correct.
- **E2 — Looped skill dispatches a subagent; `baseline..final` brackets it; session
  stays thin.** *Pass:* commit captured; main context small.
- **E3 — gh ledger ops + state model.** issue-sync stub: from a 1-plan/2-phase/3-task
  mini `plan.md`, create milestone+issues+sub-issues, set status labels, assign one,
  flip to `status:done`+close on a faux PR merge, then inject an invalid combo
  (`done` + red test) and verify reconciliation repairs it per precedence (§7).
  *Pass:* hierarchy + label/assignee transitions correct; reconcile repairs the invalid combo.
- **E4 — Assertion spec → runnable test + runner contract (the crux).** One tiny spec
  ("`answer()`→42") via `/test-driven-development`; wire it into the manifest/runner
  (§5.2); confirm fail-closed when a dep is removed. *Pass:* red→green; runner exit
  codes correct; **unrunnable = NOT done** (never green-by-default).
- **E5 — End-to-end micro-spec, unattended, BOTH orderings.** Trivial spec; run Option 1
  (`/conductor`→`/loop /autodev`) and Option 2 (`/schedule` watchdog). *Pass:* ≥1 reaches
  green with zero intervention → **lock the winning composition.**
- **E6 — Deepen-in-place enqueue converges.** 2-phase plan, phase 2 under-specified.
  *Pass:* loop files the sub-plan, blocks phase 2, works it, writes an ADR on completion,
  resumes phase 2 — no human.
- **E7 — Cross-session durability.** `/schedule` a resume; end session. *Pass:* fresh
  session resumes from pushed git + issues.
- **E8 — Parallel claim + dead-worker lease recovery.** Two loops on one pool; kill one
  mid-unit. *Pass:* no double-work; stale claim reclaimed; unit completes.
- **E9 — Failure injection / recovery (§10).** Kill a subagent mid-phase; simulate a
  push failure / GitHub outage; fake a rate-limit; remove the merge-gate preconditions.
  *Pass:* each re-fire reconciles and resumes with no lost/duplicated work; no unsafe merge.

**Stage-0 gate before planning: E0–E5.** E6 validates the trickiest control flow;
E7–E9 before relying on multi-day / parallel runs.

---

## 13. Open questions / risks

- **Composition order (§3)** — Option 1 vs Option-2-as-watchdog; **E5 decides.**
- **Hook backstop (§4/§10)** — needed, or does skill-only never drop state? **E1/E9 decide.**
- **Repo scope** — monorepo only for now.
- **Cost controls** — token-budget aware; per-phase retry cap `R`; lease TTL `L`;
  5-hr limit auto-resume (§4). Concrete values set during planning.
- **Where planning/implementation runs** — CLI (see chat); all-CLI from experiments onward.
- *(LOCKED)* unit = phase-level (§6.1); phase-instruction home = issue body; `plan.md`
  = index; loop cadence = cron interval; repo = new `conductor/`.

---

## 14. References

- Prior-art survey: Ralph Wiggum loop (ghuntley.com/ralph), Magentic-One (MS AutoGen),
  spec-kit Loop Engineering (#2977), FramLoop, OpenHands, SWE-agent, BMAD
  `bmad-dev-auto` (docs.bmad-method.org/reference/dev-auto).
- This repo's retired suite: `orchestration-protocol.md` (Sections 5, 6, 12).
- Existing skills conducted: superpowers `/brainstorming`, `/writing-plans`,
  `/subagent-driven-development`, `/verification-before-completion`,
  `/test-driven-development`, `/using-git-worktrees`, `/dispatching-parallel-agents`,
  `/code-review`, `/receiving-code-review`, `/document-release`;
  `/codex` + `requesting-code-review`; GitHub spec-kit.
- **Stage 0 (this repo):** `docs/stage0-results.md` — E0–E5 validation results + verdict;
  `docs/stage0-notes.md` — design amendments A–E from Stage 0, to fold into `/writing-plans`.
