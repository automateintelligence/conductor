"""Behavioral tests that ACTUALLY EXECUTE the rendered Tier-B driver.

The double-drive guard shipped green on a single substring assertion
(`assert "/proc/$pid/cwd" in s`) against generated text that was never run as bash. It was
broken in every deployment conductor actually lives in: the driver `cd`s to the worktree
BEFORE the loop, and `pgrep -f 'claude'` matches the driver's OWN pid whenever the project
path contains the substring `claude` — true for anything under `~/.claude/`. Every fire then
took the `exit 0` branch, silently, forever.

These tests render the driver into a real temp project, spawn real decoy processes with
controlled argv and cwd, and assert on exit codes and `$LOG` contents. No real `claude` is
invoked and no network or model call happens: fake `claude`/`conductor` bins are planted in
`$HOME/.local/bin`, which the driver's own PATH repair puts FIRST, so resolution is
deterministic regardless of what is installed on the host.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conductor import resume_script as rs

# The repo root — the child pytest run in the orphan check needs it on PYTHONPATH and as cwd.
ROOT = str(Path(__file__).resolve().parents[2])

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="driver execution needs bash"
)

# Only for tests that exercise REAL kernel locking. It used to skip the whole module, which
# meant the one host class where the driver's flock handling actually matters — a machine
# without util-linux — ran no coverage of it at all. The stub-flock tests below deliberately
# carry no such marker: they must run everywhere, including there.
_needs_real_flock = pytest.mark.skipif(
    shutil.which("flock") is None, reason="exercises real kernel locking"
)

# Bounded waits: every wait is on a FILE MARKER, never a fixed sleep, so the outcome is
# deterministic (the marker appears, or the test fails loudly at the deadline).
_DEADLINE = 20.0
_POLL = 0.02

# The fake worker writes one line per invocation here; the driver's own stdout goes to $LOG.
#
# FAKE_WORKER_LEAK models what an autodev phase legitimately does: leave a DETACHED
# descendant running (a dev server, a docker helper, anything nohup'd) after the phase
# itself returns. Every non-close-on-exec descriptor the driver held is inherited straight
# through `worker -> setsid -> grandchild`, so this is the shape that can strand the lock.
_FAKE_CLAUDE = """#!/usr/bin/env bash
printf 'WORKER-FIRED cwd=%s args=%s\\n' "$PWD" "$*" >> "$FAKE_CALLS"
if [ -n "${FAKE_WORKER_LEAK:-}" ]; then
    setsid bash -c 'echo $$ > "$FAKE_WORKER_LEAK.pid"
        while [ ! -f "$FAKE_WORKER_LEAK" ]; do sleep 0.05; done' </dev/null >/dev/null 2>&1 &
fi
if [ -n "${FAKE_WORKER_HOLD:-}" ]; then
    touch "$FAKE_WORKER_STARTED"
    while [ ! -f "$FAKE_WORKER_HOLD" ]; do sleep 0.02; done
