import subprocess
from types import SimpleNamespace

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
