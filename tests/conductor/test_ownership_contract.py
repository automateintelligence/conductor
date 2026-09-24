"""The shared ownership contract, exercised end to end: a real driver, real records, real
processes.

WHAT IS BEING PROVED, AND WHY IT NEEDS REAL PROCESSES

An interactive worker registers ownership before product work; the cron driver consults that
same record and refuses to fire while it names something live; a record whose owner is provably
gone is recoverable; and ``.conductor/resume.lock`` keeps serializing DRIVER against DRIVER and
nothing else. None of that can be established against a mock. The unit that decides liveness is
the kernel — ``/proc/<pid>/stat`` field 22 on one path, ``/proc/locks`` on the other — and a
fake with a scripted answer would be asserting that the test's own dictionary lookup works.

So: every driver here is the rendered product template run under ``bash``. Every ownership
record is written by the shipped CLI. Every "owner" is a process this suite actually forked and
can kill. The only fakes are the host binary (a recorder, so a fire leaves evidence) and a
``conductor`` shim that routes ``run`` to THIS CHECKOUT'S OWN ``bin/conductor`` and answers the
done-gate probe non-green — the shim decides which binary answers, never what it answers.

THE ANTI-FOOLING ASSERTIONS ARE PART OF THE TESTS, NOT DECORATION

Every test here asserts that its own setup was in the state the conclusion depends on, at the
moment it depended on it. A liveness test that killed its owner and then asked whether the owner
was alive would pass on a broken implementation; a serialization test that let its first fire
finish before starting the second would prove nothing about the lock. Each such check is
labelled below with the wrong-for-the-right-reason failure it exists to exclude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conductor import resume_script as rs
from conductor import run_cmd
from conductor.core import ownership, runstate
from conductor.hosts import proc

ROOT = Path(__file__).resolve().parents[2]

#: The `conductor` the driver resolves. `run` goes to the REAL CLI so ownership is decided by
#: the product's own code over real state; everything else exits 1, which is the done-gate probe
#: answering not-green and is what lets a fire happen at all.
_SHIM = '#!/bin/sh\ncase "$1" in\n  run) exec {real} "$@" ;;\nesac\nexit 1\n'

#: A `conductor` that cannot answer ANY verb — the shape a partial install, an unimportable
#: package, or a CLI predating `run owner-busy` presents as.
_BROKEN_SHIM = "#!/bin/sh\nexit 1\n"

#: The fake host. It records that a fire happened, and when — enough to tell "did it fire" from
#: "did it fire twice" without the test having to parse the driver's own log for it.
_RECORDING_HOST = """#!/usr/bin/env python3
import os, sys, time
with open({fires!r}, "a", encoding="utf-8") as handle:
    handle.write("%s %s\\n" % (time.time(), " ".join(sys.argv[1:])))
sys.exit(0)
"""

#: A fake host that BLOCKS until the test releases it, so a second driver can be run while the
#: first fire is provably still in flight. `ready` appears when the fire has started; the fire
#: returns once `go` exists.
_BLOCKING_HOST = """#!/usr/bin/env python3
import os, sys, time
with open({fires!r}, "a", encoding="utf-8") as handle:
    handle.write("%s %s\\n" % (time.time(), " ".join(sys.argv[1:])))
open({ready!r}, "a").close()
deadline = time.time() + 90
while not os.path.exists({go!r}) and time.time() < deadline:
    time.sleep(0.02)
