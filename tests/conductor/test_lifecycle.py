"""``conductor status | resume | heartbeat | finish``, and the final-PR reconciliation they share.

The invariant under all of it (A-DH-7): Conductor never completes the final default-branch pull
request. ``test_no_lifecycle_module_can_complete_a_pull_request`` is the source-level guard;
``test_finish_refuses_...`` are the behavioural ones.

Real git repositories and a recording ``gh`` fake, never mocks: the verbs resolve their state
root from git plumbing and their pull requests from ``gh``, so a mock would test the mock.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conductor import finalpr, lifecycle, run_cmd
from conductor.core import ownership, registry, runstate, schema, transaction

ROOT = Path(__file__).resolve().parents[2]

_GH_FAKE = r"""#!/usr/bin/env python3
import json, os, sys
CONFIG = json.load(open(os.environ["GH_FAKE_CONFIG"], encoding="utf-8"))
argv = sys.argv[1:]
with open(os.environ["GH_FAKE_LOG"], "a", encoding="utf-8") as h:
    h.write(json.dumps(argv) + "\n")

def fields(args):
    return args[args.index("--json") + 1].split(",") if "--json" in args else []

if argv[:2] == ["repo", "view"]:
    if "defaultBranchRef" in fields(argv):
        if CONFIG.get("default_branch") is None:
            sys.stderr.write("no default branch\n"); raise SystemExit(1)
        print(CONFIG["default_branch"]); raise SystemExit(0)
    if "nameWithOwner" in fields(argv):
        print(CONFIG["repo"]); raise SystemExit(0)
if argv[:2] == ["pr", "view"]:
    pr = CONFIG["prs"].get(argv[2])
    if pr is None:
        sys.stderr.write("no pull request %s\n" % argv[2]); raise SystemExit(1)
    print(json.dumps({f: pr[f] for f in fields(argv)})); raise SystemExit(0)
sys.stderr.write("unsupported gh invocation: %s\n" % " ".join(argv)); raise SystemExit(1)
"""

DEFAULT_BRANCH = "trunk"
REPO = "acme/widget"


class Project:
    """A repository with a bare remote, one run, and a recording ``gh`` fake on PATH."""

    def __init__(self, root: Path, bare: Path, log: Path, config: Path) -> None:
        self.root = root
        self.bare = bare
        self.log = log
        self.config = config
        self.run_key = ""

    @property
    def state_root(self) -> str:
        return os.path.join(str(self.root), ".conductor")

    @property
    def run(self) -> dict:
        doc = runstate.load(self.state_root, self.run_key)
        assert doc is not None
        return doc

    @property
    def gh_calls(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def set_prs(
        self, prs: dict, *, default_branch: str | None = DEFAULT_BRANCH
    ) -> None:
        self.config.write_text(
            json.dumps({"repo": REPO, "default_branch": default_branch, "prs": prs}),
            encoding="utf-8",
        )

    def status(self, value: str) -> None:
        runstate.set_status(self.state_root, self.run_key, value)

    def verb(self, *argv: str) -> int:
        return lifecycle.main([*argv, "--project", str(self.root)])


@pytest.fixture
def project(tmp_path, git_env, git, monkeypatch, capsys):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "alpha.md").write_text("# alpha\n")
    subprocess.run(
        ["git", "init", "-q", "-b", DEFAULT_BRANCH, str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", DEFAULT_BRANCH, str(bare)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "-u", "origin", DEFAULT_BRANCH)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(_GH_FAKE, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "gh-calls.jsonl"
    config = tmp_path / "gh-config.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_FAKE_LOG", str(log))
    monkeypatch.setenv("GH_FAKE_CONFIG", str(config))
    monkeypatch.setenv("CONDUCTOR_REPO", REPO)
    monkeypatch.setenv("CONDUCTOR_HOME", str(root))
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    for name in ("CONDUCTOR_GATE_DIR", "CONDUCTOR_GATE_SLUG", "CONDUCTOR_HOST"):
        monkeypatch.delenv(name, raising=False)

    proj = Project(root, bare, log, config)
    proj.set_prs({})
    assert run_cmd.main(["new", "docs/alpha.md", "--project", str(root)]) == 0
    proj.run_key = capsys.readouterr().out.strip()
    return proj


def _publish_integration_branch(project: Project, git) -> str:
    """Push the run's integration branch so the remote can be asked for its tip."""
    branch = project.run["integration_branch"]
    git(project.root, "branch", branch)
    git(project.root, "push", "-q", "origin", branch)
    return git(project.root, "rev-parse", branch).stdout.strip()


