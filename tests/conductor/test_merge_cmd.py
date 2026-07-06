"""`conductor merge <pr>`: fuse gate+merge so the merge IS the gate (review B-1), and never
auto-merge to the default branch (the final owner PR is owner-only)."""

from conductor import merge_cmd


class _Run:
    """Records subprocess calls; returns scripted results by matched command fragment."""

    def __init__(self, base="run-x", default="main", head="deadbeef", merge_rc=0):
        self.base, self.default, self.head, self.merge_rc = (
            base,
            default,
            head,
            merge_rc,
        )
        self.merged = False
        self.merge_argv: list = []

    def __call__(self, argv, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if "defaultBranchRef" in argv:
            r.stdout = self.default
        elif "baseRefName" in argv:
            r.stdout = self.base
        elif "headRefOid" in argv:
            r.stdout = self.head
        elif argv[:3] == ["gh", "pr", "merge"]:
            self.merged = True
            self.merge_argv = argv
            r.returncode = self.merge_rc
            r.stderr = "" if self.merge_rc == 0 else "merge boom"
        return r


def _gate_ok(*a, **k):
    return {"ok": True, "blockers": []}


def _gate_blocked(*a, **k):
    return {"ok": False, "blockers": ["merge-state:BLOCKED"]}


def test_merges_when_gate_ok_and_base_not_default():
    run = _Run(base="conductor/run-x", default="main", head="c0ffee")
    res = merge_cmd.merge("o/r", 7, local_verify="true", run=run, check_fn=_gate_ok)
    assert res["ok"] and res["merged"] and run.merged
    # anti-TOCTOU: the merge is bound to the exact SHA we gated
    assert "--match-head-commit" in run.merge_argv
    assert "c0ffee" in run.merge_argv
    assert "--admin" not in run.merge_argv and "--squash" not in run.merge_argv


def test_refuses_when_base_is_default_branch():
    """The final owner PR (base=main) must never be auto-merged — even if the gate would pass."""
    run = _Run(base="main", default="main")
    res = merge_cmd.merge("o/r", 7, local_verify="true", run=run, check_fn=_gate_ok)
    assert not res["ok"] and not res["merged"] and not run.merged
    assert any("base-is-default" in b for b in res["blockers"])


def test_base_default_allowed_with_explicit_env(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE", "1")
    run = _Run(base="main", default="main")
    res = merge_cmd.merge("o/r", 7, local_verify="true", run=run, check_fn=_gate_ok)
    assert res["ok"] and res["merged"]  # legacy 0.4.x direct-merge escape hatch


def test_does_not_merge_when_gate_blocks():
    run = _Run(base="conductor/run-x", default="main")
    res = merge_cmd.merge(
        "o/r", 7, local_verify="true", run=run, check_fn=_gate_blocked
    )
    assert not res["ok"] and not res["merged"] and not run.merged
    assert "merge-state:BLOCKED" in res["blockers"]


def test_gh_lookup_failure_fails_closed():
    def boom(argv, **kw):
        raise RuntimeError("gh down")

    res = merge_cmd.merge("o/r", 7, local_verify="true", run=boom, check_fn=_gate_ok)
    assert not res["ok"] and not res["merged"]
    assert any("lookup-error" in b for b in res["blockers"])


def test_merge_command_failure_reported():
    run = _Run(base="conductor/run-x", default="main", merge_rc=1)
    res = merge_cmd.merge("o/r", 7, local_verify="true", run=run, check_fn=_gate_ok)
    assert not res["ok"] and not res["merged"]
    assert any("merge-failed" in b for b in res["blockers"])


def test_cli_rejects_non_integer_pr():
    assert merge_cmd.main(["abc"]) == 64
    assert merge_cmd.main(["--help"]) == 0
