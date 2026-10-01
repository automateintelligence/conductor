"""``conductor review <pr>`` — launch the run's reviewer host, record a ``reviewer`` dispatch.

Real git repository with a bare remote, a real registered run, a recording ``gh`` fake driven by
a JSON config, and stub reviewer binaries replaying the recorded fixtures. Nothing here reaches
a real host (``tests/conftest.py`` refuses a real ``codex``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conductor import dispatches, review_cmd, run_cmd
from conductor.core import runstate

from . import codex_stub

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "conductor" / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex-exec-json-0.156.1.jsonl"
CLAUDE_FIXTURE = FIXTURES / "claude-print-json-2.1.286.json"

BASE_BRANCH = "trunk"
REPO = "acme/widget"
PR = "7"

_GH_FAKE = r"""#!/usr/bin/env python3
import json, os, sys
CONFIG = json.load(open(os.environ["GH_FAKE_CONFIG"], encoding="utf-8"))
argv = sys.argv[1:]
with open(os.environ["GH_FAKE_LOG"], "a", encoding="utf-8") as h:
    h.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
if argv[:2] == ["pr", "view"]:
    pr = CONFIG["prs"].get(argv[2])
    if pr is None:
        sys.stderr.write("no pull request %s\n" % argv[2]); raise SystemExit(1)
    fields = argv[argv.index("--json") + 1].split(",")
    print(json.dumps({f: pr[f] for f in fields})); raise SystemExit(0)