fi
exit "${FAKE_WORKER_RC:-0}"
"""

# `conductor assert run --level spec` — exit 0 means the done-gate is GREEN (nothing to do).
# Default 1 (gate red) so a fire proceeds.
_FAKE_CONDUCTOR = """#!/usr/bin/env bash
exit "${FAKE_GATE_RC:-1}"
"""


def _killpg(pid: int) -> None:
    """SIGKILL a whole process GROUP. Everything this rig spawns spawns children of its own —
    the fire runs the worker, the worker can leave descendants — and signalling the tracked
    pid alone left those alive past the end of pytest. Every spawn here is therefore its own
    group leader (`start_new_session`) so the group id is the pid we already hold."""
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


def _wait_for(path: Path, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(_POLL)
    raise AssertionError(f"timed out waiting for {what}: {path}")


def _wait_for_cwd(pid: int, want: Path) -> None:
    """Wait until a spawned decoy's kernel-visible cwd is the directory we want it to hold —
    that, not the fact that Popen returned, is what the guard would have read."""
    deadline = time.monotonic() + _DEADLINE
    link = Path(f"/proc/{pid}/cwd")
    while time.monotonic() < deadline:
        try:
            if link.resolve() == want.resolve():
                return
        except OSError:
            pass
        time.sleep(_POLL)
    raise AssertionError(f"decoy pid {pid} never held cwd {want}")


@dataclass
class Rig:
    """One rendered driver installed in a real temp project, with fake bins on PATH."""

    project: Path
    worktree: Path
    script: Path
    log: Path
    calls: Path
    home: Path
    spawned: list[subprocess.Popen] = field(default_factory=list)
    adopted: list[int] = field(default_factory=list)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "FAKE_CALLS": str(self.calls),
            **extra,
        }

    def run(
        self, timeout: float = _DEADLINE, **extra: str
    ) -> subprocess.CompletedProcess:
        """A fire run to completion. NOT `subprocess.run(timeout=...)`: on timeout that kills
        only the direct child and returns, leaving the worker the fire spawned running."""
        proc = self.start(_stream=subprocess.PIPE, **extra)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode or 0, out, err)

    def start(
        self, _stream: int = subprocess.DEVNULL, **extra: str
    ) -> subprocess.Popen:
        proc = subprocess.Popen(
            [str(self.script)],
            env=self.env(**extra),
            stdout=_stream,
            stderr=_stream,
            text=True,
            # Own process group, so teardown can reach the fire AND its worker.
            start_new_session=True,
        )
        self.spawned.append(proc)
        return proc

    def decoy(self, cwd: Path, tag: str) -> subprocess.Popen:
        """A real process whose argv contains `claude` and whose cwd is `cwd` — the shape of
        an agent tool shell (`zsh -c source $HOME/.claude/shell-snapshots/...`) or a live
        interactive session. It must NOT be able to block a fire."""
        ready = self.project / f"decoy-{tag}.ready"
        hold = self.project / f"decoy-{tag}.hold"
        proc = subprocess.Popen(
            [
                "bash",
                "-c",
                f"# {tag} claude tool shell\ntouch {ready!s}\n"
                f"while [ ! -f {hold!s} ]; do sleep 0.02; done",
            ],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.spawned.append(proc)
        _wait_for(ready, f"decoy {tag} start")
        _wait_for_cwd(proc.pid, cwd)
        return proc

    def plant(self, name: str, body: str) -> Path:
        """Shadow a binary the driver resolves through its own PATH repair, which puts
        `$HOME/.local/bin` FIRST. Used to give `flock` a chosen exit status without needing
        `flock` to be absent from the machine running the tests."""
        stub = self.home / ".local" / "bin" / name
        stub.write_text(body)
        stub.chmod(0o755)
        return stub

    def adopt_pidfile(self, pidfile: Path, what: str) -> int:
        """Register a descendant the test spawned INDIRECTLY (through the fake worker) so
        teardown can reach it. It is not a tracked Popen — nothing else would ever reap it."""
        _wait_for(pidfile, what)
        pid = int(pidfile.read_text().strip())
        self.adopted.append(pid)
        return pid

    def release(self, marker: Path) -> None:
        marker.write_text("go\n")

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def worker_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text().splitlines() if ln.strip()]

    def cleanup(self) -> None:
        """Tear down by GROUP, and reach everything — including a rig whose test failed before
        writing its release marker, which is the case that used to leak."""
        for pid in self.adopted:
            _killpg(pid)
        for proc in self.spawned:
            _killpg(proc.pid)
            proc.kill()  # the leader itself, in case the group was already gone
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)


@pytest.fixture
def make_rig(tmp_path):
    rigs: list[Rig] = []

    def build(dirname: str) -> Rig:
        home = tmp_path / dirname
        # The `claude` substring must be present ONLY where a test intends it: it is the
        # vector under test. pytest derives tmp_path from the test NAME, so assert rather
        # than trust.
        wants_claude = "claude" in dirname
        assert ("claude" in str(home).lower()) == wants_claude, (
            f"tmp_path {home} contaminates the claude-substring vector"
        )
        project = home / "proj"
        worktree = home / "proj-run-x"
        bindir = home / ".local" / "bin"
        for d in (project / ".conductor", worktree, bindir):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in (("claude", _FAKE_CLAUDE), ("conductor", _FAKE_CONDUCTOR)):
            fake = bindir / name
            fake.write_text(body)
            fake.chmod(0o755)
        script = project / ".conductor" / "resume-autodev.sh"
        script.write_text(rs.render(str(project), str(worktree)))
        script.chmod(0o755)
        rig = Rig(
            project=project,
            worktree=worktree,
            script=script,
            log=project / ".conductor" / "resume-autodev.log",
            calls=home / "worker-calls.txt",
            home=home,
        )
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.cleanup()


# ---- 1. the self-PID vector: the driver must not block on its own process ----


@_needs_real_flock
def test_driver_fires_under_a_dot_claude_path_despite_matching_its_own_argv(make_rig):
    """conductor itself lives at `~/.claude/conductor`, so the driver's own command line
    carries the substring `claude` and its own cwd is the worktree it just cd'd into. The
    old guard read that as "someone else is already driving" and exited 0, silently,
    100% of fires, permanently."""
    rig = make_rig(".claude-host")
    proc = rig.run()

    assert proc.returncode == 0, (proc.stdout, proc.stderr, rig.log_text())
    assert "fire-start" in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 1, rig.worker_calls()


# ---- 2. the path-independent vector: a decoy tool shell holding the project cwd ----


@_needs_real_flock
def test_a_decoy_tool_shell_in_the_project_cwd_does_not_block_a_fire(make_rig):
    """Agent tool shells (`zsh -c source .../shell-snapshots/...`) match `pgrep -f claude`
    regardless of the project path. One of them sitting in the project directory must not
    be able to stop the run's driver."""
    rig = make_rig("plain-host")
    rig.decoy(rig.project, "in-project")

    proc = rig.run()

    assert proc.returncode == 0, (proc.stdout, proc.stderr, rig.log_text())
    assert "fire-start" in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 1, rig.worker_calls()


