# Dual-host Conductor for Claude Code and Codex

**Date:** 2026-08-10

**Status:** Approved design

**Repositories:** automateintelligence/conductor, automateintelligence/spec-craft, automateintelligence/marketplace

## Summary

Conductor will become a host-neutral orchestration system with thin Claude Code and Codex adapters. A run may be started, resumed, or taken over by either host without changing its identity or losing its state. Both hosts will use the same project-local run files, branches, worktrees, assertions, GitHub issues, and pull requests.

The host that owns a run performs orchestration and delegates product implementation to fresh subagents. The opposite host reviews phase pull requests by default. An operating-system heartbeat starts a fresh host process for each fire and invokes the host's native Conductor autodev skill. Conductor checkpoints, commits, pushes, writes a handoff, and exits before context compaction.

Conductor may merge a gated phase pull request into the run's integration branch. It may open the final integration-branch pull request to the repository's default branch, but it must never merge, squash, rebase, force-push, close, or otherwise complete that final pull request. Only the repository team may merge to the default branch.

The canonical editable Conductor checkout will relocate from ~/.claude/conductor to ~/programming/conductor. Claude and Codex plugin packages remain installation artifacts rather than editable source.

## Goals

- Make Conductor a first-class plugin for both Claude Code and Codex.
- Preserve one public plugin identity: conductor in the AutomateIntelligence marketplace.
- Let either host resume the same run at any phase or checkpoint.
- Support multiple concurrent or intermittent specs in one repository without state collisions.
- Use Claude as the default reviewer for Codex-owned runs and Codex as the default reviewer for Claude-owned runs.
- Keep the orchestrator's context focused on reconciliation, delegation, gates, review routing, checkpointing, and handoff.
- Prevent context compaction from becoming the continuation mechanism.
- Preserve existing in-flight Claude runs, frozen gates, branches, worktrees, and schedules during migration.
- Keep spec-craft standalone while making its plugin and skills native on both hosts.
- Validate ordinary marketplace installation on both hosts, including automatic spec-craft availability.
- Make every public description and installation guide accurately describe Claude Code and Codex support.

## Non-goals

- Building a permanent Conductor daemon or cloud control plane.
- Sharing state between unrelated repositories.
- Coordinating simultaneously active workers across different machines or replicated copies of project-local state. Cross-host takeover means Claude Code and Codex using the same canonical project state root on one workstation.
- Allowing two worker owners to execute the same run simultaneously.
- Continuing product implementation inside the orchestration context.
- Treating compaction as a successful checkpoint.
- Allowing Conductor to merge a pull request into the repository default branch.
- Renaming the conductor plugin or creating a second Codex-specific product identity.
- Making Bubo depend on Conductor or spec-craft.

## Approaches considered

### Selected: host-neutral core with thin adapters

The shared core owns state, reconciliation, branches, gates, leases, GitHub operations, migration, heartbeat behavior, and safety invariants. Claude and Codex adapters own only the runtime details that genuinely differ: executable discovery, invocation syntax, prompt launch, permissions, hooks, process inspection, and reviewer launch.

This preserves one behavior model and makes a cross-host takeover a state transition rather than a migration between implementations.

### Rejected: duplicate Claude and Codex workflows

Maintaining separate skills and state paths would make initial support faster but would create two implementations of every gate and safety rule. They would drift, and a mid-run takeover would require translating host-specific state.

### Rejected: standalone daemon

A daemon could hide host differences, but it would introduce installation, lifecycle, security, and recovery complexity that the current local-first product does not need. An OS heartbeat plus short-lived host processes provides the required durability with less machinery.

## System architecture

The canonical source layout is:

    conductor/
    ├── .claude-plugin/plugin.json
    ├── .codex-plugin/plugin.json
    ├── bin/conductor
    ├── conductor/
    │   ├── core/
    │   ├── hosts/
    │   │   ├── base.py
    │   │   ├── claude.py
    │   │   └── codex.py
    │   ├── reviewers/
    │   ├── heartbeat/
    │   └── migration/
    ├── skills/
    └── tests/

The existing Python modules move behind stable core interfaces incrementally; this design does not require a disruptive all-at-once directory rewrite. The core must not embed Claude slash commands, Codex dollar invocations, CLAUDE_PLUGIN_ROOT, host-specific permission flags, or assumptions about one installation directory.

The adapter interface provides:

- host identifier and executable discovery
- plugin/source-root discovery
- worker prompt construction and process launch
- reviewer prompt construction and process launch
- preflight and version checks
- least-privilege permission profile validation
- process identity and liveness checks
- hook installation and hook-event translation
- native invocation rendering for user-facing diagnostics
- implementation-subagent construction and dispatch into a fresh, isolated context
- bounded structured result collection from implementation subagents

Adapters launch argument vectors directly rather than interpolating a shell command. In particular, Codex's dollar-prefixed skill invocation is passed as literal prompt text and cannot be expanded as an environment variable.

Claude uses its native subagent primitive. Codex uses a native subagent when available and enabled, otherwise a fresh non-interactive Codex child process. Both mechanisms provide a context isolated from the orchestrator and return a bounded summary. An adapter that cannot dispatch isolated implementation work fails preflight; the orchestrator never absorbs product implementation as fallback.

Claude exposes /conductor:* and /spec-craft:* skills. Codex exposes $conductor:* and $spec-craft:* skills. Both surfaces call the same CLI and core APIs.

Installed plugin caches are immutable release artifacts. The editable repository at ~/programming/conductor is never discovered implicitly by a run and is never required after installation.

## Project and run identity

Every project has one registry:

    <project>/.conductor/project.json

project.json records the state schema, its own monotonic revision, stable repository identity, normalized-spec-path-to-run-key mappings, and the workstation identity that owns this project-local state. Each spec-path mapping is an ordered generation list with at most one nonterminal run designated current; starting a path whose latest generation is terminal requires explicit --new-run. The workstation identity is a random Conductor installation ID stored in host-neutral user configuration and shared by both adapters; it is not derived from personal or hardware data. Sequential transfer to another workstation requires the explicit operator-attested rebind described under ownership; Conductor does not coordinate active workers across workstations.

