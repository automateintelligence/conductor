import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from conductor import merge_gate


def _fake_run(stdout: str):
    """Return a callable that mimics subprocess.run, returning stdout as given."""

    def run(args, **kwargs):
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run


def test_remote_for_matches_named_remote():
    remote_v = "github\thttps://github.com/o/r.git (fetch)\ngithub\thttps://github.com/o/r.git (push)\n"
    assert merge_gate._remote_for("o/r", run=_fake_run(remote_v)) == "github"


def test_remote_for_falls_back_to_origin():
    remote_v = "upstream\thttps://github.com/other/repo.git (fetch)\n"
    assert merge_gate._remote_for("o/r", run=_fake_run(remote_v)) == "origin"


def _rc(created, body="Codex review: pass", author="reviewer"):
    """A PR comment as returned by `gh pr view --json comments` (author.login shape
    verified on gh 2.4.0)."""
    return {"body": body, "createdAt": created, "author": {"login": author}}


def _clean():
    # Process legs need benign passing values: a Closes-ref body and two marker
    # reviews newer than the newest commit (final state reviewed; commit date is
    # injected via _call's newest_commit, default 2026-01-01).
    return {
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "isDraft": False,
        "body": "Closes #7",
        "comments": [
            _rc("2026-01-02T00:00:00Z"),
            _rc("2026-01-03T00:00:00Z"),
        ],
    }


