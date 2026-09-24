"""``conductor doctor relocation`` — the read-only scan that refuses to call a checkout movable.

Every fixture is a self-contained temporary repository. Nothing here reads the developer's real
crontab: a stub ``crontab`` is put on ``PATH`` for every test that touches the schedule predicate,
and it refuses writes outright. Nor the developer's real harness scheduled tasks: an autouse
fixture repoints ``$CLAUDE_CONFIG_DIR`` at a temporary directory for the whole module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from conductor import doctor
from conductor.core import atomic, ownership, runstate
from conductor.hosts import proc

RUN = "alpha-0123456789ab"

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_harness_config(tmp_path, monkeypatch):
    """The scan reads every host's scheduled-task file. Point the one that has a file at a
    temporary directory so no test can be answered by — or made flaky by — the developer's own."""
    config = tmp_path / "harness-config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture
def scheduled_tasks(isolated_harness_config):
    """Write the harness scheduled-task file the scan will read."""

    def _write(payload) -> pathlib.Path:
        path = isolated_harness_config / "scheduled_tasks.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
        return path

    return _write


@contextlib.contextmanager
def _flock_held(path: pathlib.Path):
    """Hold a real ``flock`` on ``path`` from ANOTHER process, the way the generated driver does.

    Another process, not this one: the scan must observe contention it did not create, and a lock
    taken in-process would be indistinguishable from the test's own file descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n",
            str(path),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if holder.stdout.readline().strip() == b"held":
                break
        else:  # pragma: no cover — the holder is a three-line script
            raise AssertionError("the fixture flock holder never took the lock")
        yield holder
    finally:
        holder.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            holder.wait(timeout=30)


@pytest.fixture
def checkout(tmp_path, git_env, git):
    """A committed repository with no remote, no worktrees, and no run state."""
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "trunk", str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    (root / "seed.md").write_text("# seed\n")
    git(root, "add", "seed.md")
    git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def stub_crontab(tmp_path, monkeypatch):
    """Install a ``crontab`` answering exactly the given lines. The real one is never touched."""

    def _install(lines):
        bindir = tmp_path / "stubbin"
        bindir.mkdir(exist_ok=True)
        body = "".join(f"echo {line!r}\n" for line in lines)
        script = bindir / "crontab"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then\n'
            + (body if lines else 'echo "no crontab for stub" >&2\nexit 1\n')
            + "exit 0\nfi\n"
            'echo "the stub crontab refuses writes" >&2\nexit 1\n'
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    return _install


def _manifest(root: pathlib.Path) -> dict[str, str]:
    """Path -> digest for every regular file, so a scan that writes anything is visible."""
    manifest = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            manifest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return manifest


def _record(root: pathlib.Path, identity: str) -> None:
    state_root = str(root / ".conductor")
    os.makedirs(runstate.run_dir(state_root, RUN), exist_ok=True)
    atomic.write_json_atomic(
        ownership.record_path(state_root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": identity,
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": ownership.RECORD_SCHEMA_VERSION,
        },
    )


def _identity(pid: int) -> str:
    """A real, checkable identity for ``pid`` — never a bare pid string.

    Minted while the process is still alive, because ``starttime`` has to be READ from
    ``/proc`` before it goes away. A test that wrote ``str(pid)`` would be asserting against
    an identity scheme this build refuses, and would pass or fail for reasons unrelated to
    the scan it is exercising.
    """
    identity = proc.local_identity(pid)
    assert identity is not None, f"could not mint an identity for pid {pid}"
    return identity


def _names(predicates, kind=None):
    return {
        predicate.name
        for predicate in predicates
        if predicate.blocked and (kind is None or predicate.kind == kind)
    }


# --- the three quiesce blockers, each on its own ------------------------------------------


def test_a_linked_worktree_blocks_and_nothing_else_does(checkout, git, stub_crontab):
    stub_crontab([])
    nested = checkout / ".worktrees" / "phase-1"
    git(checkout, "worktree", "add", "-q", "-b", "phase-1", str(nested))
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.QUIESCE) == {"linked-worktree"}
    finding = next(p for p in predicates if p.name == "linked-worktree").findings[0]
    assert finding.artifact == str(nested)
    assert "worktree remove" in finding.recovery


def test_an_installed_schedule_blocks_and_names_the_launcher(checkout, stub_crontab):
    launcher = str(checkout / ".conductor" / "resume-autodev.sh")
    stub_crontab([f"*/20 * * * * {launcher} # conductor-autodev {checkout}"])
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.QUIESCE) == {"installed-schedule"}
    finding = next(p for p in predicates if p.name == "installed-schedule").findings[0]
    # The launcher, not the bare checkout the marker comment repeats.
    assert finding.artifact == launcher


def test_a_crontab_naming_another_project_does_not_block(checkout, stub_crontab):
    stub_crontab(["*/20 * * * * /somewhere/else/.conductor/resume-autodev.sh"])
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_crontab_that_cannot_be_read_blocks_rather_than_reading_as_empty(
    checkout, tmp_path, monkeypatch
):
    bindir = tmp_path / "brokenbin"
    bindir.mkdir()
    script = bindir / "crontab"
    script.write_text('#!/bin/sh\necho "spool unreadable" >&2\nexit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"installed-schedule"}


def test_a_harness_scheduled_task_naming_the_checkout_blocks(
    checkout, stub_crontab, scheduled_tasks
):
    """The second scheduler. A run driven by a Claude scheduled task has NO crontab line, so a
    schedule predicate that read only the crontab cleared a checkout something still fires out
    of."""
    stub_crontab([])
    tasks = scheduled_tasks(
        [
            {
                "prompt": "/conductor:autodev",
                "cwd": str(checkout),
                "schedule": "*/10 * * * *",
            }
        ]
    )
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.QUIESCE) == {"installed-schedule"}
    finding = next(p for p in predicates if p.name == "installed-schedule").findings[0]
    assert finding.artifact == str(checkout)
    assert str(tasks) in finding.detail


def test_a_harness_scheduled_task_naming_the_checkout_in_its_prompt_blocks(
    checkout, stub_crontab, scheduled_tasks
):
    """A task's directory field is not the only way it names a path."""
    stub_crontab([])
    scheduled_tasks(
        {
            "tasks": [
                {"prompt": f"cd {checkout} && /conductor:autodev", "cwd": "/elsewhere"}
            ]
        }
    )
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"installed-schedule"}


