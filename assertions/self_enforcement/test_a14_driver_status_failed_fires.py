"""A14 — driver-status-flags-recent-failed-fires (property).

Contract pinned: with a durable driver installed, `conductor driver status` still exits
non-zero — and names the failures — when the recent tail of
`.conductor/resume-autodev.log` shows `driver-unresolved` or `fire-end rc=<non-zero>`
lines; with a clean recent tail (`fire-end rc=0`) it exits zero. The operator's health
signal must catch a driver that runs but keeps failing, not only an absent driver.

Fixture: the same stub-crontab technique as A13 provides the durable marker; log
timestamps are generated at test time so "recent" stays recent forever.
"""

import datetime
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _mk_project(tmp: Path) -> tuple:
    proj = tmp / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    (proj / ".conductor").mkdir()
    common = subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    main_root = os.path.dirname(common)
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    marker = f"*/20 * * * * {main_root}/.conductor/resume-autodev.sh # conductor-autodev {main_root}"
    crontab = stub_bin / "crontab"
    crontab.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(marker)}\nexit 0\n")
    os.chmod(crontab, 0o755)
    return proj, stub_bin


def _status(proj: Path, stub_bin: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    return subprocess.run(
        [CONDUCTOR, "driver", "status"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_recent_failed_fires_flip_status_nonzero(tmp_path):
    proj, stub_bin = _mk_project(tmp_path)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_now()} fire-start\n"
        f"{_now()} fire-end rc=3\n"
        f"{_now()} driver-unresolved claude= conductor=\n"
    )
    proc = _status(proj, stub_bin)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "driver status reported healthy while every recent fire failed\n" + out
    )
    # the failures are NAMED, not just counted into an exit code
    assert "driver-unresolved" in out or "rc=3" in out, out


def test_clean_recent_log_stays_zero(tmp_path):
    proj, stub_bin = _mk_project(tmp_path)
    (proj / ".conductor" / "resume-autodev.log").write_text(
        f"{_now()} fire-start\n{_now()} fire-end rc=0\n"
    )
    proc = _status(proj, stub_bin)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a clean log is not reported as failing
    assert "driver-unresolved" not in out, out