sys.stderr.write("unsupported gh invocation: %s\n" % " ".join(argv)); raise SystemExit(1)
"""


class Proj:
    def __init__(self, root: Path, bindir: Path, config: Path, tmp: Path) -> None:
        self.root = root
        self.bindir = bindir
        self.config = config
        self.tmp = tmp
        self.run_key = ""
        self.exec_log = tmp / "exec-calls.jsonl"
        self.brief = tmp / "brief.md"
        self.brief.write_text("Phase 3: the reviewer verb.\n", encoding="utf-8")
        self.head = ""

    @property
    def state_root(self) -> str:
        return os.path.join(str(self.root), ".conductor")

    @property
    def dispatches(self) -> list[dict]:
        doc = runstate.load(self.state_root, self.run_key)
        assert doc is not None
        return doc["dispatches"]

    @property
    def exec_calls(self) -> list[list[str]]:
        if not self.exec_log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.exec_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def set_pr(self, *, head: str | None = None, body: str = "Closes #12") -> None:
        self.config.write_text(
            json.dumps(
                {
                    "prs": {
                        PR: {
                            "number": int(PR),
                            "title": "Add the reviewer verb",
                            "body": body,
                            "url": f"https://github.com/{REPO}/pull/{PR}",
                            "baseRefName": BASE_BRANCH,
                            "headRefOid": head or self.head,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def codex(self, **kwargs) -> None:
        kwargs.setdefault("exec_fixture", CODEX_FIXTURE)
        codex_stub.install(self.bindir, exec_log=self.exec_log, **kwargs)

    def claude(self) -> Path:
        """A `claude` that logs its argv and cwd as one JSON line, then replays the fixture."""
        log = self.tmp / "claude-calls.jsonl"
        exe = self.bindir / "claude"
        exe.write_text(
            f"#!{shutil.which('python3')}\n"
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'a') as f:\n"
            "    f.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
            f"sys.stdout.write(open({str(CLAUDE_FIXTURE)!r}).read())\n",
            encoding="utf-8",
        )
        exe.chmod(0o755)
        return log

    def review(self, *extra: str) -> int:
        return review_cmd.main([PR, "--brief", str(self.brief), *extra])


@pytest.fixture
def proj(tmp_path, git_env, git, monkeypatch, capsys):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "alpha.md").write_text("# alpha\n")
    subprocess.run(
        ["git", "init", "-q", "-b", BASE_BRANCH, str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", BASE_BRANCH, str(bare)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "-u", "origin", BASE_BRANCH)
    git(root, "checkout", "-q", "-b", "phase-3")
    (root / "docs" / "alpha.md").write_text("# alpha\nphase work\n")
    git(root, "commit", "-qam", "phase work")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_GH_FAKE, encoding="utf-8")
    gh.chmod(0o755)
    config = tmp_path / "gh-config.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_FAKE_LOG", str(tmp_path / "gh-calls.jsonl"))
    monkeypatch.setenv("GH_FAKE_CONFIG", str(config))
    monkeypatch.setenv("CONDUCTOR_REPO", REPO)
    monkeypatch.setenv("CONDUCTOR_HOME", str(root))
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    for name in (
        "CONDUCTOR_GATE_DIR",
        "CONDUCTOR_GATE_SLUG",
        "CONDUCTOR_HOST",
        "CONDUCTOR_REVIEW_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root)

    p = Proj(root, bindir, config, tmp_path)
    p.head = git(root, "rev-parse", "HEAD").stdout.strip()
    p.set_pr()
    p.codex()
    assert run_cmd.main(["new", "docs/alpha.md", "--project", str(root)]) == 0
    p.run_key = capsys.readouterr().out.strip()
    return p


def test_review_prints_the_review_and_records_a_reviewer_dispatch(proj, capsys):
    assert proj.review() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert len(proj.exec_calls) == 1
    entry = proj.dispatches[-1]
    assert entry["role"] == "reviewer"
    assert entry["host"] == "codex"  # the opposite of the default run host (claude)
    assert entry["phase_id"] == "12"
    assert entry["head_sha"] == proj.head
    assert entry["outcome"] == "ok"
    assert entry["input_tokens"] == 34072
    assert entry["cached_input_tokens"] == 0
    assert entry["output_tokens"] == 22


def test_review_prompt_names_the_head_base_and_brief(proj, git):
    base_sha = git(proj.root, "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    assert proj.review() == 0
    argv = proj.exec_calls[0]
    prompt = argv[-1]
    assert f"git diff {base_sha}..{proj.head}" in prompt
    assert "PR #7: Add the reviewer verb" in prompt
    assert "Phase 3: the reviewer verb." in prompt
    assert "VERDICT: APPROVE | VERDICT: CHANGES REQUESTED" in prompt
    assert argv[argv.index("--cd") + 1] == str(proj.root)
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_review_uses_the_run_reviewer_host_when_set(proj, capsys):
    runstate.update(
        proj.state_root,
        proj.run_key,
        lambda doc: {**doc, "reviewer_host": "claude"},
    )
    log = proj.claude()
    assert proj.review() == 0
    assert capsys.readouterr().out.strip() == "alpha"
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    argv = call["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert os.path.realpath(call["cwd"]) == os.path.realpath(proj.root)
    assert proj.exec_calls == []  # codex was not launched
    entry = proj.dispatches[-1]
    assert entry["host"] == "claude"
    assert entry["role"] == "reviewer"
    assert entry["input_tokens"] is not None


def test_review_refuses_a_checkout_that_is_not_the_pr_head(proj, capsys):
    proj.set_pr(head="0" * 40)
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "checkout-stale" in err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_times_out_and_records_it(proj, capsys):
    proj.codex(exec_sleep=60)
    assert proj.review("--timeout", "1") == 4
    assert "review-timeout after 1s (codex)" in capsys.readouterr().err
    entry = proj.dispatches[-1]
    assert entry["role"] == "reviewer"
    assert entry["outcome"] == "timeout"
    assert entry["input_tokens"] is None
    assert entry["output_tokens"] is None


def test_review_host_failure_exits_3_with_the_hosts_output(proj, capsys):
    proj.codex(exec_fixture=None, exec_stderr="usage limit reached\n", exec_exit=1)
    assert proj.review() == 3
    captured = capsys.readouterr()
    assert "usage limit reached" in captured.err
    assert captured.out == ""
    assert proj.dispatches[-1]["outcome"] == "error"


def test_review_still_prints_when_recording_fails(proj, capsys, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise runstate.RevisionConflict("run changed under the review")

    monkeypatch.setattr(dispatches, "append", refuse)
    assert proj.review() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert "usage-unrecorded" in captured.err
    assert "RevisionConflict" in captured.err


def test_review_refuses_an_oversized_brief(proj, capsys):
    proj.brief.write_bytes(b"x" * 70_000)
    assert proj.review() == 2
    assert "brief-too-large" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_exits_2_when_the_reviewer_host_is_not_installed(
    proj, capsys, monkeypatch
):
    bare = proj.tmp / "no-hosts"
    bare.mkdir()
    (bare / "gh").symlink_to(proj.bindir / "gh")
    (bare / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
    (bare / "python3").symlink_to(shutil.which("python3") or "/usr/bin/python3")
    monkeypatch.setenv("PATH", str(bare))
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "`codex` is not on PATH" in err
    assert "gh pr view" not in err  # the PR was read; the host is what is missing
    assert proj.dispatches == []


def test_review_exits_2_when_gh_cannot_view_the_pr(proj, capsys):
    assert review_cmd.main(["99", "--brief", str(proj.brief)]) == 2
    assert "gh pr view" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_exits_2_when_the_brief_would_be_parsed_as_an_option(
    proj, capsys, monkeypatch
):
    monkeypatch.setattr(review_cmd, "PROMPT", "--oops {brief}")
    assert proj.review() == 2
    assert "option" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_without_usage_in_output_records_a_note(proj, capsys):
    proj.codex(exec_fixture=None)
    assert proj.review() == 3  # no agent message: an empty review is an error
    entry = proj.dispatches[-1]
    assert entry["outcome"] == "error"
    assert entry["note"] == "no-usage-in-output"


def test_review_timeout_defaults_from_the_environment(proj, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_REVIEW_TIMEOUT_S", "1")
    proj.codex(exec_sleep=60)
    assert proj.review() == 4


def test_no_gate_path_reads_dispatches():
    for mod in ("conductor/merge_gate.py", "conductor/merge_cmd.py"):
        assert "dispatches" not in (ROOT / mod).read_text()


def test_review_exits_2_for_an_unknown_reviewer_host(proj, capsys):
    runstate.update(
        proj.state_root,
        proj.run_key,
        lambda doc: {**doc, "reviewer_host": "Codex"},
    )
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "reviewer host unresolved" in err
    assert "UnknownHost" in err
    assert "Traceback" not in err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_exits_2_when_launching_the_host_raises_oserror(
    proj, capsys, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(review_cmd.bounded, "run_bounded", _only_for_host(fail))
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "reviewer host not launched" in err
    assert "Argument list too long" in err
    assert "Traceback" not in err
    assert proj.dispatches == []


def _only_for_host(replacement):
    """Send only the reviewer host launch to ``replacement``; git and gh keep running for real."""
    real = review_cmd.bounded.run_bounded

    def route(argv, **kwargs):
        if os.path.basename(argv[0]) in ("codex", "claude"):
            return replacement(argv, **kwargs)
        return real(argv, **kwargs)

    return route


def _prompt_overhead(proj, git) -> int:
    base_sha = git(proj.root, "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    empty = review_cmd.PROMPT.format(
        reviewer="codex",
        number=int(PR),
        title="Add the reviewer verb",
        url=f"https://github.com/{REPO}/pull/{PR}",
        head_sha=proj.head,
        base_sha=base_sha,
        brief="",
    )
    return len(empty.encode("utf-8"))


def test_review_accepts_a_prompt_of_exactly_the_cap(proj, git, capsys):
    cap = review_cmd._PROMPT_MAX_BYTES
    proj.brief.write_bytes(b"x" * (cap - _prompt_overhead(proj, git)))
    assert proj.review() == 0
    assert len(proj.exec_calls[0][-1].encode("utf-8")) == cap


def test_review_refuses_a_prompt_one_byte_over_the_cap(proj, git, capsys):
    cap = review_cmd._PROMPT_MAX_BYTES
    proj.brief.write_bytes(b"x" * (cap - _prompt_overhead(proj, git) + 1))
    assert proj.review() == 2
    assert "brief-too-large" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_counts_replacement_inflation_against_the_cap(proj, git, capsys):
    # Each invalid byte decodes to U+FFFD (3 bytes): well under the cap raw, over it decoded.
    cap = review_cmd._PROMPT_MAX_BYTES
    proj.brief.write_bytes(b"\xff" * (cap // 2))
    assert proj.brief.stat().st_size < cap
    assert proj.review() == 2
    assert "brief-too-large" in capsys.readouterr().err
    assert proj.exec_calls == []


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "-inf"])
def test_review_refuses_a_timeout_that_is_not_finite_and_positive(proj, capsys, bad):
    assert proj.review(f"--timeout={bad}") == 2
    assert "bad timeout" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []


@pytest.mark.parametrize("bad", ["0", "nan", "soon"])
def test_review_refuses_a_bad_timeout_from_the_environment(
    proj, capsys, monkeypatch, bad
):
    monkeypatch.setenv("CONDUCTOR_REVIEW_TIMEOUT_S", bad)
    assert proj.review() == 2
    assert "timeout" in capsys.readouterr().err.lower()
    assert proj.exec_calls == []
    assert proj.dispatches == []
