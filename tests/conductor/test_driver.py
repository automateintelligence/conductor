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
import sys

import pytest

from conductor import driver, resume_script
from conductor.hosts.base import UnknownHost

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


def test_status_recent_plugin_list_timeout_flips_nonzero_and_is_named(
    tmp_path, monkeypatch, capsys
):
    """The Codex plugin lookup runs BEFORE `fire-start` and before the flock, so when it is cut
    off this is the only line the fire ever writes. Status matched exactly two shapes —
    `driver-unresolved` and a non-zero `fire-end` — so a driver stalling here reported CLEAN,
    which is the silent-stall class the status command exists to end."""
    proj = _durable(tmp_path, monkeypatch)
    bad = f"{_now()} plugin-list-timeout bin=/usr/bin/codex limit=20s rc=124"
    (proj / ".conductor" / "resume-autodev.log").write_text(f"{bad}\n")
    assert driver.status(str(proj)) == 1
    assert bad in capsys.readouterr().out


def test_status_recent_plugin_root_unverified_flips_nonzero_and_is_named(
    tmp_path, monkeypatch, capsys
):
    """The other pre-`fire-start` marker. It is emitted alongside `driver-unresolved`, and it is
    the half that says WHICH failure it is — codex lists the plugin and its tree is gone, rather
    than the plugin being absent — so status has to print it, not just the generic one."""
    proj = _durable(tmp_path, monkeypatch)
    bad = f"{_now()} plugin-root-unverified plugin=conductor home=/home/u/.codex"
    (proj / ".conductor" / "resume-autodev.log").write_text(f"{bad}\n")
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


# ---- A1: durability evidence is the RUN'S host's, not always Claude's --------------


def _record_host(proj, host_id):
    from conductor.hosts import runhost

    return runhost.record(str(proj), host_id)


def test_a_claude_runs_scheduled_task_still_counts_as_durable(
    tmp_path, monkeypatch, capsys
):
    """The existing leg, unchanged, on the host that has it."""
    proj, root = _mk_project(tmp_path)
    _record_host(proj, "claude")
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(
        tmp_path, monkeypatch, [{"prompt": "/conductor:autodev", "cwd": root}]
    )
    assert driver.status(str(proj)) == 0
    assert "scheduled task" in capsys.readouterr().out


def test_a_codex_run_does_not_read_the_claude_harness_file_to_decide_durability(
    tmp_path, monkeypatch, capsys
):
    """`scheduled_tasks.json` is Claude's harness file. A Codex run that happens to sit on a
    machine where Claude has a task registered is NOT durably driven by it — nothing fires
    that task's prompt into codex. Counting it would false-green the one signal an operator
    has that an unattended run will actually resume."""
    proj, root = _mk_project(tmp_path)
    _record_host(proj, "codex")
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(
        tmp_path, monkeypatch, [{"prompt": "/conductor:autodev", "cwd": root}]
    )
    assert driver.status(str(proj)) == 1
    out = capsys.readouterr().out
    assert "not durable" in out.lower()
    assert "scheduled_tasks.json" not in out


def test_a_codex_run_is_durable_on_the_crontab_marker_alone(
    tmp_path, monkeypatch, capsys
):
    proj, root = _mk_project(tmp_path)
    _record_host(proj, "codex")
    _stub_crontab(tmp_path, monkeypatch, _marker_lines(root))
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    assert driver.status(str(proj)) == 0
    assert "crontab" in capsys.readouterr().out


def test_the_not_durable_message_names_only_legs_this_host_actually_has(
    tmp_path, monkeypatch, capsys
):
    proj, _root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    _isolate_scheduled_tasks(tmp_path, monkeypatch)
    _record_host(proj, "claude")
    driver.status(str(proj))
    assert "scheduled_tasks.json" in capsys.readouterr().out
    _record_host(proj, "codex")
    driver.status(str(proj))
    codex_out = capsys.readouterr().out
    assert "scheduled_tasks.json" not in codex_out
    assert "conductor-autodev" in codex_out  # the marker it DID look for is still named


# ---- A1: install records the host so the fire spawns it ----------------------------


def test_install_records_the_requested_host_and_writes_that_hosts_driver(
    tmp_path, monkeypatch
):
    from conductor.hosts import runhost

    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    assert driver.install(str(proj), str(wt), host="codex") == 0
    assert runhost.resolve(root) == "codex"
    script = (proj / ".conductor" / "resume-autodev.sh").read_text()
    assert 'CODEX_BIN="$(command -v codex || true)"' in script
    assert "command -v claude" not in script


def test_install_without_a_host_leaves_an_existing_recording_alone(
    tmp_path, monkeypatch
):
    """Re-installing a driver must not silently move a live run onto another host."""
    from conductor.hosts import runhost

    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    runhost.record(root, "codex")
    assert driver.install(str(proj), str(wt)) == 0
    assert runhost.resolve(root) == "codex"


