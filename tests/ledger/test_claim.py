from unittest.mock import MagicMock

from ledger import claim


def test_eligible_only_when_unassigned_and_open():
    assert claim.eligible(
        {"assignees": [], "labels": ["status:ready"], "state": "open"}
    )
    assert not claim.eligible(
        {"assignees": ["x"], "labels": ["status:ready"], "state": "open"}
    )
    assert not claim.eligible(
        {"assignees": [], "labels": ["status:blocked"], "state": "open"}
    )
    assert not claim.eligible(
        {"assignees": [], "labels": ["status:done"], "state": "closed"}
    )


def test_lease_staleness():
    assert claim.lease_is_stale(100, now_ts=100 + 901, L=900)
    assert not claim.lease_is_stale(100, now_ts=100 + 10, L=900)
    assert claim.lease_is_stale(None, now_ts=5, L=900)


def test_lease_body_marker_round_trip():
    body = {"v": "Phase A body."}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    claim.renew_lease("o/r", 1, "alice", 1782600000, gh)
    assert claim.read_lease("o/r", 1, gh) == {"worker": "alice", "ts": 1782600000}
    claim.renew_lease("o/r", 1, "alice", 1782600999, gh)
    assert body["v"].count("conductor-lease") == 1  # replaced, not stacked
    assert claim.read_lease("o/r", 1, gh) == {"worker": "alice", "ts": 1782600999}


def test_claim_won_mutates_status_and_lease():
    gh = MagicMock()
    gh.issue_state.side_effect = [
        {"assignees": [], "labels": ["status:ready"], "state": "open"},  # eligible
        {
            "assignees": ["me"],
            "labels": ["status:ready"],
            "state": "open",
        },  # sole owner
    ]
    gh.get_body.return_value = "body"
    assert claim.claim("o/r", 1, "me", now_ts=10, ttl=900, gh=gh) is True
    gh.set_labels.assert_called_once()
    gh.set_body.assert_called_once()


def test_claim_lost_race_backs_off_with_no_residue():  # Codex #2
    gh = MagicMock()
    gh.issue_state.side_effect = [
        {"assignees": [], "labels": ["status:ready"], "state": "open"},  # eligible
        {
            "assignees": ["other", "me"],
            "labels": ["status:ready"],
            "state": "open",
        },  # lost
    ]
    assert claim.claim("o/r", 1, "me", now_ts=10, ttl=900, gh=gh) is False
    gh.unassign.assert_called_once_with("o/r", 1, "me")  # backed off
    gh.set_labels.assert_not_called()
    gh.set_body.assert_not_called()  # NO status/lease residue


def test_release_unassigns_and_clears_lease():
    body = {"v": "x <!-- conductor-lease worker=alice ts=1 -->"}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    claim.release("o/r", 1, "alice", gh)
    gh.unassign.assert_called_once_with("o/r", 1, "alice")
    assert "conductor-lease" not in body["v"]
