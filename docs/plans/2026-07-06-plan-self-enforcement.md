# Conductor Self-Enforcement Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Normative spec:** docs/specs/2026-07-05-self-enforcement.md
**Executable assertions:** docs/specs/2026-07-05-self-enforcement.md.assertions.md

The spec is **normative over this plan** on any conflict — if a task here disagrees with the
spec, the spec wins and the plan is what gets fixed. Workers MUST read the phase's `Spec:`
sections in the normative spec **before** implementing. The executable assertions are only the
**mechanical done-floor**: the frozen tests in `assertions/self_enforcement/` gate the objective
minimum, but the spec's architecture, behaviors, and qualities (the interactive warning flow, the
canonical doc spelling, the fail-closed design stance) are the actual work — there is more of it
than the assertions capture, and the codex review of each phase PR judges that substance.

**THE DONE-GATE IS FROZEN.** The sixteen tests `assertions/self_enforcement/test_a01…test_a16`
and their entries in `assertions/manifest.yaml` are frozen (`conductor gate freeze`); the runner
fail-closes with **exit 6** on any change to them. Make a red assertion green by implementing the
**product behavior it pins** — never by editing a test, the manifest, or `assertions/.frozen`.
Every interface named in this plan (module paths, function names, CLI spellings, log tokens) is
pinned by those frozen tests; read the phase's test files before implementing.

