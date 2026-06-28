from conductor import merge_gate


def _clean():
    return {
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "isDraft": False,
    }


def _call(pr_json, *, threads=(), merge_ref_ok=True):
    return merge_gate.check(
        "o/r",
        1,
        local_verify="true",
        gh_json=lambda r, p, f: pr_json,
        threads=lambda r, p: list(threads),
        merge_ref_verify=lambda r, p, lv: merge_ref_ok,
    )


def test_all_green_passes():
    assert _call(_clean()) == {"ok": True, "blockers": []}


def test_behind_or_blocked_blocks():
    assert (
        "merge-state:BEHIND"
        in _call({**_clean(), "mergeStateStatus": "BEHIND"})["blockers"]
    )
    assert (
        "merge-state:BLOCKED"
        in _call({**_clean(), "mergeStateStatus": "BLOCKED"})["blockers"]
    )


def test_conflicts_block():
    assert any(
        "mergeable" in b
        for b in _call({**_clean(), "mergeable": "CONFLICTING"})["blockers"]
    )


def test_changes_requested_blocks():
    assert (
        "changes-requested"
        in _call({**_clean(), "reviewDecision": "CHANGES_REQUESTED"})["blockers"]
    )


def test_unresolved_threads_block():
    assert "unresolved" in _call(_clean(), threads=["unresolved"])["blockers"]


def test_unpaginated_threads_fail_closed():  # Codex minor
    assert (
        "threads-unpaginated"
        in _call(_clean(), threads=["threads-unpaginated"])["blockers"]
    )


def test_merge_ref_verify_failure_blocks():
    assert "merge-ref-verify-failed" in _call(_clean(), merge_ref_ok=False)["blockers"]


def test_draft_blocks():
    assert "draft" in _call({**_clean(), "isDraft": True})["blockers"]
