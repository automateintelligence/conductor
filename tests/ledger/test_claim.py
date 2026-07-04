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
    assert not claim.eligible(  # Fix 3: dep-blocked must be excluded (design §7)
        {"assignees": [], "labels": ["dep-blocked"], "state": "open"}
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


def test_claim_sweeps_any_status_label():  # Fix 1: all status:* removed, not just status:ready
    # 0.5.0 SEMANTIC REVERSAL: this test formerly used status:draft as its eligible
    # example — draft now BLOCKS claiming (parked / owner-opt-in, live finding), so the
    # label-sweep behavior is pinned with an arbitrary non-blocking status instead.
    gh = MagicMock()
    gh.issue_state.side_effect = [
        {"assignees": [], "labels": ["status:stale"], "state": "open"},  # eligible
        {
            "assignees": ["me"],
            "labels": ["status:stale"],
            "state": "open",
        },  # sole owner confirm
    ]
    gh.get_body.return_value = ""
    assert claim.claim("o/r", 2, "me", now_ts=10, ttl=900, gh=gh) is True
    call_kwargs = gh.set_labels.call_args
    assert call_kwargs.kwargs["add"] == ["status:in-progress"]
    assert "status:stale" in call_kwargs.kwargs["remove"]


def test_attempts_marker_round_trip():  # durable retry count (review)
    body = {"v": "Phase body. <!-- conductor-lease worker=w ts=5 -->"}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    assert claim.read_attempts("o/r", 1, gh) == 0
    assert claim.bump_attempts("o/r", 1, gh) == 1
    assert claim.bump_attempts("o/r", 1, gh) == 2
    assert claim.read_attempts("o/r", 1, gh) == 2
    assert body["v"].count("conductor-attempts") == 1  # replaced, not stacked
    assert "conductor-lease" in body["v"]  # bump preserves the lease marker
    claim.reset_attempts("o/r", 1, gh)
    assert claim.read_attempts("o/r", 1, gh) == 0
    assert "conductor-attempts" not in body["v"]  # marker removed on reset
    assert "conductor-lease" in body["v"]  # reset doesn't touch the lease


def test_release_unassigns_and_clears_lease():
    body = {"v": "x <!-- conductor-lease worker=alice ts=1 -->"}
    gh = MagicMock()
    gh.get_body.side_effect = lambda r, n: body["v"]
    gh.set_body.side_effect = lambda r, n, b: body.__setitem__("v", b)
    claim.release("o/r", 1, "alice", gh)
    gh.unassign.assert_called_once_with("o/r", 1, "alice")
    assert "conductor-lease" not in body["v"]


def test_draft_is_not_eligible():
    # 0.5.0 (live finding): status:draft means "not scheduled" — a parked/optional phase
    # must never be claimable; previously only dep-blocked kept it out.
    assert not claim.eligible(
        {"assignees": [], "labels": ["status:draft"], "state": "open"}
    )