@_needs_real_flock
def test_a_decoy_tool_shell_in_the_worktree_cwd_does_not_block_a_fire(make_rig):
    """Same vector from the other matched directory — the worktree the driver resumes in."""
    rig = make_rig("plain-host-wt")
    rig.decoy(rig.worktree, "in-worktree")

    proc = rig.run()

    assert proc.returncode == 0, (proc.stdout, proc.stderr, rig.log_text())
    assert "fire-start" in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 1, rig.worker_calls()


# ---- 3. the flock: OS-driver vs OS-driver only, and contention is EVIDENCE ----


@_needs_real_flock
def test_two_driver_invocations_serialize_on_the_lock(make_rig):
    """With the pgrep heuristic gone, `flock -n 9` on `<project>/.conductor/resume.lock` is
    the only thing keeping two OS-DRIVER fires off one run branch (it excludes nothing else —
    the in-session CronCreate tier never opens that file). A blocked fire must leave a
    greppable reason in the log — a bare `exit 0` makes a permanently blocked run
    indistinguishable from a healthy no-op."""
    rig = make_rig("plain-host-lock")
    started = rig.project / "worker.started"
    hold = rig.project / "worker.hold"
    env = {"FAKE_WORKER_HOLD": str(hold), "FAKE_WORKER_STARTED": str(started)}

    first = rig.start(**env)
    # The release belongs in `finally`: an assertion below fires while the worker is still
    # blocked on the marker, and a held fire must not depend on the test passing to be let go.
    try:
        _wait_for(started, "first fire to enter the worker holding the lock")

        second = rig.run(**env)

        assert second.returncode == 0, (second.stdout, second.stderr)
        assert "skip reason=lock-held" in rig.log_text(), rig.log_text()
        assert len(rig.worker_calls()) == 1, rig.worker_calls()
    finally:
        rig.release(hold)
    assert first.wait(timeout=_DEADLINE) == 0


