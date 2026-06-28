from unittest.mock import MagicMock
from ledger import reconcile


def _gh(state, body=""):
    g = MagicMock()
    g.issue_state.return_value = state
    g.get_body.return_value = body
    return g


def test_done_but_tests_red_reopens_to_in_progress():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=True,
        commits_since_baseline=3,
        retries=0,
        R=3,
        gh=g,
    )
    assert out["new_status"] == "status:in-progress"
    g.reopen_issue.assert_called_once()


def test_done_and_tests_green_is_permitted():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=False,
        pr_merged=True,
        commits_since_baseline=3,
        retries=0,
        R=3,
        gh=g,
    )
    assert out["action"] == "none"


def test_in_progress_no_assignee_resets_to_ready():
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": [], "id": 1}
    )
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=0,
        retries=0,
        R=3,
        gh=g,
    )
    assert out["new_status"] == "status:ready"


def test_stale_lease_reclaims_before_retry_cap():  # Codex #3
    g = _gh(
        {
            "state": "open",
            "labels": ["status:in-progress"],
            "assignees": ["dead"],
            "id": 1,
        },
        body="<!-- conductor-lease worker=dead ts=100 -->",
    )
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=1,
        retries=99,
        R=3,
        gh=g,  # retries EXHAUSTED
        now_ts=100 + 5000,
        L=900,
    )
    assert (
        out["action"] == "stale-lease-reclaim" and out["new_status"] == "status:ready"
    )
    g.unassign.assert_called_once_with("o/r", 1, "dead")  # reclaimed, NOT blocked


def test_live_owner_retry_cap_blocks():
    g = _gh(
        {
            "state": "open",
            "labels": ["status:in-progress"],
            "assignees": ["w"],
            "id": 1,
        },
        body="<!-- conductor-lease worker=w ts=100 -->",
    )
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=1,
        retries=3,
        R=3,
        gh=g,
        now_ts=110,
        L=900,  # FRESH lease -> live owner
    )
    assert out["new_status"] == "status:blocked"
