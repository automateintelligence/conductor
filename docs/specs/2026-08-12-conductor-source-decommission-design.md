# Conductor source decommission

**Date:** 2026-08-12

**Amended:** 2026-08-16 — scope decisions confirmed, P2/P4/P5 tightened, execution preconditions added

**Amended:** 2026-08-17 — predicates split into loss-risk hard gates and quiesce conditions; P1 and
P5 reframed accordingly

**Status:** Design approved and both scope decisions confirmed by the owner. **Execution is
deferred.** As of the readings below, all four loss-risk predicates are blocked,
plugin readiness is partial, and the quiesce conditions are deferred to the quarantine window by
design. Nothing is deleted, moved, or quarantined until the execution preconditions are satisfied.

**Repositories:** automateintelligence/conductor, automateintelligence/marketplace

This supersedes the relocation approach in Plan 00 of
`docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md` (lines 80–110). Plan 00 becomes a
decommission checklist rather than a relocation runbook.

## Scope decisions

Both are decided, not pending.

**The packaging-boundary work stays dropped.** The installed cache is roughly 2.4 MiB. Splitting the
repository to reclaim that would mean reopening frozen assertions and coordinating a cutover across
two repositories, which is not worth it at that size. Measurements behind the decision are below.

**Fresh clone plus staged quarantine is preferred over relocating this checkout.** The three linked
worktrees that existed when this was decided make a physical move fragile — worktree administrative
files record absolute paths on both sides of the link
([git-worktree](https://git-scm.com/docs/git-worktree.html)). Retire the worktrees explicitly rather
than moving the main tree.

## Goal

Retire `~/.claude/conductor` as the canonical checkout. Conductor is consumed as an installed
plugin from `~/.claude/plugins/cache/`. When a dev checkout is next needed it is a fresh
`git clone` into `~/programming/conductor`, not a move of the old one.

## Why this replaces the move

Plan 00's runbook had to recreate linked worktrees whose metadata is absolute; roadmap line 98
calls that the highest-risk item. A fresh clone creates worktrees from the new root, so nothing is
repointed and nothing can dangle. The blocking precondition weakens from "no process may hold
anything under this path during a rename" to "nothing unpushed and nothing unpreserved remains
before deletion" — unpushed commits, untracked files, and ignored run state alike.

## Why the packaging boundary was dropped

The installed package at `~/.claude/plugins/cache/automateintelligence/conductor/0.9.2/` is 2.0 MB:
roughly 500K runtime (`bin`, `ledger`, `skills`, `conductor`, `.claude-plugin`) and 1.5M
non-runtime (`docs` 668K, `tests` 576K, the `assertions` gate 128K, `experiments` 108K). That is
apparent size (`du -sb`, 2,043,815 bytes); on disk it occupies 2.4 MiB (`du -sh`). Either figure is
small enough that the conclusion does not turn on which one is used.

The non-runtime content is inert, not merely unused. `conductor/paths.py` `project_root()` resolves
the user's project via `$CONDUCTOR_HOME`, then `git rev-parse --show-toplevel`, then
`os.getcwd()` — never the plugin directory. The shipped assertions manifest and self-enforcement
gate are therefore never read on a user machine. The repository is public, so excluding docs from
the package conceals nothing.

There is no manifest-level file control in the plugin format. Verified: no `files`, `include`,
`exclude`, or `ignore` field, and no `.claudeignore` mechanism. The only mechanism is changing
source shape to a git-subdir entry, which would cost roughly 25 path-literal edits, an authorized
re-freeze of the 16 frozen self-enforcement assertions, and a two-repo coordinated cutover. It would
also introduce an import-shadowing hazard between a plugin-side `assertions` package and the user's
project `assertions` directory that does not exist in the current layout.

Cost exceeds harm. Revisit only if the package grows materially or the repository becomes private.

## States

**S0, today.** One directory is simultaneously the canonical checkout, the plugin source, and a
conducted project.

**S1, deploy.** Already satisfied. The marketplace entry in the separate
automateintelligence/marketplace repository is a `url` source pointing at
`github.com/automateintelligence/conductor.git`, so shipping is push plus version bump.

**S2, decommissioned.** The path is gone, Conductor runs from cache, and development happens in a
fresh clone.

## Predicates

Six runnable checks. Four of them split into two classes that gate differently; P6 is a separate
readiness question and stands on its own.

**Loss-risk predicates are hard gates.** P1, P2, P4, and the hard half of P5 ask a single question:
would anything be destroyed that exists in no second place? They must pass before quarantine,
unconditionally.

**Activity predicates are quiesce conditions.** Whether a process holds a cwd here (P3), whether
branches are in flight, and whether worktrees are still open are evaluated inside a declared quiesce
window immediately before the move — not as repo-wide invariants that must hold at all times.
Retiring the worktrees is still scheduled work (precondition 3); what changes is that it is work
done inside the window rather than a condition the repository is expected to satisfy continuously.

The split exists because this checkout is a live working environment, and the only moment an
absolute-activity predicate passes is a moment nobody arranged. P3 was reframed once already for
this reason: "no process rooted here" can never pass while anyone is working in the tree, since 59
transient processes held a cwd here — including the session doing the measurement. P1 and P5
carried the identical defect. "Is every branch merged" and "are all worktrees retired" are
statements about whether work is happening, not about whether anything is at risk: a branch fully
pushed to a remote holds nothing that could be lost even if it is unmerged, and an open worktree
whose contents all have a second copy is not a hazard. Do not re-tighten P1 or P5 back into
absolute invariants. That restores a gate which goes red whenever the project is being worked on,
and a gate that cannot pass under normal conditions gates nothing.

Current readings are in the next section.

| ID | Class | Predicate | Check |
| --- | --- | --- | --- |
| P1 | Loss-risk — hard gate | No commit exists only here | `git log --branches --not --remotes --oneline` must be empty. It lists every commit reachable from a local branch and from no remote-tracking ref, which is the subject exactly. Do **not** build this on `@{u}`: a branch with no upstream has nothing to be ahead of, so an `@{u}`-based check reports it clean and cannot observe its own subject — the defect the 2026-08-17 amendment corrects. `git log --all --not --remotes` is the stricter superset, additionally covering tags, stash entries, and detached worktree HEADs; both forms were run for the readings below and agreed. Per branch, `git branch -r --contains <tip>` answers the same question one tip at a time — it lists remote branches whose tips descend from the commit, so a non-empty result means some remote carries it. Merge status is not the subject: an unmerged branch that is fully pushed passes. |
| P2 | Loss-risk — hard gate | No untracked **or ignored** content of value | `git status --porcelain -uall --ignored`. The plain `-uall` form is not sufficient and was the first defect the 2026-08-16 amendment corrects: by construction it omits ignored paths, and `.conductor/` is ignored, so the run state most at risk — the only copy of anything that never reached GitHub — is invisible to it. `--ignored` takes an optional mode and defaults to `traditional`, which combined with `-uall` lists individual files inside ignored directories rather than collapsing them to a directory entry. The check must enumerate `.conductor/` contents explicitly rather than trusting a summary count. |
| P3 | Quiesce condition | No durable process rooted here | Scan `/proc/*/cwd`, and `crontab -l` clean of conductor entries. Transient interactive tooling with a cwd here (editors, agent sessions, MCP servers, language servers) does not fail this; a conductor driver, cron entry, or service unit does. |
| P4 | Loss-risk — hard gate | Nothing external names the path | Shell rc files, `~/.claude/settings.json`, hooks, `.conductor/resume-env.sh`, **and host-level trust and project registries that pin absolute paths** — notably `~/.codex/config.toml`, which carries a `[projects."<absolute path>"]` table trusting this checkout by literal path. Distinguish **active configuration**, which must be repointed or removed, from **historical transcripts and session logs**, which record the old path as a fact about the past. Historical records are out of scope: do not chase them. |
| P5 | Loss-risk — hard gate | No worktree holds state with no second copy | For each linked worktree in `git worktree list --porcelain`: run P2's command inside it (`git -C <path> status --porcelain -uall --ignored`) and P1's command against its branch tip, then record an explicit outcome — archive, discard, or keep — in the disposition table below. Accounting alone is not enough: a worktree can sit on a fully merged branch and still hold untracked or ignored state that exists nowhere else. `git worktree remove` runs only after that worktree is clean **and** its state is preserved; it refuses a dirty tree without `--force`, and `--force` is what discards the state this predicate exists to protect ([git-worktree](https://git-scm.com/docs/git-worktree.html)). Whether anyone is *currently working* in a worktree is a quiesce condition and not part of this gate; a worktree with an in-flight branch passes P5 once that branch is pushed and its outcome is recorded. Retiring the worktrees is still sequenced work — precondition 3 — because their administrative files record absolute paths and cannot survive the move; it is simply not what this predicate measures. |
| P6 | Readiness — hard gate | Plugin works standalone | Conductor verbs run from a working directory outside the checkout while the source still exists, against the intended cached version. |

## Current status

Readings taken 2026-08-16 in `/home/danie906/.claude/conductor` on `main` at `9cdeebd`; P1 and P5
re-measured 2026-08-17 on `main` at `765d09b` under the reframed definitions.

| ID | Status | Reading |
| --- | --- | --- |
| P1 | **Blocked** | Reframing changed which branches are implicated but not the verdict. `git log --branches --not --remotes --oneline` returns 5 commits, all on `fix/production-review-findings` (tip `f5b6bb1`), which has no upstream and whose tip no remote ref contains — `git branch -r --contains f5b6bb1` is empty. `git log --all --not --remotes` returns the same 5, so no tag, stash, or detached HEAD holds anything further; there are no stashes and no detached worktree HEADs. This is live work in `.worktrees/prod-review-fixes` and postdates the 2026-08-12 reading, when P1 did pass; it was 4 commits on 2026-08-16 and is 5 now. The other 22 local branch tips pass, including the three the earlier merge-based framing had to argue about individually — `worktree-agent-a5e09b4b44bc7a99d`, `worktree-agent-a92ff8afb354ce573`, and `0.5.0-run-packet`, the last with a `gone` upstream. Each is carried by many `origin/*` refs, so none holds anything at risk regardless of upstream state. |
| P2 | **Blocked** | 15 untracked paths under `-uall`: 11 scratch files in `.gstack/tmp/`, 3 `.serena/` project files, and `docs/reviews/2026-07-31-conductor-assertions-source-naming-fix-prompt.md`. Adding `--ignored` surfaces 221 further paths that plain status does not report, including `.conductor/goal.md`, `.conductor/resume-env.sh`, and `.conductor/run_branch` — all dated 2026-07-06 and, unlike the caches and build artifacts around them, not reproducible. |
| P3 | **Pass, pending re-check in the quiesce window** | No conductor entry in the 20-line crontab, no systemd user unit naming conductor, no durable driver. 59 processes currently hold a cwd under the path, but all are transient interactive tooling — Claude Code, `codex`, MCP servers, `pyright-langserver`, shells — including the session taking these readings. As a quiesce condition this reading is only ever provisional: re-run it inside the quarantine window. |
| P4 | **Blocked** | `~/.codex/config.toml` line 54 is `[projects."/home/danie906/.claude/conductor"]` — an active trust entry naming the old path exactly. The remaining P4 surfaces (rc files, hooks, `settings.json`) have not been swept in this pass. |
| P5 | **Blocked, and needs disposition** | **Four** linked worktrees, not the three recorded on 2026-08-12. Under the reframed hard gate, **all four** hold state with no second copy. Sorting ignored paths by whether they are reproducible, rather than reading the tree's merge status, promotes Bubo and `.omx` artifacts that the 2026-08-16 pass left inside raw counts. `codex-dual-host-design` is the one that changes verdict: 3 `.bubo/*` files and 4 `.omx/artifacts/*.md` review transcripts, on a branch fully merged into `main` — which is exactly the case the old "are all worktrees retired" framing would have waved through. Merge status decides nothing here. Separately, three of the four still carry no recorded outcome, so P5 stays unsatisfied on that count alone. See the disposition table below. |
| P6 | **Partial pass** | The cache holds 0.1.0, 0.4.0, 0.4.1, 0.8.1, 0.9.1, and 0.9.2. `main` is at 0.9.3, which is not installed, so the standalone path currently exercises code one version behind source. Owner reports conductor verbs running from an unrelated repository against cached 0.9.2; that run was not repeated in this pass. |

### Worktree disposition

P5 is not satisfied until the owner records an outcome for every row; three still read *(undecided)*
by design. The merge column is retained as context, not as a criterion — it decides nothing. "At
risk" lists
what the tree holds with no second copy, after discarding reproducible caches; counts re-measured
2026-08-17.

| Worktree | Branch | Merged into `main` | At risk | Outcome |
| --- | --- | --- | --- | --- |
| `~/.claude/conductor-run-2026-07-05-self-enforcement` | `conductor/run-2026-07-05-self-enforcement` | yes | `.conductor/handoff.md`, `.conductor/run_branch`, `assertions/run/results.json` — the run record of the self-enforcement run; 236 ignored paths in total, the rest caches | *(undecided)* |
| `.claude/worktrees/agent-a92ff8afb354ce573` | `0.5.0-run-packet` | yes | 3 untracked Bubo files (`.bubo/config.json`, `.bubo/reviews.jsonl`, `.bubo/state.json`) plus ignored `assertions/run/results.json`; 66 paths in total | *(undecided)* |
| `.claude/worktrees/codex-dual-host-design` | `docs/codex-dual-host-design` | yes | 3 ignored `.bubo/*` files and 4 ignored `.omx/artifacts/*.md` design-review transcripts from 2026-08-10; 105 paths in total | *(undecided)* |
| `.worktrees/prod-review-fixes` | `fix/production-review-findings` | **no** | 5 commits carried by no remote ref (tracked tree otherwise clean), plus 3 ignored `.bubo/*` files; 126 paths in total | keep; sole cause of the P1 failure until pushed |

## Execution preconditions

Execution stays deferred until all six preconditions below are met. In order.

1. **Clone first, never move.** `git clone https://github.com/automateintelligence/conductor.git
   ~/programming/conductor`, then verify the new tree on its own terms — `git -C
   ~/programming/conductor status --porcelain` empty, and `git -C ~/programming/conductor rev-parse
   HEAD origin/main` returning the same SHA twice — before anything at the old path is touched. The
   existing checkout is never relocated.
2. **Archive or deliberately discard all untracked and ignored run and handoff data.** Drive this
   from `git status --porcelain -uall --ignored` in every tree, not from the plain form. Each
   `.conductor/` file gets an explicit decision; caches (`.ruff_cache`, `.pytest_cache`,
   `__pycache__`, `.hypothesis`) are reproducible and may be discarded without ceremony.
3. **Give each linked worktree an explicit outcome** — archive, discard, or keep — recorded in the
   disposition table above. Run `git worktree remove` only after that worktree is clean and its
   state is preserved. `.worktrees/prod-review-fixes` must push or abandon its 5 remote-less commits
   first — landing them is one way, but pushing the branch is sufficient for P1.
4. **Remove the stale Codex project-trust entry.** Delete or repoint the
   `[projects."/home/danie906/.claude/conductor"]` table in `~/.codex/config.toml`, and sweep the
   other active-configuration surfaces named in P4. Historical transcripts and session logs are left
   alone.
5. **Install and smoke-test the intended plugin version.** Publish and install 0.9.3 so cache
   matches `main`, or record an explicit decision to decommission against 0.9.2. Then run the P6
   smoke test from a working directory outside the checkout and record which version answered.
6. **Declare a quiesce window, then quarantine, then remove.** Announce a window in which no session,
   agent, or editor works under the path; the quiesce conditions are checked only inside it, and a
   failed check means reschedule the window rather than fail the design. Then `mv` the old checkout
   to a dated quarantine directory, observe for one week, and delete only after a final re-run of all
   six checks against the quarantined copy.

## Order of operations

This governs the final verification pass, and is distinct from the remediation ordering in
*Execution preconditions* above. P6 is verified first, while the source is still present — verifying
that the cache copy works after deleting its source is not a test, which is why precondition 5
completes before precondition 6. Then the loss-risk gates P1, P2, P4, and P5, in that order and
before the quiesce window opens, since satisfying them is real work that takes time. P3 and the
remaining activity checks go last, inside the window, immediately before the `mv`.

## Reversibility

Deletion is the only irreversible step, and GitHub recovers only what was pushed to it. That is what
makes the loss-risk class load-bearing and the activity class not: a commit on no remote (P1), a
`.conductor/` run file (P2), or a worktree's handoff state (P5) has no copy anywhere else, whereas a
running editor is an inconvenience to schedule around. It is also why P2 must be run with
`--ignored` — the ignored paths are precisely the ones GitHub never saw. The final step is `mv` to a
dated quarantine directory, not `rm -rf`, with removal after an observation window. This preserves the roadmap's one-week removal predicate
(roadmap line 487) at lower cost.

## Out of scope

Codex install. Host adapter support is Plan 04 and is unwritten, so S2 means Claude only until
Plan 04 lands. Also out of scope: any change to `conductor/core`, the run-key gate, or Plan 02.

## Testing

The six predicates become `scripts/decommission-check.sh`, exiting non-zero with the failing list,
and reporting the loss-risk failures and the quiesce failures under separate headings — they mean
different things and a quiesce failure is rescheduled, not remediated. Run before quarantine and
again after. Three requirements on the script, one per defect this document has had to correct:

- P1 must be built on `git log --branches --not --remotes`, never on `@{u}`. An `@{u}`-based check
  cannot see an upstream-less branch, which is the only case that has ever failed here.
- P2 must invoke `git status --porcelain -uall --ignored` and print the `.conductor/` entries it
  finds rather than a count.
- P5 must run P1's and P2's commands inside each linked worktree and fail on any worktree without a
  recorded outcome, not merely enumerate them.