@_needs_real_flock
@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
def test_a_detached_descendant_of_a_finished_fire_does_not_hold_the_lock(make_rig):
    """The lock descriptor must NOT be inheritable by the worker.

    `exec 9>lock` opens a descriptor that is not close-on-exec, so the worker and every
    process it spawns get it. A phase that leaves ANY detached descendant running (dev
    server, docker helper, stray nohup) therefore keeps the locked open-file-description
    alive after the driver logs `fire-end rc=0` and exits — and the kernel only releases a
    flock when the LAST descriptor on that description closes. Every later fire then skips
    `lock-held` forever, with a completed, successful-looking fire in the log."""
    rig = make_rig("plain-host-leak")
    leak = rig.project / "leaked-descendant"

    first = rig.run(FAKE_WORKER_LEAK=str(leak))
    assert first.returncode == 0, (first.stdout, first.stderr, rig.log_text())
    rig.adopt_pidfile(Path(f"{leak}.pid"), "the phase's detached descendant")
    assert "fire-end rc=0" in rig.log_text(), rig.log_text()

    second = rig.run()

    assert second.returncode == 0, (second.stdout, second.stderr, rig.log_text())
    assert "skip reason=lock-held" not in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 2, rig.worker_calls()


# ---- 3b. contention is ONE reason flock fails; broken locking is not contention ----
#
# `if ! flock -n 9` treated every non-zero status as "someone else holds it": a missing
# `flock` binary (bash 127), a bad descriptor, a filesystem with no locking, a usage error.
# All logged `skip reason=lock-held` and exited 0, so a machine that CANNOT lock stalled
# forever behind the most reassuring line in the taxonomy.

# A contended lock, reported ONLY through the documented channel: util-linux exits with the
# `-E` status on conflict under -n. If the caller never asked for a distinguishable conflict
# code, this stub reports the lock FREE — so a driver that cannot tell contention from
# breakage cannot satisfy the test by treating any non-zero status as busy.
_FLOCK_CONFLICTS = """#!/usr/bin/env bash
code=""
while [ $# -gt 0 ]; do
    case "$1" in -E) shift; code="$1" ;; esac
    shift
done
exit "${code:-0}"
"""


def _flock_exits(rc: int) -> str:
    return f"#!/usr/bin/env bash\nexit {rc}\n"


@_needs_real_flock
def test_an_unopenable_lock_file_is_not_reported_as_contention(make_rig):
    """No stub anywhere: the lock PATH is a directory, so `exec 9>` fails (bash reports it
    and carries on with fd 9 closed) and flock then fails on a bad descriptor. Locking is
    broken, not busy, and it will stay broken on every future fire."""
    rig = make_rig("plain-host-unopenable")
    (rig.project / ".conductor" / "resume.lock").mkdir()

    proc = rig.run()

    log = rig.log_text()
    assert "skip reason=lock-held" not in log, log
    assert "lock-unavailable" in log, log
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert rig.worker_calls() == [], rig.worker_calls()


def test_a_flock_binary_that_cannot_run_fails_loud_instead_of_skipping(make_rig):
    """127 is what bash reports when `flock` is not on PATH at all — a machine without
    util-linux. Simulated with a stub so the test does not depend on flock being absent
    from the host, which is also the one condition under which it could never be tested."""
    rig = make_rig("plain-host-noflock")
    rig.plant("flock", _flock_exits(127))

    proc = rig.run()

    log = rig.log_text()
    assert "skip reason=lock-held" not in log, log
    assert "lock-unavailable" in log, log
    assert "127" in log, log  # the status is NAMED, so the cause is diagnosable
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert rig.worker_calls() == [], rig.worker_calls()


def test_a_flock_usage_or_os_error_fails_loud_instead_of_skipping(make_rig):
    """flock's own errors use sysexits (64 usage, 71 OS error, ...) — a filesystem that
    cannot lock, a build too old for `-E`, an internal failure. None of them is contention."""
    rig = make_rig("plain-host-flockerr")
    rig.plant("flock", _flock_exits(64))

    proc = rig.run()

    assert "skip reason=lock-held" not in rig.log_text(), rig.log_text()
    assert "lock-unavailable" in rig.log_text(), rig.log_text()
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)


def test_only_the_documented_conflict_status_reports_lock_held(make_rig):
    """The other side of the discrimination: a fire that skips must have skipped because
    flock reported ITS DOCUMENTED conflict status, not merely because it exited non-zero."""
    rig = make_rig("plain-host-conflict")
    rig.plant("flock", _FLOCK_CONFLICTS)

    proc = rig.run()

    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "skip reason=lock-held" in rig.log_text(), rig.log_text()
    assert "lock-unavailable" not in rig.log_text(), rig.log_text()
    assert rig.worker_calls() == [], rig.worker_calls()