Every spec execution has a deterministic run key:

    <spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]

The relative-path hash prevents equal filenames in different directories from colliding and remains stable when a repository or worktree moves. Generation 1 omits the -g<N> component; generation 2 and later use -g2, -g3, and so on. The suffix is part of every later run key, so branches, worktrees, gate directories, and run directories are generation-distinct. run.json stores the numeric generation redundantly alongside the stable repository-relative identity and current absolute path. A new generation requires explicit --new-run and never overwrites history.

Project-local state is:

    <project>/.conductor/
    ├── project.json
    ├── project.lock
    ├── migration.lock
    ├── migrations/
    ├── transactions/
    └── runs/
        └── <run-key>/
            ├── run.json
            ├── goal.md
            ├── handoff.md
            ├── resume-env
            ├── heartbeat.json
            ├── heartbeat.sh
            ├── heartbeat.log
            ├── compaction.marker
            ├── owner.lock
            └── state.lock

Run-owned repository artifacts are:

    <project>/.worktrees/conductor/<run-key>/integration
    <project>/.worktrees/conductor/<run-key>/phases/<phase-id>
    <project>/assertions/<run-key>/

The integration worktree tracks the run integration branch. Each implementation unit executes on a phase branch in its own dedicated worktree. Worktree paths are organizational; run.json is authoritative.

Conductor resolves one canonical state root from the repository's Git common directory and primary worktree registration. Starting from a linked worktree still finds that same root. Phase and integration worktrees never create their own .conductor registries; the adapter passes the canonical state root explicitly to every worker and subagent.

The run key is the single source shared by a new run's integration-branch suffix and done-gate directory. The resolver verifies this equality from run.json rather than recovering it from ambient project files.

When an invocation carries a run key, that key alone determines the goal, branch, gate directory, manifest, freeze baseline, and results path. Legacy .conductor/run_branch, legacy .conductor/goal.md, and ambient gate environment variables are ignored rather than consulted as fallback. Every CLI verb that resolves run or gate state—including gate, merge, status, heartbeat, and autodev—requires a run key when more than one active run exists and otherwise fails with the available keys and exact commands.

Before creating project-local worktrees or support state, preflight verifies that .worktrees/ and .conductor/ are not tracked by Git. If either is already tracked, it fails closed and reports the exact git rm -r --cached recovery command. Otherwise it establishes a local Git exclude and rechecks it. Assertions remain tracked run artifacts. The per-run resume-env is mode 0600 and contains no secrets.

run.json includes at least:

- schema version, run key, generation, and resolved spec path
- goal identity and frozen assertion digest
- run status and current phase
- worker host, reviewer host, and review policy
- integration branch and worktree
- active phase branch and worktree
- GitHub issue and pull-request references
- heartbeat schedule identifier and process identity
- lease owner, expiry, and last renewal
- monotonic state revision used as the compare-and-swap token
- last successful reconciliation and checkpoint
- last worker, reviewer, and review head SHA, plus a per-phase review record that retains the phase worker and reviewer hosts, phase head SHA, verdict, finding resolution, outstanding review-debt state, and required debt-reviewer host after merge
- creation, update, completion, and failure timestamps

Every run.json mutation is a read-modify-write guarded by the short-lived state mutex and state revision. A stale writer re-reads and retries rather than replacing a newer value. Atomic replace prevents torn files; the revision prevents lost updates.

Run status is exactly one of active, checkpointed, blocked, awaiting-team-merge, terminal, or failed. A run becomes failed only when a recorded invariant violation leaves no safe retry; recoverable stops use blocked. resume returns an eligible run to active. active, checkpointed, and blocked count as active for run-key disambiguation and manual autodev. Manual autodev on a blocked run reconciles and reports only; advancing it requires resume. awaiting-team-merge, terminal, and failed do not count as active. Commands that operate on an inactive run still require its explicit run key.

Repeated start for the same active run reconciles instead of creating another run. Starting it from the other host atomically transfers worker ownership only after both old process identities are proven exited and the local lease is stale. The new reviewer becomes the opposite host.

Renaming or moving a spec within the repository never silently creates a second run. conductor run repoint-spec --run <run-key> <new-relative-path> runs only without a live owner, acquires project.lock, owner.lock, then state.lock, verifies that the old and new paths describe the same Git rename or approved plan digest, rejects mapping collisions, and journals then applies the project.json and run.json updates so recovery completes or reverses both while retaining the run key and a path-history audit. If start sees an unmapped path whose content identity matches an existing run, it fails with that exact command instead of minting a run.

Every scheduled or non-interactive invocation carries an explicit run key. Manual autodev without a run key is allowed only when exactly one active run exists; otherwise it fails with the available keys and exact commands.

## Ownership, locking, and takeover

A run has exactly one worker owner within the supported canonical project state root. owner.lock is the authoritative execution-ownership lock and records the run key, host, wrapper process identity, launched host process identity, lease, and heartbeat identity so PID reuse cannot impersonate an owner. The corresponding run.json fields are a diagnostic mirror refreshed on renewal, not a second ownership authority. state.lock is a separate short-lived mutex for run.json mutation.

The default local lease duration is 120 seconds and the wrapper renews it at least every 30 seconds while its host is alive. Expiry is necessary but never sufficient for takeover: matching live process identity, an orphaned child, or ambiguous liveness still blocks. Repositories may lengthen these values but may not configure a renewal interval greater than one quarter of the lease duration.

A takeover performs one compare-and-swap state transition:

1. Enter through the run's heartbeat launcher; that wrapper acquires owner.lock exclusively and remains its sole holder through takeover and the launched host's lifetime.
2. Acquire state.lock, then reconcile Git, GitHub, worktrees, tests, and the recorded lease.
3. While both locks are held, prove both the prior wrapper and launched host exited and that the local lease is safely stale; abort and release on any doubt.
4. Update worker_host and reviewer_host together and invalidate any posted review on an unmerged phase whose reviewer host equals the new worker host; that phase requires a fresh opposite-host review. Merged phases retain their review records and any stored debt-reviewer requirement unchanged.
5. Commit that mutation against the recorded state revision, then release state.lock.
6. Replace only that run's heartbeat entry.
7. The same wrapper launches the new host from the durable handoff and records its process identity before returning control. There is no owner.lock release or transfer window. If launch fails, the wrapper records the failure under state.lock, restores the prior non-running ownership record, and then releases owner.lock.

