import glob
import os
import sys
from typing import TypedDict


class CheckResult(TypedDict):
    ok: bool
    missing: list[str]


# Exact commands the recipe (T5) + setup (T6) invoke. Bare names are environment-provided
# (gstack/codex here) and may be a user skill OR a plugin skill (matched suffix below).
REQUIRED_COMMANDS = [
    "/spec-craft:expectations",
    "/spec-craft:executable-assertions",
    "/conductor:assertions-to-tests",
    "/superpowers:subagent-driven-development",
    "/superpowers:requesting-code-review",
    "/superpowers:receiving-code-review",
    "/superpowers:writing-plans",
    "/code-review",
    "/codex",
    "/document-release",
]


def available_commands(claude_home: str | None = None) -> set[str]:
    """Discover invocable slash-command names from disk. user skills -> bare; plugin
    skills/commands -> '<plugin>:<name>'. (Runtime invocability is confirmed by the T7 smoke;
    this is the static availability gate.)"""
    home = claude_home or os.path.expanduser("~/.claude")
    cmds: set[str] = set()
    for md in glob.glob(f"{home}/skills/*/SKILL.md"):
        cmds.add(os.path.basename(os.path.dirname(md)))
    for path in glob.glob(f"{home}/plugins/cache/*/*/*/skills/*/SKILL.md"):
        parts = path.split(os.sep)
        plugin = parts[parts.index("cache") + 2]
        cmds.add(f"{plugin}:{os.path.basename(os.path.dirname(path))}")
    for path in glob.glob(f"{home}/plugins/cache/*/*/*/commands/*.md"):
        parts = path.split(os.sep)
        plugin = parts[parts.index("cache") + 2]
        cmds.add(f"{plugin}:{os.path.basename(path)[:-3]}")
    return cmds


def _present(cmd: str, avail: set[str]) -> bool:
    c = cmd.lstrip("/")
    if ":" in c:
        return c in avail
    return c in avail or any(a.endswith(f":{c}") for a in avail)


def check(
    required: list[str] = REQUIRED_COMMANDS,
    available: set[str] | None = None,
) -> CheckResult:
    avail = available if available is not None else available_commands()
    missing = [c for c in required if not _present(c, avail)]
    return {"ok": not missing, "missing": missing}


if __name__ == "__main__":
    result = check()
    ok: bool = result["ok"]
    missing: list[str] = result["missing"]
    for cmd in missing:
        print(f"MISSING: {cmd}", file=sys.stderr)
    sys.exit(0 if ok else 1)
