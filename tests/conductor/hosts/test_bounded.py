"""``run_bounded``: a child with no stdin, a time bound, and a kill that takes the whole group."""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from conductor.hosts import bounded


def test_run_bounded_returns_output_and_status(tmp_path):
    r = bounded.run_bounded(
        [sys.executable, "-c", "print('hi'); import sys; sys.exit(3)"],
        cwd=str(tmp_path),
        timeout=10,
    )
    assert r.returncode == 3 and r.stdout.strip() == "hi" and not r.timed_out


def _gone_or_zombie(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def test_run_bounded_kills_the_group_on_timeout(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']);"
        f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(600)"
    )
    # The timeout sits well above interpreter start-up so the pidfile exists before the kill.
    r = bounded.run_bounded(
        [sys.executable, "-c", script], cwd=str(tmp_path), timeout=5, grace=1
    )
    assert r.timed_out and r.returncode is None and r.duration_s < 15
    grandchild = int(pidfile.read_text())
    time.sleep(0.2)
    assert _gone_or_zombie(grandchild)


def test_run_bounded_escalates_to_kill_when_term_is_ignored(tmp_path):
    script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    r = bounded.run_bounded(
        [sys.executable, "-c", script], cwd=str(tmp_path), timeout=1, grace=1
    )
    assert r.timed_out and r.returncode is None and r.duration_s < 10


def test_run_bounded_gives_the_child_no_stdin(tmp_path):
    r = bounded.run_bounded(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        cwd=str(tmp_path),
        timeout=10,
    )
    assert r.stdout.strip() == "''"


def test_run_bounded_passes_the_environment_and_captures_stderr(tmp_path):
    r = bounded.run_bounded(
        [
            sys.executable,
            "-c",
            "import os,sys; print(os.environ['X_PROBE'], file=sys.stderr)",
        ],
        cwd=str(tmp_path),
        timeout=10,
        env={**os.environ, "X_PROBE": "seen"},
    )
    assert r.stderr.strip() == "seen" and r.returncode == 0


def test_run_bounded_reports_partial_output_of_a_timed_out_child(tmp_path):
    script = "import sys,time; print('early', flush=True); time.sleep(60)"
    r = bounded.run_bounded(
        [sys.executable, "-c", script], cwd=str(tmp_path), timeout=1, grace=1
    )
    assert r.timed_out and "early" in r.stdout


def test_run_bounded_ignores_a_group_that_is_already_gone(tmp_path, monkeypatch):
    real_killpg = os.killpg
    calls: list[int] = []

    def gone(pgid, sig):
        # Deliver the signal, then report the group missing, as a group that vanished
        # between the check and the kill would.
        calls.append(sig)
        real_killpg(pgid, sig)
        raise ProcessLookupError

    monkeypatch.setattr(bounded.os, "killpg", gone)
    r = bounded.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        cwd=str(tmp_path),
        timeout=0.5,
        grace=1,
    )
    assert r.timed_out and r.returncode is None
    assert calls[0] == signal.SIGTERM and signal.SIGKILL in calls


def test_run_bounded_sends_kill_even_when_the_leader_exits_on_term(tmp_path):
    pidfile = tmp_path / "member.pid"
    member = "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(600)"
    leader = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{member!r}],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(600)"
    )
    r = bounded.run_bounded(
        [sys.executable, "-c", leader], cwd=str(tmp_path), timeout=5, grace=1
    )
    assert r.timed_out
    time.sleep(0.2)
    assert _gone_or_zombie(int(pidfile.read_text()))


def test_run_bounded_returns_when_a_setsid_descendant_holds_stdout(tmp_path):
    pidfile = tmp_path / "escaper.pid"
    escaper = "import time; time.sleep(600)"
    script = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{escaper!r}],start_new_session=True);"
        f"open({str(pidfile)!r},'w').write(str(p.pid));"
        "print('partial', flush=True);time.sleep(600)"
    )
    try:
        r = bounded.run_bounded(
            [sys.executable, "-c", script], cwd=str(tmp_path), timeout=5, grace=1
        )
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass
    assert r.timed_out and r.returncode is None
    assert "partial" in r.stdout
    assert r.duration_s < 5 + 2 * 1 + 3


def test_run_bounded_propagates_a_missing_executable(tmp_path):
    with pytest.raises(FileNotFoundError):
        bounded.run_bounded(["/nonexistent/binary-xyz"], cwd=str(tmp_path), timeout=5)
