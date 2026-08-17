"""`conductor driver install|status` — the operator's on-demand health signal for the
Tier-B unattended driver (Phase 6, A13/A14).

Every test uses a STUB `crontab` on PATH (mirroring the frozen A13/A14 fixtures) and a
temp CLAUDE_CONFIG_DIR for scheduled_tasks.json — the machine's real crontab and real
harness state are NEVER read or written. Log timestamps are generated at test time so
"recent" stays recent forever.
"""

import datetime
import json
import os
import shlex
import subprocess

import pytest

from conductor import driver, resume_script

# ---- fixtures ----------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _ago(hours: float) -> str:
    return (
        datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours)
    ).isoformat(timespec="seconds")


def _mk_project(tmp):
    proj = tmp / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    (proj / ".conductor").mkdir()
    return proj, resume_script.main_root(str(proj))


def _stub_crontab(tmp, monkeypatch, lines):
    """Stub `crontab` on PATH controlling exactly what `-l` returns (empty list = no
    crontab, exit 1) and recording any `-` write — the real crontab is never touched."""
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    written = tmp / "crontab-written"
    stub = stub_bin / "crontab"
    if lines:
        body = "".join(f"printf '%s\\n' {shlex.quote(ln)}\n" for ln in lines)
        stub.write_text(
            "#!/bin/sh\n"
            'case "${1:-}" in\n'
            f'  -) cat > "{written}" ;;\n'
            f"  *) {body} ;;\n"
            "esac\nexit 0\n"
        )
    else:
        stub.write_text(
            "#!/bin/sh\n"
            'case "${1:-}" in\n'
            f'  -) cat > "{written}" ;;\n'
            '  *) echo "no crontab for user" >&2; exit 1 ;;\n'
            "esac\n"
        )
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    return written


def _isolate_scheduled_tasks(tmp, monkeypatch, payload=None):
    """Point the harness scheduled_tasks.json lookup at a temp dir; optionally seed it."""
    cfg = tmp / "claude-config"
    cfg.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    if payload is not None:
        (cfg / "scheduled_tasks.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )
    return cfg


def _marker_lines(root):
    return [
        f"@reboot sleep 30 && {root}/.conductor/resume-autodev.sh # conductor-autodev {root}",
        f"*/20 * * * * {root}/.conductor/resume-autodev.sh # conductor-autodev {root}",
    ]


# ---- durability: absent / present / mismatched ---------------------------------


def test_status_no_durable_driver_exits_nonzero_and_says_why(
    tmp_path, monkeypatch, capsys
):
    proj, _ = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 1
    out = capsys.readouterr().out
    assert "not durable" in out.lower()
    assert "conductor-autodev" in out  # the marker it looked for is NAMED


def test_status_crontab_marker_present_exits_zero(tmp_path, monkeypatch, capsys):
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, _marker_lines(root))
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 0
    out = capsys.readouterr().out
    assert "durable" in out
    assert "crontab" in out  # the marker leg is named
    assert "driver-unresolved" not in out


def test_status_marker_for_another_root_is_not_durable(tmp_path, monkeypatch, capsys):
    """A path mismatch cannot false-green: another project's marker is not this
    project's durability."""
    proj, _ = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, _marker_lines("/somewhere/else"))
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 1


def test_status_commented_out_marker_line_is_not_durable(tmp_path, monkeypatch):
    """A disabled (commented-out) crontab entry still carrying the marker is NOT an
    active driver — it must not false-green the health signal."""
    proj, root = _mk_project(tmp_path)
    _stub_crontab(
        tmp_path,
        monkeypatch,
        ["# " + ln for ln in _marker_lines(root)],
    )
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 1


# ---- durability: the scheduled_tasks.json leg ----------------------------------


def test_status_matching_scheduled_task_is_durable(tmp_path, monkeypatch, capsys):
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(
        tmp_path,
        monkeypatch,
        {"tasks": [{"prompt": "/conductor:autodev", "cwd": root}]},
    )
    assert driver.status(str(proj)) == 0
    assert "durable" in capsys.readouterr().out


def test_status_scheduled_tasks_mere_existence_is_not_durable(tmp_path, monkeypatch):
    """A stale or unrelated scheduled task would false-green the health signal: the
    entry must match THIS project (prompt AND cwd), not merely exist."""
    proj, _ = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(
        tmp_path,
        monkeypatch,
        {
            "tasks": [
                {"prompt": "/conductor:autodev", "cwd": "/some/other/project"},
                {"prompt": "/other:thing", "cwd": "IGNORED"},
            ]
        },
    )
    assert driver.status(str(proj)) == 1


