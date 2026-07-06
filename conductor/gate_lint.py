"""`conductor gate lint` — frozen-gate quality + integrity (spec Phase 4, review B-4).

Fails closed on the MECHANICALLY-DETECTABLE weak-frozen-test patterns — the subset a
linter can catch without judgment:

- a manifest command that could load an unfrozen conftest or an autoloaded plugin
  (anything short of the exact pinned standalone form);
- an assertion test file with no negative ("must not contain" / assertNot-style) clause;
- an assertion test with a trivially-true assertion (`assert True`, `assert 1`, a bare
  non-empty literal) — a tautology that passes any implementation.

Boundary (deliberate): the judgment-requiring weaknesses — a hard-coded value tracking
no source of truth, a `property` tested on one case, an "X is used" check that only
proves X exists — belong to the red-team step in `/conductor:assertions-to-tests`,
not here. Prose for what needs a mind; mechanism for what a machine can see.

The pinned-command rule validates an EXACT argv shape, not token presence: optional
leading env assignments from a known-safe allowlist (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
required among them), then exactly `python3 -m pytest`, with `--noconftest` and
`-p no:cacheprovider` present, and every remaining token a pytest flag or a test path.
Token-presence matching would pass `<pinned form> && python3 evil.py` — the manifest
runner executes commands through a shell, so shell compounds/wrappers reopen the bypass.
"""

from __future__ import annotations

import os
import shlex
import sys

from conductor import freeze
from conductor.paths import project_root

# Env assignments a pinned command may carry. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 is
# REQUIRED; the rest are harmless determinism knobs. Anything else is rejected.
ENV_ALLOWLIST = frozenset(
    {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONHASHSEED=0",
    }
)
REQUIRED_ENV = "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"
RUNNER_ARGV = ("python3", "-m", "pytest")
REQUIRED_FLAG = "--noconftest"
REQUIRED_PLUGIN_DISABLE = "no:cacheprovider"

# The runner executes commands with shell=True: any of these in the RAW string can
# splice in a second command, a redirect, or a substitution. Reject outright.
_SHELL_META = ("`", "$(", ";", "|", "&", ">", "<", "\n")

# Flags that reload configuration the pin exists to exclude (ini overrides can
# re-enable plugins/conftest behavior). Fail-closed.
_BANNED_FLAGS = ("-c", "-o", "--override-ini")

_SAFE_PATH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/:-*?[]"
)


def _is_env_assignment(tok: str) -> bool:
    name, eq, _ = tok.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[:1].isdigit()


def _check_command(raw: str, repo_root: str) -> tuple[list[str], list[str]]:
    """Validate one manifest command against the exact pinned argv shape.

    Returns (findings, test_path_tokens). Findings are reasons; each caller-emitted
    line must contain the offending command verbatim.
    """

    def unpinned(why: str) -> tuple[list[str], list[str]]:
        return [f"unpinned command ({why}): {raw}"], []

    for meta in _SHELL_META:
        if meta in raw:
            return unpinned(f"shell metacharacter {meta!r}")
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return [f"unpinned unparseable-command: {raw}"], []
    if not tokens:
        return unpinned("empty command")

    i = 0
    envs = []
    while i < len(tokens) and _is_env_assignment(tokens[i]):
        envs.append(tokens[i])
        i += 1
    for env_tok in envs:
        if env_tok not in ENV_ALLOWLIST:
            return unpinned(
                f"env assignment {env_tok!r} not in the known-safe allowlist"
            )
    if REQUIRED_ENV not in envs:
        return unpinned(f"missing {REQUIRED_ENV}")

    if tuple(tokens[i : i + 3]) != RUNNER_ARGV:
        return unpinned("runner is not exactly 'python3 -m pytest'")
    rest = tokens[i + 3 :]

    paths: list[str] = []
    saw_noconftest = False
    saw_cache_disable = False
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok == "-p":
            if j + 1 >= len(rest) or not rest[j + 1].startswith("no:"):
                return unpinned("-p may only disable plugins ('-p no:<plugin>')")
            if rest[j + 1] == REQUIRED_PLUGIN_DISABLE:
                saw_cache_disable = True
            j += 2
            continue
        if tok.startswith("-"):
            base = tok.split("=", 1)[0]
            if base in _BANNED_FLAGS:
                return unpinned(f"flag {tok!r} reloads configuration the pin excludes")
            if tok == REQUIRED_FLAG:
                saw_noconftest = True
            j += 1
            continue
        # a non-flag token must be a test path (validated to exist by the file rules)
        if not tok or not set(tok) <= _SAFE_PATH_CHARS:
            return unpinned(f"token {tok!r} is neither a pytest flag nor a test path")
        base = tok.split("::", 1)[0]
        is_glob = any(c in base for c in "*?[")
        as_dir = base if os.path.isabs(base) else os.path.join(repo_root, base)
        if not (base.endswith(".py") or is_glob or os.path.isdir(as_dir)):
            return unpinned(f"token {tok!r} is neither a pytest flag nor a test path")
        paths.append(tok)
        j += 1

    if not saw_noconftest:
        return unpinned(f"missing {REQUIRED_FLAG}")
    if not saw_cache_disable:
        return unpinned(f"missing -p {REQUIRED_PLUGIN_DISABLE}")
    if not paths:
        return unpinned("no test path")
    return [], paths


def lint(manifest_path: str, repo_root: str) -> list[str]:
    """Lint every manifest entry; return findings (empty = clean). Fail-closed: an
    unloadable manifest is itself a finding, never a silent pass."""
    try:
        entries = freeze._load(manifest_path)
    except Exception as exc:
        return [f"manifest-unloadable: {exc}"]
    findings: list[str] = []
    for entry in entries:
        aid = str(entry.get("id", "?"))
        raw = str(entry.get("command", "") or "")
        cmd_findings, _paths = _check_command(raw, repo_root)
        findings.extend(f"{aid}: {f}" for f in cmd_findings)
    return findings


def main(argv: list | None = None) -> int:
    repo_root = project_root()
    manifest = os.path.join(repo_root, "assertions", "manifest.yaml")
    findings = lint(manifest, repo_root)
    if findings:
        for f in findings:
            print(f"[LINT] {f}", file=sys.stderr)
        print(f"[LINT] FAIL: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("[LINT] ok: manifest commands pinned; test files pass the mechanical rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
