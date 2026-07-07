# Executable assertions — Conductor Upgrade Survival

Source spec: `docs/specs/2026-07-07-conductor-upgrade-survival.md`
Written by `/spec-craft:executable-assertions` (spec-craft 0.2.1) on 2026-07-07.

These are 4-part assertion **specs**, not test code. A downstream `/conductor:assertions-to-tests`
step turns each into one runnable test wired into `assertions/manifest.yaml` by id.

---

## 1. Load-bearing expectations — encoded

- **A-UPG-1 — lint recognizes semantic negative clauses.** (S2) The false-positive that hard-blocks
  runs; must hold across every recognized form, so encode as a property.
- **A-UPG-2 — freeze resolves both assertions-source spellings.** (S3) The brownfield break; without
  it, upgrade needs a manual symlink.
- **A-UPG-3 — a weakened *frozen* gate still fails verify, even with a version-stamped baseline.**
  (F1) Integrity: the grandfather mechanism must not open a tamper hole. Highest-stakes.
- **A-UPG-4 — first freeze still blocks a genuinely weak test.** (F2) The lint must keep doing its
  real job at freeze time; broadening the heuristic must not blind it.
- **A-UPG-5 — an ambiguous assertions source fails closed.** (F3) Never silently freeze against one
  of two divergent sources.
- **A-UPG-6 — the grandfather does NOT soften a post-rule gate.** (M2) The dangerous inverse of the
  grandfather: it must apply only to gates frozen before the rule existed.
- **A-UPG-7 — the start skill requires a fresh acknowledgment to arm bypass from an existing flag.**
  (M3/S5) Encoded as a *prose contract* (skill-text needle) — see the coverage flag below.
- **A-UPG-8 — the start skill does not claim silent session-mode inheritance.** (S4) Prose contract;
  prevents the exact wrong expectation this run exposed.

## 2. Deliberately NOT encoded

- **S1 ("proceeds past the gate step without mutating any frozen file")** — folded into A-UPG-6:
  "proceeds without surgery" is the observable half of "grandfather a pre-rule gate," and testing the
  full `/conductor:start` flow end-to-end is broader than an assertion. The grandfather property is
  the sharp, checkable core.
- **M4 ("trivially-true not accepted as a negative clause")** — not a standalone assertion; it is the
  explicit *must-not* inside A-UPG-1's observation and is independently caught by A-UPG-4. Encoding it
  twice would be redundant.
- **The full behavioral guarantee of #75 ("a non-bypass session can never end up running bypass fires
  without a human ack")** — NOT machine-assertable: the acknowledgment is an interactive human gate
  spanning the skill flow + `driver install`, with no exit code to check. A-UPG-7 covers only the
  *prose contract* that the skill demands the re-ack. **Flagged: the behavioral security property is
  verified by human review, not by the gate.**

## 3. The 4-part specs