def test_status_unparseable_scheduled_tasks_is_not_durable(tmp_path, monkeypatch):
    """Fail-closed: an unparseable/unmatchable file is NOT durability evidence."""
    proj, _ = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(tmp_path, monkeypatch, "{not json")
    assert driver.status(str(proj)) == 1


# ---- log tail: failures named, clean stays green --------------------------------


def _durable(tmp_path, monkeypatch):
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, _marker_lines(root))
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    return proj


def test_status_durable_with_no_log_at_all_is_healthy(tmp_path, monkeypatch):
    """A durable driver with no fires yet is healthy, not a failure."""
    proj = _durable(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 0


def test_status_recent_driver_unresolved_flips_nonzero_and_is_named(
    tmp_path, monkeypatch, capsys
):
    proj = _durable(tmp_path, monkeypatch)
    bad = f"{_now()} driver-unresolved claude= conductor="
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_now()} fire-start\n{bad}\n"
    )
    assert driver.status(str(proj)) == 1
    # the offending line is printed VERBATIM, not just counted into an exit code
    assert bad in capsys.readouterr().out


@pytest.mark.parametrize(
    "tag",
    [
        "lock-unavailable rc=127 lock=/p/.conductor/resume.lock",
        "env-unsafe mode=666 /p/.conductor/resume-env.sh",
        "worktree-missing /p/.worktrees/run-x",
    ],
)
def test_status_names_every_fail_loud_exit_the_driver_can_log(
    tmp_path, monkeypatch, capsys, tag
):
    """`driver-unresolved` was the only fail-loud tag status recognised, so the driver's
    other refusals-to-fire — locking broken or unavailable, a world-writable env file, a
    missing worktree — exited non-zero into a log that reported "recent fires clean". Every
    tag the template can print has to be a tag status reports."""
    proj = _durable(tmp_path, monkeypatch)
    bad = f"{_now()} {tag}"
    (proj / ".conductor" / "resume-autodev.log").write_text(f"{bad}\n")
    assert driver.status(str(proj)) == 1
    assert bad in capsys.readouterr().out


def test_status_recent_nonzero_fire_end_flips_nonzero_and_is_named(
    tmp_path, monkeypatch, capsys
):
    proj = _durable(tmp_path, monkeypatch)
    bad = f"{_now()} fire-end rc=3"
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_now()} fire-start\n{bad}\n"
    )
    assert driver.status(str(proj)) == 1
    assert bad in capsys.readouterr().out


def test_status_clean_recent_log_stays_zero(tmp_path, monkeypatch, capsys):
    proj = _durable(tmp_path, monkeypatch)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_now()} fire-start posture=supervised\n{_now()} fire-end rc=0\n"
    )
    assert driver.status(str(proj)) == 0
    out = capsys.readouterr().out
    assert "recent fires clean" in out
    assert "driver-unresolved" not in out


def test_status_old_failures_outside_window_stay_zero(tmp_path, monkeypatch):
    """Failures older than CONDUCTOR_DRIVER_RECENT_HOURS (default 24) are history, not
    the current health signal."""
    proj = _durable(tmp_path, monkeypatch)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_ago(48)} driver-unresolved claude= conductor=\n"
        f"{_ago(47)} fire-end rc=3\n"
        f"{_now()} fire-end rc=0\n"
    )
    assert driver.status(str(proj)) == 0


def test_status_recent_hours_env_narrows_the_window(tmp_path, monkeypatch):
    proj = _durable(tmp_path, monkeypatch)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_ago(2)} fire-end rc=3\n"
    )
    monkeypatch.setenv("CONDUCTOR_DRIVER_RECENT_HOURS", "1")
    assert driver.status(str(proj)) == 0
    monkeypatch.setenv("CONDUCTOR_DRIVER_RECENT_HOURS", "3")
    assert driver.status(str(proj)) == 1


def test_status_unparseable_timestamp_counts_as_recent(tmp_path, monkeypatch, capsys):
    """Fail-closed toward REPORTING: a failing line whose timestamp cannot be parsed is
    treated as recent, never silently aged out."""
    proj = _durable(tmp_path, monkeypatch)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        "??? driver-unresolved claude= conductor=\n"
    )
    assert driver.status(str(proj)) == 1
    assert "driver-unresolved" in capsys.readouterr().out


# ---- the skip taxonomy: a run that skips EVERY fire is not "clean" -----------------
#
# Every line below is a line the driver really emits. `_recent_failures` only ever looked
# for lines that announce their own failure, so a run permanently blocked on a stranded
# lock — or a fire that hung and never came back — reported "recent fires clean". That is
# the same silent-block class the driver's own `skip reason=` taxonomy exists to remove,
# one level up in the tool the operator actually reads.


