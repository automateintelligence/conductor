import re
from typing import Any

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.+?)\s+\[([^\]]+)\]\s*$", re.MULTILINE)
_TASK = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)
_CHECKLIST = re.compile(r"- \[[ xX]\] #(\d+)")


def parse_plan_md(text: str) -> dict[str, Any]:
    title_match = _H1.search(text)
    title = title_match.group(1).strip() if title_match else ""

    phases: list[dict[str, Any]] = []
    phase_positions: list[int] = []

    for m in _H2.finditer(text):
        phases.append(
            {"title": m.group(1).strip(), "status": m.group(2).strip(), "tasks": []}
        )
        phase_positions.append(m.end())

    for phase, pos in zip(phases, phase_positions):
        # Determine the end of this phase's section (start of next H2 or EOF)
        next_h2 = _H2.search(text, pos)
        section_end = next_h2.start() if next_h2 else len(text)
        section = text[pos:section_end]
        phase["tasks"] = [m.group(1).strip() for m in _TASK.finditer(section)]

    return {"title": title, "phases": phases}


def _phase_children(repo: str, parent: int, gh: Any) -> dict[str, int]:
    """task-title -> issue-number for issues already linked under THIS phase, via the sub-issue
    API plus the checklist fallback. Scopes task reconciliation to the phase (the same task
    title in two phases is two distinct issues — review Finding 1) and is link-aware (a task
    created but not yet linked on a failed run is absent here, so the re-run links it)."""
    children: dict[str, int] = {}
    try:
        for c in gh.list_sub_issues(repo, parent):
            children[c["title"]] = c["number"]
    except RuntimeError:
        pass  # sub-issue API unavailable -> rely on the checklist below
    for num in _CHECKLIST.findall(gh.get_body(repo, parent) or ""):
        title = gh.issue_title(repo, int(num))
        if title:
            children[title] = int(num)
    return children


def generate(repo: str, plan: dict[str, Any], gh: Any) -> dict[str, Any]:
    # Ensure one label per distinct status
    seen_statuses: set[str] = set()
    for phase in plan["phases"]:
        status = phase["status"]
        if status not in seen_statuses:
            gh.ensure_label(repo, f"status:{status}")
            seen_statuses.add(status)

    # Idempotent (review): reuse an existing milestone/issues so a re-run of /conductor:start
    # (or a retry after a mid-way gh failure) never duplicates the hierarchy.
    milestone: int = gh.find_milestone(repo, plan["title"]) or gh.create_milestone(
        repo, plan["title"]
    )

    # Find-only pre-pass: resolve each EXISTING phase and the tasks already linked under it, and
    # collect every linked issue number. This makes task identity PHASE-scoped (a shared task
    # title across phases stays distinct — Finding 1) and lets an unlinked leftover from a failed
    # run be reused, not duplicated, without ever re-linking another phase's task (Finding 2).
    phase_state: list[tuple[int | None, dict[str, int]]] = []
    linked: set[int] = set()
    for phase in plan["phases"]:
        existing_phase = gh.find_issue(repo, phase["title"], milestone)
        if existing_phase:
            children = _phase_children(repo, existing_phase["number"], gh)
            phase_state.append((existing_phase["number"], children))
            linked.update(children.values())
        else:
            phase_state.append((None, {}))

    result_phases: list[dict[str, Any]] = []
    for phase, (phase_number, children) in zip(plan["phases"], phase_state):
        if phase_number is None:
            phase_number = gh.create_issue(
                repo,
                phase["title"],
                body="",
                milestone=milestone,
                labels=[f"status:{phase['status']}"],
            )["number"]
        sub_issues: list[int] = []
        fallback = False

        for task in phase["tasks"]:
            if task in children:  # already a child of THIS phase
                sub_issues.append(children[task])
                continue
            # Reuse a SPECIFIC unlinked leftover from a failed run, skipping any same-titled
            # issue already linked to another phase (review); else create fresh. Never duplicate.
            orphan = next(
                (
                    c
                    for c in gh.find_issues(repo, task, milestone)
                    if c["number"] not in linked
                ),
                None,
            )
            if orphan:
                task_number = orphan["number"]
                task_db_id = orphan["id"]
            else:
                task_issue = gh.create_issue(repo, task, body="", milestone=milestone)
                task_number = task_issue["number"]
                task_db_id = task_issue["id"]
            linked.add(task_number)
            sub_issues.append(task_number)

            try:
                gh.add_sub_issue(repo, phase_number, task_db_id)
            except RuntimeError:
                fallback = True
                existing = gh.get_body(repo, phase_number) or ""
                line = f"- [ ] #{task_number}"
                if (
                    line not in existing
                ):  # idempotent: don't duplicate the checklist line
                    new_body = (existing + "\n" + line).lstrip("\n")
                    gh.set_body(repo, phase_number, new_body)

        result_phases.append(
            {"number": phase_number, "sub_issues": sub_issues, "fallback": fallback}
        )

    return {"milestone": milestone, "phases": result_phases}


def convert(repo: str, plan_md_path: str, gh: Any) -> dict[str, Any]:
    with open(plan_md_path) as f:
        text = f.read()
    plan = parse_plan_md(text)
    return generate(repo, plan, gh)
