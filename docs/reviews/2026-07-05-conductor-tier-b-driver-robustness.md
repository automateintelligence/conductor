# Conductor Tier-B driver: silent-stall root cause + robustness proposal

**For:** the `conductor/` plugin repo (driver generation in `/conductor:start` step 6, and the
reconcile paths in `/conductor:start` + `/conductor:autodev`).
**From:** a live conductor run (SecuritySight, Historical Similarity Search, milestone 2) that
silently stalled for ~1 day and was recovered by hand on 2026-07-05.
**Severity:** high for unattended runs — the failure is silent and indefinite; the run looks
"resumable" but never progresses.
**Status:** RESOLVED upstream in conductor **0.5.2**. The generator is now the tested
`conductor resume-script write` (runtime bin resolution, fail-loud, owner env split into
`resume-env.sh`), `/conductor:start` reconcile re-verifies + regenerates via `conductor
resume-script verify`, and `conductor remote` replaces the hardcoded `origin` in the autodev
prose. The original proposal (below) is kept as the rationale; the live-run stopgap is retained
for the record.

---

## TL;DR

The Tier-B OS-cron fallback that `/conductor:start` writes (`.conductor/resume-autodev.sh`) pins
**absolute, generation-time bin paths** for both `claude` and the `conductor` CLI. Those paths rot
on the next runtime/plugin upgrade, and when they do the driver fails **silently every fire** — the
run just stops making progress with nothing surfaced. This is a fragility of *pattern*, not of the
specific paths: replacing the dead paths with fresh ones (the obvious fix, which we did as a stopgap)
re-arms the exact same trap for the next upgrade.

**Ask:** (1) resolve bins at *run* time in the generated script instead of baking them in, and
(2) make `reconcile` (both start and autodev) *re-verify the driver resolves* and regenerate it if
not, so a runtime/plugin upgrade self-heals instead of silently stalling.

---

## What broke

The generated Tier-B script contained (paths are this machine's):

```bash
CONDUCTOR="/home/<user>/.claude/plugins/cache/automateintelligence/conductor/0.4.1/bin/conductor"
CLAUDE_BIN="$(command -v claude || echo /home/<user>/.nvm/versions/node/v20.19.5/bin/claude)"
```

Two independent rots hit it:

1. **`CLAUDE_BIN` fallback — dead + wrong-by-migration.** Cron runs with a minimal `PATH`, so
   `command -v claude` returns empty and the hardcoded fallback is used. That fallback was captured
   at generation time as a **node-version-pinned nvm path**. It broke two ways at once:
   - the node version changed (nvm now has `v20.19.5`, `v22.20.0`, `v24.11.0`) — a versioned path is
     rot-by-construction; and
   - `claude` migrated to a **standalone binary** (2.1.x) living at a stable launcher
     `~/.local/bin/claude` → the nvm/node path never held claude at all.
   Every headless fire died with `.../v20.19.5/bin/claude: No such file or directory`.

2. **`CONDUCTOR` — pinned to the *generating* plugin version.** The run was created under conductor
   `0.4.1`; the plugin was later upgraded to `0.5.1`. The cron script still pointed at
   `.../conductor/0.4.1/bin/conductor`. Here 0.4.1 happened to survive in the cache, so the
   done-gate guard still executed — but a cache cleanup would have broken it, and either way a
   resumed run keeps running an *old* CLI's semantics forever after an upgrade.

### Why it was invisible

`resume-autodev.log` accumulated a day of identical `No such file or directory` lines and **nothing
surfaced them**. The three recovery layers documented in the skill all assume the *fallback itself*
runs; none watches whether the fallback can even launch. `CronCreate durable:true` is a no-op
(already noted in the skill — confirmed again here: the harness cron is session-only), so Tier-B is
the *only* durable driver — which makes its silent failure fatal to the whole run, not degraded.

---

## Impact

- Unattended run stalled indefinitely; no phase progress, no alert. Recovered only because a human
  re-invoked `/conductor:start` and read the log.
- Any run generated before a `claude`/node/plugin upgrade is exposed to the same silent stall. The
  claude→standalone-binary migration means *every* older run with the nvm fallback is affected.

---

## Stopgap applied (reference only — NOT the upstream fix)

On the affected machine we rewrote the script to point at the current bins and to run autodev in the
run worktree via `CONDUCTOR_HOME`. It works, but it **re-hardcodes** `~/.local/bin/claude` and
`.../conductor/0.5.1/bin/conductor` — i.e. it resets the same time bomb. Do not adopt this verbatim
upstream; adopt the runtime-resolution pattern below.

---

## Proposed upstream fix

### 1. Resolve bins at run time, not generation time

In the generated script, repair `PATH` for cron and resolve from it, with self-updating fallbacks:

```bash
# Cron gives a minimal PATH — re-add the known install roots before resolving.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# claude 2.x is a standalone binary at ~/.local/bin/claude; older installs are node scripts on PATH.
CLAUDE_BIN="$(command -v claude || true)"
[ -x "$CLAUDE_BIN" ] || CLAUDE_BIN="$HOME/.local/bin/claude"

# Never pin the plugin version — a resumed run should use the newest installed conductor.
CONDUCTOR="$(command -v conductor || true)"
[ -x "$CONDUCTOR" ] || CONDUCTOR="$(ls -d "$HOME"/.claude/plugins/cache/*/conductor/*/bin/conductor 2>/dev/null | sort -V | tail -1)"

# Fail LOUDLY if either is unresolvable (see §3) instead of proceeding to a broken fire.
if [ ! -x "$CLAUDE_BIN" ] || [ ! -x "$CONDUCTOR" ]; then
    printf '%s driver-unresolved claude=%s conductor=%s\n' "$(date -Is)" "$CLAUDE_BIN" "$CONDUCTOR" \
      >> "$PROJECT/.conductor/resume-autodev.log"
    exit 3
fi
```

Notes:
- Globbing `…/conductor/*/bin/conductor | sort -V | tail -1` self-heals across plugin upgrades and
  matches the version `/conductor:start` itself would pick. If the project must be pinned to a CLI
  major, record that in run config and select the newest *within* it.
- `~/.nvm/current` is not standard; prefer the stable `~/.local/bin` launcher for claude 2.x. If a
  run must support npm-global claude, resolve `readlink -f "$(command -v claude)"` at generation and
  store the *stable* launcher, never a `versions/<v>` path.

### 2. Make reconcile re-verify (and regenerate) the driver — the systemic fix

The point-fix above still can't cover a *future* unknown migration. The durable fix is to treat the
driver like any other reconciled artifact:

- On every `/conductor:start` reconcile **and** at the top of `/conductor:autodev`, verify the
  installed Tier-B script's `CLAUDE_BIN` and `CONDUCTOR` resolve to executables (and that the
  crontab marker lines still point at it). If not, **regenerate the script** from the current
  template and re-install — idempotently, same as run_branch re-derivation.