def test_install_refuses_an_unsupported_host_before_writing_anything(
    tmp_path, monkeypatch, capsys
):
    """Both entry points refuse, and neither leaves a driver or a cron line behind. Falling
    back to a host the operator did not ask for is the one outcome that must not happen."""
    proj, _root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()

    # the argv boundary: a closed choice set, named in the error
    with pytest.raises(SystemExit) as excinfo:
        driver.main(
            [
                "install",
                "--project",
                str(proj),
                "--worktree",
                str(wt),
                "--host",
                "gemini",
            ]
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "gemini" in err and "claude" in err and "codex" in err

    # the programmatic boundary: refused before any write
    with pytest.raises(UnknownHost):
        driver.install(str(proj), str(wt), host="gemini")
    assert not (proj / ".conductor" / "resume-autodev.sh").exists()
    assert not written.exists()


def test_cli_install_passes_the_host_through(tmp_path, monkeypatch):
    from conductor.hosts import runhost

    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    assert (
        driver.main(
            [
                "install",
                "--project",
                str(proj),
                "--worktree",
                str(wt),
                "--host",
                "codex",
            ]
        )
        == 0
    )
    assert runhost.resolve(root) == "codex"


def test_install_without_a_host_records_the_one_it_actually_rendered(
    tmp_path, monkeypatch
):
    """An unrecorded run is the state every pre-A1 run is in, and leaving it unrecorded is
    what let the recording and the installed script disagree. Install writes down the host it
    just rendered — `claude` here, unchanged behaviour, now durable instead of implicit."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    assert not os.path.exists(runhost.host_file(root))
    assert driver.install(str(proj), str(wt)) == 0
    assert runhost.recorded(root) == "claude"


def test_install_without_a_host_persists_the_operator_override(tmp_path, monkeypatch):
    """`$CONDUCTOR_HOST` steers the RENDER, so it has to steer the RECORD too. Rendering a
    codex driver while recording nothing leaves the next reconcile — which runs without that
    variable — regenerating a claude driver over it."""
    from conductor.hosts import runhost

    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    assert driver.install(str(proj), str(wt)) == 0
    monkeypatch.delenv("CONDUCTOR_HOST")
    assert runhost.recorded(root) == "codex"


def test_an_ambient_override_never_moves_a_run_that_already_recorded_a_host(
    tmp_path, monkeypatch
):
    """The re-install invariant, under the one input that could break it: a stray
    `CONDUCTOR_HOST` in the operator's shell must not repoint a live run's durable host."""
    from conductor.hosts import runhost

    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    runhost.record(root, "claude")
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    assert driver.install(str(proj), str(wt)) == 0
    monkeypatch.delenv("CONDUCTOR_HOST")
    assert runhost.recorded(root) == "claude"


def test_a_refused_driver_write_leaves_no_host_recorded(tmp_path, monkeypatch):
    """The record and the driver are one durable fact; a half-applied install is the worst
    outcome available. Recording first produced exactly that: `install(host="codex")` returned 2
    because the no-clobber guard refused the old script, the recording said codex, and the
    surviving script still fired claude — so cron ran claude while status, preflight and the
    merge gate all consulted codex, permanently (the guard refuses every retry)."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    script = proj / ".conductor" / "resume-autodev.sh"
    original = "#!/usr/bin/env bash\nexport CONDUCTOR_MERGE_VERIFY='pytest -q'\n"
    script.write_text(original)

    assert driver.install(str(proj), str(wt), host="codex") == 2
    assert script.read_text() == original  # the surviving driver still fires claude
    assert runhost.recorded(root) is None  # ...and nothing claims otherwise
    assert not written.exists()


def test_a_refused_driver_write_leaves_a_live_runs_host_where_it_was(
    tmp_path, monkeypatch
):
    """Same guard, on a run that already has a host: a failed re-install must not move it."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    runhost.record(root, "claude")
    (proj / ".conductor" / "resume-autodev.sh").write_text(
        "#!/usr/bin/env bash\nexport CONDUCTOR_MERGE_VERIFY='pytest -q'\n"
    )

    assert driver.install(str(proj), str(wt), host="codex") == 2
    assert runhost.recorded(root) == "claude"


def test_a_successful_install_records_the_host_the_written_script_fires(
    tmp_path, monkeypatch
):
    """The other direction: once the driver exists, the recording names ITS host — not the one
    the project had a moment earlier."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    runhost.record(root, "claude")

    assert driver.install(str(proj), str(wt), host="codex") == 0
    assert runhost.recorded(root) == "codex"
    script = (proj / ".conductor" / "resume-autodev.sh").read_text()
    assert 'CODEX_BIN="$(command -v codex || true)"' in script
    assert "CLAUDE_BIN" not in script


# ---- A1: an install is one durable fact, so two of them serialize --------------------------
#
# `install` writes the script, records the host, then installs the crontab lines. Nothing made
# that atomic, so two installs naming different hosts interleave: codex writes, claude writes
# AND records, codex records. Both return 0, the surviving script fires claude, `.conductor/host`
# says codex. `resume-script verify` reports it; `driver status` does not, and nothing reconciles
# before the next cron tick.

#: A competing install, run as a REAL separate process so the advisory lock is what decides the
#: outcome rather than a monkeypatch. The short lock timeout is set here, in the test's own
#: program, so no test-only knob has to exist in production.
_RACING_INSTALL = """
import sys
from conductor import driver
driver.INSTALL_LOCK_TIMEOUT_S = 1.0
sys.exit(driver.install(sys.argv[1], sys.argv[2], "claude"))
"""


def _installed_host(script_path):
    """Which host the script on disk actually fires — read from the script, never from the
    recording, because agreement between the two is the whole question."""
    text = script_path.read_text()
    return "codex" if 'CODEX_BIN="$(command -v codex' in text else "claude"


def test_a_competing_install_cannot_land_between_the_write_and_the_recording(
    tmp_path, monkeypatch
):
    """The finding's exact interleaving, forced: a full claude install runs in the window after
    this codex install has written its script and before it records. Under a lock the competitor
    cannot get in at all, and the two halves of the durable fact still name one host."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    script = proj / ".conductor" / "resume-autodev.sh"
    competitor = {}
    original_write = driver.resume_script.main

    def racing_write(argv):
        rc = original_write(argv)
        competitor["proc"] = subprocess.run(
            [sys.executable, "-c", _RACING_INSTALL, str(proj), str(wt)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return rc

    monkeypatch.setattr(driver.resume_script, "main", racing_write)

    assert driver.install(str(proj), str(wt), host="codex") == 0

    assert competitor["proc"].returncode != 0, competitor["proc"].stdout
    assert runhost.recorded(root) == "codex"
    assert _installed_host(script) == "codex"


#: The OTHER documented writer of the same file, run as a REAL separate process.
#: `/conductor:start` reconcile is told to regenerate a stale driver with `conductor
#: resume-script write` (skills/start/SKILL.md §RECONCILE), which is a public path into the very
#: file `install` is half-way through writing. It passes no `--host`, so it renders the RECORDED
#: host — which is still the OLD one at that instant, because `install` records last.
_RACING_RESUME_WRITE = """
import sys
from conductor import resume_script
resume_script.INSTALL_LOCK_TIMEOUT_S = 1.0
sys.exit(resume_script.main(
    ["write", "--project", sys.argv[1], "--worktree", sys.argv[2], "--out", sys.argv[3]]
))
"""


def test_a_reconcile_write_cannot_land_between_the_install_write_and_the_recording(
    tmp_path, monkeypatch
):
    """The split-state defect through the OTHER public path. Serializing `driver install`
    against itself closes only one of the two documented writers: a reconcile
    `resume-script write` landing in the same window rewrote the script back to claude, the
    install then recorded codex, and BOTH returned 0 — `.conductor/host` said codex while the
    installed script fired claude. The lock has to cover the file, not the entry point."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    script = proj / ".conductor" / "resume-autodev.sh"
    competitor = {}
    original_write = driver.resume_script.main

    def racing_write(argv):
        rc = original_write(argv)
        competitor["proc"] = subprocess.run(
            [
                sys.executable,
                "-c",
                _RACING_RESUME_WRITE,
                str(proj),
                str(wt),
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return rc

    monkeypatch.setattr(driver.resume_script, "main", racing_write)

    assert driver.install(str(proj), str(wt), host="codex") == 0

    assert competitor["proc"].returncode != 0, competitor["proc"].stderr
    assert runhost.recorded(root) == "codex"
    assert _installed_host(script) == "codex"


def test_an_install_blocked_by_another_says_so_and_changes_nothing(
    tmp_path, monkeypatch
):
    """Refusing is only correct if it refuses CLEANLY. A blocked install must leave no script
    and no recording behind — a lock taken after the write would still return nonzero here while
    having already replaced the driver."""
    from conductor.core import locks
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj, root = _mk_project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch, [])
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(driver, "INSTALL_LOCK_TIMEOUT_S", 1.0)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time;from conductor.core import locks;"
            "f=open(sys.argv[1],'w');"
            "__import__('fcntl').flock(f, __import__('fcntl').LOCK_EX);"
            "print('held', flush=True); time.sleep(30)",
            driver.install_lock_path(root),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        rc = driver.install(str(proj), str(wt), host="codex")
    finally:
        holder.kill()
        holder.wait(timeout=30)

    assert rc != 0
    assert not (proj / ".conductor" / "resume-autodev.sh").exists()
    assert runhost.recorded(root) is None
    assert not written.exists()
    assert (
        locks is not None
    )  # the primitive under test is conductor's own, not a new one
