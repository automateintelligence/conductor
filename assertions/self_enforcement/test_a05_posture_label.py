"""A5 — posture-label-reflects-configured-flags (property).

Contract pinned: the generated Tier-B driver logs `posture=<label>` at fire-start,
DERIVED from the configured flags: `--dangerously-skip-permissions` in
CONDUCTOR_RESUME_CLAUDE_FLAGS -> `posture=full-bypass`; a `--settings <path>` form ->
`posture=scoped`; no flags -> `posture=supervised`. The label is bare: the posture line
must not leak the raw flag value or the settings-file path.

Harness: same stub-bin technique as A4 — HOME points at a temp dir whose `.local/bin`
holds stub `claude`/`conductor`, so the driver's PATH repair resolves the stubs and the
real claude can never fire. Each case runs in its own project (fresh log).

Red-team notes: a constant label passes one case but not all three (the three expected
labels are pairwise distinct); logging the whole flag string fails the leak checks.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor import resume_script as rs  # noqa: E402


def _run_case(tmp: Path, name: str, env_line: str | None) -> str:
    """Build a harness, optionally write resume-env.sh (0600), fire, return the log."""
    base = tmp / name
    project = base / "proj"
    worktree = base / "wt"
    home = base / "home"
    bindir = home / ".local" / "bin"
    for d in (project / ".conductor", worktree, bindir):
        d.mkdir(parents=True)
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(claude, 0o755)
    conductor = bindir / "conductor"
    conductor.write_text("#!/bin/sh\nexit 1\n")  # gate not green -> driver proceeds
    os.chmod(conductor, 0o755)
    driver = project / ".conductor" / "resume-autodev.sh"
    driver.write_text(rs.render(str(project), str(worktree)))
    os.chmod(driver, 0o755)
    if env_line is not None:
        env_file = project / ".conductor" / "resume-env.sh"
        env_file.write_text(env_line + "\n")
        os.chmod(env_file, 0o600)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    proc = subprocess.run(
        ["bash", str(driver)], env=env, capture_output=True, text=True, timeout=30
    )
    log_file = project / ".conductor" / "resume-autodev.log"
    log = log_file.read_text() if log_file.is_file() else ""
    # harness sanity: the stub fire must have happened, else the case proves nothing
    assert "fire-start" in log, (name, proc.returncode, proc.stdout, proc.stderr, log)
    return log


def _posture_lines(log: str) -> list[str]:
    return [ln for ln in log.splitlines() if "posture=" in ln]


def test_posture_label_tracks_flags_and_leaks_nothing(tmp_path):
    secret_settings = str(tmp_path / "scoped-secret-settings.json")

    log_bypass = _run_case(
        tmp_path,
        "bypass",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"',
    )
    log_scoped = _run_case(
        tmp_path,
        "scoped",
        f'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings {secret_settings}"',
    )
    log_supervised = _run_case(tmp_path, "supervised", None)

    for log, label in (
        (log_bypass, "posture=full-bypass"),
        (log_scoped, "posture=scoped"),
        (log_supervised, "posture=supervised"),
    ):
        lines = _posture_lines(log)
        assert lines, f"no posture= line logged; expected {label}\n{log}"
        assert any(label in ln for ln in lines), (label, lines)

    # must-not: the posture line never carries the raw flag or the settings path
    for ln in _posture_lines(log_bypass):
        assert "--dangerously-skip-permissions" not in ln, ln
    assert secret_settings not in log_scoped, log_scoped
    # scoped is a bare label, not full-bypass mislabeled
    assert not any("posture=full-bypass" in ln for ln in _posture_lines(log_scoped))
