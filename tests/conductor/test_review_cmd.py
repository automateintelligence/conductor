"""``conductor review <pr>`` — launch the run's reviewer host, record a ``reviewer`` dispatch.

Real git repository with a bare remote, a real registered run, a recording ``gh`` fake driven by
a JSON config, and stub reviewer binaries replaying the recorded fixtures. Nothing here reaches
a real host (``tests/conftest.py`` refuses a real ``codex``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conductor import dispatches, review_cmd, run_cmd
from conductor.core import resolve, runstate, schema
from conductor.hosts import bounded

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
            "import glob, json, os, sys\n"
            "args = sys.argv[1:]\n"
            "ctx = args[args.index('--add-dir') + 1] if '--add-dir' in args else ''\n"
            "diffs = {p: open(p).read() for p in glob.glob(os.path.join(ctx, '*'))}\n"
            f"with open({str(log)!r}, 'a') as f:\n"
            "    f.write(json.dumps({'argv': args, 'cwd': os.getcwd(), 'diffs': diffs}) + '\\n')\n"
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
    match = re.search(
        r"The full change is in (\S+) \(git diff (\w+)\.\.(\w+)\)\.", prompt
    )
    assert match is not None, prompt
    assert match.group(2, 3) == (base_sha, proj.head)
    assert os.path.basename(match.group(1)) == f"pr-{PR}.diff"
    assert not os.path.exists(os.path.dirname(match.group(1)))  # removed after the run
    assert "Read it, and read any file in this checkout you need for context." in prompt
    assert "Review the full change with" not in prompt  # no instruction to run git
    assert "Read-only: do not modify any file." in prompt
    assert "Do not run the project's full test suite." in prompt
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
    assert "--tools=Read,Grep,Glob" in argv and "--restricted" in argv
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


def test_review_times_out_and_records_it(proj, capsys, monkeypatch):
    monkeypatch.setattr(
        review_cmd, "_RESERVE_S", 0.0
    )  # the whole budget to the reviewer
    proj.codex(exec_sleep=60)
    assert proj.review("--timeout", "3") == 4
    assert "review-timeout after 3s (codex)" in capsys.readouterr().err
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


def _fixed_context_dir(proj, monkeypatch) -> Path:
    """Pin the per-review temp dir, so the diff path in the prompt has a known length."""
    ctx = proj.tmp / "review-ctx"

    def mkdtemp(*_args, **_kwargs):
        ctx.mkdir()
        return str(ctx)

    monkeypatch.setattr(review_cmd.tempfile, "mkdtemp", mkdtemp)
    return ctx


def _prompt_overhead(proj, git, ctx: Path) -> int:
    base_sha = git(proj.root, "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    empty = review_cmd.PROMPT.format(
        reviewer="codex",
        number=int(PR),
        title="Add the reviewer verb",
        url=f"https://github.com/{REPO}/pull/{PR}",
        head_sha=proj.head,
        base_sha=base_sha,
        diff_path=str(ctx / f"pr-{PR}.diff"),
        brief="",
    )
    return len(empty.encode("utf-8"))


def test_review_accepts_a_prompt_of_exactly_the_cap(proj, git, capsys, monkeypatch):
    ctx = _fixed_context_dir(proj, monkeypatch)
    cap = review_cmd._PROMPT_MAX_BYTES
    proj.brief.write_bytes(b"x" * (cap - _prompt_overhead(proj, git, ctx)))
    assert proj.review() == 0
    assert len(proj.exec_calls[0][-1].encode("utf-8")) == cap


def test_review_refuses_a_prompt_one_byte_over_the_cap(proj, git, capsys, monkeypatch):
    ctx = _fixed_context_dir(proj, monkeypatch)
    cap = review_cmd._PROMPT_MAX_BYTES
    proj.brief.write_bytes(b"x" * (cap - _prompt_overhead(proj, git, ctx) + 1))
    assert proj.review() == 2
    assert "brief-too-large" in capsys.readouterr().err
    assert proj.exec_calls == []
    assert proj.dispatches == []
    assert not ctx.exists()  # the refused review left nothing behind


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


def test_review_gives_the_claude_reviewer_the_diff_in_a_context_dir_then_removes_it(
    proj, git, capsys
):
    runstate.update(
        proj.state_root,
        proj.run_key,
        lambda doc: {**doc, "reviewer_host": "claude"},
    )
    log = proj.claude()
    base_sha = git(proj.root, "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    expected = git(proj.root, "diff", "--no-color", f"{base_sha}..{proj.head}").stdout
    assert proj.review() == 0
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    ctx = call["argv"][call["argv"].index("--add-dir") + 1]
    diff_path = os.path.join(ctx, f"pr-{PR}.diff")
    assert call["diffs"] == {diff_path: expected}  # present, and right, during the run
    assert "+phase work" in expected
    assert f"The full change is in {diff_path} " in call["argv"][1]
    assert not os.path.exists(ctx)


def test_review_removes_the_context_dir_when_the_host_fails(proj, capsys, monkeypatch):
    seen: list[str] = []
    real_mkdtemp = review_cmd.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        seen.append(real_mkdtemp(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(review_cmd.tempfile, "mkdtemp", recording_mkdtemp)
    proj.codex(exec_fixture=None, exec_stderr="usage limit reached\n", exec_exit=1)
    assert proj.review() == 3
    assert len(seen) == 1 and not os.path.exists(seen[0])


def test_review_timeout_defaults_to_540_seconds(proj, capsys, monkeypatch):
    """Under Claude's 600 s Bash maximum and the fire watchdog's 1800 s idle window."""
    assert review_cmd._DEFAULT_REVIEW_TIMEOUT_S == 540
    seen: list[float] = []

    def record(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return review_cmd.bounded.Bounded(
            returncode=0,
            stdout=CODEX_FIXTURE.read_text(encoding="utf-8"),
            stderr="",
            duration_s=0.1,
            timed_out=False,
        )

    monkeypatch.setattr(review_cmd.bounded, "run_bounded", _only_for_host(record))
    assert proj.review() == 0
    # The whole command's budget: what preflight used and the cleanup reserve come off it.
    (given,) = seen
    assert 540 - review_cmd._RESERVE_S - 30 < given <= 540 - review_cmd._RESERVE_S


class _Clock:
    """``review_cmd``'s monotonic clock, advanced only by the stubbed helpers below."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _budget_harness(monkeypatch, *, slow: dict[str, float], timed_out: tuple = ()):
    """Route every bounded call through a recorder. A helper named in ``slow`` (by its git/gh
    subcommand) advances the fake clock that long; one in ``timed_out`` reports a timeout."""
    clock = _Clock()
    monkeypatch.setattr(review_cmd, "_clock", clock)
    real = review_cmd.bounded.run_bounded
    seen: list[tuple[str, float]] = []

    def route(argv, **kwargs):
        name = os.path.basename(argv[0])
        what = name if name in ("codex", "claude") else f"{name} {argv[1]}"
        seen.append((what, kwargs["timeout"]))
        clock.now += slow.get(what, 0.0)
        if what in timed_out:
            return review_cmd.bounded.Bounded(
                returncode=None,
                stdout="",
                stderr="",
                duration_s=kwargs["timeout"],
                timed_out=True,
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(review_cmd.bounded, "run_bounded", route)
    return seen


def test_slow_preflight_leaves_the_reviewer_only_the_rest_of_the_budget(
    proj, capsys, monkeypatch
):
    """``--timeout`` bounds the whole command, so a slow fetch cannot push it past the
    worker's 600 s shell limit and kill a valid review."""
    seen = _budget_harness(monkeypatch, slow={"git fetch": 400.0})
    assert proj.review() == 0
    assert capsys.readouterr().out.strip() == "alpha"
    timeouts = dict(seen)
    assert timeouts["git fetch"] == review_cmd._TOOL_TIMEOUT_S
    assert timeouts["git merge-base"] == review_cmd._TOOL_TIMEOUT_S
    assert timeouts["codex"] == 540 - 400 - review_cmd._RESERVE_S
    assert proj.dispatches[-1]["outcome"] == "ok"


def test_a_helper_gets_no_more_than_the_budget_left(proj, capsys, monkeypatch):
    seen = _budget_harness(monkeypatch, slow={"git fetch": 500.0})
    assert proj.review() == 0
    timeouts = dict(seen)
    left = 540 - 500 - review_cmd._RESERVE_S
    assert timeouts["git merge-base"] == left
    assert timeouts["codex"] == left


def test_budget_spent_in_preflight_exits_4_and_launches_no_host(
    proj, capsys, monkeypatch
):
    seen = _budget_harness(monkeypatch, slow={"git fetch": 530.0})
    assert proj.review() == 4
    err = capsys.readouterr().err
    assert "review-timeout after 540s (codex)" in err
    assert "preflight" in err and "git merge-base" in err
    assert "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert [what for what, _ in seen if what in ("codex", "claude")] == []
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_a_helper_cut_short_by_the_budget_is_a_timeout_not_a_refusal(
    proj, capsys, monkeypatch
):
    _budget_harness(
        monkeypatch, slow={"git fetch": 500.0}, timed_out=("git merge-base",)
    )
    assert proj.review() == 4
    err = capsys.readouterr().err
    assert "review-timeout after 540s (codex)" in err and "git merge-base" in err
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_a_helper_timing_out_inside_the_budget_is_still_a_refusal(
    proj, capsys, monkeypatch
):
    _budget_harness(monkeypatch, slow={}, timed_out=("git fetch",))
    assert proj.review() == 2
    assert "git fetch failed: timed out" in capsys.readouterr().err
    assert proj.dispatches == []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_a_signal_during_the_review_kills_the_reviewer_and_records_it(
    proj, capsys, signame
):
    """The worker's shell limit or the fire watchdog kills `conductor review`; the reviewer it
    launched must die with it, and the attempt must still be on the run."""
    pidfile = proj.tmp / "reviewer.pid"
    exe = proj.bindir / "codex"
    exe.write_text(
        f"#!{shutil.which('python3')}\n"
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    sig = getattr(signal, signame)
    previous = signal.getsignal(sig)

    def deliver():
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pidfile.exists():  # never signal the suite itself when no reviewer started
            time.sleep(0.2)
            os.kill(os.getpid(), sig)

    sender = threading.Thread(target=deliver, daemon=True)
    sender.start()
    started = time.monotonic()
    rc = proj.review("--timeout", "60")
    sender.join(timeout=5)
    assert rc == 4
    assert time.monotonic() - started < 30
    assert f"review-interrupted ({signame})" in capsys.readouterr().err
    time.sleep(0.2)
    assert not _alive(int(pidfile.read_text()))
    entry = proj.dispatches[-1]
    assert entry["role"] == "reviewer" and entry["host"] == "codex"
    assert entry["outcome"] == "error"
    assert entry["note"] == "interrupted"
    assert signal.getsignal(sig) == previous  # handlers restored


def test_an_oserror_after_the_host_started_is_a_host_failure_not_a_refusal(
    proj, capsys, monkeypatch
):
    def broke_mid_run(argv, **kwargs):
        return bounded.Bounded(
            returncode=None,
            stdout="",
            stderr="run_bounded: OSError: [Errno 5] Input/output error",
            duration_s=1.0,
            timed_out=False,
        )

    monkeypatch.setattr(
        review_cmd.bounded, "run_bounded", _only_for_host(broke_mid_run)
    )
    assert proj.review() == 3
    err = capsys.readouterr().err
    assert "review-failed" in err and "Input/output error" in err
    assert proj.dispatches[-1]["outcome"] == "error"


def _assert_reviewed_but_unrecorded(proj, capsys, *needles: str) -> None:
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert len(proj.exec_calls) == 1  # codex: the opposite of the recorded run host
    unrecorded = [
        line for line in captured.err.splitlines() if "usage-unrecorded" in line
    ]
    assert len(unrecorded) == 1, captured.err
    assert unrecorded[0].startswith("usage-unrecorded reason=")
    for needle in needles:
        assert needle in unrecorded[0], unrecorded[0]
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("target", "exc"),
    [
        ("recover_pending", TimeoutError("state lock not acquired within 30s")),
        (
            "resolve",
            schema.SchemaError("run.json schema 9 is newer than this conductor"),
        ),
    ],
)
def test_review_runs_unrecorded_when_the_run_state_cannot_be_read(
    proj, capsys, monkeypatch, target, exc
):
    """Only RECORDING needs run.json; the reviewer host comes from ``.conductor/host``. A
    run-state failure must never cost the review."""

    def fail(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(resolve, target, fail)
    assert proj.review() == 0
    _assert_reviewed_but_unrecorded(proj, capsys, type(exc).__name__, str(exc))
    monkeypatch.undo()
    assert proj.dispatches == []


def _second_run(proj, git, capsys) -> str:
    (proj.root / "docs" / "beta.md").write_text("# beta\n")
    git(proj.root, "add", "-A")
    git(proj.root, "commit", "-qm", "beta")
    git(proj.root, "push", "-q", "origin", "HEAD")
    proj.head = git(proj.root, "rev-parse", "HEAD").stdout.strip()
    proj.set_pr()
    assert run_cmd.main(["new", "docs/beta.md", "--project", str(proj.root)]) == 0
    return capsys.readouterr().out.strip()


def test_review_runs_unrecorded_when_the_run_is_ambiguous(proj, git, capsys):
    beta = _second_run(proj, git, capsys)
    assert proj.review() == 0
    _assert_reviewed_but_unrecorded(proj, capsys, "RunAmbiguous")
    assert proj.dispatches == []
    beta_doc = runstate.load(proj.state_root, beta)
    assert beta_doc is not None and beta_doc["dispatches"] == []


def test_review_records_on_the_run_its_checkout_belongs_to_among_several(
    proj, git, capsys
):
    beta = _second_run(proj, git, capsys)
    runstate.update(
        proj.state_root, beta, lambda doc: {**doc, "phase_worktree": str(proj.root)}
    )
    assert proj.review() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert "usage-unrecorded" not in captured.err
    assert proj.dispatches == []
    beta_doc = runstate.load(proj.state_root, beta)
    assert beta_doc is not None
    assert [d["role"] for d in beta_doc["dispatches"]] == ["reviewer"]


def test_review_records_on_its_awaiting_merge_run_not_the_sole_active_one(
    proj, git, capsys
):
    """This checkout is run A's phase worktree and A awaits the team's merge; run B is the only
    active run. The reviewer dispatch belongs on A."""
    runstate.update(
        proj.state_root,
        proj.run_key,
        lambda doc: {**doc, "phase_worktree": str(proj.root)},
    )
    runstate.set_status(proj.state_root, proj.run_key, "awaiting-team-merge")
    beta = _second_run(proj, git, capsys)
    assert proj.review() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert "usage-unrecorded" not in captured.err
    assert [d["role"] for d in proj.dispatches] == ["reviewer"]
    beta_doc = runstate.load(proj.state_root, beta)
    assert beta_doc is not None and beta_doc["dispatches"] == []


def test_review_runs_unrecorded_for_an_unknown_run_key(proj, capsys):
    assert proj.review("--run", "nope-0badf00d") == 0
    _assert_reviewed_but_unrecorded(proj, capsys, "RunNotFound", "nope-0badf00d")
    assert proj.dispatches == []


def test_review_takes_the_recorded_run_host_when_the_run_cannot_be_read(
    proj, capsys, monkeypatch
):
    (proj.root / ".conductor" / "host").write_text("codex\n", encoding="utf-8")
    log = proj.claude()

    def fail(*_args, **_kwargs):
        raise TimeoutError("state lock not acquired within 30s")

    monkeypatch.setattr(resolve, "recover_pending", fail)
    assert proj.review() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "alpha"
    assert "usage-unrecorded reason=TimeoutError" in captured.err
    assert len(log.read_text().splitlines()) == 1  # claude, the opposite of codex
    assert proj.exec_calls == []


def test_review_interrupted_without_a_run_reports_it_unrecorded(
    proj, capsys, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise TimeoutError("state lock not acquired within 30s")

    monkeypatch.setattr(resolve, "recover_pending", fail)
    pidfile = proj.tmp / "reviewer.pid"
    exe = proj.bindir / "codex"
    exe.write_text(
        f"#!{shutil.which('python3')}\n"
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)

    def deliver():
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pidfile.exists():  # never signal the suite itself when no reviewer started
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=deliver, daemon=True)
    sender.start()
    rc = proj.review("--timeout", "60")
    sender.join(timeout=5)
    assert rc == 4
    err = capsys.readouterr().err
    assert "review-interrupted (SIGTERM)" in err
    assert "usage-unrecorded reason=TimeoutError" in err
    monkeypatch.undo()
    assert proj.dispatches == []


def test_review_exits_2_when_a_helper_cannot_be_executed(proj, capsys, monkeypatch):
    real = review_cmd.bounded.run_bounded

    def denied(argv, **kwargs):
        if os.path.basename(argv[0]) == "gh":
            raise PermissionError(13, "Permission denied", argv[0])
        return real(argv, **kwargs)

    monkeypatch.setattr(review_cmd.bounded, "run_bounded", denied)
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "gh pr view 7 failed" in err and "Permission denied" in err
    assert "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert proj.exec_calls == []
    assert proj.dispatches == []


def test_review_exits_2_when_no_temp_dir_can_be_made(proj, capsys, monkeypatch):
    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(review_cmd.tempfile, "mkdtemp", full)
    assert proj.review() == 2
    err = capsys.readouterr().err
    assert "no temp dir for the diff" in err and "No space left" in err
    assert "Traceback" not in err
    assert proj.exec_calls == []
    assert proj.dispatches == []
