# Conductor self-enforcement review — bypass-grant UX + prose-decay fragility (2026-07-05)

This consolidates a 4-agent parallel review (security, DX, architecture, and QA lenses) of conductor 0.5.2. Throughout this document, **CONFIRMED** means the finding was reproduced against source by the reviewer; **plausible** means it was agent-claimed but not independently reproduced.

## Verdict

Both prose-decay lenses converged independently on the same finding: conductor is mechanically strong at the leaves and prose-fragile at the joints. The Python gates (`assert run`, `freeze`, `merge_gate`, `ledger`, `resume_script`, `remote`) are fail-closed and tested, but *choosing to invoke and obey them* is still prose, and on the free-plan repos conductor targets there is no server-side backstop. Both bypass lenses converged too: the bypass is a DX flaw, not a design flaw. The least-privilege path isn't real to an operator, so the dangerous one-liner wins by inertia.

## Section A — Bypass-grant UX

| ID | Severity | Finding | Fix |
| --- | --- | --- | --- |
| A-1 | P1 | Least-privilege not scaffolded; no `settings.json`/`resume-env.sh` example anywhere in the tree; the flag that loads an allowlist is never even named, while `--dangerously-skip-permissions` is a paste-ready one-liner in three files (CONFIRMED) | Ship `conductor resume-script grant --scoped\|--full`, default scoped, writing a working allowlist plus the exact `CONDUCTOR_RESUME_CLAUDE_FLAGS` line. Make least-privilege a paste. |
| A-2 | P1 | The authority decision is entirely absent from README (CONFIRMED: grep of README for permission/bypass/allowlist = 0 hits) | Add an "Unattended authority" subsection to README §3 stating the decision and both options. |
| A-3 | P2 | The safe path's failure mode is the same silent stall the driver exists to prevent (a too-tight allowlist hangs the fire), so the product rewards the dangerous choice with instant success | grant-generated allowlist tested to actually complete a fire; a `--check-permissions` dry-run tick reporting which prompts it would hit. |
| A-4 | P2 | The `write` nudge is a category error ("put a `settings.json` in `resume-env.sh`": one is JSON claude loads, one is a sourced shell file) AND is gated on file-absent, so a re-run where `resume-env.sh` exists but has no permission posture is never nudged (the securitysight migration case). Note: the agents' stronger claim "nudge never fires" is CONFIRMED FALSE for fresh setup, since resume-script write runs before the env bullet, but the fix still holds for the re-run case | Split the nudge into two concrete branches; gate on "posture undecided," not "file missing." |
| A-5 | P2 | No confirmation/expiry on full bypass, set-and-forget, survives reboots | `grant --full` needs an explicit `--i-understand-standing-full-access` token; optional expiry the driver checks. |
| A-6 | P2 | No audit trail of posture: the fire log records everything except which authority it ran with | Log `posture=full-bypass\|scoped\|supervised` at fire-start; a committed secret-free `UNATTENDED-POSTURE.md` marker. |
| A-7 | P3 | Bypass outlives its run: the autodev STOP branch removes cron but not `resume-env.sh`, so a later different run silently re-arms full access | Disarm the flag on green, or surface loudly that it persists. |
| A-8 | P3 | `resume-env.sh` is sourced with no ownership/mode check, and it also feeds `CONDUCTOR_MERGE_VERIFY` into `shell=True`, so a world-writable `.conductor/` is a local privesc path into a full-access agent (sharpest security finding) | `chmod 0600` on create; driver refuses to source a group/world-writable file (ssh-style guard). |
| A-9 | P4 | Two flag spellings for the bypass, both in `recovery.md` (`--dangerously-skip-permissions` vs `--permission-mode bypassPermissions`) | Pick one canonical spelling. |

## Section B — Prose-decay fragility (the "invoke-and-obey-the-gate" class)

