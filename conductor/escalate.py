import os
from typing import Any

from ledger import gh as _gh


def file_followup(
    repo: str,
    kind: str,
    title: str,
    body: str,
    link_issue: int | None = None,
    gh: Any = _gh,
) -> int:
    assert kind in ("debt", "feature")
    gh.ensure_label(repo, kind)
    issue = gh.create_issue(repo, title, body, labels=[kind])
    if link_issue:
        gh._gh_api(
            "POST",
            f"repos/{repo}/issues/{link_issue}/comments",
            body={"body": f"Excavated {kind}: #{issue['number']}"},
        )
    return issue["number"]


def block_on_subplan(repo: str, phase_issue: int, gh: Any = _gh) -> None:
    gh.set_labels(
        repo,
        phase_issue,
        add=["status:blocked", "blocked-on-subplan"],
        remove=["status:in-progress"],
    )


def write_adr(adr_dir: str, slug: str, body: str) -> str:
    os.makedirs(adr_dir, exist_ok=True)
    path = os.path.join(adr_dir, f"{slug}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# ADR: {slug}\n\n{body}\n")
    return path