def _call(
    pr_json,
    *,
    threads=(),
    merge_ref_ok=True,
    newest_commit: Any = "2026-01-01T00:00:00Z",  # str, dates dict, or (repo, pr) -> dict
):
    if not callable(newest_commit) and not isinstance(newest_commit, dict):
        newest_commit = {"committedDate": newest_commit, "pushedDate": None}
    nc = newest_commit if callable(newest_commit) else (lambda r, p: newest_commit)
    return merge_gate.check(
        "o/r",
        1,
        local_verify="true",
        gh_json=lambda r, p, f: pr_json,
        threads=lambda r, p: list(threads),
        newest_commit=nc,
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


def test_gh_error_is_fail_closed():  # review: bounded subprocess / no crash on gh failure
    def boom(r, p, f):
        raise RuntimeError("gh timed out")

    out = merge_gate.check(
        "o/r",
        1,
        local_verify="true",
        gh_json=boom,
        threads=lambda r, p: [],
        merge_ref_verify=lambda r, p, lv: True,
    )
    assert out["ok"] is False and any("gh-error" in b for b in out["blockers"])


def test_merge_ref_verify_timeout_returns_false():  # review: timeout in remote/fetch fails closed
    calls = []

    def run(*a, **k):
        calls.append(a)
        if len(calls) == 1:  # the `git remote -v` lookup, which runs first
            raise subprocess.TimeoutExpired(cmd="git remote -v", timeout=1)
        return SimpleNamespace(stdout="", returncode=0)

    assert merge_gate._merge_ref_verify("o/r", 1, "true", run=run) is False


def test_merge_ref_verify_exception_is_fail_closed():  # review: check() must not crash
    def boom(r, p, lv):
        raise subprocess.TimeoutExpired(cmd="verify", timeout=1)

    out = merge_gate.check(
        "o/r",
        1,
        local_verify="true",
        gh_json=lambda r, p, f: _clean(),
        threads=lambda r, p: [],
        merge_ref_verify=boom,
    )
    assert out["ok"] is False and any("merge-ref" in b for b in out["blockers"])


def test_resolve_repo_env_wins_without_subprocess(
    monkeypatch,
):  # review: bounded autodiscovery
    monkeypatch.setenv("CONDUCTOR_REPO", "o/r")
    called = []
    assert merge_gate._resolve_repo(run=lambda *a, **k: called.append(1)) == "o/r"
    assert not called  # env set -> no gh subprocess at all


def test_resolve_repo_is_time_bounded(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_REPO", raising=False)
    seen = {}

    def run(*a, **k):
        seen.update(k)
        return SimpleNamespace(returncode=0, stdout="o/r\n", stderr="")

    assert merge_gate._resolve_repo(run=run) == "o/r"
    assert "timeout" in seen  # the `gh repo view` autodiscovery is bounded


def test_resolve_repo_timeout_propagates(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_REPO", raising=False)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh repo view", timeout=1)

    with pytest.raises(subprocess.TimeoutExpired):
        merge_gate._resolve_repo(run=boom)


def test_resolve_repo_nonzero_raises(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_REPO", raising=False)

    def run(*a, **k):
        return SimpleNamespace(returncode=1, stdout="", stderr="gh boom")

    with pytest.raises(RuntimeError):
        merge_gate._resolve_repo(run=run)


# ---- process-compliance legs: Closes #, min reviews, review-of-final-state ----


@pytest.fixture(autouse=True)
def _default_process_env(monkeypatch):
    """Pin the process-leg env to defaults so tests never depend on the ambient shell."""
    monkeypatch.delenv("CONDUCTOR_MIN_REVIEWS", raising=False)
    monkeypatch.delenv("CONDUCTOR_REVIEW_MARKER", raising=False)
    monkeypatch.delenv("CONDUCTOR_REVIEW_AUTHOR", raising=False)


def test_check_fetches_process_fields_in_one_call():
    calls = []

    def gj(r, p, f):
        calls.append(f)
        return _clean()

    merge_gate.check(
        "o/r",
        1,
        local_verify="true",
        gh_json=gj,
        threads=lambda r, p: [],
        newest_commit=lambda r, p: {
            "committedDate": "2026-01-01T00:00:00Z",
            "pushedDate": None,
        },
        merge_ref_verify=lambda r, p, lv: True,
    )
    assert len(calls) == 1  # one gh call, not three
    fields = set(calls[0].split(","))
    assert {"body", "comments"} <= fields
    assert (
        "commits" not in fields
    )  # newest commit comes from the pagination-safe helper


def test_closes_missing_blocks():
    assert "closes-missing" in _call({**_clean(), "body": "adds the gate"})["blockers"]


@pytest.mark.parametrize(
    "body",
    [
        "Closes #7",
        "close #1",
        "closed #12",
        "Fixes #3",
        "fix #4",
        "FIXED #5",
        "resolves #6",
        "Resolve #7",
        "resolved #8",
        "This PR fixes #42 and more",
    ],
)
def test_closes_verb_forms_pass(body):
    assert "closes-missing" not in _call({**_clean(), "body": body})["blockers"]


def test_zero_marker_comments_blocks_without_stale():
    out = _call({**_clean(), "comments": []})
    assert "reviews:0/2" in out["blockers"]
    assert "review-stale" not in out["blockers"]  # leg skipped at zero markers


def test_one_marker_comment_blocks():
    comments = [
        _rc("2026-01-02T00:00:00Z"),
        _rc("2026-01-02T01:00:00Z", body="unrelated chatter"),
    ]
    assert "reviews:1/2" in _call({**_clean(), "comments": comments})["blockers"]


def test_marker_match_is_case_insensitive():
    comments = [
        _rc("2026-01-02T00:00:00Z", body="CODEX REVIEW: ok"),
        _rc("2026-01-03T00:00:00Z", body="second codex review round"),
    ]
    assert _call({**_clean(), "comments": comments}) == {"ok": True, "blockers": []}


def test_min_reviews_zero_disables_both_review_legs(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_MIN_REVIEWS", "0")
    # one marker comment (< 2) that is also older than the newest commit: with the
    # legs enabled this would fire both blockers; MIN_REVIEWS=0 disables both.
    stale = {**_clean(), "comments": [_rc("2025-12-31T00:00:00Z")]}
    assert _call(stale) == {"ok": True, "blockers": []}


def test_custom_review_marker(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_REVIEW_MARKER", "LGTM-bot")
    assert (
        "reviews:0/2" in _call(_clean())["blockers"]
    )  # 'Codex review' no longer counts
    lgtm = [
        _rc("2026-01-02T00:00:00Z", body="lgtm-bot approved"),
        _rc("2026-01-03T00:00:00Z", body="LGTM-BOT approved again"),
    ]
    assert _call({**_clean(), "comments": lgtm}) == {"ok": True, "blockers": []}


def test_review_stale_when_commit_postdates_last_review():
    out = _call(_clean(), newest_commit="2026-01-04T00:00:00Z")  # after both reviews
    assert "review-stale" in out["blockers"]


def test_stale_detection_ignores_gh_json_commits_field():
    # `gh pr view --json commits` caps at the FIRST 100 commits (unpaginated), so a
    # stale/truncated commits field must not mask staleness — the last:1 helper wins.
    truncated = {**_clean(), "commits": [{"committedDate": "2025-01-01T00:00:00Z"}]}
    out = _call(truncated, newest_commit="2026-01-09T00:00:00Z")
    assert "review-stale" in out["blockers"]


def test_newest_commit_error_is_fail_closed():
    def boom(r, p):
        raise RuntimeError("gh boom")

    out = _call(_clean(), newest_commit=boom)
    assert out["ok"] is False
    assert any(b.startswith("process-check-error") for b in out["blockers"])


def test_newest_commit_not_called_when_review_legs_disabled(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_MIN_REVIEWS", "0")
    called = []

    def nc(r, p):
        called.append(1)
        return {"committedDate": "2026-01-01T00:00:00Z", "pushedDate": None}

    assert _call(_clean(), newest_commit=nc)["ok"] is True
    assert not called  # disabled legs must not spend a gh call


def test_review_at_commit_time_is_not_stale():
    tie = {
        **_clean(),
        "comments": [_rc("2026-01-01T00:00:00Z"), _rc("2026-01-01T00:00:00Z")],
    }
    assert _call(tie) == {"ok": True, "blockers": []}


def test_fresh_review_passes():
    assert _call(_clean()) == {"ok": True, "blockers": []}


def test_negative_min_reviews_is_fail_closed(monkeypatch):
    # contract: 0 disables the review legs; negative is INVALID, not "more disabled"
    monkeypatch.setenv("CONDUCTOR_MIN_REVIEWS", "-1")
    out = _call(_clean())
    assert out["ok"] is False
    assert any(b.startswith("process-check-error") for b in out["blockers"])


def test_non_integer_min_reviews_is_fail_closed(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_MIN_REVIEWS", "two")
    out = _call(_clean())
    assert out["ok"] is False
    assert any(b.startswith("process-check-error") for b in out["blockers"])


def test_malformed_timestamp_is_fail_closed():
    bad = {**_clean(), "comments": [_rc("not-a-date"), _rc("2026-01-03T00:00:00Z")]}
    out = _call(bad)
    assert out["ok"] is False
    assert any(b.startswith("process-check-error") for b in out["blockers"])


def test_missing_process_fields_fail_closed_without_crash():
    # Old-style gh_json payload without body/comments/commits: legs treat missing
    # keys as empty and block, rather than raising.
    bare = {
        k: _clean()[k]
        for k in ("mergeStateStatus", "mergeable", "reviewDecision", "isDraft")
    }
    out = _call(bare)
    assert "closes-missing" in out["blockers"]
    assert "reviews:0/2" in out["blockers"]
    assert not any(b.startswith("process-check-error") for b in out["blockers"])


def test_review_author_filter_counts_only_that_login(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_REVIEW_AUTHOR", "codex-bot")
    posers = [
        _rc("2026-01-02T00:00:00Z", author="worker"),
        _rc("2026-01-03T00:00:00Z", author="worker"),
        {"body": "Codex review", "createdAt": "2026-01-03T01:00:00Z"},  # no author
    ]
    assert "reviews:0/2" in _call({**_clean(), "comments": posers})["blockers"]
    good = [
        _rc("2026-01-02T00:00:00Z", author="codex-bot"),
        _rc("2026-01-03T00:00:00Z", author="CODEX-BOT"),  # GH logins case-insensitive
    ]
    assert _call({**_clean(), "comments": good}) == {"ok": True, "blockers": []}


def test_review_author_unset_counts_any_author():
    comments = [
        _rc("2026-01-02T00:00:00Z", author="alice"),
        _rc("2026-01-03T00:00:00Z", author="bob"),
    ]
    assert _call({**_clean(), "comments": comments}) == {"ok": True, "blockers": []}


def test_review_author_filter_applies_to_staleness(monkeypatch):
    # a post-commit marker comment from the WRONG author must not mask staleness
    monkeypatch.setenv("CONDUCTOR_REVIEW_AUTHOR", "codex-bot")
    comments = [
        _rc("2025-12-30T00:00:00Z", author="codex-bot"),
        _rc("2025-12-31T00:00:00Z", author="codex-bot"),
        _rc("2026-01-05T00:00:00Z", author="worker"),  # after the 01-01 commit
    ]
    assert "review-stale" in _call({**_clean(), "comments": comments})["blockers"]


def test_pushed_date_newer_than_committed_wins():
    # committedDate is author-controlled (backdatable); server-set pushedDate wins
    nc = {"committedDate": "2026-01-01T00:00:00Z", "pushedDate": "2026-01-05T00:00:00Z"}
    assert "review-stale" in _call(_clean(), newest_commit=nc)["blockers"]


def test_null_pushed_date_falls_back_to_committed():
    # GitHub deprecated pushedDate (null on current github.com pushes)
    ok = {"committedDate": "2026-01-01T00:00:00Z", "pushedDate": None}
    assert _call(_clean(), newest_commit=ok) == {"ok": True, "blockers": []}
    stale = {"committedDate": "2026-01-04T00:00:00Z", "pushedDate": None}
    assert "review-stale" in _call(_clean(), newest_commit=stale)["blockers"]


def test_both_commit_dates_null_fail_closed():
    out = _call(_clean(), newest_commit={"committedDate": None, "pushedDate": None})
    assert out["ok"] is False
    assert any(b.startswith("process-check-error") for b in out["blockers"])