sys.exit(0)
"""


class Harness:
    def __init__(self, root: Path, home: Path, fires: Path, run_key: str) -> None:
        self.root = root
        self.home = home
        self.fires = fires
        self.run_key = run_key

    @property
    def state_root(self) -> str:
        return os.path.join(str(self.root), ".conductor")

    @property
    def driver(self) -> Path:
        return self.root / ".conductor" / "resume-autodev.sh"

    @property
    def log(self) -> str:
        path = self.root / ".conductor" / "resume-autodev.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @property
    def fire_count(self) -> int:
        if not self.fires.is_file():
            return 0
        return len([ln for ln in self.fires.read_text().splitlines() if ln.strip()])

    def fire(self, *, env=None, timeout=60):
        """Run the real generated driver, exactly as cron would."""
        return subprocess.run(
            ["bash", str(self.driver)],
            env={
                "HOME": str(self.home),
                "PATH": "/usr/bin:/bin",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                **(env or {}),
            },
            cwd=str(self.home),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def record(self, identity: str, *, host: str = "claude", tier: str = "in-session"):
        return ownership.claim(
            self.state_root,
            self.run_key,
            host=host,
            wrapper_identity=identity,
            tier=tier,
        )

    def claude_session_identity(self, monkeypatch) -> str:
        """Make this test process look like a live Claude session, and return the identity the
        adapter mints for it.

        ``CLAUDE_PID`` is set to this process's own pid, so the identity names a process that
        genuinely is alive for the duration of the test. Nothing is stubbed: the adapter still
        reads ``/proc`` for the start ticks and the boot id.
        """
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        from conductor.hosts import base as hostbase

        identity = hostbase.load("claude").session_identity(os.environ)
        assert identity is not None
        assert identity.startswith(f"claude:{os.getpid()}:")
        return identity

    def owner_doc(self) -> dict:
        path = ownership.record_path(self.state_root, self.run_key)
        return json.loads(Path(path).read_text(encoding="utf-8"))


def _harness(
    tmp_path, git_env, git, capsys, *, host_text: str, shim_text: str | None = None
) -> Harness:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "alpha.md").write_text("# alpha\n")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")

    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    fires = tmp_path / "fires.log"
    fires.write_text("")

    host = bindir / "claude"
    host.write_text(host_text)
    host.chmod(0o755)
    shim = bindir / "conductor"
    shim.write_text(shim_text or _SHIM.format(real=str(ROOT / "bin" / "conductor")))
    shim.chmod(0o755)

    assert run_cmd.main(["new", "docs/alpha.md", "--project", str(root)]) == 0
    run_key = capsys.readouterr().out.strip()
    assert run_key, "the run was not created, so nothing below is about ownership"

    (root / ".conductor").mkdir(exist_ok=True)
    driver = root / ".conductor" / "resume-autodev.sh"
    # project == worktree: the driver exports CONDUCTOR_HOME=$WORKTREE and the real CLI has to
    # resolve this run's state from it.
    driver.write_text(rs.render(str(root), str(root), host="claude"))
    driver.chmod(0o755)
    return Harness(root, home, fires, run_key)


@pytest.fixture
def harness(tmp_path, git_env, git, capsys, monkeypatch):
    monkeypatch.delenv(ownership.INHERITED_IDENTITY_ENV, raising=False)
    return _harness(
        tmp_path,
        git_env,
        git,
        capsys,
        host_text=_RECORDING_HOST.format(fires=str(tmp_path / "fires.log")),
    )


def _identity(pid: int) -> str:
    identity = proc.local_identity(pid)
    assert identity is not None, f"could not mint an identity for pid {pid}"
    return identity


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])


# --- GATE 1: an interactive owner prevents a driver launch ----------------------------------


def test_an_interactive_owner_prevents_a_driver_launch(harness):
    """The requirement, end to end: a worker registers, and the cron tick does not fire.

    The owner here is a real process that is still running when the driver is invoked, and its
    record is written by the shipped ``ownership.claim`` — not hand-assembled — so the identity
    format the driver consults is the one production writes.
    """
    owner = _sleeper()
    try:
        record = harness.record(_identity(owner.pid))
        assert harness.owner_doc()["wrapper_identity"] == record.wrapper_identity
        assert harness.owner_doc()["tier"] == "in-session"

        result = harness.fire()

        assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
        assert harness.fire_count == 0, (
            f"the driver fired underneath a live owner: {harness.fires.read_text()}"
        )
        assert "fire-skipped reason=owner-busy" in harness.log, harness.log
        assert "state=live" in harness.log, harness.log
        # It must be the OWNERSHIP record that stopped it, not one of the other two skips —
        # a test that accepted any skip would pass with this feature deleted, because a
        # green gate or a held lock also exits 0 with nothing fired.
        assert "reason=lock-held" not in harness.log, harness.log
        assert "reason=gate-green" not in harness.log, harness.log
        assert "fire-start" not in harness.log, harness.log
        # ANTI-FOOLING: the conclusion is "a LIVE owner blocks", so the owner has to have been
        # alive for the whole of the driver's run. If it had exited, gate 2 is what this would
        # be measuring and it would pass for the wrong reason.
        assert owner.poll() is None, (
            "the owner exited during the fire; gate 1 proved nothing"
        )
        assert ownership.identity_is_live(record) is True
    finally:
        owner.kill()
        owner.wait(timeout=30)


# --- GATE 2: a stale owner does not prevent it ----------------------------------------------


def test_a_stale_owner_does_not_prevent_a_driver_launch(harness):
    """Recoverable without a timer, and without a human.

    The record is written while the owner is alive and becomes stale by that process EXITING,
    which is the only thing that yields a run. Nothing here waits for a lease.
    """
    owner = _sleeper()
    identity = _identity(owner.pid)
    owner.kill()
    owner.wait(timeout=30)
    record = harness.record(identity)
    # ANTI-FOOLING: staleness must be a PROVEN exit, not an assumption about kill(). If this
    # were None ("cannot tell") the driver would correctly refuse and the test below would be
    # asserting the opposite of what it claims.
    assert ownership.identity_is_live(record) is False

    result = harness.fire()

    assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
    assert harness.fire_count == 1, f"a stale record blocked the driver: {harness.log}"
    assert "fire-start" in harness.log, harness.log
    assert "reason=owner-busy" not in harness.log, harness.log


def test_a_stale_record_is_clearable_and_a_live_one_is_not(harness):
    """The operator's half of recovery: a supported verb, not ``rm``.

    Both directions, because a ``disown`` that cleared everything would make gate 1 unenforceable
    from the command line while leaving it green in the driver.
    """
    live = _sleeper()
    try:
        harness.record(_identity(live.pid))
        assert (
            run_cmd.main(
                ["disown", "--run", harness.run_key, "--project", str(harness.root)]
            )
            == 1
        )
        assert ownership.read(harness.state_root, harness.run_key) is not None
        assert live.poll() is None, "the owner exited; the refusal proved nothing"
    finally:
        live.kill()
        live.wait(timeout=30)

    dead = _sleeper()
    identity = _identity(dead.pid)
    dead.kill()
    dead.wait(timeout=30)
    ownership._write(
        harness.state_root,
        harness.run_key,
        ownership.OwnerRecord(
            run_key=harness.run_key,
            host="claude",
            tier="in-session",
            wrapper_identity=identity,
            acquired_at="2026-08-10T12:00:00+00:00",
        ),
    )
    assert (
        run_cmd.main(
            ["disown", "--run", harness.run_key, "--project", str(harness.root)]
        )
        == 0
    )
    assert ownership.read(harness.state_root, harness.run_key) is None


# --- GATE 3: two real driver fires serialize through resume.lock ----------------------------


def test_two_real_driver_fires_serialize_through_resume_lock(
    tmp_path, git_env, git, capsys
):
    """``resume.lock`` still does its one job, and only its one job.

    The second driver is launched while the first fire is PROVABLY still executing — its host
    process has written its start marker and is blocked waiting for this test to release it — so
    the exclusion observed is between two concurrent drivers rather than between a driver and a
    finished one.

    It must also skip for the RIGHT reason. Neither driver registers ownership (only the
    heartbeat wrapper does), so a `reason=owner-busy` here would mean the new check had
    swallowed the case `resume.lock` owns, and the two mechanisms had been collapsed into one.
    """
    ready = tmp_path / "fire.ready"
    go = tmp_path / "fire.go"
    fires = tmp_path / "fires.log"
    harness = _harness(
        tmp_path,
        git_env,
        git,
        capsys,
        host_text=_BLOCKING_HOST.format(fires=str(fires), ready=str(ready), go=str(go)),
    )

    first = subprocess.Popen(
        ["bash", str(harness.driver)],
        env={"HOME": str(harness.home), "PATH": "/usr/bin:/bin"},
        cwd=str(harness.home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 60
        while not ready.exists() and time.time() < deadline:
            assert first.poll() is None, (
                "the first driver exited before its fire started; there was never anything "
                "for the second to be excluded by"
            )
            time.sleep(0.02)
        assert ready.exists(), "the first fire never started"
        assert first.poll() is None, "the first driver is gone; nothing holds the lock"

        second = harness.fire(timeout=60)

        # ANTI-FOOLING: the whole claim is "while the first is still running". Re-check AFTER
        # the second driver has finished, not only before it started.
        assert first.poll() is None, (
            "the first driver finished during the second's run; the exclusion may have been "
            "against a released lock"
        )
        assert second.returncode == 0, (second.stdout, second.stderr, harness.log)
        assert "fire-skipped reason=lock-held" in harness.log, harness.log
        assert "reason=owner-busy" not in harness.log, harness.log
        assert harness.fire_count == 1, (
            f"both drivers fired: {fires.read_text()}\n{harness.log}"
        )
    finally:
        go.touch()
        try:
            first.wait(timeout=60)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=30)

    assert first.returncode == 0
    # And the lock is a lock, not a one-shot: once the holder is gone the next tick fires.
    third = harness.fire(timeout=60)
    assert third.returncode == 0, (third.stdout, third.stderr, harness.log)
    assert harness.fire_count == 2, (
        f"the lock did not release: {fires.read_text()}\n{harness.log}"
    )


# --- GATE 4: neither host relies on process-name matching -----------------------------------
#
# Made falsifiable rather than asserted by reading the source. A source scan for `pgrep` proves
# only that one spelling is absent; these two tests fail if ANY name predicate — a `comm`, an
# `exe` basename, an argv substring, on either host — is reintroduced anywhere in the chain from
# the driver to the kernel, because they make the name and the identity disagree on purpose.


def test_an_owner_whose_process_is_not_named_after_its_host_still_blocks(
    harness, tmp_path
):
    """UNDER-MATCH, the fail-open direction. This is the shipped Claude session, reproduced.

    The Claude Code binary is named after its VERSION, so a real session's ``comm`` and ``exe``
    basename read ``2.1.227`` and never ``claude``. Any liveness check that asks "is this one of
    my host's processes" by name answers NO for a live human session, concludes the owner
    exited, and fires into an occupied worktree — silently.

    So the owner here is deliberately named ``2.1.227``: a real, live process, recorded as this
    run's ``claude`` owner, with nothing named ``claude`` about it. It must still block.
    """
    versioned = tmp_path / "hostbin" / "2.1.227"
    versioned.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys.executable, versioned)
    owner = subprocess.Popen([str(versioned), "-c", "import time; time.sleep(300)"])
    try:
        # ANTI-FOOLING, and the exact trap this suite has been caught by before — a fixture
        # NAMED after the host makes a name-matching guard match itself and the test passes on
        # the bug. Assert positively that no name in this picture is the host id.
        assert Path(f"/proc/{owner.pid}/comm").read_text().strip() == "2.1.227"
        assert os.path.basename(os.readlink(f"/proc/{owner.pid}/exe")) == "2.1.227"
        argv = Path(f"/proc/{owner.pid}/cmdline").read_bytes().decode().split("\0")
        assert not any("claude" in token for token in argv), argv

        record = harness.record(_identity(owner.pid))
        assert record.host == "claude"
        assert ownership.identity_is_live(record) is True

        result = harness.fire()

        assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
        assert harness.fire_count == 0, (
            "the driver fired into a checkout owned by a live session whose process is not "
            f"named after its host — a name predicate has come back: {harness.log}"
        )
        assert "fire-skipped reason=owner-busy" in harness.log, harness.log
        assert "state=live" in harness.log, harness.log
        assert owner.poll() is None, "the owner exited; this proved nothing"
    finally:
        owner.kill()
        owner.wait(timeout=30)


def test_a_process_named_after_the_host_is_not_mistaken_for_an_owner(harness, tmp_path):
    """OVER-MATCH, the direction the deleted guard failed in.

    ``pgrep -f 'claude'`` matched 22 processes on the machine this project is developed on,
    including Conductor's own shells, because every tool call's argv mentions ``~/.claude``. A
    guard like that blocks a run that nothing owns.

    Here a live process IS named ``claude``, runs with its cwd inside the checkout, and carries
    the checkout path in its argv — everything the old guard matched on — while no ownership
    record exists. The driver must fire.
    """
    named = tmp_path / "decoy" / "claude"
    named.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys.executable, named)
    decoy = subprocess.Popen(
        [str(named), "-c", f"import time; time.sleep(300)  # {harness.root}"],
        cwd=str(harness.root),
    )
    try:
        # ANTI-FOOLING: the decoy has to actually look like what the old guard matched, or the
        # test is a no-op dressed as a regression guard.
        assert Path(f"/proc/{decoy.pid}/comm").read_text().strip() == "claude"
        argv = Path(f"/proc/{decoy.pid}/cmdline").read_bytes().decode()
        assert "claude" in argv and str(harness.root) in argv
        assert ownership.read(harness.state_root, harness.run_key) is None

        result = harness.fire()

        assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
        assert harness.fire_count == 1, (
            "a live process merely NAMED after the host blocked a run nothing owns — the "
            f"deleted pgrep guard is back: {harness.log}"
        )
        assert "reason=owner-busy" not in harness.log, harness.log
        assert decoy.poll() is None, "the decoy exited; this proved nothing"
    finally:
        decoy.kill()
        decoy.wait(timeout=30)


# --- the record the driver consults is the one the worker registered ------------------------


def test_the_worker_and_the_driver_use_one_record_on_both_hosts(harness, monkeypatch):
    """One contract, two hosts. The identity STRING and the liveness PROCEDURE differ; the
    record, the verbs and the refusal do not.

    Codex's half is asserted against a thread id whose lock file does not exist, which is the
    ``cannot tell`` case — and ``cannot tell`` must read as OCCUPIED, never as free. That is the
    fail-safe direction for an undocumented artifact on a 0.x CLI: if a future Codex moves the
    lock directory, every record must stop clearing rather than start clearing.
    """
    unknown_thread = "codex:019feab3-05f0-7081-90fb-18b96bc27db3:not-this-boot"
    ownership._write(
        harness.state_root,
        harness.run_key,
        ownership.OwnerRecord(
            run_key=harness.run_key,
            host="codex",
            tier="in-session",
            wrapper_identity=unknown_thread,
            acquired_at="2026-08-10T12:00:00+00:00",
        ),
    )
    # A DIFFERENT boot id is a positive exit proof — nothing survives a reboot — so this one
    # clears. The point is that a codex identity is decided by the codex adapter.
    assert (
        run_cmd.main(
            ["owner-busy", "--run", harness.run_key, "--project", str(harness.root)]
        )
        == run_cmd.EXIT_OWNER_FREE
    )

    boot = proc.boot_id()
    assert boot is not None
    ownership._write(
        harness.state_root,
        harness.run_key,
        ownership.OwnerRecord(
            run_key=harness.run_key,
            host="codex",
            tier="in-session",
            wrapper_identity=f"codex:019feab3-05f0-7081-90fb-18b96bc27db3:{boot}",
            acquired_at="2026-08-10T12:00:00+00:00",
        ),
    )
    monkeypatch.setenv("CODEX_HOME", str(harness.root / "no-such-codex-home"))
    # Lock directory absent -> cannot tell -> OCCUPIED.
    assert (
        run_cmd.main(
            ["owner-busy", "--run", harness.run_key, "--project", str(harness.root)]
        )
        == run_cmd.EXIT_OK
    )

    result = harness.fire()
    assert result.returncode == 0
    assert harness.fire_count == 0, harness.log
    assert "fire-skipped reason=owner-busy" in harness.log, harness.log
    assert "state=unreadable" in harness.log, harness.log


def test_a_descendant_of_the_owning_wrapper_is_not_blocked_by_its_own_ancestor(harness):
    """The self-deadlock, pinned. ``conductor heartbeat`` takes ownership and then launches this
    driver; without the inherited-identity handoff the driver would skip every fire forever, on
    the record its own parent wrote, and the log would read like the contract working."""
    wrapper = _sleeper()
    try:
        record = harness.record(_identity(wrapper.pid), tier="wrapper")
        # Without the handoff: blocked.
        blocked = harness.fire()
        assert blocked.returncode == 0
        assert harness.fire_count == 0, harness.log
        assert "fire-skipped reason=owner-busy" in harness.log

        # With it: fires, and the record is untouched — the descendant does not take the run
        # from its ancestor, it just does not block on it.
        allowed = harness.fire(
            env={ownership.INHERITED_IDENTITY_ENV: record.wrapper_identity}
        )
        assert allowed.returncode == 0, (allowed.stdout, allowed.stderr, harness.log)
        assert harness.fire_count == 1, harness.log
        current = ownership.read(harness.state_root, harness.run_key)
        assert current is not None
        assert current.wrapper_identity == record.wrapper_identity
        assert wrapper.poll() is None, "the wrapper exited; this proved nothing"
    finally:
        wrapper.kill()
        wrapper.wait(timeout=30)


def test_a_forged_inherited_identity_does_not_unlock_someone_elses_record(harness):
    """The handoff is an exact match against the RECORDED owner, so a stale or guessed value
    grants nothing."""
    owner = _sleeper()
    try:
        harness.record(_identity(owner.pid))
        result = harness.fire(
            env={ownership.INHERITED_IDENTITY_ENV: "proc:1:1:some-other-boot"}
        )
        assert result.returncode == 0
        assert harness.fire_count == 0, harness.log
        assert "fire-skipped reason=owner-busy" in harness.log
        assert owner.poll() is None
    finally:
        owner.kill()
        owner.wait(timeout=30)


def test_a_run_with_no_record_at_all_fires(harness):
    """The baseline the three refusals are measured against. Without it every test above could
    pass on a driver that never fires."""
    assert ownership.read(harness.state_root, harness.run_key) is None
    result = harness.fire()
    assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
    assert harness.fire_count == 1, harness.log
    assert "fire-start" in harness.log, harness.log


def test_a_version_1_record_blocks_and_names_its_recovery(harness):
    """A bare-pid record cannot be read, so nothing can ever prove its owner exited. It must
    block — and the driver's log must be a FAULT, not evidence, because the run will skip every
    tick until a human clears it."""
    from conductor.core import atomic

    os.makedirs(runstate.run_dir(harness.state_root, harness.run_key), exist_ok=True)
    atomic.write_json_atomic(
        ownership.record_path(harness.state_root, harness.run_key),
        {
            "run_key": harness.run_key,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": str(os.getpid()),
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": 1,
        },
    )
    result = harness.fire()
    assert result.returncode == 0
    assert harness.fire_count == 0, harness.log
    assert "fire-skipped reason=owner-busy" in harness.log, harness.log
    assert "state=unreadable" in harness.log, harness.log

    from conductor import driver as driver_mod

    assert any(marker in harness.log for marker in driver_mod._FAILURE_MARKERS), (
        "a permanently blocked run must be visible to `conductor driver status`"
    )

    assert (
        run_cmd.main(
            [
                "disown",
                "--run",
                harness.run_key,
                "--project",
                str(harness.root),
                "--force",
            ]
        )
        == 0
    )
    assert ownership.read(harness.state_root, harness.run_key) is None
    assert harness.fire().returncode == 0
    assert harness.fire_count == 1, harness.log


# --- the check that cannot run at all -------------------------------------------------------
#
# A CLI too old to know `run owner-busy`, an install whose Python package will not import, a
# crash. Fail-safe would say "skip", and that was the first implementation — but it made an
# unrelated CLI fault stop a run that nothing was claiming, which is a new outage in exchange
# for no new safety. The driver instead asks the one question that needs no CLI: is there a
# record to consult at all. Both directions are pinned here, because a fallback that fired past
# a PRESENT record would silently undo gate 1.


def test_an_unanswerable_check_fires_when_no_record_exists_and_says_it_was_unprotected(
    tmp_path, git_env, git, capsys
):
    harness = _harness(
        tmp_path,
        git_env,
        git,
        capsys,
        host_text=_RECORDING_HOST.format(fires=str(tmp_path / "fires.log")),
        shim_text=_BROKEN_SHIM,
    )
    assert ownership.read(harness.state_root, harness.run_key) is None

    result = harness.fire()

    assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
    assert harness.fire_count == 1, (
        f"a broken CLI stopped a run nothing was claiming: {harness.log}"
    )
    assert "owner-check-unavailable" in harness.log, harness.log
    assert "no-record-on-disk" in harness.log, harness.log
    # It fired UNPROTECTED, so `conductor driver status` has to see it. The fire succeeded and
    # nothing else in the system would ever mention that the check is not working.
    from conductor import driver as driver_mod

    assert "owner-check-unavailable" in driver_mod._FAILURE_MARKERS


def test_an_unanswerable_check_still_refuses_to_fire_past_a_record_that_exists(
    tmp_path, git_env, git, capsys
):
    """The fallback's boundary. It may only fire past the ABSENCE of a record — never past one
    it merely failed to read, which is the case gate 1 depends on."""
    harness = _harness(
        tmp_path,
        git_env,
        git,
        capsys,
        host_text=_RECORDING_HOST.format(fires=str(tmp_path / "fires.log")),
        shim_text=_BROKEN_SHIM,
    )
    owner = _sleeper()
    try:
        harness.record(_identity(owner.pid))
        assert os.path.isfile(
            ownership.record_path(harness.state_root, harness.run_key)
        )

        result = harness.fire()

        assert result.returncode == 0, (result.stdout, result.stderr, harness.log)
        assert harness.fire_count == 0, (
            f"the fallback fired past an ownership record on disk: {harness.log}"
        )
        assert "owner-check-failed" in harness.log, harness.log
        assert "owner-check-unavailable" not in harness.log, harness.log
        assert owner.poll() is None, "the owner exited; this proved nothing"
    finally:
        owner.kill()
        owner.wait(timeout=30)


# --- releasing ownership one HOLDS ----------------------------------------------------------


def test_a_live_worker_can_release_its_own_ownership(harness, monkeypatch):
    """The bug that would have left a record behind after every single fire.

    A worker is still RUNNING when it finishes its phase and lets go, so it can never satisfy
    the exit proof ``disown`` demands of a stranger. The first version of the verb refused
    exactly that — every worker following the skill's own instruction would have been told to
    run ``--force``, and any that did not would have blocked the run until a human intervened.
    Ownership one holds is one's own to drop.
    """
    identity = harness.claude_session_identity(monkeypatch)
    harness.record(identity)
    assert (
        run_cmd.main(
            ["disown", "--run", harness.run_key, "--project", str(harness.root)]
        )
        == 0
    )
    assert ownership.read(harness.state_root, harness.run_key) is None
    # And the run is free again immediately — no waiting, no force.
    result = harness.fire()
    assert result.returncode == 0
    assert harness.fire_count == 1, harness.log


def test_a_descendant_does_not_release_its_ancestors_ownership(harness, monkeypatch):
    """A worker launched BY the record's owner must leave it alone: the wrapper is still
    supervising the fire. Dropping it here hands the run to the next cron tick mid-fire."""
    owner = _sleeper()
    try:
        record = harness.record(_identity(owner.pid), tier="wrapper")
        monkeypatch.setenv(ownership.INHERITED_IDENTITY_ENV, record.wrapper_identity)
        assert (
            run_cmd.main(
                ["disown", "--run", harness.run_key, "--project", str(harness.root)]
            )
            == 0
        )
        survivor = ownership.read(harness.state_root, harness.run_key)
        assert survivor is not None
        assert survivor.wrapper_identity == record.wrapper_identity
        assert owner.poll() is None
    finally:
        owner.kill()
        owner.wait(timeout=30)


def test_releasing_never_drops_someone_elses_record(harness, monkeypatch):
    """The boundary. "My own" is an exact identity match, so a session that does not hold the
    record cannot release it out from under the session that does."""
    owner = _sleeper()
    try:
        harness.record(_identity(owner.pid))
        # This session's identity is not the record's, so the release path must not engage and
        # the ordinary exit-proof refusal must apply.
        harness.claude_session_identity(monkeypatch)
        assert (
            run_cmd.main(
                ["disown", "--run", harness.run_key, "--project", str(harness.root)]
            )
            == 1
        )
        assert ownership.read(harness.state_root, harness.run_key) is not None
        assert owner.poll() is None
    finally:
        owner.kill()
        owner.wait(timeout=30)


# --- a killed wrapper does not free a run its driver is still firing -------------------------


def _orphaned_fire(harness) -> subprocess.Popen:
    """A process holding the project's fire lock EXACTLY as the generated driver does —
    ``exec 9>"$LOCK"; flock -n 9`` — and then staying alive, like a driver whose heartbeat
    wrapper was killed underneath it. Returns once the lock is provably held."""
    lock = os.path.join(harness.state_root, "resume.lock")
    ready = Path(harness.state_root) / "fire-lock-held"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 9>"$1"; flock -n 9 || exit 7; : > "$2"; sleep 300',
            "fire",
            lock,
            str(ready),
        ]
    )
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        assert holder.poll() is None, (
            "the fire-lock holder exited before holding the lock"
        )
        time.sleep(0.02)
    if not ready.exists():
        holder.kill()
        raise AssertionError("the fire-lock holder never took the lock")
    # Anti-fooling: the lock really is held, by a process that is not this test.
    probe = subprocess.run(["flock", "-n", lock, "true"], capture_output=True)
    assert probe.returncode != 0, "resume.lock is not held; nothing below is about it"
    return holder


def _dead_wrapper_record(harness) -> ownership.OwnerRecord:
    wrapper = _sleeper()
    identity = _identity(wrapper.pid)
    wrapper.kill()
    wrapper.wait(timeout=30)
    record = ownership.OwnerRecord(
        run_key=harness.run_key,
        host="claude",
        tier="wrapper",
        wrapper_identity=identity,
        acquired_at="2026-08-10T12:00:00+00:00",
    )
    ownership._write(harness.state_root, harness.run_key, record)
    assert ownership.identity_is_live(record) is False, (
        "the wrapper is not provably gone"
    )
    return record


def test_a_driver_that_outlives_its_killed_wrapper_keeps_the_run_owned(
    harness, monkeypatch, capsys
):
    """The recorded wrapper is provably dead, but the driver it launched is still firing and
    still holds ``resume.lock``. Registering, ``owner-busy`` and a fresh claim must all see an
    OCCUPIED run — and all see a free one the moment that fire is gone."""
    dead = _dead_wrapper_record(harness)
    fire = _orphaned_fire(harness)
    try:
        harness.claude_session_identity(monkeypatch)
        capsys.readouterr()
        assert (
            run_cmd.main(
                ["own", "--run", harness.run_key, "--project", str(harness.root)]
            )
            == run_cmd.EXIT_FAIL
        )
        err = capsys.readouterr().err
        assert "resume.lock" in err and "no write occurred" in err, err
        assert harness.owner_doc()["wrapper_identity"] == dead.wrapper_identity

        assert (
            run_cmd.main(
                ["owner-busy", "--run", harness.run_key, "--project", str(harness.root)]
            )
            == run_cmd.EXIT_OK
        )
        assert "state=live" in capsys.readouterr().out

        with pytest.raises(ownership.OwnerBusy):
            ownership.claim(harness.state_root, harness.run_key, host="claude")
        assert fire.poll() is None, "the fire exited; this proved nothing"
    finally:
        fire.kill()
        fire.wait(timeout=30)

    assert (
        run_cmd.main(["own", "--run", harness.run_key, "--project", str(harness.root)])
        == run_cmd.EXIT_OK
    )


def test_a_worker_launched_by_the_fire_holding_the_lock_can_still_register(harness):
    """A cron-launched driver holds ``resume.lock`` and its worker then runs ``conductor run
    own`` — over a record a crashed earlier fire left behind. That worker IS the fire; refusing
    it would stop the run forever. Its lock descriptor is closed (``9>&-``) so the decision rests
    on the holder being its ancestor, not on an inherited descriptor."""
    _dead_wrapper_record(harness)
    lock = os.path.join(harness.state_root, "resume.lock")
    script = (
        'exec 9>"$1"; flock -n 9 || exit 7; '
        'CLAUDE_PID=$$ "$2" run own --run "$3" --project "$4" 9>&-'
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "fire",
            lock,
            str(ROOT / "bin" / "conductor"),
            harness.run_key,
            str(harness.root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "CONDUCTOR_HOST": "claude",
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "owned by claude" in result.stdout, result.stdout


# --- only a DEFINITIVE "no repository" answers free ------------------------------------------


def test_owner_busy_outside_any_repository_is_free(tmp_path, capsys):
    """git answered, and its answer is that there is no repository: nothing can own a run here."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert (
        run_cmd.main(["owner-busy", "--project", str(plain)]) == run_cmd.EXIT_OWNER_FREE
    )
    assert "state=free reason=no-repository" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git", "rev-parse"], 30),
        subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: detected dubious ownership in repository at '/x'\n",
        ),
    ],
    ids=["timeout", "other-git-failure"],
)
def test_owner_busy_fails_closed_when_git_cannot_answer(
    tmp_path, monkeypatch, capsys, failure
):
    """A git that timed out or failed for any other reason has NOT said there is no repository.
    Reading that as free would fire a driver past a record nobody could consult."""
    from conductor.core import resolve

    def cannot_answer(start=None):
        raise failure

    monkeypatch.setattr(resolve, "repo_root", cannot_answer)
    rc = run_cmd.main(["owner-busy", "--project", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == run_cmd.EXIT_OK, out
    assert "state=unreadable" in out and "state=free" not in out, out
    assert "no write occurred" in out and str(tmp_path) in out, out
    assert "git -C" in out, out


def test_an_ancestor_merely_having_the_lock_file_open_does_not_make_a_foreign_fire_its_own(
    harness,
):
    """``flock`` belongs to an open file DESCRIPTION, not to whoever has the file open. A process
    in the caller's ancestry that opened ``resume.lock`` without locking it (a reader, a stale
    descriptor, a failed ``flock -n``) must not turn a foreign holder's fire into the caller's."""
    fire = _orphaned_fire(harness)
    lock = os.path.join(harness.state_root, "resume.lock")
    unlocked = os.open(lock, os.O_RDONLY)
    try:
        refusal = ownership.running_fire(harness.state_root)
        assert refusal is not None and "resume.lock" in refusal, refusal
        assert fire.poll() is None, "the fire exited; this proved nothing"
    finally:
        os.close(unlocked)
        fire.kill()
        fire.wait(timeout=30)


# --- no path clears a record while a foreign fire still holds resume.lock ---------------------


@pytest.mark.parametrize("force", [False, True], ids=["exit-proof", "force"])
def test_disown_leaves_the_record_while_a_foreign_fire_holds_the_lock(harness, force):
    """The recorded wrapper is provably gone, but its fire is not. Clearing the record would
    hand the run to the next claimant under a running fire, so neither the exit proof nor
    ``--force`` clears it; stopping the fire is the documented way out."""
    dead = _dead_wrapper_record(harness)
    fire = _orphaned_fire(harness)
    try:
        outcome, detail = ownership.disown(
            harness.state_root, harness.run_key, force=force
        )
        assert outcome == "refused", detail
        assert "resume.lock" in detail and "nothing was removed" in detail, detail
        assert harness.owner_doc()["wrapper_identity"] == dead.wrapper_identity
        assert fire.poll() is None, "the fire exited; this proved nothing"
    finally:
        fire.kill()
        fire.wait(timeout=30)
    outcome, detail = ownership.disown(harness.state_root, harness.run_key, force=force)
    assert outcome == "cleared", detail


def test_release_leaves_the_record_while_a_foreign_fire_holds_the_lock(harness):
    """Even the record's own identity does not drop it under a fire it cannot account for."""
    owner = _sleeper()
    try:
        record = harness.record(_identity(owner.pid), tier="wrapper")
        fire = _orphaned_fire(harness)
        try:
            refusal = ownership.release(
                harness.state_root,
                harness.run_key,
                wrapper_identity=record.wrapper_identity,
            )
            assert refusal is not None and "resume.lock" in refusal, refusal
            assert harness.owner_doc()["wrapper_identity"] == record.wrapper_identity
        finally:
            fire.kill()
            fire.wait(timeout=30)
        assert (
            ownership.release(
                harness.state_root,
                harness.run_key,
                wrapper_identity=record.wrapper_identity,
            )
            is None
        )
        assert ownership.read(harness.state_root, harness.run_key) is None
    finally:
        owner.kill()
        owner.wait(timeout=30)


def test_run_disown_of_ones_own_record_refuses_under_a_foreign_fire(
    harness, monkeypatch, capsys
):
    identity = harness.claude_session_identity(monkeypatch)
    harness.record(identity)
    fire = _orphaned_fire(harness)
    try:
        capsys.readouterr()
        rc = run_cmd.main(
            ["disown", "--run", harness.run_key, "--project", str(harness.root)]
        )
        err = capsys.readouterr().err
        assert rc == run_cmd.EXIT_FAIL, err
        assert "resume.lock" in err, err
        assert harness.owner_doc()["wrapper_identity"] == identity
    finally:
        fire.kill()
        fire.wait(timeout=30)
