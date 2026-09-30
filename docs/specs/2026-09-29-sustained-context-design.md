# Sustained context for Conductor runs — design

**Date:** 2026-09-29
**Status:** Design agreed in conversation 2026-09-29; written spec awaiting owner review.
**Track:** B (after the 0.10.0 dual-host release). Relates to Plans 05 and 07 in
`docs/plans/2026-08-10-codex-dual-host-ROADMAP.md`.
**Amends:** the "Explicit non-goal: Codex session continuation" decision in
`docs/plans/2026-08-10-plan-04-host-adapters.md:58-68` (see §7).

## 1. Goal

Cut the tokens Conductor spends re-building context on both hosts, without weakening the
properties the run model depends on: durable state is authoritative, a crash costs one fire, any
machine can resume a run, and no session is ever compacted.

Two mechanisms, both agreed:

1. **A per-phase reviewer session.** The opposite-host reviewer keeps one resumable session for
   all review rounds of one phase, so round 2 onwards sends only what changed.
2. **A curated run digest.** A compact, durable summary of decisions, open findings, and the
   reviewer's standing concerns, which each fresh fire and each fresh reviewer session loads
   instead of re-reading everything.

Orchestrator fires stay stateless: cron, a fresh process, reconcile from durable state.

## 2. Why this shape (and not a long-lived session)

- **A resumed session is not free.** Every turn re-reads the whole history as input. It is
  cheaper than a cold start only when the prompt cache is warm (cached input is billed at about a
  tenth, with a cache life of minutes to an hour), or when the cold alternative is an expensive
  re-hydration. Review rounds within one phase happen minutes apart, which is the warm case.
  Fires 20 minutes apart are mostly not.
- **Reviewer independence has value.** A reviewer that has watched every phase absorbs the
  author's framing. Rotating the reviewer session at each phase boundary keeps a fresh second
  opinion per phase and bounds the history each session carries.
- **A long-lived orchestrator session was rejected.** It gives up crash resilience, the
  no-compaction rule, and handoff across machines. Those are the reasons fires are stateless.

## 3. Measure first

Nothing in §4–§5 is built until there is a baseline. This design's first deliverable is token
accounting:

- Every host dispatch (worker fire, reviewer call) records its usage in `run.json` `dispatches`:
  host, role (`worker` | `reviewer`), phase id, PR head sha, input tokens, cached input tokens,
  output tokens, wall time.
- Sources: `claude -p --output-format json` (usage block) and `codex exec --json` (usage events).
  If a host does not report a field, record it as `null`, never `0`.
- `conductor status --run <run-key>` (the existing read-only status verb) prints per-phase
  totals split by role; `--json` includes them.

**Go / no-go.** Build §4 only if measured reviewer input tokens for round 2+ of a phase are a
material share of review spend on at least three real phases, and §5 only if fresh-fire
re-hydration is. "Material" is decided by the owner from the numbers; the spec does not
pre-commit a threshold.

## 4. Per-phase reviewer session

### Launch path

Today's reviews go through gstack's wrapper skills (`codex` / `claude`), which start a fresh
`codex exec` / `claude -p` process on every call and return no session id. Resuming requires
Conductor to launch the reviewer itself. This design implements the already-declared adapter
member `reviewer_argv` (`conductor/hosts/base.py:187`) for both hosts, plus a resume variant:

- **Claude:** `claude -p --output-format json …` on the first round, returning a `session_id`;
  `claude -p --resume <session_id> …` on later rounds.
- **Codex:** `codex exec --json …` on the first round, returning a session id;
  `codex exec resume <id> …` on later rounds.

Exact flags are verified against the pinned host versions in the plan's first task (ground-truth
pass), not assumed here. Every reviewer invocation stays time-bounded, the same as every other
host call (A-DH-4).

### State

A new `run.json` field, `reviewer_session`, written under `state.lock` with the current revision
like every other `run.json` write:

| Key | Meaning |
| --- | --- |
| `host` | the reviewer host (`claude` / `codex`) |
| `session_id` | the host's id for the session |
| `phase_id` | the phase this session belongs to |
| `workstation_id` | the machine the session lives on |
| `rounds` | review rounds served so far |
| `started_at`, `last_used_at` | timestamps |

### Rules

1. **Scope is one phase.** The first review round of a phase always starts a new session, seeded
   with the run digest (§5), the phase's spec sections and ADRs, and the PR. Later rounds of the
   same phase resume it and send the fix delta plus the new head sha.
2. **Every round reviews the current PR state.** The resumed prompt asks for a review of the PR
   at the new head, not only of the delta; the session's history just saves re-reading unchanged
   material. This keeps the merge gate's "review of final state" meaning intact.
3. **Any failure falls back to a cold start.** If the id is missing, belongs to another
   workstation or phase, the host reports the session gone, or the resume call errors or times
   out, Conductor starts a fresh session exactly as in rule 1. It records the reason in the
   dispatch record. A failed resume is never a failed review.