The conductor heartbeat wrapper holds owner.lock for the lifetime of the host process it launches. The worker takes state.lock only around bounded state updates. A dead wrapper with a live launched host is an orphaned state, not a takeover-eligible stale owner. It blocks takeover until conductor heartbeat prune --run <run-key> proves that the recorded host exited. Prune acquires owner.lock then state.lock for inspection and clearing. If the identity-matched host is still live, the non-destructive prune refuses and prints an explicit conductor heartbeat prune --run <run-key> --terminate-orphan <identity-token> recovery command; that command terminates only the identity-matched orphan and proves exit before clearing the record.

A live owner causes a harmless already-running result. A conflicting, orphaned, or ambiguous state fails before mutation. No takeover translates a bypass or unsafe permission profile from one host into the other. Explicitly invoking start for an active run through the other host is operator consent to takeover; the ownership change is printed, logged, and posted to the run's durable GitHub record. A checkout whose project registry identifies a different workstation refuses automatic takeover; a GitHub marker alone never authorizes distributed takeover.

Sequential workstation transfer uses conductor project rebind --confirm-prior-workstation-quiesced <recorded-workstation-id>. It acquires project.lock, every run's owner.lock in sorted run-key order, and then every run's state.lock in the same order. It refuses while any identity-matched local owner or local schedule remains, clears stale ownership records through a project transaction, and retains the prior workstation ID in project.json history. Conductor cannot verify processes or schedules on the prior workstation; the named flag is the operator's explicit attestation that all of them are stopped. Without that attestation, rebind fails closed. Rebind installs no schedules: each transferred nonterminal run requires conductor resume --run <run-key> on the new workstation to reconcile and, when its state is work-capable, install its local heartbeat. awaiting-team-merge remains schedule-less.

## Branch, worktree, and pull-request model

The run integration branch is:

    conductor/run-<run-key>

New runs use that form. A migrated legacy-slug-v1 run retains the integration and phase branch names recorded during migration. Every branch lookup reads run.json; no operation re-derives a migrated branch from the run key.

Each phase uses a branch such as:

    conductor/<run-key>/phase-<phase-id>

Product changes must be committed and pushed from the phase worktree. A phase pull request targets the run integration branch. Conductor may merge it only after all applicable tests, frozen-gate verification, ownership checks, review checks, and head-SHA checks pass.

When all phases and the final done-gate pass, Conductor opens a pull request from the run integration branch to the repository default branch. It then records the URL and stops at awaiting-team-merge.

The merge CLI independently resolves the repository default branch from authoritative remote metadata. If the default branch cannot be resolved, every automated merge is refused; there is no fallback default.

Every automated phase merge additionally requires that the pull-request base equal this run's integration branch as recorded in run.json and re-derived from the run key or recorded legacy identity. A pull request targeting another run's branch, a stale run branch, an arbitrary feature branch, or the default branch is rejected.

The final-PR boundary prohibits automated merge, squash, rebase, force-push, close, base mutation, auto-merge enablement, merge-queue enrollment, or any equivalent action that could cause the final pull request to complete without an explicit repository-team merge.

Conductor does not directly merge or rebase the repository default branch into the run integration branch. If the default branch advances and the repository team requires the final pull request to be updated, conductor resume --run <run-key> creates a dedicated synchronization phase. It appends the plan-external phase ID sync-<n> to run.json's ordered phase IDs and the GitHub issue ledger, uses branch conductor/<run-key>/phase-sync-<n> and worktree <project>/.worktrees/conductor/<run-key>/phases/sync-<n>, and records the pre-sync integration SHA and authoritative default-branch SHA. The phase ID is also its synchronization ID. A clean merge of those exact parents is attributed to the synchronization ID rather than an implementation dispatch. Conflicting or additional product changes require an isolated implementation dispatch and normal dispatch attribution. The synchronization branch passes the normal tests and opposite-host review and enters the integration branch only through a phase pull request. After it merges, Conductor re-runs the final done-gate and records the new audited integration head before the final pull request can return to awaiting-team-merge. The final pull request itself is never retargeted or rewritten.

## Reviewer routing

The default reviewer is always the opposite host:

| Worker | Reviewer |
| --- | --- |
| Claude Code | Codex |
| Codex | Claude Code |

The reviewer receives the phase pull request, frozen assertions, relevant plan/expectations, test evidence, and exact head SHA. It returns a structured verdict containing reviewer host, verdict, findings, reviewed head SHA, timestamp, and prompt/schema version.

The core posts or updates the GitHub review artifact. The merge gate accepts it only if:

- the reviewer host matches policy
- the reviewed head SHA equals the current pull-request head
- the verdict is passing
- required findings are resolved
- the review has not expired

A review expires whenever the pull-request head changes or its configured maximum age passes; the default maximum age is 24 hours. Findings are resolved only by a later reviewer verdict that names their IDs as resolved at the current head SHA.

One transient reviewer launch failure—process spawn failure, missing reviewer executable, or timeout before a verdict—is retried once. Repository configuration exposes require_opposite_review=true by default: an unavailable opposite host blocks the phase. run.json stores the derived review_policy as exactly one of opposite-required, same-host-fallback-allowed, or blocked-pending-opposite-host.

A repository may explicitly set require_opposite_review=false to permit a clearly labeled same-host fallback in a fresh, separate reviewer process. Fallback records review debt in run.json and on the pull request, including the host required to discharge it: the host opposite the phase worker and fallback reviewer at debt creation. conductor review --run <run-key> --phase <phase-id> --discharge-debt launches that stored required debt-reviewer host against the phase head SHA recorded in run.json, including after the phase pull request is merged. It never recomputes the host from the current worker_host, which a takeover may have inverted. Only a passing verdict from the stored host for that exact recorded SHA discharges the debt. Outstanding review debt blocks the final integration-branch pull request.

