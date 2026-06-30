# E6 — Install Smoke Runbook

`smoke.sh` is the live **install + machinery** smoke for the conductor plugin. It installs
conductor (and its `spec-craft` dependency) from the **published GitHub catalog**
(`automateintelligence/marketplace`) into your real `~/.claude`, then proves the installed
cluster works end to end on a tiny spec.

It is the only test that exercises the *real install path* — the published marketplace catalog,
dependency auto-install, preflight discovery, the `${CLAUDE_PLUGIN_ROOT}` CLI location, and the
installed done-gate. It tests **pushed** code (whatever the catalog's plugin repos serve on
their default branch); for pre-push local checks use `claude --plugin-dir ./conductor` +
`conductor preflight`. Everything else (`tests/`) runs against the working tree, not an install.

## When to run it

- After any change to the marketplace catalog (the `automateintelligence/marketplace` repo),
  `bin/conductor`, `conductor/preflight.py`, or the install instructions in the README.
- Before cutting a release / merging the install cluster.
- As a first-time dogfood check ("does `/plugin install` actually work?").

It is **deterministic and headless** — no GitHub, no nested `claude -p`, no cron. Safe in CI.

## Run

```bash
bash experiments/E6-install-smoke/smoke.sh
```

No env vars, no gating. Exit `0` = all markers green; non-zero = at least one failed (see the
`E6 SUMMARY` block it prints). It leaves conductor **installed** (by design — see Side effects).

### Prerequisites

- `claude` CLI on `PATH`.
- `python3` on `PATH` (the done-gate runner).
- For `[P4]` to pass, the *conducted* skill stack must be resolvable in this environment:
  `/spec-craft:*` (auto-installed as conductor's dependency), `/superpowers:*`, and the
  environment-provided `/code-review`, `/codex`, `/document-release`. A bare machine without the
  superpowers/gstack stack will fail `[P4]` — that is an environment gap, not an E6 bug.

## What each marker validates

| Marker | Checks | Proves |
|--------|--------|--------|
| `[P1] INSTALL/UPDATE` | Adds the `automateintelligence/marketplace` GitHub catalog (or updates it), then installs `conductor@automateintelligence` (or updates it). Idempotent. | The published catalog is installable; `/plugin install` succeeds non-interactively. |
| `[P2] PLUGINS PRESENT` | `claude plugin list` shows **both** `conductor@` and `spec-craft@`. | The `dependencies: ["spec-craft"]` declaration auto-installs the dependency. |
| `[P3] CLI REACHABLE` | Resolves the installed bin by globbing `~/.claude/plugins/cache/*/conductor/*/bin/conductor` (newest version), asserts it is executable. | The CLI is reachable from the cache even though plugins are not on `PATH`. |
| `[P4] PREFLIGHT` | The **installed** `<bin> preflight` exits 0. | The whole conducted skill stack resolves post-install (the preflight-discovery fix). |
| `[P5] MACHINERY` | The installed `<bin> assert run --level spec` goes RED (exit 1, no `hello.py`) then GREEN (exit 0, after writing `hello.py`) on a tiny `hello()` manifest in a temp dir. | The installed done-gate runs the real RED→GREEN loop. |

## Failure triage

| Failing marker | Likely cause | Triage |
|----------------|--------------|--------|
| `[P1]` | `claude` not on PATH; bad `marketplace.json`; network. | `which claude`; `claude plugin validate . --strict`; `claude plugin marketplace list`. |
| `[P2]` — conductor missing | Install didn't land (usually `[P1]` already failed). | Re-read `[P1]` output; `claude plugin list`. |
| `[P2]` — spec-craft missing | Dependency did **not** auto-install. | Confirm `marketplace.json` lists `spec-craft` with a reachable source; try `claude plugin install spec-craft@automateintelligence`. |
| `[P3]` | No bin in the cache (install didn't materialize) or a different cache layout. | `find ~/.claude/plugins/cache -path '*conductor*/bin/conductor'`. |
| `[P4]` — `MISSING: /spec-craft:*` | Dependency problem — same as `[P2]`. | See `[P2]` row. |
| `[P4]` — `MISSING: /superpowers:*` or `/codex` etc. | Those conducted skills aren't installed in this environment. | Install the superpowers/gstack stack, or accept that a bare env can't pass preflight. For a dev tree, `export CONDUCTOR_PLUGIN_DIRS=<spec-craft path>` (not needed once installed). |
| `[P5]` — RED not 1 | Runner returned 3 (unparseable manifest) or 6 (a frozen baseline leaked — E6 already points `CONDUCTOR_FREEZE_BASELINE` at a nonexistent path to prevent this). | Read the runner output above the marker; check `python3` works. |
| `[P5]` — GREEN not 0 | `hello.py` not importable (module resolution) or `python3` missing. | The runner executes with `cwd = plugin dir`, so import is via `PYTHONPATH` (E6 sets it); confirm `python3 -c "import sys"`. |

## Side effects

- **Installs conductor + spec-craft into your real `~/.claude`** (scope user) and adds the
  `automateintelligence` marketplace to your user settings. **No teardown** — this is intentional
  (the dogfood policy keeps conductor installed).
- The installed runner writes `assertions/run/results.json` **inside the plugin cache dir** during
  `[P5]` (harmless).
- The temp spec dir is `mktemp`-created and removed on exit. The repo and all git remotes are
  untouched.

Re-running is safe and idempotent: marketplace and plugin both take the **update** path on
subsequent runs.

## Undo the install (optional)

E6 deliberately does not uninstall. To revert manually:

```bash
claude plugin uninstall conductor              # remove conductor
claude plugin prune                            # drop spec-craft if nothing else needs it
claude plugin marketplace remove automateintelligence
```

## Related

- `experiments/E5-end-to-end/promote_check.sh` — the **full live agent loop** (install-first via
  the same helper, then `/conductor:start` + `/conductor:autodev` with real GitHub + cron).
  Gated `RUN_CONDUCTOR_E2E=1`; needs an interactive `claude` and a target repo.
- `experiments/lib/install_conductor.sh` — the shared install/update helper used by both E6 and E5.
- `tests/conductor/test_e2e.py` — the offline, deterministic machinery E2E (no install).
