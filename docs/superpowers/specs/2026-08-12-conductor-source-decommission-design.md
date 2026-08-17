# Conductor source decommission

**Date:** 2026-08-12

**Amended:** 2026-08-16 — scope decisions confirmed, P2/P4/P5 tightened, execution preconditions added

**Status:** Design approved and both scope decisions confirmed by the owner. **Execution is
deferred.** As of the readings below, four predicates are blocked or undecided and the other two
hold only conditionally. Nothing is deleted, moved, or quarantined until the execution preconditions
are satisfied.

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

Six runnable checks. Each states what must hold; current readings are in the next section.

| ID | Predicate | Check |
| --- | --- | --- |
| P1 | No unpushed commits | `git log origin/main..main` empty, and every local branch tip either an ancestor of `main` or present on `origin`. A branch with no upstream is not automatically safe: confirm with `git branch -r --contains <branch>` that some remote ref carries the tip. |
| P2 | No untracked **or ignored** content of value | `git status --porcelain -uall --ignored`. The plain `-uall` form is not sufficient and was the first defect this amendment corrects: by construction it omits ignored paths, and `.conductor/` is ignored, so the run state most at risk — the only copy of anything that never reached GitHub — is invisible to it. `--ignored` takes an optional mode and defaults to `traditional`, which combined with `-uall` lists individual files inside ignored directories rather than collapsing them to a directory entry. The check must enumerate `.conductor/` contents explicitly rather than trusting a summary count. |
| P3 | No durable process rooted here | Scan `/proc/*/cwd`, and `crontab -l` clean of conductor entries. Transient interactive tooling with a cwd here (editors, agent sessions, MCP servers, language servers) does not fail this; a conductor driver, cron entry, or service unit does. |
| P4 | Nothing external names the path | Shell rc files, `~/.claude/settings.json`, hooks, `.conductor/resume-env.sh`, **and host-level trust and project registries that pin absolute paths** — notably `~/.codex/config.toml`, which carries a `[projects."<absolute path>"]` table trusting this checkout by literal path. Distinguish **active configuration**, which must be repointed or removed, from **historical transcripts and session logs**, which record the old path as a fact about the past. Historical records are out of scope: do not chase them. |
| P5 | Every worktree dispositioned | `git worktree list`, then an explicit recorded outcome — archive, discard, or keep — for each linked worktree. Accounting alone is not enough, which was the second defect this amendment corrects: a worktree can sit on a fully merged branch and still hold untracked or ignored state that exists nowhere else. `git worktree remove` runs only after that worktree is clean **and** its state is preserved; it refuses a dirty tree without `--force`, and `--force` is what discards the state this predicate exists to protect ([git-worktree](https://git-scm.com/docs/git-worktree.html)). |
| P6 | Plugin works standalone | Conductor verbs run from a working directory outside the checkout while the source still exists, against the intended cached version. |

## Current status

Readings taken 2026-08-16 in `/home/danie906/.claude/conductor` on `main` at `9cdeebd`.

| ID | Status | Reading |
| --- | --- | --- |
| P1 | **Blocked** | `main` is neither ahead of nor behind `origin/main` — both at `9cdeebd`. 22 of 23 local branch tips are ancestors of `main`, including the local-only `worktree-agent-a5e09b4b44bc7a99d`, `worktree-agent-a92ff8afb354ce573`, and `0.5.0-run-packet`. The exception is `fix/production-review-findings`: 4 commits ahead of `main`, no upstream, and no remote ref contains its tip. This is live work in `.worktrees/prod-review-fixes` and postdates the 2026-08-12 reading, when P1 did pass. |
| P2 | **Blocked** | 15 untracked paths under `-uall`: 11 scratch files in `.gstack/tmp/`, 3 `.serena/` project files, and `docs/reviews/2026-07-31-conductor-assertions-source-naming-fix-prompt.md`. Adding `--ignored` surfaces 221 further paths that plain status does not report, including `.conductor/goal.md`, `.conductor/resume-env.sh`, and `.conductor/run_branch` — all dated 2026-07-06 and, unlike the caches and build artifacts around them, not reproducible. |
| P3 | **Conditional pass** | No conductor entry in the 20-line crontab, no systemd user unit naming conductor, no durable driver. 59 processes currently hold a cwd under the path, but all are transient interactive tooling — Claude Code, `codex`, MCP servers, `pyright-langserver`, shells — including the session taking these readings. Recheck immediately before quarantine rather than treating this as settled. |
| P4 | **Blocked** | `~/.codex/config.toml` line 54 is `[projects."/home/danie906/.claude/conductor"]` — an active trust entry naming the old path exactly. The remaining P4 surfaces (rc files, hooks, `settings.json`) have not been swept in this pass. |
| P5 | **Needs disposition** | **Four** linked worktrees, not the three recorded on 2026-08-12. Three sit on branches fully merged into `main`; the fourth does not. Two hold state that exists nowhere else. No outcome has been recorded for any of them. See the disposition table below. |
| P6 | **Partial pass** | The cache holds 0.1.0, 0.4.0, 0.4.1, 0.8.1, 0.9.1, and 0.9.2. `main` is at 0.9.3, which is not installed, so the standalone path currently exercises code one version behind source. Owner reports conductor verbs running from an unrelated repository against cached 0.9.2; that run was not repeated in this pass. |

### Worktree disposition

Outcome column is unfilled by design — P5 is not satisfied until the owner records one per row.

| Worktree | Branch | Merged into `main` | State held | Outcome |
| --- | --- | --- | --- | --- |
| `~/.claude/conductor-run-2026-07-05-self-enforcement` | `conductor/run-2026-07-05-self-enforcement` | yes | clean of untracked; 236 ignored paths including `.conductor/handoff.md` and `.conductor/run_branch` | *(undecided)* |
| `.claude/worktrees/agent-a92ff8afb354ce573` | `0.5.0-run-packet` | yes | 3 untracked Bubo files (`.bubo/config.json`, `.bubo/reviews.jsonl`, `.bubo/state.json`); 63 ignored paths | *(undecided)* |
| `.claude/worktrees/codex-dual-host-design` | `docs/codex-dual-host-design` | yes | clean of untracked; 105 ignored paths | *(undecided)* |
| `.worktrees/prod-review-fixes` | `fix/production-review-findings` | **no** — 4 unpushed commits | 4 modified tracked files, active work | keep; blocks P1 and P5 until landed |

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
   state is preserved. `.worktrees/prod-review-fixes` must land or be abandoned first, since it
   holds the only copy of four commits.
4. **Remove the stale Codex project-trust entry.** Delete or repoint the
   `[projects."/home/danie906/.claude/conductor"]` table in `~/.codex/config.toml`, and sweep the
   other active-configuration surfaces named in P4. Historical transcripts and session logs are left
   alone.
5. **Install and smoke-test the intended plugin version.** Publish and install 0.9.3 so cache
   matches `main`, or record an explicit decision to decommission against 0.9.2. Then run the P6
   smoke test from a working directory outside the checkout and record which version answered.
6. **Quarantine, then remove.** `mv` the old checkout to a dated quarantine directory, observe for
   one week, and delete only after a final re-run of all six checks against the quarantined copy.

## Order of operations

This governs the final verification pass, and is distinct from the remediation ordering in
*Execution preconditions* above. P6 is verified first, while the source is still present — verifying
that the cache copy works after deleting its source is not a test, which is why precondition 5
completes before precondition 6. Then P1 and P2, then P3, P4, and P5, then quarantine.

## Reversibility

Deletion is the only irreversible step, and GitHub recovers tracked content only — never untracked
files and never ignored ones. That is what makes P2 load-bearing, and why P2 must be run with
`--ignored`: an unpushed branch tip, a `.conductor/` run file, or a worktree's handoff state has no
copy anywhere else. The final step is `mv` to a dated quarantine directory, not `rm -rf`, with
removal after an observation window. This preserves the roadmap's one-week removal predicate
(roadmap line 487) at lower cost.

## Out of scope

Codex install. Host adapter support is Plan 04 and is unwritten, so S2 means Claude only until
Plan 04 lands. Also out of scope: any change to `conductor/core`, the run-key gate, or Plan 02.

## Testing

The six predicates become `scripts/decommission-check.sh`, exiting non-zero with the failing list.
Run before quarantine and again after. Two requirements on the script, from the defects this
amendment corrects: P2 must invoke `git status --porcelain -uall --ignored` and print the
`.conductor/` entries it finds rather than a count, and P5 must fail on any linked worktree without
a recorded outcome, not merely enumerate them.
