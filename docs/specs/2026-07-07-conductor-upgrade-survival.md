# Conductor Upgrade Survival

A conductor version upgrade must never strand an already-running or paused run. Today it does:
upgrading the plugin under a run whose gate was frozen by an older version can hard-block the next
`/conductor:start` at the gate step, forcing the owner to edit and re-freeze the frozen done-gate
just to resume — the exact surgery the frozen-gate invariant forbids workers from doing mid-run.

This spec makes a plugin upgrade safe for in-flight runs, and closes the smaller authority-flow and
driver-observability gaps the same live run exposed.

**Origin:** surfaced by the securitysight `2026-06-09-historical-image-search-spec` run on
2026-07-07 — a paused run (Phase 0 merged, Phase 1 code-complete, gate frozen and `gate verify`
clean) that could not be resumed after a routine 0.5.x → 0.7.0 upgrade without editing three frozen
tests (A2/A5/A7), symlinking the assertions source, and re-freezing.

**Tracking issues:** #73 (P1, this spec's core), #74, #75 (P2 — authority), #76, #77 (P3 —
lifecycle). Related prior debt: #68, #69.

## Problem

Three coupled 0.7.0 behaviors turn a routine upgrade into mandatory frozen-gate surgery, plus two
smaller gaps in the authority and driver-lifecycle flows.

### P1 — a stricter gate check retroactively hard-blocks already-frozen runs (#73)

1. **`start` re-lints unconditionally.** `skills/start/SKILL.md` step 3 runs `conductor gate lint`
   on every reconcile and treats *any* finding as a hard run-block. The `SKIP if .frozen exists and
   gate verify is clean` clause guards only the *freeze* substep, not the *lint*. So any upgrade
   that adds or tightens a lint rule fails every previously-frozen run at resume — even one whose
   baseline is intact and verifies clean.