def test_a_harness_scheduled_task_for_another_project_does_not_block(
    checkout, stub_crontab, scheduled_tasks
):
    """The anti-stub half: the leg must be capable of clearing, or it pins every checkout on a
    machine that has ever scheduled anything."""
    stub_crontab([])
    scheduled_tasks([{"prompt": "/conductor:autodev", "cwd": "/somewhere/else"}])
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_scheduled_task_naming_a_sibling_path_that_extends_the_checkouts_does_not_block(
    checkout, stub_crontab, scheduled_tasks
):
    """``/projects/app`` is a string prefix of ``/projects/app-backup`` and of
    ``/projects/app.old``, not a parent of either. Only a path boundary names the checkout."""
    stub_crontab([])
    scheduled_tasks(
        [
            {"prompt": f"cd {checkout}-backup && /conductor:autodev", "cwd": "/x"},
            {"prompt": f"cd {checkout}.old && /conductor:autodev", "cwd": "/x"},
            {"prompt": f"run /mirror{checkout} now", "cwd": "/x"},
            {"prompt": f"cd {checkout}+backup && go", "cwd": "/x"},
            {"prompt": f"cd {checkout}@old && go", "cwd": "/x"},
        ]
    )
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_scheduled_task_naming_the_checkout_or_beneath_it_in_free_text_blocks(
    checkout, stub_crontab, scheduled_tasks
):
    stub_crontab([])
    for text in (
        f"cd '{checkout}' && go",
        f"--project={checkout}/sub",
        f"{checkout}",
        f'"{checkout}"',
        f"cd {checkout}&&go",
        f"work in {checkout}.",
        f"run `{checkout}/bin/x` nightly",
        f"cd {checkout} #nightly",
        f"# nightly: cd {checkout}",
    ):
        scheduled_tasks([{"prompt": text, "cwd": "/x"}])
        assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {
            "installed-schedule"
        }, text