# ---- 4. a real fire reaches the worker and is distinguishable in the log ----


@_needs_real_flock
def test_a_fire_reaches_the_worker_and_logs_a_distinguishable_outcome(make_rig):
    """End-to-end through the driver's own bin resolution: the worker runs in the RUN
    WORKTREE with the autodev prompt, and the log carries a bracketed fire whose rc is the
    worker's — never a skip."""
    rig = make_rig(".claude-host-fire")

    proc = rig.run(FAKE_WORKER_RC="7")

    assert proc.returncode == 7, (proc.stdout, proc.stderr, rig.log_text())
    calls = rig.worker_calls()
    assert len(calls) == 1, calls
    assert f"cwd={rig.worktree}" in calls[0], calls[0]
    assert "-p /conductor:autodev" in calls[0], calls[0]
    log = rig.log_text()
    assert "fire-start posture=supervised" in log, log
    assert "fire-end rc=7" in log, log
    assert "skip reason=" not in log, log


# ---- 6. the rig's own teardown: a FAILING run must not leak processes ----
#
# Every test above spawns real bash that blocks on a marker file. `cleanup` SIGKILLed the
# tracked parent pids only, so the fire's own child — the fake worker in its wait loop — was
# never signalled and outlived pytest. It only shows up when a test fails BEFORE its release
# marker is written, which is precisely when the suite is least able to tell you about it. So
# the check runs the failure for real, in a child pytest, and looks at /proc afterwards.

_ORPHAN_CASE = """
import pytest
from tests.conductor.test_resume_driver_exec import Rig, make_rig, _wait_for  # noqa: F401


def test_fails_while_a_fire_holds_the_lock(make_rig):
    rig = make_rig("plain-host-orphan")
    started = rig.project / "worker.started"
    hold = rig.project / "worker.hold"
    rig.start(FAKE_WORKER_HOLD=str(hold), FAKE_WORKER_STARTED=str(started))
    _wait_for(started, "the fire to reach the worker")
    # The failure a real assertion would raise here — the release marker is never written.
    assert False, "deliberate failure with a fire in flight"
"""


def _procs_under(needle: str) -> list[str]:
    """Live processes whose argv mentions `needle` — the child run's basetemp, so nothing
    belonging to this pytest or the developer's machine can match."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if needle in argv:
            found.append(f"{entry.name}: {argv.strip()}")
    return found


@_needs_real_flock
def test_a_failing_test_leaves_no_process_behind(tmp_path):
    """Teardown has to reap the whole process GROUP. Killing the tracked pid leaves the fake
    worker — a grandchild blocked on a marker that a failed test never writes — running after
    pytest returns, which is how a stray `claude` was observed surviving the suite."""
    basetemp = tmp_path / "child-basetemp"
    case = tmp_path / "test_orphan_case.py"
    case.write_text(_ORPHAN_CASE)

    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(case),
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={basetemp}",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode != 0, "the child run was supposed to FAIL"
    assert "deliberate failure" in child.stdout, child.stdout

    try:
        # A short grace period only: teardown already ran before the child exited, so
        # anything still alive here is genuinely orphaned, not merely slow to die.
        deadline = time.monotonic() + 5.0
        survivors = _procs_under(str(basetemp))
        while survivors and time.monotonic() < deadline:
            time.sleep(_POLL)
            survivors = _procs_under(str(basetemp))
        assert not survivors, "orphans survived a failing run:\n" + "\n".join(survivors)
    finally:
        for line in _procs_under(str(basetemp)):
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(line.split(":", 1)[0]), signal.SIGKILL)


# ---- 5. the other silent `exit 0`: a green done-gate ----


@_needs_real_flock
def test_a_green_done_gate_skip_is_logged(make_rig):
    """`conductor assert run --level spec` going green is a legitimate no-op, but it must
    still be evidence: same taxonomy, different reason, and no worker fired."""
    rig = make_rig("plain-host-gate")

    proc = rig.run(FAKE_GATE_RC="0")

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "skip reason=gate-green" in rig.log_text(), rig.log_text()
    assert rig.worker_calls() == []
