# Executable assertions — self-enforcement hardening

Derived from `2026-07-05-self-enforcement.md` Expectations. These are the machine-checked done-gate;
each is RED until the phase that implements it lands. Revised 2026-07-06 after a codex spec review
that found several assertions could pass while the intent was violated (hard-coded preview, hard-coded
posture, group-writable file, resolvers that exist but aren't called) — the exact gate-green-≠-done
trap this spec exists to kill. Every assertion below is written to fail if the intent is faked.

## Encoded (load-bearing)

- **A1 authority-preview-covers-the-recipe's-op-set** — silent-stall: an omitted op means the owner under-elevates and the fire hangs; anchored to the recipe so a hard-coded list can't pass.
- **A2 unknown-mode-resolves-least-privileged** — security: a misread session mode must under-grant, never arm standing full access.
- **A3 resume-env-mode-0600** — security: the file carries the bypass flag + a shell-executed verify command.
- **A4 driver-refuses-group-or-world-writable-env** — security: group-writable is a privesc too, not only world-writable.
- **A5 posture-label-reflects-configured-flags** — auditability: the logged posture must track the actual flags, not a constant.
- **A6 gate-lint-fail-closed-on-unpinned** — integrity: an unpinned command reopens the frozen-gate conftest bypass.
- **A7 gate-lint-flags-missing-negative-clause** — integrity: a tautological test that only asserts the happy string freezes a hollow gate.
- **A8 gate-freeze-needle-present** — anti-rot: the freeze step must not silently drop out of the skill.
- **A9 freeze-covers-assertions-source** — integrity: the human-owned done-definition must be tamper-evident too.
- **A10 default-branch-never-empty** — correctness: an empty resolver value makes git operate on the wrong ref.
- **A11 run-branch-name-deterministic** — correctness: two skills deriving the slug differently orphans the run.
- **A12 skills-call-the-resolvers** — correctness: a resolver that exists but isn't called leaves the prose fragility in place.
- **A13 driver-status-nonzero-without-durable-driver** — silent-stall: the operator's health signal must be honest about absence.
- **A14 driver-status-flags-recent-failed-fires** — silent-stall: the signal must also catch a driver that runs but keeps failing.
- **A15 readme-authority-present-and-no-grant-leftover** — regression: the doc must describe the session-inherit model and never the removed `grant`.
- **A16 gate-lint-flags-trivially-true-assertion** — integrity: a tautological frozen test (`assert True`) passes any implementation and hollows the gate.

## Deliberately not encoded

- Success 2/3 interaction (the full-auto warning + acknowledgment; the less-privileged CHOICE) and
  posture DETECTION — these run interactively in the owner's live `start` session (agent-executed),
  not a pure function. A2 freezes the fail-closed *resolution* and A1 freezes the dry-run *content*;
  the interactive gates themselves are verified by review.
- Failure 2 (start proceeds full-auto without acknowledgment) — depends on the interactive gate;
  unprovable by a unit check. (Failure 3's over-grant direction IS now bounded mechanically by A2.)
- Must-not 1 (no bypass baked by default) — already covered by the existing `test_render_never_bakes_a_permission_bypass`.
- Must-not 5 (no phase touches the live run's `.conductor/`) — a process invariant about how the work is done, enforced by review.

---

## A1 — authority-preview-covers-the-recipe's-op-set
- **Claim:** `conductor authority preview` emits every privileged operation the autodev recipe performs, sourced from one declared set — not a literal list baked into the preview.
- **Setup:** a representative plan; a single declared `RECIPE_PRIVILEGED_OPS` set that the preview is generated from.
- **Observation:** (a) the preview output covers every entry in `RECIPE_PRIVILEGED_OPS` (drop one from the source → the preview drops it → the test that compares against the source still holds, so the preview cannot silently omit a declared op); AND (b) a companion needle asserts `RECIPE_PRIVILEGED_OPS` contains each privileged verb the autodev recipe actually names — create branch, `git push`, `gh pr`, `conductor merge`, docker (via `CONDUCTOR_MERGE_VERIFY`), subagent spawn, file writes — so adding a recipe op without adding it to the set fails. A hard-coded preview that ignores the set MUST fail (a).
- **Kind:** property (the reported set equals the declared privileged surface, and the declared surface tracks the recipe).

## A2 — unknown-mode-resolves-least-privileged
- **Claim:** the posture-resolution function maps an unknown / unreadable / ambiguous session mode to `supervised` (least-privileged), never to `full-bypass`.
- **Setup:** call the resolver with each of: a recognized bypass mode, a recognized less-privileged mode, and an unknown/empty/garbage mode.
- **Observation:** unknown/empty/garbage → `supervised`. It MUST NOT return `full-bypass` (or any bypass posture) for any input that is not an affirmatively-recognized bypass mode. Recognized bypass → `full-bypass`; recognized less-privileged → its scoped/supervised posture.
- **Kind:** property (fail-closed across all non-bypass inputs).

## A3 — resume-env-mode-0600
- **Claim:** any `resume-env.sh` the tool writes is created with mode `0600`.
- **Setup:** run the authority flow that writes `resume-env.sh` (whatever command performs it — not tied to a specific subcommand name).
- **Observation:** `stat` of the written file → owner read/write only; every group and other bit (read/write/execute) MUST be `0`.
- **Kind:** property.

## A4 — driver-refuses-group-or-world-writable-env
- **Claim:** the generated Tier-B driver refuses to source a `resume-env.sh` that is group- OR world-writable, and fails loud instead of firing.
- **Setup:** a generated driver plus a `resume-env.sh` at each of: `0660` (group-writable), `0606`/`0666` (world-writable), and `0600` (safe).
- **Observation:** for `0660` and world-writable → non-zero exit AND a refusal logged AND it MUST NOT reach the `claude -p` fire. For `0600` → it proceeds. (A guard that rejects world-writable but accepts group-writable MUST fail this.)
- **Kind:** property.

## A5 — posture-label-reflects-configured-flags
- **Claim:** the posture label the driver logs at `fire-start` is derived from the configured flags, not a constant.
- **Setup:** the driver's posture computation exercised with three inputs: `CONDUCTOR_RESUME_CLAUDE_FLAGS` containing `--dangerously-skip-permissions`; a `--settings <path>` (scoped) form; and empty.
- **Observation:** bypass flags → `posture=full-bypass`; scoped `--settings` → `posture=scoped`; empty → `posture=supervised`. The logged label MUST NOT be a fixed string across these inputs, and MUST NOT contain the raw flag value or the settings-file path.
- **Kind:** property.

## A6 — gate-lint-fail-closed-on-unpinned
- **Claim:** `conductor gate lint` exits non-zero for a manifest command that can load an unfrozen conftest, and zero only for the pinned standalone form.
- **Setup:** two manifests — `pytest tests/x.py` (unpinned) and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider tests/x.py` (pinned).
- **Observation:** unpinned → non-zero AND names the offending command; pinned → zero; an unparseable/ambiguous command → non-zero (fail-closed, never pass).
- **Kind:** property.

## A7 — gate-lint-flags-missing-negative-clause
- **Claim:** `conductor gate lint` flags an assertion test whose file contains no negative ("must not contain" / `assertNot`-style) check.
- **Setup:** two assertion test files — one with only a positive/happy assertion, one with an explicit negative check.
- **Observation:** the positive-only file → flagged (non-zero / named); the file with a negative clause → not flagged.
- **Kind:** property.

## A8 — gate-freeze-needle-present
- **Claim:** the start contract test asserts the presence of a `gate freeze` needle, so the freeze step cannot silently rot out of the skill.
- **Setup:** the start skill contract test (`tests/conductor/test_skill_outputs.py`).
- **Observation:** its needle list contains `gate freeze` (or `conductor gate freeze`); removing the freeze step from `skills/start/SKILL.md` MUST fail the contract test.
- **Kind:** contract.

## A9 — freeze-covers-assertions-source
- **Claim:** `conductor gate freeze` records a digest of `<spec>.assertions.md`, and `gate verify` fails after that file changes.
- **Setup:** a frozen gate; then edit `<spec>.assertions.md`.
- **Observation:** `gate verify` before the edit → zero; after → non-zero (tamper).
- **Kind:** contract.

## A10 — default-branch-never-empty
- **Claim:** `conductor default-branch` always prints a non-empty branch name; on resolution failure it prints `main`.
- **Setup:** a normal repo, and a simulated `gh`/`git` resolution failure.
- **Observation:** stdout is non-empty in both cases; on the failure path stdout is exactly `main`. It MUST NOT print an empty line.
- **Kind:** property.

## A11 — run-branch-name-deterministic
- **Claim:** `conductor run-branch name <spec>` is deterministic and canonical.
- **Setup:** a fixed spec path.
- **Observation:** two invocations produce byte-identical output matching `conductor/run-<slug>`; different spec paths produce different slugs.
- **Kind:** property.

## A12 — skills-call-the-resolvers
- **Claim:** the autodev and start skills invoke `conductor run-branch name` and `conductor default-branch` rather than deriving the slug / default branch in prose.
- **Setup:** the skill contract test over `skills/autodev/SKILL.md` and `skills/start/SKILL.md`.
- **Observation:** both skills contain a `conductor run-branch name` invocation and a `conductor default-branch` invocation (needles); the branch-currency/final-PR steps reference them, not a bare `<default>` placeholder derived in prose.
- **Kind:** contract.

## A13 — driver-status-nonzero-without-durable-driver
- **Claim:** `conductor driver status` exits non-zero when no durable driver exists and zero when one does.
- **Setup:** a project with no `conductor-autodev` crontab marker / no `scheduled_tasks.json`; then one with a marker present.
- **Observation:** absent → non-zero; present → zero.
- **Kind:** property.

## A14 — driver-status-flags-recent-failed-fires
- **Claim:** `conductor driver status` reports (and exits non-zero on) recent driver failures even when a durable driver is installed.
- **Setup:** a project with a durable driver present AND a `resume-autodev.log` whose recent lines include `driver-unresolved` and/or `fire-end rc=<non-zero>`.
- **Observation:** status names the recent failures AND exits non-zero; with a clean log (recent `fire-end rc=0`) → zero.
- **Kind:** property.

## A15 — readme-authority-present-and-no-grant-leftover
- **Claim:** the README documents the session-inherit authority model and no doc references the removed `grant` command.
- **Setup:** the USER-FACING docs after Phase 2 — `README.md`, `experiments/E5-end-to-end/recovery.md`, `skills/*/SKILL.md`. (This spec and its `.assertions.md` legitimately name the removed command to document its removal and are EXCLUDED from the check — else it self-references.)
- **Observation:** README contains an "Unattended authority" section describing session-inherited posture (warning+acknowledgment / dry-run); none of the user-facing docs above contains `grant --scoped` or `grant --full`.
- **Kind:** example.

## A16 — gate-lint-flags-trivially-true-assertion
- **Claim:** `conductor gate lint` flags an assertion test whose only assertion is trivially true (passes any implementation).
- **Setup:** two assertion test files — one whose body is `assert True` (or a bare non-empty literal / `assert 1`), one that asserts against real behavior.
- **Observation:** the trivially-true file → flagged (non-zero / named); the real-behavior file → not flagged for this reason. An unparseable test → flagged (fail-closed), never silently passed.
- **Kind:** property.