## Heartbeat and autodev

Codex has no direct equivalent of Claude's managed /loop surface, so Conductor uses an operating-system scheduler for both hosts. The user-facing execution chain is:

    OS cron heartbeat
      -> conductor heartbeat --run <run-key>
      -> migrate legacy state under migration.lock before resolving any per-run lock, when needed
      -> load run.json and acquire owner.lock
      -> launch a fresh Claude or Codex process
      -> invoke /conductor:autodev or $conductor:autodev
      -> execute at most one coherent phase or checkpoint
      -> commit, push, hand off, and exit

Driver may remain an internal module name, but user documentation and diagnostics call this mechanism the heartbeat. The heartbeat is the clock; autodev is the worker.

Each run owns its schedule, marker, log, and lock. Completing or cancelling one run removes only its schedule. A skipped fire caused by a live lock is successful and does not create another process.

The scheduler entry invokes the run's absolute heartbeat.sh path. That launcher passes the absolute project root and run key to conductor heartbeat, sets the project root as the working directory, and uses generated safely quoted values rather than unresolved environment variables or ambient shell state.

A checkpoint-only fire is a successful fire but does not advance the phase ledger. A run that reaches awaiting-team-merge removes its schedule. conductor resume --run <run-key> may reactivate it while the recorded final pull request remains open and unmerged, for example to address team feedback or create a synchronization phase; after successful reconciliation it restores only that run's schedule. If the repository team closes the final pull request without merging, reconciliation marks the run blocked and resume may admit further phases and open a replacement final pull request from the same integration branch. After the repository team merges the current recorded final pull request, conductor finish --run <run-key> first verifies from authoritative remote metadata that it is merged, its base is the repository default branch, its recorded head SHA matches the audited run head, and no review debt remains. Otherwise finish refuses and prints the pull-request URL and current state. Only after those checks does it remove run worktrees and eligible local branches, mark the run terminal, and retain run state and assertions as audit evidence.

Heartbeat reconciliation also detects schedule entries whose project or run no longer exists. It reports them as orphaned and removes them only through an explicit conductor heartbeat prune command, never as a side effect of another run.

## Orchestrator context contract

Every worker prompt begins with this exact instruction:

> **You are the Conductor orchestrator, not the executor. Save your context for orchestration.** Your responsibilities are reconciliation, delegation, gates, review routing, checkpointing, and handoff. Product implementation belongs in fresh phase/task subagents.

Immediately before phase execution, it repeats:

> **Orchestrator reminder: you are not the executor. Save your context for orchestration.** Delegate product-code changes to a fresh implementation subagent. If it needs more work, dispatch a follow-up or checkpoint for the next session; do not absorb the implementation into this context.

The orchestrator may perform deterministic Git/GitHub mechanics, state reconciliation, gate execution, review routing, and handoff writing. Product-code changes belong to fresh implementation subagents. Subagent results return as bounded summaries and evidence, not wholesale transcripts. Material follow-up implementation receives a fresh subagent.

Each implementation dispatch receives a durable dispatch ID in run.json. Product commits carry that dispatch ID as a commit trailer or equivalent machine-readable association. An orchestrator-created checkpoint commit carries every originating dispatch ID whose working-tree changes it captures. The phase gate refuses product changes and checkpoint commits for which no recorded implementation dispatch exists; unattributed operator product commits receive no implicit exemption. The only non-dispatch attribution is a designated synchronization phase's clean default-branch merge commit: its synchronization ID and two parents must exactly match the recorded pre-sync integration and authoritative default SHAs. Any conflict resolution or other tree change still requires a dispatch ID.

Phases are enumerated by the approved run plan and mirrored in the GitHub issue ledger. Synchronization phases are the only plan-external additions; they are appended to run.json's ordered phase IDs and the ledger with their synchronization record while the approved plan digest remains unchanged. run.json records the plan digest, ledger reference, ordered phase IDs, and current phase. Reconciliation uses this evidence precedence:

1. Git branch heads and commits
2. GitHub pull-request and review state
3. done-gate results.json
4. run.json
5. handoff.md

A handoff older than the phase branch head is advisory and cannot override Git. After a hard crash with no handoff, reconciliation resumes from the first four sources.

Prompt contract tests assert both reminders at their required anchors, and adapter tests prove that product work crosses the isolated-dispatch boundary.

## Context exhaustion and no-compaction policy

One heartbeat starts one fresh host process. A process handles at most one coherent phase and has a configurable per-fire context budget. The budget is a fraction of the host's declared context window, defaults to 0.60, and is enforced through host telemetry or a conservative adapter token estimator. Budget enforcement is the primary boundary on both hosts.

Supported Claude and Codex versions expose PreCompact hooks. Codex's documented contract stops before compaction when the hook returns continue=false. Preflight verifies the minimum host version, installs the host-native hook response, and executes a contract probe proving that it requests a checkpoint and blocks normal continuation. A missing, untrusted, disabled, or ineffective required hook blocks unattended mode rather than allowing an unbounded session.

When the context threshold or PreCompact hook fires, the orchestrator must:

1. Stop dispatching new implementation work.
2. Capture the active task, branch, worktree, tests, and unresolved findings.
3. If uncommitted product changes exist, create an explicit WIP checkpoint commit on the phase branch with the originating implementation dispatch ID or IDs; otherwise skip the commit.
4. Push the branch.
5. Renew the local ownership lease and refresh the durable GitHub ownership marker.
6. Atomically write the per-run handoff.
7. Mark the run checkpointed.
8. Exit the process without compacting.

WIP checkpoint commits never become mergeable merely because they exist. The phase pull request still needs its full tests, frozen gate, review, and merge checks.

Before returning its stop response, the PreCompact hook atomically writes compaction.marker with the run key, host-session identity, process identity, state revision, and timestamp. A process that owns or cannot disprove ownership of that marker may only checkpoint and exit; it may not continue implementation. A later heartbeat may archive and clear the marker only after proving the marked process exited, reconciling durable evidence, and recording the completed or recovered checkpoint under state.lock. This prevents both post-compaction implementation and an endless fresh-session checkpoint loop.

