from __future__ import annotations

import os

from conductor.paths import project_root

REQUIRED = [
    "goal",
    "paths",
    "active_plan",
    "milestone",
    "phase_issue",
    "phase_status",
    "baseline",
    "final",
    "last_unit_summary",
    "next_unit",
    "open_issues",
    "branch",
    "resume_cmd",
]


def build(ctx: dict) -> str:
    p: dict = ctx["paths"]
    oi: dict = ctx["open_issues"]
    return f"""# Conductor handoff

**Goal / done:** {ctx["goal"]}  (done = `conductor assert run --level spec` exits 0)

**Reference docs:** spec={p["spec"]}; expectations={p["expectations"]}; assertions={p["assertions"]};
plan-index={p["plan_index"]}; ADRs={p["adr_dir"]}

**Active:** plan={ctx["active_plan"]}; milestone=#{ctx["milestone"]}; phase issue #{ctx["phase_issue"]} ({ctx["phase_status"]})

**Last unit:** {ctx["baseline"]}..{ctx["final"]} — {ctx["last_unit_summary"]}
**Next unit:** {ctx["next_unit"]}

**Open:** debt={oi["debt"]} feature={oi["feature"]} blocked={oi["blocked"]}
**Branch/worktree:** {ctx["branch"]}

**Resume:** `{ctx["resume_cmd"]}`
"""


def write(ctx: dict, path: str | None = None) -> str:
    """Write the handoff into the PROJECT's ``.conductor/`` (goal + handoff live with the
    project, not the plugin dir). A relative ``path`` resolves against the project root; an
    absolute ``path`` is used as-is; the default is ``<project>/.conductor/handoff.md``."""
    missing = [k for k in REQUIRED if k not in ctx]
    if missing:
        raise ValueError(f"handoff missing required fields: {missing}")
    if path is None:
        path = os.path.join(project_root(), ".conductor", "handoff.md")
    elif not os.path.isabs(path):
        path = os.path.join(project_root(), path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(ctx))
    return path