def test_an_unreadable_harness_scheduled_task_file_blocks(
    checkout, stub_crontab, scheduled_tasks
):
    stub_crontab([])
    scheduled_tasks("{not json")
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"installed-schedule"}


def test_an_unreadable_crontab_still_reports_the_scheduled_task_leg(
    checkout, tmp_path, monkeypatch, scheduled_tasks
):
    """Both legs, not the first one to fail. An operator told only "crontab unreadable" fixes
    that and re-runs; the task naming the checkout would surface only on the second pass."""
    bindir = tmp_path / "brokenbin"
    bindir.mkdir()
    script = bindir / "crontab"
    script.write_text('#!/bin/sh\necho "spool unreadable" >&2\nexit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    scheduled_tasks([{"prompt": "/conductor:autodev", "cwd": str(checkout)}])
    findings = next(
        p for p in doctor.scan(str(checkout)) if p.name == "installed-schedule"
    ).findings
    assert len(findings) == 2, findings
    assert any("crontab could not be read" in f.detail for f in findings)
    assert any("scheduled task registered in" in f.detail for f in findings)


# --- the driver's fire lock: the only trace a cron-launched driver leaves --------------------


def test_a_held_driver_fire_lock_blocks_with_no_owner_record_and_no_crontab(
    checkout, stub_crontab
):
    """The reviewer's exact false negative, reproduced. A cron-launched driver writes no
    ownership record, so ``live-owner`` sees nothing; its crontab line belongs to another user
    (or was never installed, as here); and the scan printed CLEAR while a fire held the lock."""
    stub_crontab([])
    lock = checkout / ".conductor" / "resume.lock"
    with _flock_held(lock) as holder:
        predicates = doctor.scan(str(checkout))
        assert _names(predicates, doctor.QUIESCE) == {"driver-fire-lock"}
        finding = next(p for p in predicates if p.name == "driver-fire-lock").findings[
            0
        ]
        assert finding.artifact == str(lock)
        assert str(holder.pid) in finding.detail
        assert str(holder.pid) in finding.recovery
        assert holder.poll() is None, (
            "the holder died, so nothing was observed against it"
        )


def test_the_scan_refuses_rather_than_clearing_while_a_fire_lock_is_held(
    checkout, stub_crontab, capsys
):
    """Through the entry point, because CLEAR is what an operator acts on."""
    stub_crontab([])
    with _flock_held(checkout / ".conductor" / "resume.lock"):
        assert doctor.main(["relocation", "--checkout", str(checkout)]) == 1
    captured = capsys.readouterr()
    assert "CLEAR" not in captured.out
    assert "driver-fire-lock" in captured.err


def test_an_unheld_fire_lock_does_not_block(checkout, stub_crontab):
    """The anti-stub half: the file survives every fire, so its presence cannot be the signal."""
    stub_crontab([])
    lock = checkout / ".conductor" / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_checkout_with_no_fire_lock_at_all_does_not_block(checkout, stub_crontab):
    stub_crontab([])
    assert not (checkout / ".conductor" / "resume.lock").exists()
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_an_unanswerable_lock_table_blocks_rather_than_clearing(
    checkout, stub_crontab, monkeypatch
):
    """ "I cannot tell who holds this" is not clearance — the same rule the owner record follows."""
    stub_crontab([])
    lock = checkout / ".conductor" / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    monkeypatch.setattr(doctor, "PROC_LOCKS", str(checkout / "no-such-lock-table"))
    assert doctor.flock_holders(str(lock)) is None
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"driver-fire-lock"}


def test_the_holder_probe_never_takes_the_lock_it_reports_on(checkout):
    """The whole point of reading ``/proc/locks``: probing by acquiring would exclude a driver
    that was about to start, from a scan contracted to disturb nothing."""
    lock = checkout / ".conductor" / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    assert doctor.flock_holders(str(lock)) == []
    # If the probe had taken it, a second holder could not.
    with _flock_held(lock) as holder:
        assert doctor.flock_holders(str(lock)) == [("FLOCK", holder.pid)]


def test_a_live_owner_record_blocks_and_names_the_process(checkout, stub_crontab):
    stub_crontab([])
    identity = _identity(os.getpid())
    _record(checkout, identity)
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.QUIESCE) == {"live-owner"}
    assert (
        next(p for p in predicates if p.name == "live-owner").findings[0].artifact
        == identity
    )