If commit, push, lease renewal, or handoff persistence fails, the run blocks rather than exiting as if the checkpoint succeeded. If compaction slips through despite the hook, the compacted process follows the marker rule and may only checkpoint and exit.

The next heartbeat launches a fresh process, reconciles durable evidence, reads handoff.md, and resumes the same run and phase.

## Packaging and installation

The Conductor source repository contains both native manifests:

- .claude-plugin/plugin.json
- .codex-plugin/plugin.json

The Codex manifest uses only supported fields and exposes ./skills/. Host hooks use the supported host configuration/discovery mechanism rather than unsupported manifest fields.

The AutomateIntelligence marketplace repository contains:

- .claude-plugin/marketplace.json for Claude's remote-source catalog
- .agents/plugins/marketplace.json for Codex's local package catalog
- plugins/conductor and plugins/spec-craft as generated release bundles

The canonical code remains in the Conductor and spec-craft repositories. Marketplace bundles are produced from immutable tags, record their source commit and version, and are verified against those sources in CI. They are not development copies.

Claude keeps Conductor's spec-craft dependency. Codex does not support a plugin-level dependencies field, so the Codex marketplace marks spec-craft with policy.installation=INSTALLED_BY_DEFAULT. Adding the AutomateIntelligence marketplace therefore makes spec-craft available before Conductor is installed. Release CI treats this marketplace field as an external contract: it validates the catalog with the supported Codex CLI, adds the marketplace in an isolated configuration, and proves a fresh session discovers spec-craft before Conductor is installed. A failed probe blocks publication.

Ordinary installation flows are:

Claude Code:

    /plugin marketplace add automateintelligence/marketplace
    /plugin install conductor@automateintelligence

or:

    claude plugin marketplace add automateintelligence/marketplace
    claude plugin install conductor@automateintelligence

Codex:

    codex plugin marketplace add automateintelligence/marketplace
    codex plugin add conductor@automateintelligence

Fresh sessions must discover the appropriate Conductor and spec-craft skills after installation.

The initial supported host floor is Claude Code 2.1.224 and Codex CLI 0.147.0, the versions against which plugin discovery, marketplace policy, non-interactive launch, native subagents, and PreCompact contracts are verified. Manifests, preflight diagnostics, and installation documentation publish these minimums; lowering them later requires the same contract suite.

## spec-craft compatibility

spec-craft's reasoning workflow is already host-neutral: it reads and writes Markdown and uses no Claude tools, hooks, services, or runtime code. Its packaging and invocation prose are Claude-specific and must change.

The spec-craft repository will:

- add .codex-plugin/plugin.json
- validate both manifests
- replace the Claude-specific $ARGUMENTS assumption with host-neutral input resolution from the invocation text
- show both /spec-craft:* and $spec-craft:* examples
- update README and GitHub descriptions to say Claude Code and Codex
- retain its standalone, Conductor-agnostic behavior
- pass fresh-session behavior smokes on both hosts

Conductor must fail closed with an exact installation/recovery command if spec-craft is unavailable despite the marketplace policy.

## Public messaging

The following surfaces must describe Conductor as supporting Claude Code and Codex:

- Conductor README title, introduction, prerequisites, installation, usage, architecture, and troubleshooting
- Conductor .claude-plugin and .codex-plugin descriptions
- automateintelligence/conductor GitHub repository description
- both AutomateIntelligence marketplace entries and marketplace README
- automateintelligence/marketplace GitHub repository description
- installation examples and verification commands

spec-craft receives the equivalent dual-host updates. Bubo is already dual-host; its product text and standalone behavior are preserved. Marketplace-wide wording must not regress Bubo to a Claude-only description.

GitHub repository descriptions are release-owner metadata rather than PR-managed files. The repository administrator applies the approved exact text after the related PR merges, and the release checklist verifies it through the GitHub API.

## Source relocation and quarantine

The canonical editable checkout becomes:

    ~/programming/conductor

After preservation and repointing checks, the old checkout is renamed to:

    ~/.claude/conductor.quarantine-2026-08-10

No symlink is left at ~/.claude/conductor. Leaving the former path absent ensures stale references fail visibly instead of silently using the wrong checkout.

Before the rename, the new canonical checkout is cloned from automateintelligence/conductor and verified against the expected origin and commit. The old checkout is inventoried for local branches, commits not reachable from origin, linked worktrees, stashes, and untracked files. Local-only commits and refs are transferred through an auditable bundle or explicit refs. Intentional untracked source or documentation is copied deliberately; tool caches and transient files are not promoted into source.

Every in-flight run that references the old checkout is checkpointed or quiesced. Its schedule, hook, resume environment, and launcher are repointed to an installed plugin artifact or the new canonical tooling and pass a dry-run probe. No live process may retain a working directory or executable under the old checkout. Only after those conditions and the linked-worktree recreation below hold is the old directory renamed into quarantine. Quiesced schedules are then re-enabled and each run must reconcile successfully on its next heartbeat.

Renaming a main working tree can invalidate absolute linked-worktree metadata, especially when worktree paths are nested under that checkout. Before quarantine, every dirty retained worktree is checkpointed, its branch and commit are transferred, and the worktree is recreated from the canonical clone at the same commit under the new canonical path. The old registration is removed only after the recreated worktree passes git worktree list and git status and its run.json path is updated. No worktree remains registered beneath the old checkout and no work is completed under quarantine.

The quarantine remains for one week. During that week, checks cover shell/plugin configuration, cron entries, host hooks, running processes, documentation, scripts, linked worktrees, local refs, and installation behavior.

The final scan passes only when:

- no active cron, systemd, launchd, hook, launcher, or runtime configuration names the old path
- no live process has its executable or working directory under quarantine
- every retained commit, branch, stash, and intentional untracked file exists in the canonical checkout or an explicit archive
- every retained linked worktree is healthy against its intended repository
- public Claude and Codex installation smokes pass without the quarantine present in resolution
- no unexplained write occurred inside the quarantine during the observation week

