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

import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conductor import resume_script as rs

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("bash") is None,
    reason="driver execution needs bash + flock",
)

# Bounded waits: every wait is on a FILE MARKER, never a fixed sleep, so the outcome is
# deterministic (the marker appears, or the test fails loudly at the deadline).
_DEADLINE = 20.0
_POLL = 0.02

# The fake worker writes one line per invocation here; the driver's own stdout goes to $LOG.
_FAKE_CLAUDE = """#!/usr/bin/env bash
printf 'WORKER-FIRED cwd=%s args=%s\\n' "$PWD" "$*" >> "$FAKE_CALLS"
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
        return subprocess.run(
            [str(self.script)],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start(self, **extra: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [str(self.script)],
            env=self.env(**extra),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        )
        self.spawned.append(proc)
        _wait_for(ready, f"decoy {tag} start")
        _wait_for_cwd(proc.pid, cwd)
        return proc

    def release(self, marker: Path) -> None:
        marker.write_text("go\n")

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def worker_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text().splitlines() if ln.strip()]

    def cleanup(self) -> None:
        for proc in self.spawned:
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass


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


def test_a_decoy_tool_shell_in_the_worktree_cwd_does_not_block_a_fire(make_rig):
    """Same vector from the other matched directory — the worktree the driver resumes in."""
    rig = make_rig("plain-host-wt")
    rig.decoy(rig.worktree, "in-worktree")

    proc = rig.run()

    assert proc.returncode == 0, (proc.stdout, proc.stderr, rig.log_text())
    assert "fire-start" in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 1, rig.worker_calls()


# ---- 3. flock is the SOLE fire-vs-fire exclusion, and contention is EVIDENCE ----


def test_two_driver_invocations_serialize_on_the_lock(make_rig):
    """With the pgrep heuristic gone, `flock -n 9` on `<project>/.conductor/resume.lock` is
    the only thing keeping two fires off one run branch. A blocked fire must leave a
    greppable reason in the log — a bare `exit 0` makes a permanently blocked run
    indistinguishable from a healthy no-op."""
    rig = make_rig("plain-host-lock")
    started = rig.project / "worker.started"
    hold = rig.project / "worker.hold"
    env = {"FAKE_WORKER_HOLD": str(hold), "FAKE_WORKER_STARTED": str(started)}

    first = rig.start(**env)
    _wait_for(started, "first fire to enter the worker holding the lock")

    second = rig.run(**env)

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "skip reason=lock-held" in rig.log_text(), rig.log_text()
    assert len(rig.worker_calls()) == 1, rig.worker_calls()

    rig.release(hold)
    assert first.wait(timeout=_DEADLINE) == 0


# ---- 4. a real fire reaches the worker and is distinguishable in the log ----


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


# ---- 5. the other silent `exit 0`: a green done-gate ----


def test_a_green_done_gate_skip_is_logged(make_rig):
    """`conductor assert run --level spec` going green is a legitimate no-op, but it must
    still be evidence: same taxonomy, different reason, and no worker fired."""
    rig = make_rig("plain-host-gate")

    proc = rig.run(FAKE_GATE_RC="0")

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "skip reason=gate-green" in rig.log_text(), rig.log_text()
    assert rig.worker_calls() == []
