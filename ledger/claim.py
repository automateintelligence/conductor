import re
from typing import Any

BLOCKING = {"status:blocked", "status:done"}
_LEASE = re.compile(r"<!--\s*conductor-lease worker=(\S+) ts=(\d+)\s*-->")


def eligible(state: dict[str, Any]) -> bool:
    labels = set(state.get("labels", []))
    return (
        not state.get("assignees")
        and state.get("state") != "closed"
        and not (labels & BLOCKING)
        and "dep-blocked" not in labels
    )


def lease_is_stale(lease_ts: int | None, now_ts: int, L: int) -> bool:
    return lease_ts is None or (now_ts - lease_ts) > L


def read_lease(repo: str, n: int, gh: Any) -> dict[str, Any] | None:
    m = _LEASE.search(gh.get_body(repo, n) or "")
    return {"worker": m.group(1), "ts": int(m.group(2))} if m else None


def renew_lease(repo: str, n: int, worker: str, now_ts: int, gh: Any) -> None:
    body = _LEASE.sub("", gh.get_body(repo, n) or "").rstrip()
    gh.set_body(
        repo, n, f"{body}\n\n<!-- conductor-lease worker={worker} ts={now_ts} -->"
    )


def claim(repo: str, n: int, worker: str, now_ts: int, ttl: int, gh: Any) -> bool:
    if not eligible(gh.issue_state(repo, n)):
        return False
    gh.assign(repo, n, worker)
    confirm = gh.issue_state(repo, n)
    if confirm["assignees"] != [worker]:  # lost the race (Codex #2)
        gh.unassign(repo, n, worker)  # back off; no status/lease touched yet
        return False
    gh.set_labels(
        repo,
        n,
        add=["status:in-progress"],
        remove=[lbl for lbl in confirm["labels"] if lbl.startswith("status:")],
    )
    renew_lease(repo, n, worker, now_ts, gh)
    return True


def release(repo: str, n: int, worker: str, gh: Any) -> None:
    gh.unassign(repo, n, worker)
    body = _LEASE.sub("", gh.get_body(repo, n) or "").rstrip()
    gh.set_body(repo, n, body)
