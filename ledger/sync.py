import re
from typing import Any

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.+?)\s+\[([^\]]+)\]\s*$", re.MULTILINE)
_TASK = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)


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

    result_phases: list[dict[str, Any]] = []

    for phase in plan["phases"]:
        existing_phase = gh.find_issue(repo, phase["title"], milestone)
        if existing_phase:
            phase_number = existing_phase["number"]
        else:
            phase_issue = gh.create_issue(
                repo,
                phase["title"],
                body="",
                milestone=milestone,
                labels=[f"status:{phase['status']}"],
            )
            phase_number = phase_issue["number"]
        sub_issues: list[int] = []
        fallback = False

        for task in phase["tasks"]:
            existing_task = gh.find_issue(repo, task, milestone)
            if existing_task:  # already created + linked on a prior run
                sub_issues.append(existing_task["number"])
                continue
            task_issue = gh.create_issue(repo, task, body="", milestone=milestone)
            task_number: int = task_issue["number"]
            task_db_id: int = task_issue["id"]
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