### A-UPG-1 — lint recognizes semantic negative clauses
- **Claim.** `gate_lint`'s negative-clause rule treats `assert len(x) == 0`, `assert len(set(x)) ==
  len(x)`, and `assert value <= THRESHOLD` (comparison to a named/zero/empty constant) as negative
  clauses, so a test file whose only negatives use these forms produces no "no negative clause"
  finding.
- **Setup.** Three fixture test files, each containing exactly one assertion, one per form above; and
  one control fixture containing only a trivially-true assertion (`assert True`) and one positive
  equality that is neither zero nor a uniqueness/threshold comparison.
- **Observation.** Run the lint's negative-clause check over each fixture. **Must-contain:** zero
  "no negative clause" findings for each of the three semantic-negative fixtures. **Must-not-contain:**
  a pass for the trivially-true/positive-only control — it MUST still be flagged (guards against a
  stub that reports every file as having a negative clause). Fail = any semantic-negative fixture
  flagged, OR the control fixture not flagged.
- **Kind.** property (must hold across all three negative forms; the control fixes the lower bound).

### A-UPG-2 — freeze resolves both assertions-source spellings
- **Claim.** `gate freeze` resolves and records the assertions source whether it is named
  `<spec>.assertions.md` (stem) or `<spec>.md.assertions.md` (full-path append).
- **Setup.** Two fixture projects identical except for the assertions-source filename — one stem, one
  full-append — each named by the project's `.conductor/goal.md`; no symlink present.
- **Observation.** Run `gate freeze` in each. **Must-contain:** the written `.frozen` baseline has a
  non-empty `sources` entry whose key is the actual source file and whose value is that file's digest,
  for BOTH projects. **Must-not-contain:** a baseline written with an empty/absent `sources` block
  (a vacuous "success" that froze nothing), and no `missing-assertions-source` error. Fail = either
  project errors on resolution, or freezes with no source digest recorded.
- **Kind.** property (holds across both naming conventions; one case can pass while the other breaks).

### A-UPG-3 — a weakened frozen gate still fails verify, even with a version-stamped baseline
- **Claim.** With a `.frozen` baseline that records a lint/conductor version, `gate verify` still
  fails when a frozen assertion is removed, its manifest command changed, or its referenced test file
  edited.
- **Setup.** A frozen fixture project whose baseline includes the new version field; then three
  mutated variants — (a) a frozen id deleted from the manifest, (b) a frozen entry's command changed,
  (c) a byte changed in a referenced frozen test file.
- **Observation.** Run `gate verify` on each mutation. **Must-contain:** a non-zero exit and a
  `tampered` reason naming the changed id/file, for all three mutations. **Must-not-contain:** any
  path where the presence of the version field causes verify to short-circuit to ok/clean. Fail =
  any mutation verifying clean.
- **Kind.** property (the tamper guard must hold across every mutation kind, not one).

### A-UPG-4 — first freeze still blocks a genuinely weak test
- **Claim.** On a project with no prior `.frozen`, `gate lint` exits non-zero when a manifest command
  is unpinned, OR a referenced test has no negative clause in any recognized form, OR a referenced
  test's only assertion is trivially true.
- **Setup.** Three fresh (unfrozen) fixture projects, one per weakness: an unpinned manifest command;
  a test whose assertions are all positive non-negative comparisons; a test whose only assertion is
  `assert True`.
- **Observation.** Run `gate lint` in each. **Must-contain:** non-zero exit and a finding naming the
  offending command/file, for all three. **Must-not-contain:** a clean lint for any of the three
  (guards against the broadened heuristic in A-UPG-1 swallowing the clauseless or trivially-true
  case). Fail = any weak fixture linting clean.
- **Kind.** property (all three weakness kinds must still be caught).

### A-UPG-5 — an ambiguous assertions source fails closed
- **Claim.** When both `<spec>.assertions.md` and `<spec>.md.assertions.md` exist with differing
  content and no goal disambiguates, `gate freeze` fails closed rather than choosing one.
- **Setup.** A fixture project with both source files present, differing content, and a
  `.conductor/goal.md` that does not uniquely name one (or is absent).
- **Observation.** Run `gate freeze`. **Must-contain:** non-zero exit and an `ambiguous-assertions-source`
  (or equivalent fail-closed) reason. **Must-not-contain:** a written `.frozen` baseline (nothing may
  be frozen), and no silent selection of either file's digest. Fail = freeze succeeds, or records
  either source.
- **Kind.** example (one concrete ambiguous configuration).

### A-UPG-6 — the grandfather does not soften a post-rule gate
- **Claim.** A lint finding is downgraded to a non-blocking warning only when the frozen baseline's
  recorded lint version predates the rule; a gate frozen at or after the rule's version (or freshly
  frozen) still hard-blocks on the same finding.
- **Setup.** Two frozen fixtures carrying the SAME real weak test that the rule flags: one baseline
  stamped with a pre-rule version, one stamped at/after the rule's version.
- **Observation.** Exercise the resume/lint path on each. **Must-contain:** for the pre-rule baseline,
  a warning and a zero/proceed outcome; for the at/after baseline, a non-zero hard block. **Must-not-contain:**
  the at/after baseline proceeding (that would make the grandfather a universal bypass). Fail = both
  behave the same, or the post-rule gate is softened.
- **Kind.** property (the two contrasting versions are the range the invariant must separate).

### A-UPG-7 — the start skill requires a fresh acknowledgment to arm bypass from an existing flag
- **Claim.** `skills/start/SKILL.md` step 6 states that when `resume-env.sh` already carries the
  bypass flag and the launching session is not itself in bypass, arming requires a fresh explicit
  acknowledgment.
- **Setup.** The installed `skills/start/SKILL.md`, excluding this spec and its `.assertions.md`
  (which legitimately quote the requirement) from the search scope.
- **Observation.** Grep the skill's authority section. **Must-contain:** an instruction that a
  pre-existing bypass flag on a non-bypass session triggers a re-acknowledgment before arming.
  **Must-not-contain:** language that an existing bypass flag is honored silently / without
  acknowledgment. Fail = the re-ack instruction absent, or a silent-honor path present.
- **Kind.** contract (a skill-text contract; note the *behavioral* guarantee is human-verified, per
  the coverage flag above — this asserts only that the contract is written).

### A-UPG-8 — the start skill does not claim silent session-mode inheritance
- **Claim.** `skills/start/SKILL.md` states the harness does not currently expose the session
  permission mode and that `/conductor:start` therefore asks once, and it makes no claim that the
  mode is auto-detected/inherited silently.
- **Setup.** The installed `skills/start/SKILL.md`, excluding this spec and its `.assertions.md` from
  scope.
- **Observation.** Grep the authority section. **Must-contain:** a statement that the session
  permission mode is not surfaced by the harness / that start asks once. **Must-not-contain:** an
  unqualified claim that the unattended run inherits or auto-detects the session's permission mode
  without asking. Fail = the honest statement absent, or an unqualified silent-inheritance claim
  present.
- **Kind.** contract (skill-text contract).