def test_an_owner_record_whose_process_exited_does_not_block(checkout, stub_crontab):
    """Otherwise every crashed run would pin the checkout in place forever, and the scan would
    be routed around rather than fixed."""
    stub_crontab([])
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    identity = _identity(dead.pid)
    dead.kill()
    dead.wait(timeout=30)
    _record(checkout, identity)
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_recycled_pid_does_not_resurrect_an_exited_owner(checkout, stub_crontab):
    """The reuse defence, at the scan. A bare-pid record could not express this at all: the
    identity would be a number some later process now legitimately holds, and the scan would
    block a checkout on a run that ended. ``starttime`` is what tells the two apart."""
    stub_crontab([])
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        scheme, pid, ticks, boot = _identity(live.pid).split(":")
        forged = f"{scheme}:{pid}:{int(ticks) + 1}:{boot}"
        _record(checkout, forged)
        assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()
        assert live.poll() is None, (
            "the process died, so nothing was observed against it"
        )
    finally:
        live.kill()
        live.wait(timeout=30)


def test_a_record_from_a_previous_boot_does_not_block(checkout, stub_crontab):
    """``starttime`` is ticks since boot, so it is meaningless across a restart: without the
    boot id a post-reboot process holding the same pid and tick count would read as the old
    owner and pin the checkout forever."""
    stub_crontab([])
    scheme, pid, ticks, _ = _identity(os.getpid()).split(":")
    _record(checkout, f"{scheme}:{pid}:{ticks}:00000000-0000-0000-0000-000000000000")
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


def test_a_bare_pid_record_blocks_rather_than_being_believed(checkout, stub_crontab):
    """A version 1 record names its owner as a bare pid with no reuse defence. Reading it as
    though it had one is the direction that loses work, so it is refused — and a refused record
    BLOCKS, because "I cannot interpret this" is not clearance to relocate the checkout."""
    stub_crontab([])
    state_root = str(checkout / ".conductor")
    os.makedirs(runstate.run_dir(state_root, RUN), exist_ok=True)
    atomic.write_json_atomic(
        ownership.record_path(state_root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": str(os.getpid()),
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": 1,
        },
    )
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"live-owner"}


def test_an_uninterpretable_owner_record_blocks(checkout, stub_crontab):
    stub_crontab([])
    state_root = str(checkout / ".conductor")
    os.makedirs(runstate.run_dir(state_root, RUN), exist_ok=True)
    atomic.write_json_atomic(
        ownership.record_path(state_root, RUN), {"run_key": RUN, "host": "claude"}
    )
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == {"live-owner"}


# --- the control, and the two classes ------------------------------------------------------


def test_a_quiet_checkout_is_cleared_to_move(checkout, stub_crontab, capsys):
    stub_crontab([])
    assert doctor.main(["relocation", "--checkout", str(checkout)]) == 0
    assert "CLEAR" in capsys.readouterr().out


def test_loss_risk_findings_are_reported_but_do_not_refuse_the_move(
    checkout, stub_crontab, capsys
):
    """The checkout has a commit no remote carries and untracked run state — the normal
    condition of a live project. Those gate DELETING the quarantined copy, a week after the
    move; refusing the move on them produces a gate that can never pass while anyone works."""
    stub_crontab([])
    (checkout / ".conductor").mkdir(exist_ok=True)
    (checkout / ".conductor" / "goal.md").write_text("goal\n")
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.LOSS_RISK) == {
        "unpushed-commits",
        "unpreserved-state",
    }
    assert doctor.main(["relocation", "--checkout", str(checkout)]) == 0
    out = capsys.readouterr().out
    assert ".conductor/goal.md" in out


def test_strict_refuses_on_the_loss_risk_gates(checkout, stub_crontab):
    stub_crontab([])
    assert doctor.main(["relocation", "--checkout", str(checkout), "--strict"]) == 1


def test_a_pushed_checkout_with_a_clean_tree_passes_the_loss_risk_gates(
    checkout, tmp_path, git, git_env, stub_crontab
):
    """The anti-stub half of the loss-risk pair: they must be capable of passing."""
    stub_crontab([])
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(checkout, "remote", "add", "github", str(bare))
    git(checkout, "push", "-q", "github", "trunk")
    assert _names(doctor.scan(str(checkout)), doctor.LOSS_RISK) == set()