def _pr(base: str, head: str, state: str, number: int) -> dict:
    return {
        "baseRefName": base,
        "headRefOid": head,
        "state": state,
        "url": f"https://github.com/{REPO}/pull/{number}",
    }


def _publish_pull_ref(project: Project, git, number: int, branch: str) -> None:
    git(project.root, "push", "-q", "origin", f"{branch}:refs/pull/{number}/head")


# --- the invariant, at source level ---------------------------------------------------------


def test_no_lifecycle_module_can_complete_a_pull_request() -> None:
    """A-DH-7 as a source guard on the two modules that see the final pull request.

    The behavioural assertion sweeps the CLI; this catches the edit that would break it while
    the sweep's fixture happens not to reach the new line."""
    forbidden = (
        "pr merge",
        "pr close",
        '"merge"',
        "--squash",
        "--rebase",
        "--auto",
        "--admin",
        "enablePullRequestAutoMerge",
        "enqueuePullRequest",
        "mergeQueue",
        "push --force",
        "--force-with-lease",
    )
    for module in (finalpr, lifecycle):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        # Only executable lines: the module headers describe the prohibition in prose.
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        code = code.split('"""')[0] + "".join(code.split('"""')[2::2])
        for token in forbidden:
            assert token not in code, f"{module.__name__} contains {token!r}"


# --- finalpr --------------------------------------------------------------------------------


def test_candidate_numbers_reads_the_remotes_own_pull_refs(project, git) -> None:
    branch = _publish_integration_branch(project, git)
    assert branch
    _publish_pull_ref(project, git, 7, DEFAULT_BRANCH)
    _publish_pull_ref(project, git, 42, DEFAULT_BRANCH)
    assert finalpr.candidate_numbers(str(project.root), "origin") == [7, 42]


def test_remote_tip_is_none_for_a_branch_the_remote_does_not_have(project) -> None:
    assert finalpr.remote_tip(str(project.root), "origin", "no/such/branch") is None
    assert finalpr.remote_tip(str(project.root), "origin", DEFAULT_BRANCH)


def test_recorded_number_refuses_a_malformed_reference(project) -> None:
    run = dict(project.run, github={"final_pr": "not-a-number"})
    with pytest.raises(finalpr.FinalPullRequestError):
        finalpr.recorded_number(run)
    assert finalpr.recorded_number(project.run) is None


def test_reconcile_prefers_the_recorded_reference_over_rediscovery(
    project, git
) -> None:
    project.set_prs({"9": _pr(DEFAULT_BRANCH, "deadbeef", "MERGED", 9)})
    run = dict(project.run, github={"final_pr": 9})
    pull, recovered = finalpr.reconcile(
        repo_root=str(project.root),
        repo=REPO,
        remote="origin",
        default_branch=DEFAULT_BRANCH,
        run=run,
    )
    assert (pull.number, pull.merged, recovered) == (9, True, False)
    assert not any(call[:2] == ["git", "ls-remote"] for call in project.gh_calls)


def test_reconcile_recovers_the_pull_request_the_integration_branch_points_at(
    project, git
) -> None:
    head = _publish_integration_branch(project, git)
    branch = project.run["integration_branch"]
    _publish_pull_ref(project, git, 11, branch)
    _publish_pull_ref(project, git, 12, branch)
    project.set_prs(
        {
            # Same base, but only #12 sits at the integration branch's tip.
            "11": _pr(DEFAULT_BRANCH, "0" * 40, "OPEN", 11),
            "12": _pr(DEFAULT_BRANCH, head, "OPEN", 12),
        }
    )
    pull, recovered = finalpr.reconcile(
        repo_root=str(project.root),
        repo=REPO,
        remote="origin",
        default_branch=DEFAULT_BRANCH,
        run=project.run,
    )
    assert (pull.number, recovered) == (12, True)


