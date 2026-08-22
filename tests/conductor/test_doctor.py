"""``conductor doctor relocation`` — the read-only scan that refuses to call a checkout movable.

Every fixture is a self-contained temporary repository. Nothing here reads the developer's real
crontab: a stub ``crontab`` is put on ``PATH`` for every test that touches the schedule predicate,
and it refuses writes outright.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

import pytest

from conductor import doctor
from conductor.core import atomic, ownership, runstate

RUN = "alpha-0123456789ab"


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
        },
    )


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


def test_a_live_owner_record_blocks_and_names_the_process(checkout, stub_crontab):
    stub_crontab([])
    _record(checkout, str(os.getpid()))
    predicates = doctor.scan(str(checkout))
    assert _names(predicates, doctor.QUIESCE) == {"live-owner"}
    assert next(p for p in predicates if p.name == "live-owner").findings[
        0
    ].artifact == str(os.getpid())


def test_an_owner_record_whose_process_exited_does_not_block(checkout, stub_crontab):
    """Otherwise every crashed run would pin the checkout in place forever, and the scan would
    be routed around rather than fixed."""
    stub_crontab([])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    _record(checkout, str(dead.pid))
    assert _names(doctor.scan(str(checkout)), doctor.QUIESCE) == set()


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


def test_the_scan_writes_nothing_at_all(checkout, git, stub_crontab):
    """Including ``.git/index``: a plain ``git status`` refreshes the index stat cache and
    rewrites it, so a scan whose entire contract is "refuses before mutating anything" would
    mutate the tree it just refused to touch. ``--no-optional-locks`` is what prevents that."""
    stub_crontab([f"*/20 * * * * {checkout}/.conductor/resume-autodev.sh"])
    nested = checkout / ".worktrees" / "phase-1"
    git(checkout, "worktree", "add", "-q", "-b", "phase-1", str(nested))
    _record(checkout, str(os.getpid()))
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