# --- the mutation contract -------------------------------------------------------------------


def test_the_cli_writes_no_bytecode_into_the_checkout_it_judges(
    checkout, tmp_path, git, git_env, stub_crontab, isolated_harness_config
):
    """Through ``bin/conductor``, because that is where the contract was broken.

    The scan's own git calls carry ``--no-optional-locks`` so it cannot touch the tree. Python
    broke the promise one level up: importing ``conductor.doctor`` wrote ``__pycache__/`` into the
    plugin tree, which in the deployment this scan exists for IS the checkout being judged. The
    scan then created untracked content and ``--strict`` refused on the files it had just made,
    under a report saying nothing was written.

    So the fixture is that deployment: the CLI and its package live INSIDE the checkout, and
    everything is committed and pushed, which is the one state in which ``--strict`` can pass at
    all. It must, and the tree must be byte-identical afterwards."""
    stub_crontab([])
    shutil.copytree(
        ROOT / "conductor",
        checkout / "conductor",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (checkout / "bin").mkdir()
    shutil.copy2(ROOT / "bin" / "conductor", checkout / "bin" / "conductor")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "vendor the CLI into the checkout")
    git(checkout, "remote", "add", "github", str(bare))
    git(checkout, "push", "-q", "github", "trunk")

    before = _manifest(checkout)
    proc = subprocess.run(
        [
            str(checkout / "bin" / "conductor"),
            "doctor",
            "relocation",
            "--checkout",
            str(checkout),
            "--strict",
        ],
        cwd=str(checkout),
        env={
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(isolated_harness_config),
            "GIT_CONFIG_GLOBAL": git_env["GIT_CONFIG_GLOBAL"],
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    report = f"{proc.stdout}\n{proc.stderr}"
    written = sorted(
        str(p.relative_to(checkout)) for p in checkout.rglob("__pycache__")
    )
    assert not written, (
        f"the scan wrote bytecode into the checkout it reports as untouched: {written}\n{report}"
    )
    assert _manifest(checkout) == before, "the scan mutated the checkout it judged"
    assert proc.returncode == 0, (
        "--strict refused a fully committed, fully pushed checkout with no live artifact — "
        f"the only content it can be refusing on is content the scan itself created:\n{report}"
    )
    assert "CLEAR" in proc.stdout, report


def test_the_scan_writes_nothing_at_all(checkout, git, stub_crontab):
    """Including ``.git/index``: a plain ``git status`` refreshes the index stat cache and
    rewrites it, so a scan whose entire contract is "refuses before mutating anything" would
    mutate the tree it just refused to touch. ``--no-optional-locks`` is what prevents that."""
    stub_crontab([f"*/20 * * * * {checkout}/.conductor/resume-autodev.sh"])
    nested = checkout / ".worktrees" / "phase-1"
    git(checkout, "worktree", "add", "-q", "-b", "phase-1", str(nested))
    _record(checkout, _identity(os.getpid()))
    # Backdate a TRACKED file so its stat no longer matches what the index recorded. That is the
    # condition under which git refreshes the stat cache and writes .git/index — without it the
    # index is already accurate, git has nothing to update, and this test passes with
    # --no-optional-locks removed while proving nothing.
    os.utime(checkout / "seed.md", (0, 0))
    before = _manifest(checkout)
    assert ".git/index" in before
    assert doctor.main(["relocation", "--checkout", str(checkout)]) == 1
    assert _manifest(checkout) == before
    assert checkout.is_dir()


def test_a_missing_checkout_is_refused_without_inspection(tmp_path):
    assert doctor.main(["relocation", "--checkout", str(tmp_path / "nope")]) == 64


def test_the_verb_requires_its_subcommand(capsys):
    assert doctor.main([]) == 64
    assert "usage" in capsys.readouterr().err


def test_the_cli_registers_the_verb():
    """The entry point is half the contract: the scan is unreachable without it."""
    root = pathlib.Path(__file__).resolve().parents[2]
    assert "  doctor)" in (root / "bin" / "conductor").read_text()
