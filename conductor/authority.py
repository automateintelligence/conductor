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

import argparse
import os
import re
import shlex
import sys

from ledger.sync import parse_plan_md

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
    path. Each line is ``export KEY={shlex.quote(value)}`` — never wrapped in extra double
    quotes, which would smuggle literal quote characters into the driver's unquoted
    ``${CONDUCTOR_RESUME_CLAUDE_FLAGS:-}`` expansion. Keys are validated BEFORE anything is
    written, so a bad env never leaves a partial file behind.

    ``export`` is load-bearing, not cosmetic. The driver SOURCES this file
    (``resume_script.render``: ``. "$ENV_FILE"``) and then execs ``conductor assert run`` and
    ``claude -p /conductor:autodev``. Every variable here except
    ``CONDUCTOR_RESUME_CLAUDE_FLAGS`` — which the driver itself expands — is read by one of
    those CHILDREN: ``CONDUCTOR_SPEC_ROOTS`` and ``CONDUCTOR_PLUGIN_DIRS`` by ``conductor``,
    ``CONDUCTOR_MERGE_VERIFY`` by ``conductor merge`` a level below that, ``DOCKER_HOST`` by
    the docker CLI below THAT. A bare ``KEY=value`` is a shell variable: it exists in the
    driver and in nothing it launches, so the whole file worked interactively (where the
    values are already in the operator's environment) and silently did nothing under cron.

    Exporting at the point of definition is preferred to having the driver pass values on each
    child's command line because this file is OWNER-OWNED and hand-editable — the generated
    driver deliberately bakes in none of its contents, so an explicit pass would force the
    template to enumerate every key an owner might set, and each new variable would then need
    a ``TEMPLATE_VERSION`` bump plus a regeneration of every installed driver. It also reaches
    only the direct child, while ``DOCKER_HOST`` is needed two processes deeper."""
    for key in env:
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid env key name: {key!r}")
    path = os.path.join(project_root, ".conductor", "resume-env.sh")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())
    # 0600 at creation (never umask-dependent), then an unconditional chmod so a
    # pre-existing looser file is tightened, not inherited.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    finally:
        os.chmod(path, 0o600)
    return path


def preview(plan_text: str) -> str:
    """The less-privileged dry-run report (spec Phase 1 case B): for every phase of the
    plan, the concrete privileged operations one unattended autodev fire performs. The op
    lines are GENERATED by iterating ``RECIPE_PRIVILEGED_OPS`` — never a parallel literal
    list — so the report cannot drift from the declaration (frozen A1). Raises ValueError
    on a plan with no recognizable phases (fail-closed: an empty report would read as
    'nothing privileged happens')."""
    plan = parse_plan_md(plan_text)
    if not plan["phases"]:
        raise ValueError("no phases found in plan — nothing to preview")
    lines = [
        "Unattended authority preview — the privileged operations each unattended",
        "autodev fire performs, per phase. Promptability cannot be introspected from",
        "the plan alone, so FAIL-CLOSED every op below is marked owner-required: in a",
        "non-bypass session it PROMPTS unless the session's own permission config",
        "pre-authorizes it — a headless fire cannot answer, so that phase stalls until",
        "the owner is present.",
    ]
    for phase in plan["phases"]:
        lines.append("")
        lines.append(f"{phase['title']}:")
        for op in sorted(RECIPE_PRIVILEGED_OPS):
            lines.append(
                f"  - {op} — [owner-required: prompts unless the session pre-authorizes it]"
            )
    lines += [
        "",
        "Every op above is marked owner-required/manual because promptability cannot be",
        "introspected from the plan; a bypass session pre-authorizes all of them.",
        "Options: elevate (relaunch the session in bypass mode — you will be warned and",
        "asked to acknowledge), widen the session's own allowlist to cover the operations",
        "above, or proceed knowing exactly which steps will need you.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="conductor authority",
        description="Session-mode-aware unattended authority (dry-run preview).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser(
        "preview", help="per-phase privileged-operation report for a plan"
    )
    sp.add_argument("plan", help="path to the plan.md to preview")
    args = p.parse_args(argv)
    try:
        with open(args.plan, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"cannot read plan: {e}", file=sys.stderr)
        return 1
    try:
        report = preview(text)
    except ValueError as e:
        print(f"cannot preview {args.plan}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