| ID | Severity | Finding (CONFIRMED unless noted) | Fix |
| --- | --- | --- | --- |
| B-1 | CRITICAL | Merge gate is advisory, not interposed. No `conductor merge` wrapper exists (CONFIRMED); a worker can skip merge-gate and `gh pr merge --admin` straight to the run branch, or land the final PR on main unreviewed. `merge-gate` does refuse the final PR via base-mismatch, but only if consulted | `conductor merge <pr>`: run the gate, merge only on `ok`, refuse `base=default`. Swap recipe plus needle from `merge-gate` to `merge`. |
| B-2 | high | Green-but-unmergeable phase loops forever. "PROGRESS SELF-CHECK" is two words of prose; `commits_since_baseline` is a reserved, unused reconcile param (CONFIRMED `reconcile.py:13`) and the retry cap fires only on `tests_red` (CONFIRMED `reconcile.py:54`). Ties to the Codex-fallback: a `CONDUCTOR_REVIEW_AUTHOR`-pinned run makes a green phase un-mergeable -> this exact loop | Wire the reserved param: bump a durable no-progress counter for an owned in-progress phase making no progress, independent of `tests_red`; at cap -> `blocked` plus escalate. |
| B-3 | high | Crontab install/removal is hand-typed shell in two skills, the untested other half of the 0.5.2 fix; two hand-derived markers must byte-match, a `--show-toplevel` slip means cron fires forever or strips wrong lines | `conductor resume-script install-cron/uninstall-cron` owning the marker derivation once; both skills call it. |
| B-4 | high | Frozen-gate quality holes: no validator that the manifest command is pinned (`--noconftest`) so an unfrozen conftest is a gate bypass (CONFIRMED no validator); no `gate freeze` needle so the freeze step can silently rot out (CONFIRMED); nothing checks a frozen test is good (has a must-not clause); `.assertions.md` is not itself frozen | `conductor gate lint` at start pre-freeze; add `gate freeze` needle plus runtime WARN if unfrozen; hash `.assertions.md` in freeze. |
| B-5 | med | Cross-skill string contracts with no shared deriver: run-branch slug derived in prose in two skills (CONFIRMED no code deriver); default branch hardcoded (same class as the origin bug `remote.py` fixed); missing `run_branch` file silently disables the `merge-gate` base leg (CONFIRMED returns `None`); `CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE` has no code consumer (CONFIRMED) | `conductor run-branch name <spec>` plus `conductor default-branch` (mirror `remote.py`), fold into `conductor run-sync`; `merge-gate` emits `topology-off:no-run_branch`; actually consume the `ALLOW_DIRECT` env. |
| B-6 | med | Tier-B install is conditioned on the agent correctly reading the CronCreate response, should be fail-closed unconditional | `conductor driver install` always writes script plus cron; `conductor driver status` exits non-zero unless a durable driver exists. |
| B-7 | med | "Worker must NEVER modify run infra" is pure prose, already violated once (2026-07-02, a worker rewrote its own watchdog) (plausible) | Freeze-style digest over resume script plus crontab plus merge-env; autodev preflight verifies unchanged. |

## Also found (CLI/DX quick wins, all CONFIRMED)

- `conductor merge-gate --help` crashes with a raw `ValueError` traceback (unguarded `int(sys.argv[1])` at `merge_gate.py:286`).
- `conductor preflight` (the documented "verify" step) is silent on success, a blank line, indistinguishable from doing nothing.
- `conductor remote` prints a bare `origin` with no context and ignores `--help`.

## Do NOT mechanize (architect's restraint note)

"Build to the spec / gate-green necessary-not-sufficient," "ask no questions," and per-task commit granularity are correctly left as prose. Mechanizing "did the agent honor intent" would manufacture false confidence.

## Recommended build order

1. `conductor merge <pr>` (B-1): unskippable gate, highest blast radius, smallest surface.
2. Wire `commits_since_baseline` into reconcile (B-2): activates stubbed plumbing, kills the green-but-stuck loop.
3. `gate lint` plus `gate freeze` needle (B-4).
4. A-1 plus A-2 (`grant --scoped` plus README authority section): flips the bypass inertia.
5. Cron subcommands plus run-sync/run-branch/default-branch (B-3/B-5/B-6).
6. CLI quick wins (`merge-gate` crash, `preflight` silence): trivial.

## Implementation note

B-1, B-2, and the three CLI quick wins are being bootstrapped by hand (they are self-referential: the autonomous run needs them live before it starts). The remaining additive items (A-1/A-2, B-3/B-4/B-5/B-6) are scoped for an autonomous conductor run against `docs/specs/2026-07-05-self-enforcement.md`.