def test_reconcile_refuses_rather_than_guessing_between_two_candidates(
    project, git
) -> None:
    """The integration branch does not resolve, so uniqueness is all the identification has."""
    _publish_pull_ref(project, git, 11, DEFAULT_BRANCH)
    _publish_pull_ref(project, git, 12, DEFAULT_BRANCH)
    project.set_prs(
        {
            "11": _pr(DEFAULT_BRANCH, "a" * 40, "OPEN", 11),
            "12": _pr(DEFAULT_BRANCH, "b" * 40, "OPEN", 12),
        }
    )
    with pytest.raises(finalpr.FinalPullRequestError, match="Refusing to guess"):
        finalpr.reconcile(
            repo_root=str(project.root),
            repo=REPO,
            remote="origin",
            default_branch=DEFAULT_BRANCH,
            run=project.run,
        )


def test_reconcile_ignores_a_pull_request_onto_a_non_default_base(project, git) -> None:
    _publish_pull_ref(project, git, 5, DEFAULT_BRANCH)
    project.set_prs({"5": _pr("some/phase-branch", "a" * 40, "OPEN", 5)})
    with pytest.raises(finalpr.FinalPullRequestError, match="has base"):
        finalpr.reconcile(
            repo_root=str(project.root),
            repo=REPO,
            remote="origin",
            default_branch=DEFAULT_BRANCH,
            run=project.run,
        )


# --- status ---------------------------------------------------------------------------------


def test_status_reports_the_run_and_exits_zero(project, capsys) -> None:
    assert project.verb("status", "--run", project.run_key) == 0
    out = capsys.readouterr().out
    assert project.run_key in out
    assert "active" in out
    assert project.run["integration_branch"] in out


def test_status_json_carries_the_owner_and_its_liveness(project, capsys) -> None:
    with ownership.acquire(project.state_root, project.run_key, host="claude"):
        assert project.verb("status", "--run", project.run_key, "--json") == 0
        report = json.loads(capsys.readouterr().out)
    assert report["owner"]["state"] == "live"
    assert report["owner"]["identity"] == str(os.getpid())
    assert report["status"] == "active"


def test_status_reports_a_pending_transaction_without_recovering_it(
    project, capsys
) -> None:
    """Read-only means read-only: the journal is still there afterwards."""
    doc = project.run
    after = schema.clone(doc)
    after["revision"] = doc["revision"] + 1
    transaction.prepare(
        project.state_root,
        "probe-journal",
        [
            {
                "path": runstate.run_path(project.state_root, project.run_key),
                "before": doc,
                "after": after,
                "lock": {
                    "path": runstate.state_lock_path(
                        project.state_root, project.run_key
                    ),
                    "run_key": project.run_key,
                },
            }
        ],
    )
    assert project.verb("status", "--run", project.run_key) == 0
    captured = capsys.readouterr()
    assert "probe-journal" in captured.out
    assert "prepared" in captured.out
    assert transaction.pending(project.state_root) == ["probe-journal"]


def test_status_refuses_when_the_run_key_names_nothing(project) -> None:
    assert (
        project.verb("status", "--run", "no-such-run-abcdef12") == lifecycle.EXIT_NO_RUN
    )


# --- resume ---------------------------------------------------------------------------------


def test_resume_returns_a_checkpointed_run_to_active_and_writes_the_mirror(
    project, capsys
) -> None:
    project.status("checkpointed")
    assert project.verb("resume", "--run", project.run_key) == 0
    assert project.run["status"] == "active"
    assert project.run["last_reconciled_at"]
    doc = registry.load(project.state_root)
    assert doc is not None
    found = registry.find_run(doc, project.run_key)
    assert found is not None
    _, generation = found
    assert generation["status"] == "active"
    assert transaction.pending(project.state_root) == []


def test_resume_returns_a_blocked_run_to_active(project) -> None:
    project.status("blocked")
    assert project.verb("resume", "--run", project.run_key) == 0
    assert project.run["status"] == "active"


def test_resume_refuses_a_terminal_run(project, capsys) -> None:
    project.status("awaiting-team-merge")
    project.status("terminal")
    assert project.verb("resume", "--run", project.run_key) == 1
    assert "not resumable" in capsys.readouterr().err
    assert project.run["status"] == "terminal"