Only then is the quarantined checkout removed as an explicit, reported cleanup action.

## Legacy run migration

Migration is available explicitly through conductor migrate and runs automatically at the start and heartbeat entry points when legacy state is detected, before any per-run lock is acquired and never from inside a launched host's autodev. Every migration holds the project-level .conductor/migration.lock for its full read, journal, and mutation sequence; a concurrent mutating invocation waits or reports migration-in-progress. status is read-only: it detects legacy or in-progress migration state and prints conductor migrate, but never migrates as a side effect.

The migration:

1. Reads the legacy flat .conductor state without writing.
2. Resolves its spec, branch, worktree, phase, frozen assertions, schedule, and GitHub references.
3. Stops before mutation if any identity is ambiguous or conflicting.
4. Writes a byte-for-byte backup under .conductor/migrations/.
5. Opens a migration journal and creates project.json plus the per-run directory through revision-guarded steps.
6. Adopts the legacy shared branch/gate slug as that run's key, records generation=1 and identity_scheme=legacy-slug-v1, and retains the exact recorded branch names. Because the old flat format can represent only one run, this preserves the branch-to-gate equality without creating a collision. project.json maps the normalized spec path to this legacy key; generation 2 and later use the hashed generation-aware key with the corresponding suffix.
7. Reuses the exact integration/phase branches, worktrees, pull requests, and frozen assertions. The existing assertions/<legacy-slug> directory is adopted as the run namespace without rewriting frozen content. The new resolver proves that run.json, branch, gate directory, manifest, and baseline agree and that gate verify does not produce an ambient-dodge failure.
8. Installs and verifies the new per-run heartbeat.
9. Removes the legacy schedule only after the replacement is verified.
10. Records migrated worker_host=claude unless stronger evidence identifies a different active owner. It assigns the opposite reviewer only after verifying that host is installed and launchable; otherwise it records review_policy=blocked-pending-opposite-host with the exact installation command.

Migration never regenerates expectations, assertion specs, assertion tests, or the frozen definition of done. Legacy paths remain readable for one compatibility release only by explicit migration/diagnostic code; run-key-bearing operations never use them as fallback. All new writes use the per-run layout. The journal makes recovery idempotent and records the schema/source version and last completed step.

## Failure handling

All state writes use a sibling temporary file, flush, fsync, and atomic replace. run.json writes additionally require state.lock and the current revision. project.json mutations require project.lock and its current revision. An operation that updates project.json and one or more run.json files writes and fsyncs a project transaction first; every project entry point completes or reverses an unfinished transaction before reading mappings, so a crash cannot leave a silently split identity. The global lock order is migration.lock when applicable, then project.lock when applicable, then owner.lock, then state.lock; multi-run project operations acquire run locks in sorted run-key order. Ownership locks, state locks, and schedules are scoped by run key.

The local execution lease in owner.lock prevents duplicate Claude or Codex processes over the canonical project state root and is the sole ownership authority within the supported scope. Its run.json fields are diagnostic mirrors. The GitHub issue or pull-request ownership marker is durable audit and crash-recovery evidence, not a distributed mutex. Conductor refuses cross-machine automatic takeover rather than inferring exclusivity from that marker. Neither the lease nor the marker substitutes for state.lock or the revision check.

Every actionable failure reports:

- run key and current state
- failed invariant or operation
- affected branch, worktree, pull request, or state path
- whether any write occurred
- the exact inspect, retry, takeover, migrate, or recovery command

Important failures are fail-closed:

- missing or unsafe host permissions block launch
- missing required plugin capability blocks the run
- ambiguous legacy state blocks migration
- conflicting live ownership blocks takeover
- failed checkpoint persistence blocks normal exit
- stale or failing review blocks phase merge
- changed frozen assertions block progress
- unresolved default-branch metadata blocks every automated merge
- a pull-request base different from this run's integration branch blocks phase merge
- a default-branch merge target is rejected unconditionally

A failed heartbeat leaves durable evidence for a later heartbeat or manual autodev reconciliation. Retrying the same operation is safe.

Operational rollback disables the failing host adapter and transfers the run to the healthy host while retaining the per-run schema. State is never downgraded silently. Restoring the pre-migration backup is allowed only when no run evidence has advanced since migration; otherwise recovery remains forward on the new schema.

## Verification strategy

### Unit and contract tests

- run-key derivation, generation suffixes, and stability after repository relocation
- spec-path repoint preserving the run key, path-history audit, collision refusal, and cross-file transaction recovery after each possible crash point
- per-run path resolution, no flat writes, and no ambient fallback
- two simultaneous run keys resolving distinct goals, gates, manifests, baselines, and results despite conflicting legacy files or environment variables
- schema validation, exact status and review-policy vocabularies, failed-versus-blocked transitions, blocked-autodev reconciliation-only behavior, resume-to-active, and active-run classification
- concurrent project mappings using project revision checks without lost updates
- migration idempotence, migration-before-per-run-lock ordering, project-lock serialization, and read-only status behavior
- legacy migration preserving branch-to-gate identity and passing the first gate verify
- revision-guarded writes rejecting stale concurrent updates
- lock, lease, PID-reuse, takeover race, and inactivity recheck under lock
- dead-wrapper/live-host orphan refusal, prune lock ordering, identity-matched termination, and cross-machine automatic-takeover refusal
- workstation rebind refusing without attestation or while a local owner or schedule remains, retaining prior-workstation history, leaving schedules absent, and per-run resume installing eligible local schedules
- Claude and Codex adapter contracts
- isolated implementation-subagent dispatch and dispatch-to-commit attribution
- worker/reviewer inversion
- takeover invalidating a review authored by the new worker host
- permission profiles and bypass non-transfer
- heartbeat installation, awaiting-team-merge removal, explicit resume, orphan reporting, closed-unmerged final-PR recovery, and finish refusal while the final pull request is unmerged
- prompt anchor reminders
- context-budget, PreCompact probe, compaction-marker fencing, no-change checkpoint behavior, and fresh-session marker recovery contracts
- hard-crash reconciliation without handoff
- review schema and head-SHA freshness
- review-debt creation, discharge anchored to the stored required reviewer host across an intervening takeover, and final-PR blocking
- phase-merge acceptance only when the base equals this run's integration branch
- rejection of another run's branch, stale branch, arbitrary branch, or default branch as a phase base
- default-branch resolution failure blocking every automated merge
- rejection of final-PR auto-merge, merge-queue enrollment, and base mutation
- default-branch drift handled only through a gated synchronization phase whose plan-external phase ID is present in run.json and the ledger, with exact-parent attribution, dispatch-required conflict resolution, final done-gate rerun, and audited-head refresh
- tracked .conductor or .worktrees paths failing with exact recovery commands
- spec-craft dual-manifest and host-neutral input contracts
- missing spec-craft failing closed with the exact host-native installation command
- public-text guards against unintended Claude-only claims

