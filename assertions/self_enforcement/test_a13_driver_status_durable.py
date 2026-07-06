"""A13 — driver-status-nonzero-without-durable-driver (property).

Contract pinned: `conductor driver status` (project = CONDUCTOR_HOME / cwd repo) exits
non-zero when no durable driver exists for the project — no `# conductor-autodev
<main-root>` crontab marker and no scheduled_tasks.json — and zero when the crontab
marker is present (a durable driver with no fires yet is healthy, not a failure).

Fixture: a stub `crontab` prepended to PATH controls exactly what `crontab -l` returns,
so the test never reads or touches the machine's real crontab. The marker's main-root is
computed the same way the install path does (dirname of --git-common-dir), so a path
mismatch cannot false-green the present case.
"""

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")


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
    return proj, main_root


def _stub_crontab(tmp: Path, lines: list) -> Path:
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    crontab = stub_bin / "crontab"
    if lines:
        body = "".join(f"printf '%s\\n' {shlex.quote(ln)}\n" for ln in lines)
        crontab.write_text("#!/bin/sh\n" + body + "exit 0\n")
    else:
        crontab.write_text('#!/bin/sh\necho "no crontab for user" >&2\nexit 1\n')
    os.chmod(crontab, 0o755)
    return stub_bin


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


def test_no_durable_driver_exits_nonzero(tmp_path):
    proj, _ = _mk_project(tmp_path)
    stub_bin = _stub_crontab(tmp_path, [])
    proc = _status(proj, stub_bin)
    assert proc.returncode != 0, (
        "driver status reported healthy with no durable driver\n"
        + proc.stdout
        + proc.stderr
    )


def test_crontab_marker_present_exits_zero(tmp_path):
    proj, main_root = _mk_project(tmp_path)
    stub_bin = _stub_crontab(
        tmp_path,
        [
            f"@reboot {main_root}/.conductor/resume-autodev.sh # conductor-autodev {main_root}",
            f"*/20 * * * * {main_root}/.conductor/resume-autodev.sh # conductor-autodev {main_root}",
        ],
    )
    proc = _status(proj, stub_bin)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a healthy durable driver is not reported as failing
    assert "driver-unresolved" not in out, out
