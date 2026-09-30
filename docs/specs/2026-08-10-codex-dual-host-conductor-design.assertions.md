# Executable assertions — Dual-host Conductor for Claude Code and Codex

Source spec: `docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md`
Written by `/spec-craft:executable-assertions` (spec-craft 0.2.1) on 2026-08-17.

These are 4-part assertion **specs**, not test code. A downstream `/conductor:assertions-to-tests`
step turns each into one runnable test wired into `assertions/<run-key>/manifest.yaml` by id.

**Every assertion below runs headlessly.** None requires a Claude or Codex account, a network call,
or an installed second host. See §0 for why that is a deliberate boundary and not a gap.

---

## 0. Why nothing here needs a live host

The spec's central claim is that Conductor runs on both hosts. The tempting way to gate that is to
launch a real Codex and a real Claude and watch them work. That is the wrong gate, for a reason that
decides whether the gate is usable at all:

An assertion that cannot execute in the run's own environment has exactly two outcomes. It is
permanently red — and an autonomous run parks forever on a condition it structurally cannot satisfy
— or someone marks it skipped, and the gate reports green on a check that never ran. Neither is a
definition of done.

So the boundary drawn here is: **the gate proves the contract, a human release checklist proves the
product.** Operationally, "runs on both hosts" means Conductor constructs a correct invocation for
whichever host the run records, dispatches it as a real argument vector, and never reaches for the
other host. Recording fake `claude`, `codex`, and `gh` executables prove exactly that, and prove it
*better* than a live host does — a live run shows the command worked once, while a fake captures the
precise argv, environment, and prompt bytes, so a regression is caught by content rather than by
luck. This is also what the spec itself already prescribes in §"Integration tests" ("fake Claude,
Codex, and `gh` executables verify exact arguments, environment, prompts…").

What genuinely needs a live host is listed in §2 and belongs to the release checklist.

## 1. Load-bearing expectations — encoded

- **A-DH-1 — host vocabulary is confined to the adapter layer.** (M1, F3) The structural precondition
  for every other dual-host claim. If host syntax stays in the shared core, the Codex path is a
  facade and every downstream check is testing the Claude path twice.
- **A-DH-2 — the worker launch targets the run's recorded host and only that host.** (S2, F1, F3, M3)
  The central claim, made headless. Today this is red by construction: the fire hardcodes
  `"$CLAUDE_BIN" -p "/conductor:autodev"`.
- **A-DH-3 — the Codex worker launch resolves using only artifacts Conductor installs.** (S2) The
  ground-truth finding with the quietest failure: `$conductor:autodev` is an unowned third-party
  convention, so a launch that depends on it works on the author's machine and silently does nothing
  on a stock Codex install.
- **A-DH-4 — every host invocation is time-bounded and reports on expiry.** (F2) A hang is not a
  failure: nothing errors, nothing reports, and an unattended fire leaves a stuck owner instead of a
  recoverable one. `codex --help` hanging without stdin redirection is the known instance.
- **A-DH-5 — relocation refuses while any run artifact lives under the old checkout.** (M2) Highest
  unrecoverable damage in the spec. Absolute linked-worktree metadata does not survive the rename, so
  the loss is discovered late.
- **A-DH-6 — unresolvable default-branch metadata refuses every automated merge.** (AC "Default-branch
  resolution failure blocks automation") A deliberate inversion of current behavior:
  `conductor/branches.py:91` fails **open** to `main` today. Fail-open plus an irreversible action is
  the worst pairing in the spec.
- **A-DH-7 — Conductor never completes the final default-branch pull request.** (AC "Conductor can
  open but cannot retarget, auto-enroll, close, or complete") The one invariant whose violation cannot
  be undone by Conductor, by the owner, or by a later fire.

## 2. Deliberately NOT encoded

**Requires a live host account — release checklist, not the run's gate:**

- **Marketplace installation on both hosts, and spec-craft arriving by default** (AC 1–2). Needs a
  real marketplace, a real network, and an isolated test user. The spec already assigns these to
  §"Installation smoke tests" run by a release owner; duplicating them as gate assertions would park
  the run permanently.
- **Fresh sessions discovering the native skill names** (AC 3). Same reason. A-DH-3 covers the part
  that is a Conductor contract (the launch depends only on artifacts Conductor installs); whether a
  live host's skill loader then honors it is the host's behavior, not Conductor's.
- **The Codex `PreCompact` hook contract probe.** The ground-truth review lists this contract as
  explicitly unverified. Asserting a contract nobody has confirmed exists would freeze an assumption,
  and the assertion could be permanently red for a reason outside this repository.
- **The end-to-end takeover matrix** (Claude↔Codex mid-run). Needs two live hosts, two subscriptions,
  and wall-clock lease expiry. The mechanical core — that ownership is a compare-and-swap over
  `owner.lock` — is testable headlessly, but it belongs to Plan 02's own assertions, not to this
  spec-level gate.

**Not machine-provable:**

- **S3 (spec-craft produces the same written result on both hosts).** spec-craft's output is
  LLM-generated prose; two runs are not byte-identical even on one host, so "same result" has no
  Boolean form. The checkable residue — that input resolution is host-neutral — lives in SKILL.md
  prose interpreted by the model, not in code with an exit code. **Flagged: verified by human
  review.**
- **M4 (marketplace wording must not regress Bubo to Claude-only).** Checkable in principle, but the
  artifact lives in the `automateintelligence/marketplace` repository, outside anything this run's
  gate can see. Belongs to Plan 10's release checklist.

**Folded into another assertion rather than duplicated:**

- **M3 (Codex native session resume is not a continuation mechanism).** Not a standalone assertion —
  it is a must-not-contain clause inside A-DH-2's argv observation, where the recorded argument vector
  is already under inspection. A separate assertion would need its own launch harness to check one
  absent token.
- **F3 (dual-host in name only).** Its stated observable symptom — behavior changing when the Claude
  executable is removed — is precisely what A-DH-1 and A-DH-2 measure. Encoding it separately would
  restate them.
- **S1 (single installed host is enough to work) — not encoded at all.** Its first half, whether
  `start` succeeds with only one host installed, is genuinely undetermined: §"Reviewer routing" and
  §"Failure handling" support opposite readings, which the spec's own §"Open questions" now records.
  Encoding either reading would settle the question by gate. Its second half — an unavailable opposite
  host must never yield a merged phase — *is* unambiguous and load-bearing, but it is Plan 07's
  reviewer-routing contract and belongs to that plan's own assertions; folding it into A-DH-7 would
  have given that assertion two Booleans and made the final-PR invariant fail for review-policy
  reasons. **Flagged: the owner must settle the open question before Plan 04, and Plan 07 must carry
  the no-merge-without-opposite-review assertion.**

**Not load-bearing enough:**

- **The 0.60 per-fire context budget default.** A tuning value, not a boundary. The load-bearing part
  (a checkpoint is pushed rather than compacted through) is Plan 05's contract.
- **Run-key derivation, generation suffixes, lock ordering.** Real invariants, but they are Plan 01's,
  and Plan 01 is written and carries its own interfaces. Restating them here would create two owners
  for one check.

## 3. The 4-part specs

### A-DH-1 — host vocabulary is confined to the adapter layer

- **Claim.** No Python module outside the host-adapter layer contains a Claude slash-command
  invocation, a Codex dollar-prefixed skill invocation, `CLAUDE_PLUGIN_ROOT`, or a host-specific
  permission or sandbox flag string.
- **Setup.** The installed Conductor package tree. Two disjoint file sets derived from the package
  itself, not from a hand-maintained list: the **adapter set** (the modules the adapter loader
  resolves, plus the adapter package they live in) and the **core set** (every other Python module in
  the package). Documentation, specs, tests, and this assertions file are outside both sets. The
  token list is the spec's own enumeration, in two subsets — **Claude-specific:** `/conductor:`,
  `/spec-craft:`, `CLAUDE_PLUGIN_ROOT`, `--dangerously-skip-permissions`; **Codex-specific:**
  `$conductor:`, `$spec-craft:`, `--dangerously-bypass-approvals-and-sandbox`, `--sandbox`. Bare
  executable names are deliberately excluded from this static scan — they are too ambiguous to match
  reliably, and A-DH-2 catches wrong-host spawning behaviorally instead.
- **Observation.** Scan both sets for the token list. **Must-contain:** at least one token from the
  Claude-specific subset and at least one from the Codex-specific subset *within the adapter set* —
  this proves the scan is looking at a populated tree, so an empty or missing package cannot pass
  vacuously. This anti-stub clause is satisfied by the permission/sandbox flags alone, so it stays
  valid whichever way the plan writer settles Codex's skill-invocation syntax. **Must-not-contain:**
  any token from either subset in any core-set module. Fail = a token found in the core set, OR the
  adapter set yielding no Claude-subset token, OR the adapter set yielding no Codex-subset token.
- **Kind.** property (must hold across every core module and every token, not one sampled pair).

### A-DH-2 — the worker launch targets the run's recorded host and only that host

- **Claim.** For a run whose recorded worker host is H, the fire spawns H's executable exactly once
  with a real argument vector, and spawns the opposite host's executable zero times.
- **Setup.** A temporary project with two runs identical except for the recorded worker host — one
  `claude`, one `codex`. `PATH` contains recording fakes for both executables; each fake appends its
  full raw `argv`, working directory, and environment to a distinct log and exits 0. The worker prompt
  fixture deliberately contains shell-hostile bytes: a `$`-prefixed token, a backtick pair, a
  semicolon, an embedded double quote, and a newline.
- **Observation.** Fire each run once and read both logs. **Must-contain:** for the `codex` run,
  exactly one record in the codex log whose `argv[1]` is `exec`; for the `claude` run, exactly one
  record in the claude log. In both cases the prompt must appear as a **single discrete argv element
  (or on stdin) byte-identical to the fixture**, including the `$` token, backticks, semicolon, quote,
  and newline — any expansion, stripping, word-splitting, or requoting proves the launch went through
  a shell string rather than an argument vector. **Must-not-contain:** any record at all in the
  opposite host's log for either run; any `argv` element among the codex records equal to `resume`,
  `--last`, or `fork` (Codex native session continuation must not become the continuation mechanism —
  every fire is a cold start reconciled from durable state). Fail = a wrong-host invocation, a mangled
  prompt, a session-continuation flag, or a count other than exactly one per run.

  Note the byte-identity requirement is also what proves the launch used an argument vector rather
  than an interpolated shell string: a shell would expand the `$` token, split on the semicolon, and
  consume the backticks. No separate check for `/bin/sh` in the spawn chain is needed, and none is
  specified — inspecting the spawn chain is platform-specific and brittle where this is neither.
- **Kind.** property (the invariant must separate both host directions; a single-host case passes
  while the inversion is broken, which is exactly today's failure mode).

### A-DH-3 — the Codex worker launch resolves using only artifacts Conductor installs

- **Claim.** Every artifact the Codex worker launch depends on in order to resolve Conductor's autodev
  skill was written by Conductor itself, so the launch does not depend on any pre-existing third-party
  convention.
- **Setup.** A temporary Conductor package root at a path chosen at test time (not a fixed location),
  a recording fake `codex`, and a scratch `HOME` and `CODEX_HOME` that start **empty** — no
  `AGENTS.md`, no `skills/`, no dispatch convention of any kind — snapshotted before the fire so that
  anything present afterward is provably Conductor's own write.
- **Observation.** Fire a Codex-owned run. Collect the prompt bytes the fake recorded together with
  every file Conductor created under the scratch `HOME`/`CODEX_HOME` and package root.
  **Must-contain:** a resolution artifact — either a filesystem path named in the prompt, or a
  dispatch/convention file Conductor installed that the prompt's token resolves through — which (a)
  exists and is readable, (b) is absent from the pre-fire snapshot or lives under the package root,
  i.e. Conductor wrote or shipped it, and (c) resolves to Conductor's autodev skill, identified by
  `SKILL.md` frontmatter rather than by filename. Re-run with the package root relocated to a second
  test-chosen path: the resolution artifact must **follow the relocation**, which defeats a hardcoded
  constant that happens to exist. **Must-not-contain:** any dependency on a file under the scratch
  `HOME`/`CODEX_HOME` that Conductor did not write; a fire that resolves only because such a file was
  pre-seeded. Fail = no traceable resolution artifact, an artifact that does not track the package
  root, one resolving to something other than autodev, or a launch that works only when a foreign
  convention file is present.

  **This assertion is deliberately mechanism-neutral.** The ground-truth review *recommends* emitting
  an explicit `SKILL.md` path instead of a bare `$conductor:autodev` token, but records that as a
  recommendation the plan writer owns, not a decision. Asserting the path form specifically would
  settle that question by gate. Either form passes here; what fails is depending on something
  Conductor did not install.
- **Kind.** contract (a postcondition on the adapter's launch construction).

### A-DH-4 — every host invocation is time-bounded and reports on expiry

- **Claim.** Every Conductor-initiated host subprocess — version probe, preflight, capability check,
  worker launch, reviewer launch — terminates within its configured timeout and, when the child never
  exits on its own, Conductor kills it and returns a non-zero actionable failure.
- **Setup.** Two interchangeable sets of fake `claude` and `codex` executables on `PATH`: a **hanging**
  set that blocks on a read from stdin indefinitely and ignores termination-by-politeness, and a
  **responsive** set that answers immediately with well-formed output. A short configured timeout so
  the check is fast. The full list of Conductor entry points that spawn a host process, enumerated
  from the adapter interface rather than hand-listed.
- **Observation.** Run every entry point against both sets. **Must-contain:** against the hanging set,
  every entry point returns within its timeout plus a fixed margin, with a non-zero exit and a failure
  report naming the run key and current state, the failed operation, whether any write occurred, and
  an exact recovery command; and no descendant of the fake remains alive afterward. **Must-contain
  (anti-stub):** against the responsive set, every one of those same entry points returns within the
  same bound **without reporting a timeout** — this prevents "always time out immediately" from
  satisfying the check. It is deliberately not "completes successfully": an entry point may still fail
  against a responsive fake for unrelated state reasons, and requiring success would make this
  assertion red for causes it does not govern. **Must-not-contain:** any entry point still running
  after the bound; any entry point exiting zero against the hanging set; any orphaned fake process.
  Fail = an unbounded entry point, a hang reported as success, a leaked child, or a timeout reported
  against the responsive set.
- **Kind.** property (must hold across every host-invoking entry point and across both fake sets; one
  bounded call proves nothing about the others).

### A-DH-5 — relocation refuses while any run artifact lives under the old checkout

- **Claim.** The relocation safety check refuses, before mutating anything, when a live process, a
  registered linked worktree, or an installed schedule resolves beneath the checkout being relocated.
- **Setup.** Fixture checkouts, each a self-contained temporary directory — **no test performs or
  simulates a relocation of a real Conductor installation.** Four variants: (a) a run with a
  registered linked worktree nested under the checkout; (b) a run whose owner record names a
  process identity that is currently alive; (c) a run with an installed schedule entry whose launcher
  path is under the checkout; and (d) a control with none of the three, all runs checkpointed and no
  live artifacts. A recorded byte-level manifest of each fixture taken before the check runs.
- **Observation.** Run the relocation safety check against each variant. **Must-contain:** for (a),
  (b), and (c) independently, a non-zero exit and a report naming the specific blocking artifact and
  its path plus an exact recovery command; and the post-check fixture manifest **byte-identical** to
  the pre-check manifest, including that the old directory still exists under its original name and
  every worktree registration is unchanged. **Must-contain (anti-stub):** for the control (d), the
  check reports clear-to-proceed — otherwise a check that refuses unconditionally passes while
  proving nothing. **Must-not-contain:** for any blocked variant, any rename, any partial move, any
  modified worktree registration, or a report that names a blocker generically without its path. Fail
  = any single blocker failing to block on its own, any mutation under a blocked variant, or the
  control being refused.
- **Kind.** property (each of the three blockers must be independently sufficient; a check that only
  catches the combination lets the single-blocker case through, which is the realistic case).

### A-DH-6 — unresolvable default-branch metadata refuses every automated merge

- **Claim.** When the repository's default branch cannot be resolved from authoritative remote
  metadata, every automated merge is refused, and no code path substitutes a literal fallback branch
  name.
- **Setup.** A temporary repository with a bare remote and a fake `gh`, in two configurations:
  **unresolvable** — the remote's default-branch metadata is absent and the `gh` fake returns a
  failure for the default-branch query — and **resolvable** — the same repository whose authoritative
  default branch is deliberately named something other than `main` or `master`. A phase pull request
  otherwise fully eligible to merge: tests passing, gate frozen and verified, review passing at the
  current head, base equal to the run's integration branch.
- **Observation.** Attempt the automated merge in both configurations. **Must-contain:** in the
  unresolvable configuration, a non-zero exit, a refusal naming unresolved default-branch metadata,
  and the pull request still open and unmerged. **Must-contain (anti-stub):** in the resolvable
  configuration, the same otherwise-eligible merge **succeeds** — this proves the refusal is caused by
  the unresolved metadata and not by a merge path that is simply broken or unreachable.
  **Must-not-contain:** in the unresolvable configuration, any recorded `gh` merge call, any git merge
  commit, or the literal strings `main` or `master` appearing **as the resolved default-branch value**
  in the merge decision or in any state write. Scoped to the resolved value specifically, not to log
  text in general — run and phase branch names legitimately contain those words, and a broader match
  would go red for a reason this assertion does not govern. Fail = the merge proceeding, a literal
  fallback being used as the resolved value, or the resolvable case failing.
- **Kind.** property (the invariant is the separation between the two configurations; checking only
  the refusal is satisfied by a merge path that never works).

### A-DH-7 — Conductor never completes the final default-branch pull request

- **Claim.** No Conductor code path performs, requests, or enables any action that could cause the
  final integration-to-default pull request to complete.
- **Setup.** A temporary project with a fake `gh` recording every invocation, and a run driven to
  `awaiting-team-merge` with the final pull request open. Then every Conductor CLI verb that touches
  Git or GitHub state — enumerated from the CLI's own registered verb list, not hand-listed — is
  invoked against that run, including `merge`, `resume`, `finish`, `heartbeat`, `status`, and `gate`.
- **Observation.** Read the recorded `gh` log. **Must-not-contain:** any invocation targeting the
  final pull request's number that merges, squashes, rebases, force-pushes, closes, mutates its base,
  enables auto-merge, or enrolls it in a merge queue — the spec's enumerated prohibition list.
  **Must-contain (anti-stub):** at least one recorded `gh` call that merges a *phase* pull request
  into its run integration branch during the sweep — without this, a Conductor that makes no GitHub
  calls at all, or whose merge path is unreachable, satisfies the prohibition vacuously.
  **Must-contain:** a `finish` attempt on the unmerged final pull request exiting non-zero and
  printing its URL and current state. Fail = any prohibited action recorded, no permitted phase merge
  recorded, or `finish` succeeding while the final pull request is unmerged.
- **Kind.** property (must hold across every CLI verb and every prohibited action, not one sampled
  command).

## 4. Known residual

The spec's final-PR prohibition ends with "or any equivalent action." A-DH-7 checks the eight
enumerated actions, because a gate can only check a finite list. An unlisted GitHub capability that
completes a pull request by another route would pass this assertion. The spec's §"Open questions"
records this; closing it requires either declaring the enumeration exhaustive or defining how an
equivalent action is recognized. **Flagged: not closable by assertion alone.**
