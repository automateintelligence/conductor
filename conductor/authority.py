"""Session-mode-aware unattended authority (spec Phase 1, 2026-07-05 self-enforcement).

Owner decision (2026-07-06): conductor inherits Claude Code's existing permission model —
it invents NO permission flags or tokens of its own. This module is the tested core of that
decision:

- ``RECIPE_PRIVILEGED_OPS`` — the ONE declared set of privileged operations an autodev
  phase performs. `authority preview` (and `/conductor:start`'s less-privileged dry-run)
  iterate THIS set, so the report can never drift from the declaration.
- ``resolve_posture`` — maps a detected (possibly unknown/unreadable) session permission
  mode to the run's posture, FAIL-CLOSED: a misread can only ever under-grant, never
  over-grant (frozen invariant A2).
- ``write_resume_env`` — the only way conductor writes ``.conductor/resume-env.sh``. The
  file can carry the bypass flag and a shell-executed ``CONDUCTOR_MERGE_VERIFY`` command,
  so it is mode 0600 in every case (frozen invariant A3).
"""

from __future__ import annotations

import os
import re
import shlex

# The privileged operations one autodev phase performs (the per-phase recipe: implement on
# a phase branch -> review -> PR -> gated merge into the run branch). Each privileged verb
# the spec names (branch, push, gh pr, merge, docker via CONDUCTOR_MERGE_VERIFY, subagent,
# writes) has its own DISTINCT entry — frozen A1 rejects a mega-string.
RECIPE_PRIVILEGED_OPS: frozenset[str] = frozenset(
    {
        "create the phase branch (git branch/checkout, forked from the run branch)",
        "git push (phase branch + run branch to the remote)",
        "gh pr create/comment (open the phase PR, post review comments)",
        "conductor merge <pr> (gated gh-based merge into the run branch)",
        "docker via CONDUCTOR_MERGE_VERIFY (the owner's verify command runs as shell)",
        "subagent spawn (fresh implementation subagent per phase)",
        "file writes (broad repo edits across the worktree)",
    }
)

# Affirmative EXACT matches only — substring/prefix matching would let an ambiguous or
# token-embedded mode string over-grant ("bypassPermissions extra" MUST stay supervised).
_BYPASS_MODES = frozenset({"bypassPermissions"})
_MODE_POSTURE = {"default": "supervised", "plan": "supervised", "acceptEdits": "scoped"}

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def resolve_posture(mode: str | None) -> str:
    """Fail-closed: anything not an affirmatively-recognized bypass mode never returns a
    bypass posture; unknown/empty/None/ambiguous resolves to supervised (spec A2)."""
    if not isinstance(mode, str):
        return "supervised"
    m = mode.strip()
    if m in _BYPASS_MODES:
        return "full-bypass"
    return _MODE_POSTURE.get(m, "supervised")


def write_resume_env(project_root: str, env: dict[str, str]) -> str:
    """Write ``<project_root>/.conductor/resume-env.sh`` (mode 0600, always) and return its
    path. Each line is ``KEY={shlex.quote(value)}`` — never wrapped in extra double quotes,
    which would smuggle literal quote characters into the driver's unquoted
    ``${CONDUCTOR_RESUME_CLAUDE_FLAGS:-}`` expansion. Keys are validated BEFORE anything is
    written, so a bad env never leaves a partial file behind."""
    for key in env:
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid env key name: {key!r}")
    path = os.path.join(project_root, ".conductor", "resume-env.sh")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in env.items())
    # 0600 at creation (never umask-dependent), then an unconditional chmod so a
    # pre-existing looser file is tightened, not inherited.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    finally:
        os.chmod(path, 0o600)
    return path
