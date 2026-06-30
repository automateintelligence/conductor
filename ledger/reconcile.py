import time
from typing import Any

from ledger import claim


def reconcile(
    repo: str,
    n: int,
    *,
    tests_red: bool,
    pr_merged: bool,
    commits_since_baseline: int,  # reserved: Plan-4 §7 git-commits precedence leg; not yet consumed by any repair
    R: int,
    gh: Any,
    now_ts: int | None = None,
    L: int = 900,
) -> dict[str, Any]:
    if now_ts is None:
        now_ts = int(time.time())
    st = gh.issue_state(repo, n)
    labels = set(st["labels"])
    status = next((lbl for lbl in labels if lbl.startswith("status:")), None)
    closed = st["state"] == "closed"

    def repair(
        new_status: str | None, action: str, reopen: bool = False
    ) -> dict[str, Any]:
        if reopen:
            gh.reopen_issue(repo, n)
        if new_status:
            gh.set_labels(
                repo,
                n,
                add=[new_status],
                remove=[lbl for lbl in labels if lbl.startswith("status:")],
            )
        return {"action": action, "new_status": new_status}

    # Precedence: git/tests > PR > label.
    # 1. STALE-LEASE RECLAIM FIRST (Codex #3): dead owner -> reclaim, do NOT count vs retry cap.
    #    The 'stale-lease-reclaim' action tells the caller to reset this phase's retry counter.
    if status == "status:in-progress" and st["assignees"] and now_ts is not None:
        lease = claim.read_lease(repo, n, gh)
        if claim.lease_is_stale(lease["ts"] if lease else None, now_ts, L):
            for w in st["assignees"]:
                gh.unassign(repo, n, w)
            claim.reset_attempts(repo, n, gh)  # fresh owner -> fresh count (Codex #3)
            return repair("status:ready", "stale-lease-reclaim")

    # 2. Retry cap (§6.1) — a LIVE owner's genuine repeated failures -> blocked. The attempt
    #    count is DURABLE (issue body), so it survives across fires and fresh worker contexts;
    #    this fire's still-red result counts as one failed attempt.
    if tests_red and status == "status:in-progress" and st["assignees"]:
        if claim.bump_attempts(repo, n, gh) >= R:
            return repair("status:blocked", "retry-cap-exceeded")

    # 3. done/closed but tests red -> reopen -> in-progress.
    if (status == "status:done" or closed) and tests_red:
        return repair("status:in-progress", "reopen-tests-red", reopen=True)

    # 4. in-progress but no assignee -> reset ready (abandoned).
    if status == "status:in-progress" and not st["assignees"]:
        return repair("status:ready", "reset-abandoned")

    # 5. closed but PR not merged -> reopen.
    if closed and not pr_merged:
        return repair("status:in-progress", "reopen-unmerged", reopen=True)

    return {"action": "none", "new_status": status}
