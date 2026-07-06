"""A4 — driver-refuses-group-or-world-writable-env (property).

Contract pinned: the generated Tier-B driver checks `resume-env.sh` permissions BEFORE
sourcing it or firing: group-writable (0660) and world-writable (0606, 0666) each cause a
non-zero exit with an `env-unsafe` line in the resume log and NO fire; 0600 proceeds.

Harness: the driver is executed for real, with HOME pointed at a temp dir whose
`.local/bin` holds stub `claude`/`conductor` binaries — the driver's PATH repair puts
`$HOME/.local/bin` first, so the stubs always win over any real install (the real claude
can never fire from this test). The stub claude touches a `fired` sentinel, so "the fire
was reached" is observed directly, not inferred from the log.

Red-team notes: a guard that rejects only world-writable passes 0666/0606 but fails the
0660 case; a guard that always refuses fails the 0600-proceeds case.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor import resume_script as rs  # noqa: E402


def _mk_harness(tmp: Path):
    project = tmp / "proj"
    worktree = tmp / "wt"
    home = tmp / "home"
    bindir = home / ".local" / "bin"
    for d in (project / ".conductor", worktree, bindir):
        d.mkdir(parents=True)
    fired = tmp / "fired"
    claude = bindir / "claude"
    claude.write_text(f"#!/bin/sh\ntouch {fired}\nexit 0\n")
    os.chmod(claude, 0o755)
    conductor = bindir / "conductor"
    conductor.write_text("#!/bin/sh\nexit 1\n")  # gate not green -> driver proceeds
    os.chmod(conductor, 0o755)
    driver = project / ".conductor" / "resume-autodev.sh"
    driver.write_text(rs.render(str(project), str(worktree)))
    os.chmod(driver, 0o755)
    return project, driver, home, fired


def _fire(driver: Path, home: Path) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    return subprocess.run(
        ["bash", str(driver)], env=env, capture_output=True, text=True, timeout=30
    )


def _log(project: Path) -> str:
    log = project / ".conductor" / "resume-autodev.log"
    return log.read_text() if log.is_file() else ""


def test_group_or_world_writable_env_is_refused_loud(tmp_path):
    for mode in (0o660, 0o606, 0o666):
        harness_dir = tmp_path / f"case-{oct(mode)}"
        harness_dir.mkdir()
        project, driver, home, fired = _mk_harness(harness_dir)
        env_file = project / ".conductor" / "resume-env.sh"
        env_file.write_text('CONDUCTOR_RESUME_CLAUDE_FLAGS=""\n')
        os.chmod(env_file, mode)
        proc = _fire(driver, home)
        log = _log(project)
        assert proc.returncode != 0, (oct(mode), proc.stdout, proc.stderr, log)
        assert "env-unsafe" in log, (oct(mode), log)
        # must-not: the fire is never reached with an unsafe env file
        assert not fired.exists(), oct(mode)
        assert "fire-start" not in log, (oct(mode), log)


def test_0600_env_proceeds_to_the_fire(tmp_path):
    project, driver, home, fired = _mk_harness(tmp_path)
    env_file = project / ".conductor" / "resume-env.sh"
    env_file.write_text('CONDUCTOR_RESUME_CLAUDE_FLAGS=""\n')
    os.chmod(env_file, 0o600)
    proc = _fire(driver, home)
    log = _log(project)
    assert "env-unsafe" not in log, log
    assert fired.exists(), (proc.stdout, proc.stderr, log)
    assert "fire-start" in log, log
    assert proc.returncode == 0, (proc.returncode, log)