def _log(proj, *lines: str) -> None:
    (proj / ".conductor" / "resume-autodev.log").write_text(
        "".join(f"{ln}\n" for ln in lines)
    )


def test_status_reports_a_stranded_lock_when_fires_skip_with_no_fire_in_flight(
    tmp_path, monkeypatch, capsys
):
    """The signature of a leaked lock descriptor: the last fire ENDED (rc=0, healthy on its
    face), and every heartbeat since skips lock-held. Nothing holds the lock that is running
    a fire, so the run makes zero headless progress — forever, silently, until now."""
    proj = _durable(tmp_path, monkeypatch)
    skips = [
        f"{_ago(h)} skip reason=lock-held lock=/p/.conductor/resume.lock"
        for h in (2, 1, 0)
    ]
    _log(
        proj,
        f"{_ago(3)} fire-start posture=supervised",
        f"{_ago(3)} fire-end rc=0",
        *skips,
    )

    assert driver.status(str(proj)) == 1
    out = capsys.readouterr().out
    assert "recent fires clean" not in out
    assert "lock-held" in out
    assert skips[-1] in out  # the evidence is NAMED, not just counted


def test_status_stays_clean_while_a_long_phase_legitimately_holds_the_lock(
    tmp_path, monkeypatch, capsys
):
    """The anti-cry-wolf case, and the reason a raw lock-held count cannot be the rule: a
    phase that takes longer than the heartbeat SHOULD make later fires skip lock-held. The
    log says who holds it — an unmatched `fire-start` — so this is healthy deferral."""
    proj = _durable(tmp_path, monkeypatch)
    _log(
        proj,
        f"{_ago(0.7)} fire-start posture=supervised",
        f"{_ago(0.4)} skip reason=lock-held lock=/p/.conductor/resume.lock",
        f"{_ago(0.1)} skip reason=lock-held lock=/p/.conductor/resume.lock",
    )

    assert driver.status(str(proj)) == 0
    assert "recent fires clean" in capsys.readouterr().out


def test_status_tolerates_a_single_lock_held_skip_after_a_completed_fire(
    tmp_path, monkeypatch
):
    """A heartbeat CAN legitimately land in the window between a fire taking the lock and
    printing `fire-start` — the whole spec done-gate runs in between. One skip is that
    race; a stranded lock produces an unbroken run of them."""
    proj = _durable(tmp_path, monkeypatch)
    _log(
        proj,
        f"{_ago(1)} fire-start posture=supervised",
        f"{_ago(1)} fire-end rc=0",
        f"{_now()} skip reason=lock-held lock=/p/.conductor/resume.lock",
    )

    assert driver.status(str(proj)) == 0


def test_status_reports_a_fire_start_the_lock_was_reacquired_over(
    tmp_path, monkeypatch, capsys
):
    """A `fire-start` with no `fire-end`, followed by evidence the lock was taken AGAIN
    (another fire-start, or a gate-green skip — both happen only after flock succeeds):
    that fire died without logging its outcome. `fire-end rc=` never got to run."""
    proj = _durable(tmp_path, monkeypatch)
    hung = f"{_ago(2)} fire-start posture=supervised"
    _log(proj, hung, f"{_now()} skip reason=gate-green")

    assert driver.status(str(proj)) == 1
    out = capsys.readouterr().out
    assert "recent fires clean" not in out
    assert hung in out


def test_status_reports_a_fire_in_flight_past_the_stall_window(
    tmp_path, monkeypatch, capsys
):
    """A headless `-p` fire BLOCKED on a permission prompt it cannot answer hangs holding
    the lock: `fire-start`, then lock-held skips forever, no `fire-end`. Indistinguishable
    from a long phase except by elapsed time, so time is the discriminator."""
    proj = _durable(tmp_path, monkeypatch)
    hung = f"{_ago(9)} fire-start posture=supervised"
    _log(proj, hung, f"{_now()} skip reason=lock-held lock=/p/.conductor/resume.lock")

    assert driver.status(str(proj)) == 1
    assert hung in capsys.readouterr().out


def test_status_stall_hours_env_widens_the_in_flight_tolerance(tmp_path, monkeypatch):
    """Same override discipline as the recency window: the operator can widen it for a
    repo whose phases genuinely run long, and a malformed value degrades to the default."""
    proj = _durable(tmp_path, monkeypatch)
    _log(
        proj,
        f"{_ago(9)} fire-start posture=supervised",
        f"{_now()} skip reason=lock-held lock=/p/.conductor/resume.lock",
    )

    assert driver.status(str(proj)) == 1
    monkeypatch.setenv("CONDUCTOR_DRIVER_STALL_HOURS", "24")
    assert driver.status(str(proj)) == 0
    monkeypatch.setenv("CONDUCTOR_DRIVER_STALL_HOURS", "not-a-number")
    assert driver.status(str(proj)) == 1


