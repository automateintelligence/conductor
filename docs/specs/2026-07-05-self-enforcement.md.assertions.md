# Executable assertions — self-enforcement hardening

Derived from `2026-07-05-self-enforcement.md` Expectations. These are the machine-checked done-gate;
each is RED until the phase that implements it lands.

## Encoded (load-bearing)

- **A1 authority-preview-lists-all-privileged-ops** — silent-stall: an omitted operation means the owner elevates too little and the unattended fire hangs.
- **A3 resume-env-mode-0600** — security: the file carries the bypass flag + a shell-executed verify command.
- **A4 driver-refuses-world-writable-env** — security: a writable env file is a privesc into the agent.
- **A5 posture-label-logged-no-secret** — auditability + exposure: posture must be visible and must not leak the value.
- **A6 gate-lint-fail-closed-on-unpinned** — integrity: an unpinned command reopens the frozen-gate conftest bypass.
- **A7 freeze-covers-assertions-source** — integrity: the human-owned done-definition must be tamper-evident too.
- **A8 default-branch-never-empty** — correctness: an empty resolver value makes git operate on the wrong ref.
- **A9 run-branch-name-deterministic** — correctness: two skills deriving the slug differently orphans the run.
- **A10 driver-status-nonzero-without-durable-driver** — silent-stall: the operator's on-demand health signal must be honest.

## Deliberately not encoded

- Success 2/3 (the full-auto warning+acknowledgment; the less-privileged dry-run CHOICE) and posture
  DETECTION — these run interactively in the owner's live `start` session (agent-executed prose),
  not a pure function; the frozen gate is the dry-run's *content* (A1), not the interaction.
- Failure 2/3 (start proceeds full-auto without acknowledgment; start misreads the session posture)
  — both depend on live session-mode detection + an interactive gate, unprovable by a unit check;
  they are the load-bearing interactive behaviors, verified by review, not frozen.
- Success 5 (README discoverability) — a doc-presence grep; low blast radius, better as a review item.
- Must-not 1 (default behavior unchanged / no bypass baked) — already covered by the existing
  `test_render_never_bakes_a_permission_bypass`; no new assertion needed.
- Must-not 5 (no phase touches the live run's `.conductor/`) — a process invariant about how the work
  is done, not a runtime property of the product; enforced by review, not a test.

---

## A1 — authority-preview-lists-all-privileged-ops
- **Claim:** `conductor authority preview` (the dry-run) names **every** privileged operation the recipe performs for a plan, so the owner can never elevate too little.
- **Setup:** a representative plan with at least one gated phase and a `CONDUCTOR_MERGE_VERIFY` that invokes docker.
- **Observation:** the preview output MUST contain each of: create-branch, `git push`, `gh pr` (create/merge), docker (from the verify command), subagent spawn, and file writes. It MUST NOT omit an operation the recipe actually runs — the dangerous failure is a missing entry, since an unlisted op is one the owner won't authorize and the fire will stall on.
- **Kind:** property (holds across plans: the reported set covers the recipe's privileged surface).

## A3 — resume-env-mode-0600
- **Claim:** any `resume-env.sh` created by `grant` has file mode `0600`.
- **Setup:** a temp project; run `grant --scoped`.
- **Observation:** `stat` of the created file → owner read/write only; group and other read/write/execute bits MUST be `0`.
- **Kind:** property.

## A4 — driver-refuses-world-writable-env
- **Claim:** the generated Tier-B driver refuses to source a group- or world-writable `resume-env.sh` and fails loud instead of firing.
- **Setup:** a generated driver plus a `resume-env.sh` chmod'd `0666`.
- **Observation:** executing the driver's source-guard → non-zero exit AND a refusal logged; it MUST NOT proceed to the `claude -p` fire.
- **Kind:** example.

## A5 — posture-label-logged-no-secret
- **Claim:** the generated driver logs a bare permission-posture label at `fire-start`.
- **Setup:** render a driver.
- **Observation:** the driver text MUST contain a `posture=` token on the fire-start log line, resolving to one of `supervised` / `scoped` / `full-bypass`; that logged label MUST NOT contain the raw `$CONDUCTOR_RESUME_CLAUDE_FLAGS` value or a `settings.json` path.
- **Kind:** property.

## A6 — gate-lint-fail-closed-on-unpinned
- **Claim:** `conductor gate lint` exits non-zero for a manifest command that can load an unfrozen conftest, and zero only for the pinned standalone form.
- **Setup:** two manifests — one command `pytest tests/x.py` (unpinned), one `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider tests/x.py`.
- **Observation:** unpinned → non-zero AND names the offending command; pinned → zero. An unparseable/ambiguous command → non-zero (fail-closed, never pass).
- **Kind:** property.

## A7 — freeze-covers-assertions-source
- **Claim:** `conductor gate freeze` records a digest of `<spec>.assertions.md`, and `gate verify` fails after that file changes.
- **Setup:** a frozen gate; then edit `<spec>.assertions.md`.
- **Observation:** `gate verify` before edit → zero; after edit → non-zero (tamper).
- **Kind:** contract.

## A8 — default-branch-never-empty
- **Claim:** `conductor default-branch` always prints a non-empty branch name; on resolution failure it prints `main`.
- **Setup:** a normal repo, and a simulated `gh`/`git` resolution failure.
- **Observation:** stdout is non-empty in both cases; on the failure path stdout is exactly `main`. It MUST NOT print an empty line.
- **Kind:** property.

## A9 — run-branch-name-deterministic
- **Claim:** `conductor run-branch name <spec>` is deterministic and canonical.
- **Setup:** a fixed spec path.
- **Observation:** two invocations produce byte-identical output matching `conductor/run-<slug>`; different spec paths produce different slugs.
- **Kind:** property.

## A10 — driver-status-nonzero-without-durable-driver
- **Claim:** `conductor driver status` exits non-zero when no durable driver exists and zero when one does.
- **Setup:** a project with no `conductor-autodev` crontab marker / no `scheduled_tasks.json`; then one with a marker present.
- **Observation:** absent → non-zero; present → zero.
- **Kind:** property.