def test_resume_refuses_while_a_live_owner_holds_the_run(project, capsys) -> None:
    project.status("checkpointed")
    with ownership.acquire(project.state_root, project.run_key, host="claude"):
        assert project.verb("resume", "--run", project.run_key) == 1
    assert "is owned by" in capsys.readouterr().err
    assert project.run["status"] == "checkpointed"


def test_bare_resume_refuses_a_run_awaiting_the_teams_merge(
    project, git, capsys
) -> None:
    head = _publish_integration_branch(project, git)
    _publish_pull_ref(project, git, 202, project.run["integration_branch"])
    project.set_prs({"202": _pr(DEFAULT_BRANCH, head, "OPEN", 202)})
    project.status("awaiting-team-merge")
    assert project.verb("resume", "--run", project.run_key) == 1
    err = capsys.readouterr().err
    assert "/pull/202" in err and "OPEN" in err and "--reactivate" in err
    assert project.run["status"] == "awaiting-team-merge"


def test_reactivate_returns_a_run_awaiting_the_teams_merge_to_active(
    project, git, capsys
) -> None:
    head = _publish_integration_branch(project, git)
    _publish_pull_ref(project, git, 202, project.run["integration_branch"])
    project.set_prs({"202": _pr(DEFAULT_BRANCH, head, "OPEN", 202)})
    project.status("awaiting-team-merge")
    assert project.verb("resume", "--run", project.run_key, "--reactivate") == 0
    assert project.run["status"] == "active"


def test_resume_sends_a_merged_final_pull_request_to_finish_instead(
    project, git, capsys
) -> None:
    head = _publish_integration_branch(project, git)
    _publish_pull_ref(project, git, 202, project.run["integration_branch"])
    project.set_prs({"202": _pr(DEFAULT_BRANCH, head, "MERGED", 202)})
    project.status("awaiting-team-merge")
    assert project.verb("resume", "--run", project.run_key, "--reactivate") == 1
    assert "conductor finish --run" in capsys.readouterr().err
    assert project.run["status"] == "awaiting-team-merge"


# --- heartbeat ------------------------------------------------------------------------------