4. **Rotation.** A new phase, a change of reviewer host, or a same-host fallback (usage limit)
   retires the session. The old id is dropped; nothing is carried across.

### Durable state stays authoritative (answers Plan 04's objection)

Plan 04 rejected session continuation because a session that remembers something the ledger
does not becomes a second source of truth, which the system cannot detect. This design keeps the
session out of every decision:

- The review verdict is still posted as the marker-prefixed PR comment, and the merge gate still
  reads only that (`CONDUCTOR_REVIEW_MARKER`, `CONDUCTOR_MIN_REVIEWS`, review-postdates-head).
- No Conductor code path reads anything back from a session except the text of that round's
  review.
- Losing the session loses only tokens: rule 3 recreates it from durable state.
- The **worker** (the author) never resumes a session. It stays a cold start per fire.

## 5. Run digest

A compact, structured summary that each fire updates and each fresh fire and each fresh
reviewer session loads first.

### Contents (bounded)

- **Decisions:** one line each, with the phase and the evidence it rests on (commit, PR, ADR).
- **Open findings:** unresolved review findings by severity, with PR and file:line; a finding
  leaves the list when a later review or commit closes it.
- **Reviewer's standing concerns:** at most five, carried between phases so a fresh reviewer
  session starts with them.
- **Current position:** phase, PR, head sha, and what the next fire should do.

It has a hard size cap (default 4,000 tokens, configurable), and it is overwritten, never
appended. When over the cap, the oldest decisions go first; open findings and standing concerns are never dropped.

### Where it lives

The handoff file `.conductor/handoff.md` is gitignored and local to one machine, so it cannot be
the digest. The ledger has no run-level issue today (issue-sync creates a milestone, phase issues
and task sub-issues), and GitHub milestones cannot hold comments.

**Recommendation:** a dedicated **run digest issue**, created by `/conductor:start` alongside the
ledger, attached to the run's milestone and labelled `conductor:digest`. Its body is the digest,
edited in place each fire, with a local cache. An issue body holds up to 65,536 characters,
comfortably above the cap. The ledger is already GitHub, so the digest travels with it to any
machine and adds nothing to the project's git history. The run record stores the issue number
(`digest_issue`), and the final owner PR links it.

The alternative is a committed file on the run branch. It is simpler to read, but it adds a
commit per fire and would land in the final owner PR.

### Authority

The digest is a derived summary, not a source of truth. Reconciliation precedence is unchanged:
git heads, GitHub state, `results.json`, `run.json`, then handoff/digest. If the digest
contradicts a higher source, the higher source wins, and the fire rewrites the digest.

## 6. Errors and limits

| Situation | Behaviour |
| --- | --- |
| Host does not report usage | record `null`; totals mark the phase incomplete |
| Resume fails for any reason | cold start (§4 rule 3); reason recorded |
| Session on another machine | cold start; never attempt a cross-machine resume |
| Digest issue missing or unreadable | rebuild from durable state (recreating the issue if needed); never block a fire |
| Digest over the cap | drop the oldest decisions; never drop open findings or standing concerns |
| Reviewer hits a usage limit | existing same-host fallback; session retired (§4 rule 4) |

## 7. Amendment to Plan 04's non-goal

Plan 04 (`2026-08-10-plan-04-host-adapters.md:58-68`) records: no adapter method takes or
returns a session id, and revisiting that "requires re-deciding the reconciliation model, not
just adding a flag". This design re-decides it for the **reviewer role only**:

- The reconciliation model is unchanged. Durable state stays authoritative, and sessions are
  advisory context that is safe to lose (§4).
- The worker stays cold-start.
- The adapter gains session-aware reviewer members; the worker members do not.

When this spec is approved, Plan 04's section gets a dated note pointing here. Its text is not
rewritten.

## 8. Testing

- **Unit:** the reviewer-session record (create, resume, rotate on new phase / host change /
  fallback), each rule-3 fallback trigger, the digest size cap and eviction order, and `null`
  usage handling. All host calls are stubbed; no test runs a real `claude` or `codex` (the repo's
  existing guard).
- **Contract:** a test that the merge gate's inputs are unchanged, i.e. no gate path reads
  `reviewer_session` or the digest.
- **Opt-in live:** behind the existing live-host env flags, one resumed review round on each host
  to confirm the session id round-trips and usage is reported.
- **Measurement check:** the §3 numbers from three real phases, before and after, recorded in
  `docs/reviews/`.

## 9. Out of scope

- Worker (author) session continuation.
- Reviewer routing and structured verdicts beyond what §4 needs (Plan 07).
- Heartbeat, checkpoint, and compaction policy (Plan 05), except that nothing here compacts a
  session.
- Cross-machine session transfer.
