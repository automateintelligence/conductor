# The dual-host done-gate, measured

**Date:** 2026-08-21
**Branch:** `feature/dual-host-done-gate` (`5271084`)
**Run it:** `CONDUCTOR_GATE_SLUG=2026-08-10-codex-dual-host-conductor-design-5f6520fc ./bin/conductor assert run`

The spec `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md` now has an
executable done-gate. Before this it had seven assertions in prose and nothing that ran them.

    [PASS] a-dh-1-host-vocabulary-confined-to-adapters
    [PASS] a-dh-2-worker-launch-targets-the-recorded-host
    [PASS] a-dh-3-codex-launch-resolves-only-conductor-artifacts
    [FAIL] a-dh-4-host-invocations-are-time-bounded
    SUMMARY: 3/4 green, 1 red -> gate NOT done (exit 1)

A-DH-5, A-DH-6 and A-DH-7 are not implemented. So the spec stands at **3 of 7**.

## Which track each assertion belongs to

Measured, not estimated. Each row's blocker was reproduced.

| Assertion | State | Blocked on | Evidence |
|---|---|---|---|
| A-DH-1 host vocabulary confined to adapters | GREEN | — | 3 code fixes routed through `native_invocation`; 16 prose hits reworded without losing a fact |
| A-DH-2 launch targets the recorded host | GREEN | — | renders and executes the real driver; 18 clauses, 4 mutations kill it |
| A-DH-3 Codex launch uses only Conductor artifacts | GREEN | — | 7 clauses at two package roots; 3 mutations |
| A-DH-4 host invocations time-bounded | **RED** | **Plan 05** | the worker launch is unbounded on both hosts and runs inside `flock -n 9` |
| A-DH-5 relocation refuses with live run artifacts | unbuilt | **Plan 00** | Plan 00 is gated on the owner confirming every run quiesced |
| A-DH-6 unresolvable default branch refuses merges | unbuilt | **Plan 06** | `conductor/branches.py:91` still returns the literal `"main"` on any failure |
| A-DH-7 never completes the final default-branch PR | unbuilt | **Plan 06** | needs a run driven to `awaiting-team-merge` and an introspectable verb list |

**Four of seven depend on Track B.** The spec cannot go green until Plans 00, 05 and 06 land.
Plan 00 additionally cannot start without an owner decision.

## What A-DH-4 found

Not a bookkeeping gap — a live defect.

The worker launch has no ceiling on either host. `"$CLAUDE_BIN" -p …` and `"$CODEX_BIN" exec --cd …`
run inside `flock -n 9`, so a host that never answers **holds `resume.lock` indefinitely** and every
later 20-minute tick fails the lock and exits 0 silently. A permanently blocked run is
indistinguishable from a healthy idle one.

Reproduced: driver fired against a hanging fake, 70s cap, `fire-start posture=supervised` logged,
no `fire-end`, no kill, both hosts. Wrapping the fire in `timeout -k 5 20` turns four red clauses
green, which confirms unboundedness is the cause.

`conductor/hosts/codex.py:328-339` already cites this exact reasoning as why the plugin lookup is
bounded *before* the lock. The hazard is understood one call earlier and unguarded one call later.

**A hard ceiling is the wrong fix.** A legitimate phase outruns the tick, and skipping while one is
genuinely in flight is correct. The missing capability is telling a *long* fire from a *dead* one —
a heartbeat, and a `driver status` that reports a lock held past a threshold with no progress.
That is Plan 05.

## Corroboration for PR #86

A-DH-4's claude driver leg returned `rc=0`, no log, in 0.0s — it never fired. The fixture directory
was named `claude`; the driver's own cmdline is its script path; `pgrep -f 'claude'` filtered by
cwd-under-`$WORKTREE` matched **the driver itself**.

By the same mechanism **this repo's own path, `~/.claude/conductor`, satisfies the match**, so a cron
fire driven from here would self-block. Not verified against a live run. Plan 00's relocation would
*mask* this rather than fix it, which argues for landing #86 on its own merits.

## Dead code removed

`base.probe_version` and `base.assert_minimum_version` had zero callers anywhere. Worse than
unreached: `assert_minimum_version` calls `adapter.version()` / `.minimum_version()` /
`.upgrade_hint()`, none of which either adapter implements, so it raises `AttributeError` against any
real adapter. Both deleted.

Deleting them was only safe once A-DH-4's enumeration reached the driver. Before that,
`probe_version` was the **only Python-level Claude spawner in the repo** — deleting it would have
taken A-DH-4's Claude coverage to zero while looking like cleanup.

## Two runner gaps found along the way

- `conductor assert run` has **no `--run` flag**. `bin/conductor:9` execs `assertions/run.py` with no
  key and `assertions/run.py:52` calls `resolve_gate(PROJECT)` in legacy mode, so run-key mode is
  unreachable from the runner. This gate can only be selected via `CONDUCTOR_GATE_SLUG`.
- `resolve_gate(run_key=…)` refuses without the run's `run.json`, and no run is registered for this
  spec, so the gate directory had to be derived through `runkey.run_key` + `names.derived_names`
  rather than by asking the resolver.

## The open decision

The `/goal` names this spec, whose completion is all seven assertions. The later instruction was to
ship Codex-capable first and defer everything else to improvement work. Those conflict: satisfying
the goal now means building Plans 00, 05 and 06 — the work that was explicitly deferred.

Either re-point the goal at Track A's three assertions, or accept that it stays red until Track B
lands. Not resolvable by writing.
