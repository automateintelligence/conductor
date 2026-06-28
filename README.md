# conductor

Autonomous **spec-completion loop**: state a goal, walk away, come back to either
finished work or a clear, recoverable note about why it stopped.

This repo is the **build** of the `/conductor` + `/autodev` design. It currently holds
the **Stage 0 framework-validation gate** (§11/§12 of the design) — the experiments
that must pass *before* the implementation plan is written.

## Layout

| Path | What |
|---|---|
| `docs/specs/2026-06-28-autodev-design.md` | The design being validated (copied from `orchestration/` for provenance). |
| `docs/stage0-results.md` | Per-experiment pass/fail + evidence (the §12 "Outstanding: experiment results"). |
| `docs/stage0-notes.md` | Design amendments discovered while running Stage 0. |
| `experiments/E0-…E5` | Tiny stubs, one dir per experiment. |
| `assertions/` | E4 assertion runner + `manifest.yaml` (the done-gate prototype). |

## Stage 0 gate

`E0–E5` must pass and be recorded before any `/writing-plans` or MVP build (design §11).
`E5` locks the composition (Option 1 in-session `/loop` vs Option 2 cloud `/schedule`
watchdog). See `docs/stage0-results.md` for status.

## Durability model (validated here)

- **Clock:** cron `/loop` (local, primary) — re-fires the worker on an interval.
- **Freshness (no `/clear`, no `/compact`):** each fire runs heavy work in a fresh
  `claude -p` process **or** a fresh subagent; context never balloons. A new process
  *is* a new session.
- **Cross-session recovery:** cloud `/schedule` (fresh container) for multi-day specs
  and crash recovery.
- **Ground truth is durable:** every iteration commits+pushes git and writes GitHub
  issues, so a fresh container resumes with zero local context.
