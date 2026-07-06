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
        "o/r", 1, tests_red=True, pr_merged=True, commits_since_baseline=3, R=3, gh=g
    )
    assert out["new_status"] == "status:in-progress"
    g.reopen_issue.assert_called_once()


def test_done_and_tests_green_is_permitted():
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=True, commits_since_baseline=3, R=3, gh=g
    )
    assert out["action"] == "none"


def test_in_progress_no_assignee_resets_to_ready():
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": [], "id": 1}
    )
    out = reconcile.reconcile(
        "o/r", 1, tests_red=True, pr_merged=False, commits_since_baseline=0, R=3, gh=g
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
        body="<!-- conductor-lease worker=dead ts=100 --> <!-- conductor-attempts n=99 -->",
    )
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=1,
        R=3,
        gh=g,
        now_ts=100 + 5000,  # lease long expired -> reclaim wins over the durable count
        L=900,
    )
    assert (
        out["action"] == "stale-lease-reclaim" and out["new_status"] == "status:ready"
    )
    g.unassign.assert_called_once_with("o/r", 1, "dead")  # reclaimed, NOT blocked


def test_live_owner_retry_cap_blocks():  # durable count reaches the cap THIS fire
    g = _gh(
        {
            "state": "open",
            "labels": ["status:in-progress"],
            "assignees": ["w"],
            "id": 1,
        },
        body="<!-- conductor-lease worker=w ts=100 --> <!-- conductor-attempts n=2 -->",
    )
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=1,
        R=3,
        gh=g,
        now_ts=110,  # FRESH lease -> live owner; attempts 2 -> bump 3 -> >= R
        L=900,
    )
    assert out["new_status"] == "status:blocked"


def test_live_owner_under_cap_bumps_not_block():  # below the cap: keep going, but count it
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
        R=3,
        gh=g,
        now_ts=110,
        L=900,
    )
    assert out["action"] == "none" and out["new_status"] == "status:in-progress"
    assert g.set_body.called  # the failed attempt was recorded durably (0 -> 1)


# --- B-2: green-but-unmergeable phase must escalate on no progress, not loop forever ---


def test_green_no_progress_blocks_at_cap():
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1},
        body="<!-- conductor-lease worker=w ts=100 --> <!-- conductor-attempts n=2 -->",
    )
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=False, commits_since_baseline=0,
        R=3, gh=g, now_ts=110, L=900,  # green, zero commits, attempts 2 -> bump 3 -> cap
    )
    assert out["new_status"] == "status:blocked"
    assert out["action"] == "no-progress-cap-exceeded"


def test_green_no_progress_under_cap_bumps():
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1},
        body="<!-- conductor-lease worker=w ts=100 -->",
    )
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=False, commits_since_baseline=0,
        R=3, gh=g, now_ts=110, L=900,
    )
    assert out["action"] == "none" and out["new_status"] == "status:in-progress"
    assert g.set_body.called  # the no-progress fire is counted durably


def test_green_with_progress_does_not_bump():
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1},
        body="<!-- conductor-lease worker=w ts=100 -->",
    )
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=False, commits_since_baseline=5,
        R=3, gh=g, now_ts=110, L=900,
    )
    assert out["action"] == "none"
    assert not g.set_body.called  # genuine progress is not a failed attempt


def test_commits_not_reported_never_blocks():  # fail-safe: -1 = unknown, never a false block
    g = _gh(
        {"state": "open", "labels": ["status:in-progress"], "assignees": ["w"], "id": 1},
        body="<!-- conductor-lease worker=w ts=100 --> <!-- conductor-attempts n=2 -->",
    )
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=False, commits_since_baseline=-1,
        R=3, gh=g, now_ts=110, L=900,  # unknown count -> no no-progress escalation despite n=2
    )
    assert out["action"] == "none"
    assert not g.set_body.called


def test_stale_reclaim_resets_attempts():  # reclaim must zero the durable counter (§6.1)
    store = {
        "v": "<!-- conductor-lease worker=dead ts=100 --> <!-- conductor-attempts n=5 -->"
    }
    g = MagicMock()
    g.issue_state.return_value = {
        "state": "open",
        "labels": ["status:in-progress"],
        "assignees": ["dead"],
        "id": 1,
    }
    g.get_body.side_effect = lambda r, n: store["v"]
    g.set_body.side_effect = lambda r, n, b: store.__setitem__("v", b)
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=1,
        R=3,
        gh=g,
        now_ts=100 + 5000,
        L=900,
    )
    assert out["action"] == "stale-lease-reclaim"
    assert "conductor-attempts" not in store["v"]  # counter reset on reclaim


def test_closed_green_phase_is_terminal_even_without_pr_state():
    # codex PR-28 #3: git/tests > PR — a closed phase with a GREEN (derived) gate must stay
    # closed even when the caller cannot supply pr_merged (defaults False), else every
    # completed phase gets reopened by a routine reconcile.
    g = _gh({"state": "closed", "labels": ["status:done"], "assignees": [], "id": 1})
    out = reconcile.reconcile(
        "o/r", 1, tests_red=False, pr_merged=False, commits_since_baseline=0, R=3, gh=g
    )
    assert out["action"] == "none"
    g.reopen_issue.assert_not_called()
