# Conductor source decommission

**Date:** 2026-08-12

**Status:** Approved design; two scope decisions pending owner confirmation

**Repositories:** automateintelligence/conductor, automateintelligence/marketplace

This supersedes the relocation approach in Plan 00 of
`docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md` (lines 80–110). Two scope decisions
are recorded here and are pending owner confirmation: the packaging boundary work is dropped, and
Plan 00 becomes a decommission checklist rather than a relocation runbook.

## Goal

Retire `~/.claude/conductor` as the canonical checkout. Conductor is consumed as an installed
plugin from `~/.claude/plugins/cache/`. When a dev checkout is next needed it is a fresh
`git clone` into `~/programming/conductor`, not a move of the old one.

## Why this replaces the move

Plan 00's runbook had to recreate linked worktrees whose metadata is absolute; roadmap line 98
calls that the highest-risk item. A fresh clone creates worktrees from the new root, so nothing is
repointed and nothing can dangle. The blocking precondition weakens from "no process may hold
anything under this path during a rename" to "nothing unpushed remains before deletion."

## Why the packaging boundary was dropped

The installed package at `~/.claude/plugins/cache/automateintelligence/conductor/0.9.2/` is 2.0 MB:
roughly 500K runtime (`bin`, `ledger`, `skills`, `conductor`, `.claude-plugin`) and 1.5M
non-runtime (`docs` 668K, `tests` 576K, the `assertions` gate 128K, `experiments` 108K).

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

Six runnable checks.

| ID | Predicate | Check |
| --- | --- | --- |
| P1 | No unpushed commits | `git log origin/main..main` empty and no local branch ahead of its remote. As of 2026-08-12 both hold; the local-only branches `worktree-agent-a5e09b4b44bc7a99d` and `worktree-agent-a92ff8afb354ce573`, and `0.5.0-run-packet` whose upstream is gone, are all ancestors of `main`. |
| P2 | No untracked content of value | `git status --porcelain -uall`. As of 2026-08-12 that is scratch files under `.gstack/tmp/`, `.serena/` project configuration, and `docs/reviews/2026-07-31-conductor-assertions-source-naming-fix-prompt.md`. |
| P3 | No live process rooted here | Scan `/proc/*/cwd`, and `crontab -l` clean of conductor entries. |
| P4 | Nothing external names the path | Shell rc files, `~/.codex/`, `~/.claude/settings.json`, hooks, `resume-env.sh`. |
| P5 | Worktrees accounted for | `git worktree list`. As of 2026-08-12 that is three linked worktrees — `.claude/worktrees/agent-a92ff8afb354ce573` and `.claude/worktrees/codex-dual-host-design` nested under the checkout, and the sibling `~/.claude/conductor-run-2026-07-05-self-enforcement` — on branches `0.5.0-run-packet`, `docs/codex-dual-host-design`, and `conductor/run-2026-07-05-self-enforcement`, all already merged into `main`. |
| P6 | Plugin works standalone | Conductor verbs run from a working directory outside the checkout while the source still exists. |

## Order of operations

P6 runs first, while the source is still present. Verifying that the cache copy works after
deleting its source is not a test. Then P1 and P2, then P3, P4, and P5, then quarantine.

## Reversibility

Deletion is the only irreversible step, and GitHub recovers tracked content only, which makes P2
load-bearing. The final step is `mv` to a dated quarantine directory, not `rm -rf`, with removal
after an observation window. This preserves the roadmap's one-week removal predicate (roadmap
line 487) at lower cost.

## Out of scope

Codex install. Host adapter support is Plan 04 and is unwritten, so S2 means Claude only until
Plan 04 lands. Also out of scope: any change to `conductor/core`, the run-key gate, or Plan 02.

## Testing

The six predicates become `scripts/decommission-check.sh`, exiting non-zero with the failing list.
Run before quarantine and again after.