### Integration tests

Temporary repositories, worktrees, bare remotes, and fake Claude, Codex, and gh executables verify exact arguments, environment, prompts, Git transitions, scheduler entries, review routing, and crash recovery. Integration races a takeover against a newly firing prior heartbeat over the same canonical state root and proves only one owner survives. Another test kills the worker without a handoff and proves the next heartbeat reconciles from Git, GitHub, gate results, and run.json. Relocation integration recreates nested linked worktrees from the canonical clone and proves no retained worktree or process remains under quarantine.

### End-to-end matrix

- Claude worker with Codex reviewer
- Codex worker with Claude reviewer
- Claude-to-Codex takeover mid-run
- Codex-to-Claude takeover mid-run
- two concurrent specs in one repository
- manual autodev with zero, one, and multiple active runs
- context threshold checkpoint and fresh-session resume
- compaction-hook checkpoint
- hard crash followed by fresh-session reconciliation without handoff
- opposite reviewer transient failure and retry
- visible same-host fallback with review debt
- strict opposite-review halt
- outstanding review debt blocking the final pull request
- stale review after new commits
- in-flight legacy migration
- in-flight run surviving source-checkout relocation and schedule/hook repointing
- stable run identity after moving the project repository
- stable run identity after an explicit in-repository spec rename
- phase pull request merged into the run branch
- phase pull request targeting another run branch rejected
- final pull request opened but never merged by Conductor
- final pull request protected from auto-merge, merge queue, close, and base mutation
- early finish refused until authoritative remote metadata proves the final pull request merged
- repository-team close without merge, blocked transition, further phase, and replacement final pull request

### Installation smoke tests

Smoke tests use isolated test users or disposable environments rather than mutating an existing operator configuration.

They prove:

- Claude's ordinary Conductor install also installs spec-craft
- Codex marketplace addition installs spec-craft by default
- Codex's ordinary Conductor install succeeds
- fresh sessions discover the host-native skill names
- a minimal spec passes through spec-craft and starts Conductor on both hosts
- a Conductor package installed outside the marketplace fails closed with the exact spec-craft recovery command
- older host versions fail preflight with the documented minimum-version command
- installed executables, hooks, schedules, and runtime configuration do not resolve through ~/programming/conductor or ~/.claude/conductor

Pre-merge smokes install branch/tag artifacts through normal host commands. After the repository teams merge and publish, the same tests run against the public default branches.

## Repository and release sequence

Work spans three repositories. Each change set uses its own worktree, branch, commits, tests, and pull request.

1. Establish ~/programming/conductor as the canonical clone and prove all local refs, stashes, and intentional files are preserved there or explicitly archived.
2. Checkpoint or quiesce every in-flight run, repoint every schedule, hook, resume environment, and launcher that references the old checkout, and prove no live process remains under it.
3. Recreate every retained linked worktree from the canonical clone at the same commit, update its recorded run path, and prove git worktree list and git status are healthy with no retained worktree beneath the old checkout.
4. Rename the old checkout into quarantine, re-enable the quiesced schedules, and prove each in-flight run reconciles on its next heartbeat.
5. spec-craft becomes dual-host and publishes a versioned artifact.
6. Conductor becomes dual-host against that supported spec-craft version.
7. The AutomateIntelligence marketplace publishes both host catalogs and generated bundles.
8. Repository descriptions and public documentation are updated after the corresponding repository team accepts the changes.
9. Branch-based installation smokes run before merge.
10. Public installation smokes run after repository-team merge.
11. The quarantined checkout is removed only after the one-week observation period and final scan.

Conductor never merges any of these default-branch pull requests.

## Acceptance criteria

- Claude Code and Codex install Conductor through their ordinary marketplace workflows.
- Adding the AutomateIntelligence marketplace makes spec-craft available on both hosts before Conductor is installed; a Conductor package installed from another source fails closed with the exact spec-craft installation command.
- Both hosts expose and successfully invoke their native Conductor and spec-craft skill names.
- Either host can take over an active run over the same canonical project state root without changing the run key or losing durable state; automatic cross-machine takeover is refused.
- Concurrent specs in one project have independent state, gates, worktrees, locks, logs, and schedules.
- Codex-owned runs use Claude review by default; Claude-owned runs use Codex review by default.
- Reviews are structured and tied to the current pull-request head SHA.
- Outstanding same-host review debt prevents the final pull request.
- The orchestrator delegates product work and repeats both required context-preservation reminders.
- Product commits are associated with an isolated implementation dispatch rather than the orchestrator context.
- Context exhaustion produces a pushed checkpoint and fresh-session handoff, never implementation through compaction.
- Every implementation unit is committed, pushed, reviewed, and merged through a phase pull request into the run branch.
- Default-branch resolution failure blocks automation; Conductor can open but cannot retarget, auto-enroll, close, or complete the final default-branch pull request.
- Existing in-flight runs migrate without regenerating or weakening frozen assertions.
- In-flight runs survive the source relocation, and the quarantine passes its one-week removal predicate.
- No installed executable, hook, schedule, runtime configuration, or active run depends on the old editable checkout path.
- Conductor, spec-craft, and marketplace public descriptions name both Claude Code and Codex.
- All unit, integration, end-to-end, manifest, and installation smoke gates pass with no known safety or migration errors.