- This turns "claude/node/plugin upgraded underneath the run" from a silent-stall into a
  self-healing no-op on the next reconcile.

### 3. Surface repeated fire failures

Silent is the real defect. Cheap options, any of which would have caught this in minutes:
- On reconcile, tail `resume-autodev.log`; if the last N fires are non-launch errors
  (`No such file or directory`, `driver-unresolved`, non-zero before the autodev call), **warn the
  owner loudly** and (with §2) regenerate.
- Have the script write a heartbeat/last-success sentinel (`.conductor/last-fire`); reconcile flags
  a stale sentinel against the cron cadence.

---

## Secondary observations (lower priority)

- **Owner-checkout resume gap.** On resume from the owner's main checkout, registering the in-session
  (Tier-A) cron is unsafe: `/conductor:autodev` operates *on the run branch* (step 1b), so firing it
  from the owner checkout would branch-switch/dirty it — only a `CONDUCTOR_HOME`=worktree driver
  (Tier-B) can fire safely. But Tier-B's guard-(a) no-ops while any live claude session holds the
  project/worktree cwd. Net: a resumed run makes **zero progress until the owner's session goes
  headless**, with nothing saying so. Consider having the start reconcile either (a) offer to fire
  one autodev in the run worktree, or (b) state explicitly that progress resumes when the session
  closes.
- **`CONDUCTOR_RUN_BRANCH` env vs `.conductor/run_branch` file precedence.** `merge_gate._expected_base()`
  prefers the env var. A driver that exports the env *and* writes the file (belt-and-suspenders) can
  silently diverge if a future reconcile updates the file but not the exported env — env would win
  with a stale branch. Prefer a single source of truth (the file), or have the script read the file
  rather than re-export a literal.
- **`_remote_for()` is robust; the skill prose is not.** The merge-gate code correctly derives the
  git remote by URL (falls back to `origin`). But the autodev skill *prose* hardcodes
  `git fetch origin` / `git ls-remote origin` (steps 1, 1b). On repos whose remote isn't named
  `origin` (this one is `github`), a worker that follows the prose literally fails its
  run-branch-currency merge. Either normalize the prose to "the remote from `_remote_for`/`git remote`"
  or have the CLI expose the resolved remote for the worker to use.

---

## Environment facts / repro

- `claude`: standalone ELF, `~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.199`
  (no node dependency). `--version` → `2.1.199 (Claude Code)`.
- node (nvm): `v20.19.5`, `v22.20.0`, `v24.11.0` present; the generated fallback pinned `v20.19.5`.
- conductor plugin cache: `0.1.0`…`0.5.1` all present; run generated under `0.4.1`, upgraded to `0.5.1`.
- cron `PATH` is minimal (no `~/.local/bin`, no nvm bin) — the trigger for the fallback path.
- Repro: generate a run's Tier-B script under one claude/node/plugin layout, upgrade any of the
  three, let the `*/20` heartbeat fire headless → silent `No such file or directory`, run stalls.