2. **The lint's negative-clause heuristic is too narrow.** `conductor/gate_lint.py::_is_negative_assert`
   recognizes only `NotIn` / `NotEq` / `IsNot` / `UnaryOp(Not)` / `assertNot*`. It does not
   recognize semantically-negative forms — `== 0`, `len(set(x)) == len(x)`, comparison against a
   constant — so it false-flags legitimate must-not assertions as "no negative clause." (Same class
   as #69, conductor's own manifest failing its own lint.)

3. **`gate freeze` accepts only one assertions-source spelling.** `conductor/freeze.py` derives the
   human-authored source by appending `.assertions.md` to the full `docs/specs/<name>.md` match,
   i.e. it requires `<name>.md.assertions.md`. spec-craft's `executable-assertions` and the skill
   prose describe the stem sibling `<name>.assertions.md`. A repo on the stem convention fails
   `gate freeze` with `missing-assertions-source` on upgrade.

### P2 — the authority flow's framing doesn't match harness capability (#74, #75)

4. **"Inherits your session permission mode" over-promises (#74).** The start skill says
   `/conductor:start` reads the session's permission mode "if the harness exposes it." It does not:
   a live session carries `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_EFFORT`,
   `CLAUDE_CODE_ENTRYPOINT`, but no permission-mode variable. So the mode is never auto-detected and
   the run *always* takes the ask-once path — which the prose frames as a fallback, misleading the
   owner into expecting silent inheritance. (The allowlist in `settings.json` *is* readable, so the
   less-privileged preview could still introspect prompt/no-prompt per operation.)

5. **A non-bypass session can arm full-bypass via a pre-existing `resume-env.sh` flag (#75).** The
   skill says "conductor NEVER writes bypass flags from a less-privileged session; elevate = relaunch
   in bypass," but is silent on the case where the flag already exists from a prior opt-in and
   `driver install` preserves it (no-clobber). A session in `defaultMode: auto` can therefore arm a
   standing full-bypass driver. Defensible (durable prior decision + acknowledgment gate) but
   undefined — the behavior must be decided and stated.

### P3 — driver/ledger lifecycle gaps (#76, #77)

6. **A fail-loud driver is still invisible until the next manual `/conductor:start` (#76).** The v4
   driver fails loud (logs `driver-unresolved` / `fire-end rc=<nonzero>` and exits non-zero) instead
   of stalling silently, but nothing surfaces those failures out-of-band; the start-skill reconcile
   only tails the log on the *next* invocation. Between arming and the owner's next check-in, a
   driver failing every fire looks identical to one making progress.

7. **`ledger reconcile` never closes stale blocker/escalation issues (#77).** A blocker filed for a
   transient condition (e.g. "Docker daemon down") stays OPEN after the condition clears; reconcile
   aligns phase issues against the gate but does not re-check escalation issues against reality.

## Design

### P1 — grandfather already-frozen gates; broaden the heuristic; accept both source spellings

- **Lint gates first-freeze, not resume.** The lint's job is to keep a *weak* check from being
  frozen. Once a gate is frozen and `gate verify` is clean, it was vetted at freeze time; re-linting
  it on every reconcile is wrong. Move the lint inside the not-yet-frozen / re-freezing path so the
  `SKIP if .frozen exists and gate verify is clean` condition covers lint as well as freeze.
- **A finding on an already-frozen, clean-verifying gate is a warning, not a block.** When lint does
  run against a gate that is already frozen (e.g. an explicit `conductor gate lint` audit), findings
  are surfaced loudly and the owner may choose to re-freeze — but they never hard-block a resume.
  Record the conductor/lint version in the `.frozen` baseline (which already carries `version: 1`)
  so a gate frozen before a rule existed is grandfathered against that rule.
- **Broaden `_is_negative_assert`.** Recognize `== 0` / `!= 0`-style comparisons against a zero or
  empty-collection constant, `len(set(x)) == len(x)` uniqueness checks, and comparison against a
  named threshold constant, in addition to the existing forms — so a legitimate must-not assertion
  is not flagged regardless of phrasing.
- **`gate freeze` accepts both sibling spellings.** Resolve the assertions source by trying both
  `<name>.assertions.md` (stem) and `<name>.md.assertions.md` (full-path append); a repo using
  either convention freezes and verifies without a symlink bridge. Ambiguity (both present with
  different content) fails closed as today.

### P2 — tell the truth about session mode; define the pre-existing-flag case

- **Rewrite the authority prose (#74)** to state that the harness does not currently expose the
  session permission mode, so `/conductor:start` asks once — this is the expected path, not a
  degraded fallback. Keep the fail-closed resolver semantics. Where the `settings.json` allowlist is
  readable, the less-privileged preview annotates each op prompt/no-prompt instead of marking all
  owner-required.
- **Define the pre-existing-flag path (#75).** When `resume-env.sh` already carries a bypass flag and
  the launching session is not itself in bypass, `/conductor:start` must re-surface the standing
  full-access warning and require a fresh acknowledgment before honoring it — it must never silently
  arm bypass off a stale flag. Document that the revocation lever is `resume-env.sh` / `driver
  uninstall`, not a session-mode change.

### P3 — surface failing fires; reconcile stale blockers

- **Surface a failing driver out-of-band (#76).** On reconcile, and via a lightweight periodic
  check, run `conductor driver status`; when it reports repeated `driver-unresolved` or non-zero
  `fire-end` lines, raise it where the owner will see it (push notification and/or a persisted
  warning), not only in the next interactive `/conductor:start`.
- **Reconcile resolved blockers (#77).** `ledger reconcile` (or a `conductor escalate reconcile`)
  re-checks open blocker/escalation issues against current reality and closes those whose condition
  has cleared, or at minimum surfaces "N stale blocker issues" in the handoff and final owner packet.

### Non-goals

- Changing what `conductor merge`, `merge-gate`, or the run-branch topology enforce.
- Inventing a conductor-specific permission command — conductor continues to inherit Claude Code's
  permission model and invents no flags of its own.
- Auto-starting Docker or managing environment daemons.

## Expectations

### Success scenarios

- Upgrading the conductor plugin and re-running `/conductor:start` on a run whose gate is already
  frozen and whose `conductor gate verify` is clean proceeds past the gate step to the driver step
  **without editing, re-freezing, or otherwise mutating any frozen assertion or its test file.**
- `conductor gate lint` reports **no** "no negative clause" finding for a test whose only negative
  assertion is written `assert len(x) == 0`, `assert len(set(x)) == len(x)`, or `assert value <=
  THRESHOLD` against a named constant.
- `conductor gate freeze` resolves and records the assertions source when it is named
  `<spec>.assertions.md`, and also when it is named `<spec>.md.assertions.md`, with no symlink.
- `/conductor:start` output about unattended authority states that the session permission mode is
  not auto-detected and that the owner is asked once; it makes no claim that bypass is silently
  inherited from the session.
- When `resume-env.sh` already carries the bypass flag and the launching session is not in bypass,
  `/conductor:start` prints the standing full-access warning and requires a fresh acknowledgment
  before arming the driver.

### Failure scenarios (fail-closed)

- A genuinely weakened **frozen** gate — a frozen assertion removed, its manifest command changed,
  or its referenced test file edited — still fails `conductor gate verify` and blocks the run. The
  grandfather rule must not let real tampering through.
- On a **first** freeze (no prior `.frozen`), a genuinely weak test — an unpinned manifest command,
  a test with no negative clause in any recognized form, or a trivially-true assertion — still fails
  `conductor gate lint` and blocks freezing.
- `conductor gate freeze` still fails closed with `ambiguous-assertions-source` when both source
  spellings exist with differing content and no goal disambiguates them.

### Must-nots

- `/conductor:start` must **not** require editing or re-freezing a frozen gate solely to satisfy a
  lint rule that did not exist when the gate was frozen.
- The lint-version grandfather must **not** downgrade a finding on a newly-frozen (post-rule) gate
  to a warning — new gates still hard-block on a real finding.
- Conductor must **not** arm bypass without an explicit acknowledgment in the current session, even
  when `resume-env.sh` already carries the bypass flag.
- Broadening the negative-clause heuristic must **not** cause a trivially-true assertion (`assert
  True`, `assert 1`) to be accepted as a negative clause.