## Expectations

The acceptance criteria above are the owner's original definition of done. This section adds only
the conditions that were left implicit — mostly at the seams the Codex ground-truth review
(`docs/reviews/2026-08-12-codex-host-ground-truth.md`) exposed, where a result can look correct on
a Claude machine and be wrong on a Codex one. Nothing here restates an acceptance criterion.

### Success scenarios

- **S1 — one installed host is enough to work.** On a workstation where only one of the two hosts
  is installed, a run still starts, takes ownership, dispatches implementation, and advances phase
  work on the installed host. The missing opposite host blocks the *phase merge* — recorded as
  `blocked-pending-opposite-host` with the exact installation command — and does not block starting
  or advancing the run. Dual installation is required to finish a phase, not to begin one.
- **S2 — the Codex worker launches on a stock Codex install.** A Conductor fire on a machine
  carrying only an official Codex CLI at or above the host floor — no third-party `AGENTS.md`, no
  oh-my-codex, no community skill-dispatch convention present — reaches Conductor's autodev
  behavior. Whether the launch names a skill path, inlines the instruction, or uses some other
  mechanism is an implementation choice; what must hold is that no part of the launch depends on a
  convention Conductor does not itself install.
- **S3 — spec-craft behaves identically on both hosts.** Invoking the same spec-craft skill against
  the same spec file produces the same written result under Claude Code and under Codex, including
  when the spec path is supplied as an argument, embedded in a sentence, or omitted entirely. The
  host-neutral input resolution replaces `$ARGUMENTS`, and it is the *behavior* that must match, not
  merely the manifests.
- **S4 — the run has a reachable finish line of its own.** The work Conductor performs autonomously
  is done when the final integration-branch pull request is open and the run sits at
  `awaiting-team-merge` with no outstanding review debt. The acceptance criteria that require merged
  public state in three repositories — public marketplace installs, published repository
  descriptions, post-merge public smokes — belong to the release, not to the run, and their absence
  is never evidence that the run is incomplete.

### Failure scenarios (fail-closed)

- **F1 — installs cleanly, then reaches for the wrong host.** A Codex-published Conductor package
  that installs without error and then attempts to spawn `claude` at its first fire is a failure,
  not a partial success. Installation succeeding is never accepted as evidence of host support; the
  condition that decides it is a first fire on a machine with the other host absent entirely.
- **F2 — a probe that hangs instead of failing.** Every Conductor-initiated host invocation —
  version check, capability probe, preflight, worker launch, reviewer launch — either completes or
  reports a bounded, actionable failure. An invocation that blocks indefinitely is a failure of this
  expectation even though nothing errored, because an unattended fire that hangs leaves a stuck
  owner rather than a recoverable one, and the run's own failure reporting never runs.
- **F3 — dual-host in name only.** A result where both manifests, both catalog entries, and both
  sets of public text exist, but the Codex execution path was never exercised because host-specific
  vocabulary still lives in the shared core, is a failure. The observable symptom is that removing
  or renaming the Claude executable changes behavior in code that is supposed to be host-neutral.

### Must-nots

- **M1 — no host vocabulary in the core.** No shared core module may contain a Claude slash-command
  invocation, a Codex dollar-prefixed invocation, `CLAUDE_PLUGIN_ROOT`, a host-specific permission
  or sandbox flag value, or an assumption that there is exactly one installation directory. These
  belong to adapters only. This is stated in §"System architecture" as a design constraint; it is
  repeated here because it is also a condition of done.
- **M2 — never relocate under a live run.** The old editable checkout must not be renamed, moved,
  quarantined, or removed while any run holds a live process, a registered worktree, an installed
  schedule, or a hook resolving beneath it. Absolute linked-worktree metadata does not survive the
  rename, so the damage — work committed under a quarantined path, worktree registrations pointing
  at a directory that no longer exists — is discovered late and is not cleanly reversible.
- **M3 — host-native session resume is not a continuation mechanism.** Codex's native session
  resume must not be used to carry a run forward between fires. Every fire is a cold start that
  reconciles from durable evidence, on both hosts. This is an explicit non-goal rather than an
  oversight: one reconciliation model is the reason a mid-run takeover is a state transition instead
  of a translation, and a second model would let the two disagree silently.
- **M4 — no collateral regression of sibling products.** Applying dual-host wording across the
  marketplace must not narrow any existing product's description to a single host. Bubo is already
  dual-host and its public text must not come back Claude-only.

### Open questions

These are places the spec genuinely does not determine what done means. They are recorded rather
than resolved, and should be settled by the owner before the affected plan is written.

- **Preflight versus review policy.** S1 above reads §"Reviewer routing" as authoritative: a missing
  opposite host blocks the phase. §"Failure handling" also says "missing required plugin capability
  blocks the run," which can be read to mean the opposite host is a launch prerequisite. If the
  second reading is intended, S1 is wrong and a single-host workstation is unsupported outright.
- **Is the final-PR prohibition list exhaustive?** §"Branch, worktree, and pull-request model"
  enumerates merge, squash, rebase, force-push, close, base mutation, auto-merge enablement, and
  merge-queue enrollment, then adds "or any equivalent action." A gate can only check a finite list.
  Either the enumeration is the contract, or something must define how an unlisted equivalent action
  is recognized.
- **Codex native subagent availability.** §"System architecture" allows either a native Codex
  subagent or a fresh non-interactive child process. Which one is available at the host floor was
  not established. Done should not be defined against whichever branch happens to be implemented
  first.

## References

- OpenAI, Build plugins: https://learn.chatgpt.com/docs/build-plugins
- OpenAI, Build skills: https://learn.chatgpt.com/docs/build-skills
- OpenAI, Scheduled tasks: https://learn.chatgpt.com/docs/automations
- OpenAI, Non-interactive mode: https://learn.chatgpt.com/docs/non-interactive-mode
- OpenAI, Codex hooks: https://learn.chatgpt.com/docs/hooks
- Anthropic, Claude Code hooks: https://code.claude.com/docs/en/hooks
