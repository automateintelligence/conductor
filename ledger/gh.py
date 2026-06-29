import json
import subprocess
from typing import Any


def _gh_api(method: str, path: str, body: Any = None, jq: str | None = None) -> Any:
    cmd = ["gh", "api", "--method", method, path]
    if jq:
        cmd += ["--jq", jq]
    stdin = None
    if body is not None:
        cmd += ["--input", "-"]  # JSON request body on stdin (arrays/ints correct)
        stdin = json.dumps(body)
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if out.returncode != 0:
        detail = " ".join(p for p in (out.stderr.strip(), out.stdout.strip()) if p)
        raise RuntimeError(f"gh api {method} {path} failed: {detail}")
    if jq:
        return out.stdout.rstrip("\n")
    return json.loads(out.stdout) if out.stdout.strip() else None


def ensure_label(repo: str, name: str, color: str = "ededed") -> None:
    try:
        _gh_api("POST", f"repos/{repo}/labels", body={"name": name, "color": color})
    except RuntimeError as e:
        if "already_exists" not in str(e):
            raise


def create_milestone(repo: str, title: str) -> int:
    return _gh_api("POST", f"repos/{repo}/milestones", body={"title": title})["number"]


def find_milestone(repo: str, title: str) -> int | None:
    """Number of an existing milestone with this exact title, or None. Makes generate()
    idempotent: a re-run reuses the milestone instead of creating a duplicate. (First 100;
    a plan's milestone is created once, so this is a small set.)"""
    data = _gh_api("GET", f"repos/{repo}/milestones?state=all&per_page=100")
    for m in data or []:
        if m.get("title") == title:
            return m["number"]
    return None


def find_issue(
    repo: str, title: str, milestone: int | None = None
) -> dict[str, Any] | None:
    """{number, id} of an existing (open or closed) issue with this exact title in the given
    milestone, or None. Excludes pull requests (the issues endpoint returns both). Scopes the
    query to the milestone so the title match is unambiguous within a plan."""
    path = f"repos/{repo}/issues?state=all&per_page=100"
    if milestone is not None:
        path += f"&milestone={int(milestone)}"
    data = _gh_api("GET", path)
    for it in data or []:
        if it.get("pull_request"):
            continue
        if it.get("title") == title:
            return {"number": it["number"], "id": it["id"]}
    return None


def create_issue(
    repo: str,
    title: str,
    body: str,
    milestone: int | None = None,
    labels: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "body": body}
    if milestone:
        payload["milestone"] = int(milestone)
    if labels:
        payload["labels"] = list(labels)
    data = _gh_api("POST", f"repos/{repo}/issues", body=payload)
    return {"number": data["number"], "id": data["id"]}


def add_sub_issue(repo: str, parent: int, child_db_id: int) -> Any:
    return _gh_api(
        "POST",
        f"repos/{repo}/issues/{parent}/sub_issues",
        body={"sub_issue_id": int(child_db_id)},
    )


def set_labels(
    repo: str,
    n: int,
    add: tuple[str, ...] | list[str] = (),
    remove: tuple[str, ...] | list[str] = (),
) -> None:
    cur = set(issue_state(repo, n)["labels"])
    new = sorted((cur | set(add)) - set(remove))
    _gh_api(
        "PATCH", f"repos/{repo}/issues/{n}", body={"labels": new}
    )  # real array; [] clears


def assign(repo: str, n: int, login: str) -> None:
    _gh_api("POST", f"repos/{repo}/issues/{n}/assignees", body={"assignees": [login]})


def unassign(repo: str, n: int, login: str) -> None:
    _gh_api("DELETE", f"repos/{repo}/issues/{n}/assignees", body={"assignees": [login]})


def close_issue(repo: str, n: int) -> None:
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"state": "closed"})


def reopen_issue(repo: str, n: int) -> None:
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"state": "open"})


def issue_state(repo: str, n: int) -> dict[str, Any]:
    d = _gh_api("GET", f"repos/{repo}/issues/{n}")
    return {
        "state": d["state"],
        "id": d["id"],
        "labels": [lbl["name"] for lbl in d.get("labels", [])],
        "assignees": [a["login"] for a in d.get("assignees", [])],
    }


def get_body(repo: str, n: int) -> str:
    return _gh_api("GET", f"repos/{repo}/issues/{n}").get("body") or ""


def set_body(repo: str, n: int, body: str) -> None:
    _gh_api("PATCH", f"repos/{repo}/issues/{n}", body={"body": body})