def _install_driver(project: Project, body: str) -> Path:
    from conductor import resume_script

    path = Path(resume_script.driver_script_path(str(project.root)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_heartbeat_reports_an_orphaned_schedule_on_a_run_awaiting_the_teams_merge(
    project, capsys
) -> None:
    project.status("awaiting-team-merge")
    assert project.verb("heartbeat", "--run", project.run_key) == 1
    err = capsys.readouterr().err
    assert "orphaned schedule entry" in err
    assert f"conductor finish --run {project.run_key}" in err


def test_heartbeat_on_a_blocked_run_reconciles_and_reports_only(
    project, capsys
) -> None:
    project.status("blocked")
    marker = project.root / "fired"
    _install_driver(project, f"#!/bin/sh\ntouch {marker}\n")
    assert project.verb("heartbeat", "--run", project.run_key) == 0
    assert not marker.exists()
    assert project.run["status"] == "blocked"
    assert project.run["last_reconciled_at"]
    assert "conductor resume --run" in capsys.readouterr().out


def test_heartbeat_refuses_when_the_run_has_no_durable_driver(project, capsys) -> None:
    assert project.verb("heartbeat", "--run", project.run_key) == 1
    assert "conductor driver install" in capsys.readouterr().err


def test_heartbeat_launches_the_driver_under_exclusive_ownership(project) -> None:
    """The fire runs, and it runs while this run's ownership record names its wrapper."""
    record = project.root / "owner-during-fire.json"
    _install_driver(
        project,
        "#!/bin/sh\n"
        f"cp {ownership.record_path(project.state_root, project.run_key)} {record}\n",
    )
    assert project.verb("heartbeat", "--run", project.run_key) == 0
    assert json.loads(record.read_text())["run_key"] == project.run_key
    assert project.run["last_reconciled_at"]
    # Released afterwards: the next fire must not find itself locked out.
    assert ownership.read(project.state_root, project.run_key) is None


def test_heartbeat_skips_successfully_while_another_owner_is_live(
    project, capsys
) -> None:
    marker = project.root / "fired"
    _install_driver(project, f"#!/bin/sh\ntouch {marker}\n")
    ownership._write(
        project.state_root,
        project.run_key,
        ownership.OwnerRecord(
            run_key=project.run_key,
            host="claude",
            tier="wrapper",
            wrapper_identity=str(os.getppid()),
            acquired_at="2026-01-01T00:00:00+00:00",
        ),
    )
    assert project.verb("heartbeat", "--run", project.run_key) == 0
    assert not marker.exists()
    assert "fire skipped" in capsys.readouterr().err


def test_heartbeat_reports_a_failing_fire(project, capsys) -> None:
    _install_driver(project, "#!/bin/sh\nexit 9\n")
    assert project.verb("heartbeat", "--run", project.run_key) == 1
    assert "fire ended rc=9" in capsys.readouterr().err


# --- finish ---------------------------------------------------------------------------------


def _awaiting(
    project: Project, git, *, state: str, base: str = DEFAULT_BRANCH, number: int = 202
):
    head = _publish_integration_branch(project, git)
    _publish_pull_ref(project, git, number, project.run["integration_branch"])
    project.set_prs({str(number): _pr(base, head, state, number)})
    project.status("awaiting-team-merge")
    return head


def test_finish_refuses_a_run_that_is_not_awaiting_the_teams_merge(
    project, capsys
) -> None:
    assert project.verb("finish", "--run", project.run_key) == 1
    err = capsys.readouterr().err
    assert "not awaiting-team-merge" in err
    assert project.run["status"] == "active"


def test_finish_refuses_the_unmerged_final_pull_request_and_names_it(
    project, git, capsys
) -> None:
    _awaiting(project, git, state="OPEN")
    assert project.verb("finish", "--run", project.run_key) == 1
    err = capsys.readouterr().err
    assert "/pull/202" in err
    assert "OPEN" in err
    assert "not MERGED" in err
    assert project.run["status"] == "awaiting-team-merge"
    assert not any(call[:2] == ["pr", "merge"] for call in project.gh_calls)


def test_finish_records_the_reference_it_reconciled_even_while_refusing(
    project, git, capsys
) -> None:
    _awaiting(project, git, state="OPEN")
    assert project.run["github"]["final_pr"] is None
    assert project.verb("finish", "--run", project.run_key) == 1
    capsys.readouterr()
    assert project.run["github"]["final_pr"] == 202
    assert transaction.pending(project.state_root) == []


def test_finish_refuses_when_the_final_pull_requests_base_is_not_the_default_branch(
    project, git, capsys
) -> None:
    """Reachable only through an explicit or recorded number: reconciliation SELECTS on the base,
    so a wrong-base pull request never reaches the checks that way. The check exists for the
    other route — a number handed in by an operator, or cached in run.json before the repository
    changed its default branch."""
    _awaiting(project, git, state="MERGED", base="release/1.x")
    assert project.verb("finish", "--run", project.run_key, "--pr", "202") == 1
    assert "not the repository default branch" in capsys.readouterr().err
    assert project.run["status"] == "awaiting-team-merge"


def test_finish_refuses_when_the_head_is_not_the_audited_run_head(
    project, git, capsys
) -> None:
    _awaiting(project, git, state="MERGED")
    runstate.update(
        project.state_root,
        project.run_key,
        lambda doc: {**doc, "last_review_head_sha": "c" * 40},
    )
    assert project.verb("finish", "--run", project.run_key) == 1
    assert "is not the audited run head" in capsys.readouterr().err
    assert project.run["status"] == "awaiting-team-merge"


def test_finish_refuses_while_review_debt_is_outstanding(project, git, capsys) -> None:
    _awaiting(project, git, state="MERGED")
    runstate.update(
        project.state_root,
        project.run_key,
        lambda doc: {
            **doc,
            "phase_reviews": [
                {
                    "phase_id": "phase-2",
                    "review_debt": {"outstanding": True, "required_host": "codex"},
                }
            ],
        },
    )
    assert project.verb("finish", "--run", project.run_key) == 1
    err = capsys.readouterr().err
    assert "owes a review from codex" in err
    assert project.run["status"] == "awaiting-team-merge"


def test_finish_refuses_when_the_default_branch_cannot_be_resolved(
    project, git, capsys
) -> None:
    """A-DH-6's rule reaches finish too: no literal fallback base, so no completion."""
    _awaiting(project, git, state="MERGED")
    project.set_prs(json.loads(project.config.read_text())["prs"], default_branch=None)
    subprocess.run(
        ["git", "-C", str(project.root), "remote", "set-head", "origin", "-d"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert project.verb("finish", "--run", project.run_key) == 1
    assert "unresolvable-default-branch" in capsys.readouterr().err
    assert project.run["status"] == "awaiting-team-merge"


def test_finish_completes_a_merged_run_and_retains_its_evidence(
    project, git, capsys
) -> None:
    head = _awaiting(project, git, state="MERGED")
    branch = project.run["integration_branch"]
    assert project.verb("finish", "--run", project.run_key) == 0
    out = capsys.readouterr().out
    assert "/pull/202" in out
    assert project.run["status"] == "terminal"
    assert project.run["completed_at"]
    doc = registry.load(project.state_root)
    assert doc is not None
    found = registry.find_run(doc, project.run_key)
    assert found is not None
    _, generation = found
    assert generation["status"] == "terminal"
    # Local branch gone; the REMOTE ref it was pushed to is untouched.
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(project.root),
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        ).returncode
        != 0
    )
    assert finalpr.remote_tip(str(project.root), "origin", branch) == head
    # Audit evidence retained.
    assert runstate.load(project.state_root, project.run_key) is not None


def test_finish_removes_the_runs_registered_worktree(project, git, capsys) -> None:
    worktree = (
        project.root / ".worktrees" / "conductor" / project.run_key / "integration"
    )
    git(project.root, "worktree", "add", "-q", "-b", "wt-branch", str(worktree))
    runstate.update(
        project.state_root,
        project.run_key,
        lambda doc: {**doc, "integration_worktree": str(worktree)},
    )
    _awaiting(project, git, state="MERGED")
    assert project.verb("finish", "--run", project.run_key) == 0
    assert f"removed worktree {worktree}" in capsys.readouterr().out
    assert not worktree.exists()


def test_finish_is_idempotent_on_a_terminal_run(project, git, capsys) -> None:
    _awaiting(project, git, state="MERGED")
    assert project.verb("finish", "--run", project.run_key) == 0
    capsys.readouterr()
    assert project.verb("finish", "--run", project.run_key) == 0
    assert "already terminal" in capsys.readouterr().out


def test_finish_refuses_while_a_live_owner_holds_the_run(project, git, capsys) -> None:
    _awaiting(project, git, state="MERGED")
    with ownership.acquire(project.state_root, project.run_key, host="claude"):
        assert project.verb("finish", "--run", project.run_key) == 1
    assert "is owned by" in capsys.readouterr().err
    assert project.run["status"] == "awaiting-team-merge"


# --- CLI surface ----------------------------------------------------------------------------


def test_the_wrapper_registers_all_four_verbs() -> None:
    text = (ROOT / "bin" / "conductor").read_text(encoding="utf-8")
    for verb in ("status", "resume", "heartbeat", "finish"):
        assert f"\n  {verb}) shift;" in text, f"bin/conductor does not register {verb}"


def test_an_unknown_verb_is_a_usage_error(capsys) -> None:
    assert lifecycle.main(["nonsense"]) == lifecycle.EXIT_USAGE
    assert "conductor finish" in capsys.readouterr().err


def test_a_bare_verb_resolves_the_single_active_run(project, capsys) -> None:
    assert project.verb("status") == 0
    assert project.run_key in capsys.readouterr().out


def test_a_bare_verb_is_ambiguous_with_two_active_runs(project, capsys) -> None:
    (project.root / "docs" / "beta.md").write_text("# beta\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project.root), "add", "-A"],
        capture_output=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(project.root),
            "-c",
            "user.email=t@e.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "beta",
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run_cmd.main(["new", "docs/beta.md", "--project", str(project.root)]) == 0
    capsys.readouterr()
    assert project.verb("status") == lifecycle.EXIT_AMBIGUOUS
    assert "active runs" in capsys.readouterr().err