Per-phase recipe: subagent implement on a phase branch forked FROM THE RUN BRANCH → /code-review per task (against the phase's Spec sections, not just the diff) → commit per task → one PR per phase with base = the run branch (`Closes #<phase-issue>`) → codex review ×2 posted as "Codex review" PR comments → `conductor merge-gate` → merge into the run branch → `/document-release` → `conductor ledger phase-done`.

**Goal:** Convert conductor's prose-fragile joints into tested CLI surface: session-mode-aware
unattended authority, posture visibility, a fail-closed `gate lint`, single-sourced branch
resolvers, and owned driver install/status — per the 2026-07-05 self-enforcement spec.

**Architecture:** Each phase adds NEW CLI surface (`conductor authority|gate lint|run-branch|
default-branch|driver|resume-script install-cron/uninstall-cron`) as Python modules under
`conductor/`, dispatched from `bin/conductor`, with skill prose updated to *call* the tested
surface instead of deriving behavior in prose. One PR per phase into the run branch.

**Tech Stack:** Python 3.12 stdlib (ast, shlex, hashlib, subprocess), bash (`bin/conductor`,
driver template), pytest under `tests/conductor/`, Markdown skills.

## Global Constraints (spec must-nots — every task implicitly includes these)

- **No default-behavior change:** an operator who invokes none of the new commands gets no
  permission bypass and today's merge/reconcile/gate semantics. Every addition is opt-in.
- **Never touch the live run:** no phase modifies the executing run's `.conductor/` scratch,
  its installed Tier-B driver, or the machine's real crontab. Unit tests for cron behavior use
  a stub `crontab` on `PATH` (see A13/A14 fixtures) — never `crontab -l` for real. New driver
  template behavior goes live only when the owner updates the plugin after the run.
- **`gate lint` is fail-closed:** an unparseable or ambiguous manifest command or test file
  counts as reject, never pass.
- **Resolvers never emit empty values:** `default-branch` and `run-branch name` always print
  one non-empty line; failure falls open to a safe default (`main`), never to `""`.
- **Frozen gate:** never edit `assertions/self_enforcement/*`, `assertions/manifest.yaml`
  entries, or `assertions/.frozen`. Implement product behavior until the tests pass.
- **`start` never over-grants:** the unattended run never gets more authority than the
  launching session had, and full autonomy always passes through the warning + acknowledgment.
- **Any `resume-env.sh` conductor writes is mode 0600** — it can carry the bypass flag and a
  shell-executed `CONDUCTOR_MERGE_VERIFY` command.
- **Quality gate before any task is complete:** `ruff check . && ruff format --check . &&
  pyright . && pytest -q tests/` (NOT bare `pytest -q`: repo-root discovery collects the
  still-RED frozen assertion suite and would block every phase until the whole spec is done),
  plus the phase's OWN frozen assertion tests run via their exact manifest commands. The full
  `a1…a16` sweep happens once, at the end of Phase 6.
- **Existing skill contract needles must keep passing** (`tests/conductor/test_skill_outputs.py`)
  — when editing `skills/start/SKILL.md` or `skills/autodev/SKILL.md`, never delete text a
  needle matches; needles may be ADDED, never weakened.

---

## Phase 1 — Session-mode-aware unattended authority (a1-authority-preview-covers-recipe-ops, a2-unknown-mode-resolves-least-privileged, a3-resume-env-mode-0600, a4-driver-refuses-writable-env)

**Spec:** §"Phase 1 — Session-mode-aware unattended authority (review A-1, A-3, A-5, A-8)" — read the
owner decision (inherit Claude Code's permission model, NO conductor-specific flags), the (A)
bypass warning/acknowledgment and (B) less-privileged dry-run cases, the fail-closed posture
resolution invariant, and the open implementation question about mode detection.

**Files:**
- Create: `conductor/authority.py`
- Modify: `bin/conductor` (add `authority` dispatch + usage line)
- Modify: `conductor/resume_script.py` (env-file safety guard in `render`; `TEMPLATE_VERSION` bump)
- Modify: `skills/start/SKILL.md` (posture detection, warning/acknowledgment, dry-run choice)
- Test: `tests/conductor/test_authority.py` (create), `tests/conductor/test_resume_script.py` (extend)

**Interfaces (pinned by frozen tests A1–A4 — read them first):**
- `conductor.authority.RECIPE_PRIVILEGED_OPS: frozenset[str]` — ONE declared set; each of the
  seven recipe verb categories (branch / push / gh pr / merge / docker via
  `CONDUCTOR_MERGE_VERIFY` / subagent / writes) covered by a DISTINCT entry (no mega-string).
- `conductor.authority.resolve_posture(mode: str | None) -> str` — returns exactly one of
  `"full-bypass" | "scoped" | "supervised"`; fail-closed.
- `conductor.authority.write_resume_env(project_root: str, env: dict[str, str]) -> str` —
  writes `<project>/.conductor/resume-env.sh`, returns its path, mode 0600 in every case.
  Serialization: each line is `KEY={shlex.quote(value)}` — NEVER `KEY="{shlex.quote(value)}"`
  (double-quoting a shlex-quoted value preserves the quote characters and breaks the driver's
  unquoted `${CONDUCTOR_RESUME_CLAUDE_FLAGS:-}` expansion for values with spaces, e.g.
  `--settings /path`). Validate key names (`[A-Z_][A-Z0-9_]*`) before writing.
- `conductor authority preview <plan.md>` — exit 0, prints every `RECIPE_PRIVILEGED_OPS` entry
  verbatim, per phase of the plan; never mentions `grant --scoped` / `grant --full`.
- Driver: refuses a group- or world-writable `resume-env.sh` BEFORE sourcing it — logs an
  `env-unsafe` line to `resume-autodev.log`, exits non-zero, never reaches the fire; proceeds
  on 0600 (or no file).

- [ ] **Task 1.1 — `conductor/authority.py`: the declared op set + posture resolver + env writer (TDD).**
  Read `assertions/self_enforcement/test_a01_authority_preview.py`, `test_a02_posture_fail_closed.py`,
  `test_a03_resume_env_mode.py` first. Write failing unit tests in
  `tests/conductor/test_authority.py` covering: (a) the set covers all seven categories with
  distinct entries; (b) `resolve_posture` — recognized bypass → `full-bypass`; `default`/`plan`
  → `supervised`; `acceptEdits` → `scoped` or `supervised`; the full garbage sweep (`""`,
  `None`, `"bypas"`, `"bypassPermissions extra"`, nonsense) → exactly `"supervised"`; closed
  vocabulary; (c) `write_resume_env` fresh / pre-existing-0644 / empty-env all end 0600 at
  `<project>/.conductor/resume-env.sh`. Run → FAIL. Then implement:

  ```python
  RECIPE_PRIVILEGED_OPS: frozenset[str] = frozenset({
      "create the phase branch (git branch/checkout, forked from the run branch)",
      "git push (phase branch + run branch to the remote)",
      "gh pr create/comment (open the phase PR, post review comments)",
      "conductor merge <pr> (gated gh-based merge into the run branch)",
      "docker via CONDUCTOR_MERGE_VERIFY (the owner's verify command runs as shell)",
      "subagent spawn (fresh implementation subagent per phase)",
      "file writes (broad repo edits across the worktree)",
  })

  _BYPASS_MODES = frozenset({"bypassPermissions"})       # affirmative EXACT match only
  _MODE_POSTURE = {"default": "supervised", "plan": "supervised", "acceptEdits": "scoped"}

  def resolve_posture(mode: str | None) -> str:
      """Fail-closed: anything not an affirmatively-recognized bypass mode never returns a
      bypass posture; unknown/empty/None/ambiguous resolves to supervised (spec A2)."""
      if not isinstance(mode, str):
          return "supervised"
      m = mode.strip()
      if m in _BYPASS_MODES:
          return "full-bypass"
      return _MODE_POSTURE.get(m, "supervised")
  ```

  Exact set-membership only — never substring matching (`"bypassPermissions extra"` MUST
  resolve `supervised`). `write_resume_env` creates `.conductor/` if needed, writes
  `KEY={shlex.quote(value)}` lines (see the Interfaces note — never wrap the quoted value in
  extra double quotes), and `os.chmod(path, 0o600)` unconditionally AFTER the
  write (tightens a pre-existing looser file; do not rely on umask — use
  `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` plus the explicit chmod).
  Run unit tests → PASS. `ruff check . && ruff format --check . && pyright . && pytest -q`.
  Commit.

- [ ] **Task 1.2 — `conductor authority preview <plan.md>` (TDD).** `authority.py` gains
  `preview(plan_text: str) -> str` and `main(argv) -> int` handling `preview <plan.md>`.
  Parse phases with the existing plan dialect parser (`ledger.sync.parse_plan_md` /
  `_phase_heading` — do not write a second parser). For EACH phase, print the phase title and
  then every entry of `RECIPE_PRIVILEGED_OPS` **verbatim, generated by iterating the set**
  (`for op in sorted(RECIPE_PRIVILEGED_OPS)`) — a literal list that ignores the set fails
  frozen A1 the moment they diverge. Header/footer prose states these are the operations an
  unattended fire performs per phase and which a non-bypass session would prompt for. Exit 0
  on success; unreadable/phaseless plan → non-zero with a reason (fail-closed). Never emit
  `grant --scoped` / `grant --full`. Unit tests in `tests/conductor/test_authority.py`: preview
  over a two-phase fixture plan covers every op; drop-one-op simulation via monkeypatched set
  confirms output tracks the set. Add to `bin/conductor`:

  ```bash
  authority) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.authority "$@" ;;
  ```

  and extend the usage block with `conductor authority preview <plan.md>`. Verify the frozen
  gate leg goes green: run the exact manifest commands for
  `a1-authority-preview-covers-recipe-ops`, `a2-unknown-mode-resolves-least-privileged`,
  `a3-resume-env-mode-0600`. Quality gate. Commit.

- [ ] **Task 1.3 — driver refuses a writable `resume-env.sh` (TDD).** Read
  `assertions/self_enforcement/test_a04_driver_refuses_writable_env.py` first — it executes the
  rendered driver for real with stub bins. In `conductor/resume_script.py::render`, immediately
  BEFORE the line that sources `resume-env.sh`, insert a permission guard; bump
  `TEMPLATE_VERSION` to 3 (so `resume-script verify` flags installed v2 drivers stale and
  reconcile self-heals AFTER the owner upgrades — never touch the live run's installed driver
  in this phase):

  ```bash
  ENV_FILE="$PROJECT/.conductor/resume-env.sh"
  if [ -f "$ENV_FILE" ]; then
      ENV_MODE="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
      if [ $(( 8#$ENV_MODE & 8#022 )) -ne 0 ]; then
          printf '%s env-unsafe mode=%s %s\n' "$(ts)" "$ENV_MODE" "$ENV_FILE" >> "$LOG"
          exit 5
      fi
      . "$ENV_FILE"
  fi
  ```

  The mask `022` catches BOTH group-writable (0660) and world-writable (0606/0666) — a
  world-only guard fails the frozen test. The guard must run before any `fire-start` log line
  and before the fire. Extend `tests/conductor/test_resume_script.py` with unit tests
  mirroring the A4 harness (temp HOME + stub `claude`/`conductor`, modes 0660/0606/0666 refuse
  + log `env-unsafe` + no fire; 0600 proceeds), plus a static check that `render()` contains
  `env-unsafe` and the sourcing is gated. Confirm all existing `test_resume_script.py` tests
  still pass (no rot patterns, no baked bypass, sourcing still present). Run the manifest
  command for `a4-driver-refuses-writable-env` → PASS. Quality gate. Commit.

- [ ] **Task 1.4 — `skills/start/SKILL.md`: interactive posture flow (review-verified — no frozen
  assertion; spec §Phase 1 cases (A) and (B)).** Extend step 6's UNATTENDED PERMISSIONS block:
  - **Detect the launching session's posture.** If the harness exposes the session permission
    mode, read it; if it cannot be read, ask the owner ONCE ("what posture should the
    unattended run use?"). Either way resolve it with the `conductor.authority.resolve_posture`
    semantics — an unknown/unreadable/ambiguous answer is treated as **supervised**
    (fail-closed; never assume bypass). Conductor inherits Claude Code's permission model —
    it invents NO permission flags or tokens of its own.
  - **(A) bypass mode:** print a big explicit warning — a standing full-access agent will fire
    every heartbeat with the owner's credentials (gh merge, push, docker, broad edits,
    subagents), surviving reboots, until the gate is green — and require the owner to
    **acknowledge to continue**. The acknowledgment IS the gate; no extra flag. Never start
    unattended full-auto silently.
  - **(B) less-privileged mode:** run `conductor authority preview <plan.md>` and show the
    owner the concrete per-phase privileged-operation list (the dry-run), annotated with
    **which of those the current mode would prompt for**: if the session's allowlist can be
    introspected, mark each op prompt/no-prompt; if promptability CANNOT be introspected, mark
    every listed op as owner-required/manual (fail-closed toward "will stall") — never merely
    list them unannotated. Then offer the real three-way choice: (i) elevate to bypass — which
    means the OWNER relaunches/reconfigures the session itself in bypass mode (with the (A)
    warning); conductor NEVER writes bypass flags into `resume-env.sh` from a less-privileged
    session (must-not 2: the run never gets more authority than the launching session had);
    (ii) widen the session's own scoped allowlist to cover the listed operations; or (iii)
    proceed knowing exactly which steps will require them.
  - Any `resume-env.sh` written on this path goes through `conductor.authority.write_resume_env`
    (0600 — never a hand `printf > file`).
  Keep every existing needle string in `test_start_skill_contract` intact. Run
  `pytest tests/conductor/test_skill_outputs.py -v` → PASS. Quality gate. Commit.

---

## Phase 2 — README "Unattended authority" + canonical bypass spelling (a15-readme-authority-no-grant-leftover)

**Spec:** §"Phase 2 — README 'Unattended authority' + canonical bypass spelling (review A-2, A-9)".

**Files:**
- Modify: `README.md` (add an "Unattended authority" subsection under "## Use" → "### 3. Walk away")
- Modify: `experiments/E5-end-to-end/recovery.md` (canonical bypass spelling)
- Test: frozen `assertions/self_enforcement/test_a15_readme_authority.py` (read first); no new
  unit test file needed — A15 is the doc contract.

- [ ] **Task 2.1 — README "Unattended authority" subsection.** Add a subsection titled exactly
  **"Unattended authority"** inside README §3 ("### 3. Walk away" in "## Use"), within ~3000
  chars of the heading covering all three needles the frozen test checks: the model plainly
  stated — an unattended run **inherits the permission mode of the session you launch
  `/conductor:start` in** (no conductor-specific permission command); launch in bypass mode →
  you are **warned** about the standing full-access blast radius and must **acknowledge** to
  continue; launch in a less-privileged mode → `start` shows a **dry-run** (`conductor
  authority preview`) naming the concrete privileged operations each phase performs and which
  would need you, then offers elevate / widen-allowlist / proceed. State the safety floor: any
  `resume-env.sh` conductor writes is mode 0600 and the driver refuses a group- or
  world-writable one. Run the manifest command for `a15-readme-authority-no-grant-leftover` →
  the section test passes. Commit.

- [ ] **Task 2.2 — one canonical bypass spelling across user-facing docs (review-verified — no
  frozen assertion).** Canonical form: **`--dangerously-skip-permissions`** (it is what
  `CONDUCTOR_RESUME_CLAUDE_FLAGS` actually carries in the driver, `skills/start/SKILL.md:140`,
  and `recovery.md:55`). Rewrite `experiments/E5-end-to-end/recovery.md`'s Security note
  (line 85) to use the canonical spelling instead of `--permission-mode bypassPermissions`;
  sweep README and `skills/*/SKILL.md` so the alternate spelling appears in no user-facing doc
  (`grep -rn "permission-mode bypassPermissions" README.md experiments/ skills/` → empty).
  The Python-side mode token `bypassPermissions` inside `conductor/authority.py` and its tests
  is a session-mode VALUE, not a doc spelling — leave it.

- [ ] **Task 2.3 — no `grant` leftovers.** Verify no user-facing doc (README,
  `experiments/E5-end-to-end/recovery.md`, `skills/*/SKILL.md`) contains `grant --scoped` or
  `grant --full` (the spec + its `.assertions.md` are exempt by construction). Run
  `pytest via the a15 manifest command` and the full quality gate. Commit.

---

## Phase 3 — Posture visibility in the generated driver (a5-posture-label-reflects-flags)

**Spec:** §"Phase 3 — Posture visibility in the generated driver (review A-4, A-6)".

**Files:**
- Modify: `conductor/resume_script.py` (`render`: posture derivation + labeled `fire-start`;
  `_write`: split + regated nudge; `TEMPLATE_VERSION` bump to 4)
- Test: `tests/conductor/test_resume_script.py` (extend)

- [ ] **Task 3.1 — `posture=` label at fire-start (TDD).** Read
  `assertions/self_enforcement/test_a05_posture_label.py` first. In `render`, after the env
  file is sourced (Phase 1's guard) and before the fire, DERIVE the label from the configured
  flags — never a constant, and never echo the raw flag value or the settings path:

  ```bash
  POSTURE="supervised"
  case " ${CONDUCTOR_RESUME_CLAUDE_FLAGS:-} " in
      *"--dangerously-skip-permissions"*) POSTURE="full-bypass" ;;
      *"--settings"*)                     POSTURE="scoped" ;;
  esac
  printf '%s fire-start posture=%s\n' "$(ts)" "$POSTURE" >> "$LOG"
  ```

  (bypass wins if both appear; the posture line carries ONLY the bare label). Bump
  `TEMPLATE_VERSION` to 4. Extend `tests/conductor/test_resume_script.py` with the three-input
  behavioral case (stub-bin harness as in A5: bypass flags → `posture=full-bypass`; a
  `--settings <path>` form → `posture=scoped` with the path absent from the whole log; empty →
  `posture=supervised`) plus a static must-not: the `printf` posture line never interpolates
  `$CONDUCTOR_RESUME_CLAUDE_FLAGS`. Run → PASS; run the manifest command for
  `a5-posture-label-reflects-flags` → PASS; re-run the a4 manifest command (same template) →
  still PASS. Quality gate. Commit.

- [ ] **Task 3.2 — write-nudge split + regate (review-verified — no frozen assertion; spec:
  "gated on 'permission posture undecided' rather than 'resume-env.sh absent'").** In
  `_write`, replace the `if not os.path.isfile(env_path):` nudge gate with a posture probe:
  read `resume-env.sh` if present; posture is DECIDED iff its `CONDUCTOR_RESUME_CLAUDE_FLAGS`
  line contains `--dangerously-skip-permissions` or `--settings`. When UNDECIDED — including
  when the file exists but sets no posture — print the nudge, split into two concrete named
  branches: **(scoped)** `CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings <path-to-scoped-settings.json>"`
  (least privilege: git/gh/pytest/ruff/pyright/conductor/docker allowlist) and **(full)**
  `CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"` (standing full-access
  posture — the owner's explicit call; never defaulted). Unit tests: nudge fires with an
  existing posture-less `resume-env.sh`; nudge silent when either posture is set; both path
  spellings named in the nudge text. Quality gate. Commit.

---

## Phase 4 — conductor gate lint + freeze covers the assertions source (a6-gate-lint-fail-closed-on-unpinned, a7-gate-lint-flags-missing-negative, a16-gate-lint-flags-trivially-true, a8-gate-freeze-needle-present, a9-freeze-covers-assertions-source)

**Spec:** §"Phase 4 — `conductor gate lint`: frozen-gate quality + integrity (review B-4)" —
including the deliberate Boundary note: lint catches only the mechanically-detectable holes;
judgment-requiring weaknesses stay with the `/conductor:assertions-to-tests` red-team step.

**Files:**
- Create: `conductor/gate_lint.py`
- Modify: `conductor/freeze.py` (dispatch `lint`; digest `<spec>.assertions.md` in
  `record`/`verify`), `bin/conductor` (usage: `conductor gate {lint|freeze|verify}`)
- Modify: `tests/conductor/test_skill_outputs.py` (add the `gate freeze` needle),
  `skills/start/SKILL.md` (run `gate lint` before `gate freeze`)
- Test: `tests/conductor/test_gate_lint.py` (create), `tests/conductor/test_freeze.py` (extend)

**Interfaces (pinned by frozen A6/A7/A16/A8/A9):** `conductor gate lint` runs against the
project's `assertions/manifest.yaml` (project = `CONDUCTOR_HOME`, already exported by
`bin/conductor`), exit 0 clean / non-zero with one named finding per line. Fail-closed
throughout: unparseable command → reject; unparseable (SyntaxError) test file → reject.

- [ ] **Task 4.1 — `conductor/gate_lint.py`: pinned-command rule (TDD).** Read
  `assertions/self_enforcement/test_a06_gate_lint_unpinned.py` first. Write failing tests in
  `tests/conductor/test_gate_lint.py` (fixture projects in `tmp_path` with `CONDUCTOR_HOME`
  pointed at them, exactly like the frozen tests). Rule: load the manifest through the
  runner's own loader (mirror `freeze._load`); for each entry, `shlex.split(command)` —
  `ValueError` → reject (`unparseable-command`, print the raw command). A command passes ONLY
  in the full pinned standalone form, validated as an EXACT argv shape — not token presence:
  optional leading env assignments limited to a known-safe allowlist
  (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` required among them), then exactly `python3 -m pytest`,
  with `--noconftest` and `-p no:cacheprovider` present, and every remaining token a pytest
  flag or a test path. Reject shell compounds and wrappers outright (`&&`, `;`, `|`,
  backticks, `$(`, `env`, `bash -c`, unrecognized env prefixes, a second command after the
  test paths) — token-presence matching would pass
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest --noconftest -p no:cacheprovider x.py && python3 evil.py`,
  which reopens the bypass. Anything short or ambiguous — bare `pytest`, missing
  `--noconftest`, missing the autoload env, any non-pytest command shape — is rejected with a
  line containing the word `unpinned` and the offending command verbatim (the frozen test asserts the test
  path appears in output, and that `unpinned` never appears for a clean gate). Implement,
  PASS, quality gate, commit.

- [ ] **Task 4.2 — negative-clause + trivially-true rules over the referenced test files (TDD).**
  Read `test_a07_gate_lint_negative_clause.py` and `test_a16_gate_lint_trivial.py` first.
  Resolve each entry's referenced `.py` test files from its command tokens (same token→path
  resolution stance as `freeze._referenced_files`; a referenced file that does not exist →
  reject, fail-closed). Per file, `ast.parse` — `SyntaxError` → reject (fail-closed, names the
  file). Then two independent rules, each reporting `<path>: <reason>`:
  - **missing-negative (A7):** flag when NO assert in the file is a negative check. Negative =
    an `ast.Assert` whose test contains `ast.NotIn`, `ast.NotEq`, `ast.IsNot`, or
    `ast.UnaryOp(op=ast.Not)`, or a call to a method whose lowercased name starts with
    `assertnot`. Reason line contains `no negative` (the frozen test asserts that string is
    absent for a compliant file, present naming `test_sample.py` for the offender).
  - **trivially-true (A16):** flag any `ast.Assert` whose test is a bare truthy
    `ast.Constant` (`True`, `1`, a non-empty str/bytes literal). Reason line contains
    `trivially-true` and the file name; a real-behavior file must produce NO output containing
    `trivial`. Note the frozen fixtures deliberately include a negative clause alongside the
    trivial assert — the rules must be independent.
  Wire `main()` and dispatch: in `freeze.main`, add `if cmd == "lint": from conductor import
  gate_lint; return gate_lint.main()`; update `bin/conductor`'s usage text to
  `conductor gate {lint|freeze|verify}`. Run the manifest commands for
  `a6-gate-lint-fail-closed-on-unpinned`, `a7-gate-lint-flags-missing-negative`,
  `a16-gate-lint-flags-trivially-true` → PASS. Quality gate. Commit.

- [ ] **Task 4.3 — freeze/verify covers `<spec>.assertions.md` (TDD).** Read
  `test_a09_freeze_covers_assertions_source.py` first. In `freeze.py`: discover the
  human-authored assertions source — parse `<project>/.conductor/goal.md` for a
  `docs/specs/<name>.md` path and take its `.assertions.md` sibling (the preferred, precise
  path). Fall back to globbing `docs/specs/*.assertions.md` ONLY when no goal identifies one:
  exactly one match → use it; MULTIPLE matches with no goal → fail closed with
  `ambiguous-assertions-source` (freezing every spec's assertions silently would let an edit
  to an UNRELATED spec's assertions break this run's gate); none → no source entry, old
  behavior. `record()` stores their digests under a new top-level baseline key
  `"sources": {relpath: sha256}` alongside `"ids"`; `verify()` compares and reports
  `assertions-source-changed (<rel>)` / `assertions-source-removed (<rel>)` through the
  existing `[GATE] TAMPERED:` path (the frozen test greps case-insensitively for `tamper`).
  A baseline WITHOUT a `"sources"` key (pre-upgrade `.frozen`) verifies exactly as before —
  backward compatible, no default-behavior change. Extend `tests/conductor/test_freeze.py`:
  freeze → verify clean → edit the `.assertions.md` → verify tampered; and an old-format
  baseline stays clean. Run the manifest command for `a9-freeze-covers-assertions-source` →
  PASS. Quality gate. Commit.

- [ ] **Task 4.4 — the `gate freeze` contract needle + `gate lint` in the start skill.** Read
  `test_a08_freeze_needle.py` first: it requires an ACTIVE needle line inside
  `test_start_skill_contract` matching `^\s*["'](conductor )?gate freeze["'],?\s*$`. In
  `tests/conductor/test_skill_outputs.py::test_start_skill_contract`, add two needles to the
  list, each on its own line: `"gate freeze",` and `"conductor gate lint",`. In
  `skills/start/SKILL.md` step 3, add the lint step BEFORE the freeze: run
  `conductor gate lint` and treat any finding as fail-closed — fix the assertion tests via
  `/conductor:assertions-to-tests` (or the manifest command form), NEVER weaken the lint or
  skip it; only a clean lint proceeds to `conductor gate freeze`. (The skill already says
  `gate freeze`, so the a8 needle holds today — this task makes it enforced.) Run
  `pytest tests/conductor/test_skill_outputs.py -v` and the manifest command for
  `a8-gate-freeze-needle-present` → PASS. Quality gate. Commit.

---

## Phase 5 — Single-sourced identifiers: run-branch name + default-branch (a10-default-branch-never-empty, a11-run-branch-name-deterministic, a12-skills-call-the-resolvers)

**Spec:** §"Phase 5 — Single-sourced identifiers: `run-branch name` + `default-branch` (review B-5)"
— mirror the `conductor remote` precedent: one implementation per cross-skill string contract.

**Files:**
- Create: `conductor/branches.py`
- Modify: `bin/conductor` (dispatch `run-branch` and `default-branch` + usage),
  `conductor/merge_gate.py` (`topology-off:no-run_branch` line),
  `skills/start/SKILL.md` + `skills/autodev/SKILL.md` (call the resolvers),
  `README.md` (CLI reference rows)
- Test: `tests/conductor/test_branches.py` (create), `tests/conductor/test_merge_gate.py` (extend)

**Interfaces (pinned by frozen A10/A11/A12):**
- `conductor run-branch name <spec>` → exactly one line `conductor/run-<slug>` matching
  `conductor/run-[a-z0-9][a-z0-9._-]*`, byte-identical across invocations, slug carries the
  spec filename's stem, different specs → different names.
- `conductor default-branch` → exactly one non-empty line; resolvable repo → its actual
  default (the frozen fixture uses `trunk`, so `echo main` hard-coding fails); resolution
  failure → exactly `main`, exit 0 (fail-open, NEVER empty).

- [ ] **Task 5.1 — `conductor/branches.py` resolvers (TDD).** Read
  `test_a10_default_branch.py` and `test_a11_run_branch_name.py` first. Failing unit tests in
  `tests/conductor/test_branches.py`, then implement:

  ```python
  def run_branch_name(spec_path: str) -> str:
      stem = pathlib.PurePath(spec_path).stem.lower()
      slug = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-.")
      if not slug or not re.match(r"[a-z0-9]", slug):
          slug = "spec-" + hashlib.sha256(spec_path.encode()).hexdigest()[:8]
      return f"conductor/run-{slug}"

  def default_branch() -> str:
      # 1) gh repo view --json defaultBranchRef --jq .defaultBranchRef.name (timeout, may fail)
      # 2) git symbolic-ref refs/remotes/<remote>/HEAD, remote via conductor.remote's
      #    resolver (fall back "origin"); strip the refs/remotes/<remote>/ prefix
      # 3) ANY failure or empty result -> "main"  (fail-open; NEVER return "")
  ```

  Pure function + subprocess fallbacks, every exception path lands on `"main"`. `main(argv)`
  handles `name <spec>` and `default` verbs; `bin/conductor` dispatch:

  ```bash
  run-branch) shift; [ "${1:-}" = "name" ] || { echo "usage: conductor run-branch name <spec.md>" >&2; exit 64; }; shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.branches name "$@" ;;
  default-branch) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.branches default "$@" ;;
  ```

  Unit tests: determinism, format regex, distinctness, weird stems (spaces, unicode, leading
  dots), gh-failure + no-origin-HEAD → `main`, resolvable `trunk` fixture (stub `gh` on PATH
  as in A10 — never the network). Run the manifest commands for
  `a10-default-branch-never-empty` and `a11-run-branch-name-deterministic` → PASS. Quality
  gate. Commit.

- [ ] **Task 5.2 — skills call the resolvers (TDD against frozen A12).** Read
  `test_a12_skills_call_resolvers.py` first. In `skills/start/SKILL.md` step 5b: replace
  "compute `conductor/run-<spec-slug>` from THIS spec's filename" with
  `RB="$(conductor run-branch name <spec>)"` then
  `git ls-remote "$(conductor remote)" "refs/heads/$RB"`; replace "create off the default
  branch" with `conductor default-branch` as the base source. In `skills/autodev/SKILL.md`
  step 1: replace "recompute the EXACT name `conductor/run-<spec-slug>` from the goal's spec
  path" with the `conductor run-branch name` invocation; step 1b/3a: `<default>` comes from
  `D="$(conductor default-branch)"`. Both skills must contain the literal invocations
  `conductor run-branch name` and `conductor default-branch`, and the banned prose substring
  `` `conductor/run-<spec-slug>` from `` must be GONE from both. Preserve every existing
  contract needle (`conductor/run-` may still appear as the output format; `run_branch`,
  `base-mismatch`, `keep the run branch current`, etc. stay). Run
  `pytest tests/conductor/test_skill_outputs.py -v` and the manifest command for
  `a12-skills-call-the-resolvers` → PASS. Quality gate. Commit.

- [ ] **Task 5.3 — merge-gate `topology-off:no-run_branch` line (review-verified — no frozen
  assertion; spec: "instead of silently disabling the base leg").** In
  `conductor/merge_gate.py`, where `_expected_base()` returns `None` because the
  `.conductor/run_branch` file is absent (and `CONDUCTOR_RUN_BRANCH` unset), emit one
  informational line `topology-off:no-run_branch` (stderr) — NOT a blocker: 0.4.x direct-merge
  runs keep working, the silence is what changes. Unit test in
  `tests/conductor/test_merge_gate.py`: gate run without a run_branch file emits the line and
  the base leg stays disabled; with the file present the line is absent. Update README's CLI
  reference table with `conductor run-branch name <spec>` and `conductor default-branch`
  rows. Quality gate. Commit.

---

## Phase 6 — conductor driver install|status (a13-driver-status-nonzero-without-driver, a14-driver-status-flags-failed-fires)

**Spec:** §"Phase 6 — `conductor driver install|status`: unconditional Tier-B + cron ownership
(review B-3, B-6)".

**Files:**
- Create: `conductor/driver.py`
- Modify: `conductor/resume_script.py` (shared marker helpers + `install-cron`/`uninstall-cron`
  subcommands), `bin/conductor` (dispatch `driver` + usage),
  `skills/start/SKILL.md` (step 6 uses `conductor driver install`/`status`),
  `skills/autodev/SKILL.md` (step 3b uses `uninstall-cron`), `README.md` (CLI rows)
- Test: `tests/conductor/test_driver.py` (create), `tests/conductor/test_resume_script.py` (extend)

**Interfaces (pinned by frozen A13/A14):** `conductor driver status` (project =
`CONDUCTOR_HOME`/cwd repo) exits non-zero when no durable driver exists (no
`# conductor-autodev <main-root>` crontab marker AND no `scheduled_tasks.json`) and zero when
the marker is present with a clean recent log; with a durable driver but recent
`driver-unresolved` / `fire-end rc=<non-zero>` log lines it NAMES them and exits non-zero.
The marker's `<main-root>` is `dirname` of `git rev-parse --path-format=absolute
--git-common-dir` — identical from the owner checkout and the run worktree.

- [ ] **Task 6.1 — shared cron marker: `install-cron`/`uninstall-cron` (TDD; spec: "compute the
  marker once so start (install) and autodev step 3b (removal) share one implementation and
  cannot drift" — review-verified pairing, the marker computation itself is exercised by
  frozen A13/A14).** In `conductor/resume_script.py` add:

  ```python
  def main_root(path: str) -> str:
      common = subprocess.run(["git", "-C", path, "rev-parse",
                               "--path-format=absolute", "--git-common-dir"], ...).stdout.strip()
      return os.path.dirname(common)

  def cron_marker(root: str) -> str:
      return f"# conductor-autodev {root}"
  ```

  New argparse subcommands: `install-cron --project <path>` — read `crontab -l` (absent
  crontab = empty), drop any line already carrying this project's exact marker (fixed-string
  match — idempotent), append one `@reboot sleep 30 && <root>/.conductor/resume-autodev.sh
  <marker>` line and one `*/20 * * * * <root>/.conductor/resume-autodev.sh <marker>` line,
  write back via `crontab -`. `uninstall-cron --project <path>` — filter out exactly the lines
  containing this project's marker (the `grep -F -v --` semantics, in Python) and write back;
  removal matches install BECAUSE both call `cron_marker(main_root(...))` — one
  implementation. Unit tests in `tests/conductor/test_resume_script.py` use a **stub
  `crontab` on PATH** (as in the frozen A13 fixture) recording its stdin to a file — the
  machine's real crontab is NEVER touched. Assert install→uninstall round-trips to the
  original crontab, install is idempotent, and unrelated lines survive. Quality gate. Commit.

- [ ] **Task 6.2 — `conductor driver status` (TDD).** Read `test_a13_driver_status_durable.py`
  and `test_a14_driver_status_failed_fires.py` first. Create `conductor/driver.py`:
  `status(project) -> int` — compute `root = resume_script.main_root(project)`; durable =
  `resume_script.cron_marker(root)` appears in `crontab -l` output (rc≠0/empty = no crontab)
  OR a harness `scheduled_tasks.json` entry matches THIS project (an entry whose prompt is
  `/conductor:autodev` AND whose cwd/project field points at `root` — the file merely
  EXISTING is not durability evidence: a stale or unrelated scheduled task would false-green
  the health signal; unmatchable/unparseable file → not durable, fail-closed). No durable driver → print why, exit 1. Durable → tail
  `<root>/.conductor/resume-autodev.log`: RECENT lines = leading ISO timestamp within
  `CONDUCTOR_DRIVER_RECENT_HOURS` (default 24; a line whose timestamp cannot be parsed counts
  as recent — fail-closed toward reporting). Failures = recent lines containing
  `driver-unresolved` or matching `fire-end rc=<n>` with n≠0; any → print each offending line
  verbatim (the frozen test requires the failures NAMED, not just counted) and exit 1; else
  print `driver: durable (<marker leg>), recent fires clean` and exit 0. No log file at all
  with a durable marker = healthy (a driver with no fires yet), exit 0. `install(project,
  worktree) -> int` — the fail-closed default, no durability judgment call: run the
  `resume-script write --out <root>/.conductor/resume-autodev.sh` path (respecting its
  inline-owner-env no-clobber guard) then `install_cron`. `bin/conductor`:

  ```bash
  driver) shift; PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m conductor.driver "$@" ;;
  ```

  plus usage lines for `conductor driver {install --worktree <path>|status}` and
  `conductor resume-script {install-cron|uninstall-cron} --project <root>`. Unit tests in
  `tests/conductor/test_driver.py` mirror the frozen fixtures (stub crontab, generated-now
  timestamps, clean vs failing tails, marker-path mismatch → not durable). Run the manifest
  commands for `a13-driver-status-nonzero-without-driver` and
  `a14-driver-status-flags-failed-fires` → PASS. Quality gate. Commit.

- [ ] **Task 6.3 — skills own the wiring through the tested surface (review-verified — no frozen
  assertion).** `skills/start/SKILL.md` step 6: for an unattended run, replace the
  judgment-gated Tier-B installation ("if the response does NOT confirm persistence…") with
  the fail-closed default — always `conductor driver install` (script + crontab lines via
  `install-cron`; the marker is computed by the CLI, never derived in prose) and then
  `conductor driver status` to verify durability + surface recent failed fires (this replaces
  the prose log-tail instruction with the tested command). `skills/autodev/SKILL.md` step 3b:
  the terminal crontab removal becomes `conductor resume-script uninstall-cron --project
  <main-root>` — the SAME marker implementation as install, so removal cannot drift; keep the
  existing explanatory text about the literal `# conductor-autodev <main-root>` marker and its
  `grep -F -v --` fixed-string semantics (contract needles `# conductor-autodev` and
  `grep -f -v` must keep passing; the CLI now implements what the prose describes). Keep
  "removal is the ONLY sanctioned mutation of run infrastructure". Update README (Tier-B
  paragraph + CLI table) to name `conductor driver install|status`. Run
  `pytest tests/conductor/test_skill_outputs.py -v` → PASS. Full quality gate; then run the
  ENTIRE self-enforcement gate — every `a1…a16` manifest command — and confirm all sixteen
  are green. Commit.

---

## Self-Review (spec coverage)

- Spec Phase 1 checkboxes → Tasks 1.1–1.4 (preview = 1.2; warning/ack + dry-run choice = 1.4,
  review-verified; 0600 + driver refusal = 1.1/1.3). Fail-closed posture invariant = 1.1.
- Spec Phase 2 checkboxes → Tasks 2.1 (README section), 2.3 (no grant leftovers), 2.2
  (canonical spelling, review-verified).
- Spec Phase 3 checkboxes → Task 3.1 (posture label), 3.2 (nudge split + regate,
  review-verified).
- Spec Phase 4 checkboxes → Tasks 4.1 (unpinned), 4.2 (negative + trivially-true), 4.4
  (freeze needle + lint-before-freeze in start), 4.3 (freeze covers `.assertions.md`).
- Spec Phase 5 checkboxes → Tasks 5.1 (resolvers), 5.2 (skills call them), 5.3
  (`topology-off:no-run_branch`, review-verified).
- Spec Phase 6 checkboxes → Tasks 6.1 (identical marker install/removal), 6.2 (status
  non-zero/zero + failed-fire reporting), 6.3 (skill wiring, review-verified).
- Out of scope (deliberate, per spec): B-7 run-infra digest guard, A-7 bypass disarm on
  completion.