def test_status_ignores_a_resolved_stranded_lock_run(tmp_path, monkeypatch):
    """History, not the current signal: a run of lock-held skips that a later `fire-end`
    proves is over must not keep failing status forever."""
    proj = _durable(tmp_path, monkeypatch)
    _log(
        proj,
        f"{_ago(40)} skip reason=lock-held lock=/p/.conductor/resume.lock",
        f"{_ago(39)} skip reason=lock-held lock=/p/.conductor/resume.lock",
        f"{_ago(38)} skip reason=lock-held lock=/p/.conductor/resume.lock",
        f"{_ago(1)} fire-start posture=supervised",
        f"{_now()} fire-end rc=0",
    )

    assert driver.status(str(proj)) == 0


# ---- install: fail-closed default, no durability judgment -----------------------


def test_install_writes_script_and_cron_lines(tmp_path, monkeypatch):
    proj, root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    assert driver.install(str(proj), str(wt)) == 0
    script = proj / ".conductor" / "resume-autodev.sh"
    assert script.is_file()
    assert script.read_text() == resume_script.render(root, str(wt))
    body = written.read_text()
    marker = resume_script.cron_marker(root)
    assert sum(marker in ln for ln in body.splitlines()) == 2
    assert "@reboot sleep 30 && " in body
    assert "*/20 * * * * " in body


def test_install_respects_the_inline_owner_env_no_clobber_guard(tmp_path, monkeypatch):
    """A driver carrying inline owner env must NOT be overwritten (resume-script write
    exits 2) — and then no cron lines are installed for the refused script."""
    proj, _root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    script = proj / ".conductor" / "resume-autodev.sh"
    original = "#!/usr/bin/env bash\nexport CONDUCTOR_MERGE_VERIFY='pytest -q'\n"
    script.write_text(original)
    assert driver.install(str(proj), str(wt)) == 2
    assert script.read_text() == original  # untouched
    assert not written.exists()  # cron NOT installed after the refusal


# ---- CLI ------------------------------------------------------------------------


def test_cli_status_defaults_project_to_conductor_home(tmp_path, monkeypatch, capsys):
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, _marker_lines(root))
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_HOME", str(proj))
    assert driver.main(["status"]) == 0
    assert "durable" in capsys.readouterr().out


def test_cli_status_on_a_non_repo_fails_with_a_named_reason(
    tmp_path, monkeypatch, capsys
):
    _stub_crontab(tmp_path, monkeypatch, [])
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(not_repo))
    assert driver.main(["status"]) == 1
    assert "cannot resolve main root" in capsys.readouterr().err


def test_cli_install_wires_worktree_and_project_defaults(tmp_path, monkeypatch):
    """The argparse install branch itself: --worktree flows through, project defaults
    to CONDUCTOR_HOME, and the rc plumbs back out of main()."""
    proj, root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(proj))
    assert driver.main(["install", "--worktree", str(wt)]) == 0
    assert (proj / ".conductor" / "resume-autodev.sh").read_text() == (
        resume_script.render(root, str(wt))
    )
    assert resume_script.cron_marker(root) in written.read_text()


def test_status_without_crontab_binary_still_uses_scheduled_task_leg(
    tmp_path, monkeypatch, capsys
):
    """A machine with NO crontab binary at all (the environment the scheduled-task leg
    exists for) must not traceback — the crontab leg reads as absent and the matching
    scheduled task still proves durability."""
    import shutil

    proj, root = _mk_project(tmp_path)
    bare_bin = tmp_path / "bare-bin"
    bare_bin.mkdir()
    git = shutil.which("git")
    assert git
    os.symlink(git, bare_bin / "git")
    monkeypatch.setenv("PATH", str(bare_bin))
    _isolate_scheduled_tasks(
        tmp_path,
        monkeypatch,
        {"tasks": [{"prompt": "/conductor:autodev", "cwd": root}]},
    )
    assert driver.status(str(proj)) == 0
    assert "scheduled task" in capsys.readouterr().out


def test_recent_hours_env_nonfinite_degrades_to_default(tmp_path, monkeypatch):
    """'nan'/'inf'/non-positive overrides must degrade to the 24h default, never crash
    timedelta."""
    proj = _durable(tmp_path, monkeypatch)
    (proj / ".conductor" / "resume-autodev.log").write_text(f"{_now()} fire-end rc=3\n")
    for bad in ("nan", "inf", "-5", "0"):
        monkeypatch.setenv("CONDUCTOR_DRIVER_RECENT_HOURS", bad)
        assert driver.status(str(proj)) == 1  # default window still reports the failure
