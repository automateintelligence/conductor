import time
from typing import Any

from ledger import claim


def reconcile(
    repo: str,
    n: int,
    *,
    tests_red: bool,
    pr_merged: bool,
    commits_since_baseline: int = -1,  # commits the phase made THIS fire; -1 = not reported (fail-safe: no no-progress escalation)
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

    # 2. Retry / no-progress cap (§6.1) — a LIVE owner's phase that isn't ADVANCING -> blocked.
    #    "Not advancing" = tests still red, OR this fire made zero commits while the phase stays
    #    in-progress (`commits_since_baseline == 0`). The second leg closes the green-but-
    #    unmergeable infinite loop (review B-2): a phase that can't merge is fired forever making
    #    no progress and was never counted, because the old cap only saw `tests_red`.
    #    `commits_since_baseline == -1` = "not reported" -> fail-safe: a caller that omits the
    #    count can NEVER be falsely blocked. Genuine progress (commits > 0, tests green) doesn't
    #    bump. The count is DURABLE (issue body), surviving fires and fresh worker contexts.
    if status == "status:in-progress" and st["assignees"] and (
        tests_red or commits_since_baseline == 0
    ):
        if claim.bump_attempts(repo, n, gh) >= R:
            reason = "retry-cap-exceeded" if tests_red else "no-progress-cap-exceeded"
            return repair("status:blocked", reason)

    # 3. done/closed but tests red -> reopen -> in-progress.
    if (status == "status:done" or closed) and tests_red:
        return repair("status:in-progress", "reopen-tests-red", reopen=True)

    # 4. in-progress but no assignee -> reset ready (abandoned).
    if status == "status:in-progress" and not st["assignees"]:
        return repair("status:ready", "reset-abandoned")

    # 5. closed but PR not merged -> reopen — ONLY while the gate is red. A closed phase
    #    with a GREEN gate is terminal even when the caller can't supply PR state
    #    (--pr-merged defaults False): git/tests > PR, so derived test truth outranks the
    #    flag. Without this guard a routine `reconcile --from-gate` reopens every
    #    completed phase (codex PR-28 #3).
    if closed and not pr_merged and tests_red:
        return repair("status:in-progress", "reopen-unmerged", reopen=True)

    return {"action": "none", "new_status": status}
