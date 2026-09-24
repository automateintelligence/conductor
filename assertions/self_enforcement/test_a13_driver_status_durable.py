"""A13 — driver-status-nonzero-without-durable-driver (property).

Contract pinned: `conductor driver status` exits non-zero when this run has no durability
evidence its OWN host can be driven by — no `# conductor-autodev <main-root>` crontab
marker, and no scheduled-task entry belonging to a mechanism that host actually reads —
and zero when such evidence exists (a durable driver with no fires yet is healthy, not a
failure).

Re-derived host-neutrally from the original Claude-phrased A13, which named
`scheduled_tasks.json` in its setup. That file was never the invariant; it is one host's
evidence leg. The invariant is that status must not claim durability it cannot verify,
and must not deny durability it can. On a host with no scheduled-task mechanism the leg is
ABSENT, not empty — so the same file, on the same machine, must green a Claude-hosted run
and refuse a Codex-hosted one, because nothing fires a Claude harness task's prompt into
codex. Both halves are pinned below: asserting only the refusal would let an
implementation that deleted the leg outright pass, and asserting only the crontab leg
would let one that read a Claude path for every host pass.

Fixtures: a stub `crontab` prepended to PATH controls exactly what `crontab -l` returns,
and `CLAUDE_CONFIG_DIR` points at a temp dir, so the test never reads or touches the
machine's real crontab or real harness state. `CONDUCTOR_HOST` is cleared from the child
environment so the recorded `.conductor/host` is what decides the host. The marker's
main-root is computed the same way the install path does (dirname of --git-common-dir), so
a path mismatch cannot false-green the present case.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")

#: Every host conductor supports. A new host added without extending its durability
#: reporting fails here rather than silently reporting a Claude answer for itself.
HOSTS = ("claude", "codex")


def _mk_project(tmp: Path, host: str, name: str = "proj") -> tuple:
    proj = tmp / name
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    (proj / ".conductor").mkdir()
    (proj / ".conductor" / "host").write_text(f"{host}\n")
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


def _stub_crontab(tmp: Path, lines: list, name: str = "stub-bin") -> Path:
    stub_bin = tmp / name
    stub_bin.mkdir(exist_ok=True)
    crontab = stub_bin / "crontab"
    if lines:
        body = "".join(f"printf '%s\\n' {shlex.quote(ln)}\n" for ln in lines)
        crontab.write_text("#!/bin/sh\n" + body + "exit 0\n")
    else:
        crontab.write_text('#!/bin/sh\necho "no crontab for user" >&2\nexit 1\n')
    os.chmod(crontab, 0o755)
    return stub_bin


def _claude_config(tmp: Path, tasks=None) -> Path:
    """A temp CLAUDE_CONFIG_DIR — empty by default, optionally seeded with the harness
    scheduled_tasks.json Claude reads. Isolating it is what keeps the absent cases
    hermetic on a developer machine that really does have tasks registered."""
    cfg = tmp / "claude-config"
    cfg.mkdir(exist_ok=True)
    if tasks is not None:
        (cfg / "scheduled_tasks.json").write_text(json.dumps(tasks))
    return cfg


def _status(proj: Path, stub_bin: Path, cfg: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CONDUCTOR_HOST", None)
    env["CONDUCTOR_HOME"] = str(proj)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    return subprocess.run(
        [CONDUCTOR, "driver", "status"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("host", HOSTS)
def test_no_durable_driver_exits_nonzero(tmp_path, host):
    proj, _ = _mk_project(tmp_path, host)
    proc = _status(proj, _stub_crontab(tmp_path, []), _claude_config(tmp_path))
    assert proc.returncode != 0, (
        f"driver status reported healthy with no durable driver on host {host}\n"
        + proc.stdout
        + proc.stderr
    )


@pytest.mark.parametrize("host", HOSTS)
def test_crontab_marker_present_exits_zero(tmp_path, host):
    proj, main_root = _mk_project(tmp_path, host)
    stub_bin = _stub_crontab(
        tmp_path,
        [
            f"@reboot {main_root}/.conductor/resume-autodev.sh # conductor-autodev {main_root}",
            f"*/20 * * * * {main_root}/.conductor/resume-autodev.sh # conductor-autodev {main_root}",
        ],
    )
    proc = _status(proj, stub_bin, _claude_config(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a healthy durable driver is not reported as failing
    assert "driver-unresolved" not in out, out


def test_scheduled_task_greens_only_the_host_whose_mechanism_it_is(tmp_path):
    """One machine, one harness file, one task naming each project exactly — and the
    verdict must differ by host. The Claude-hosted run is durable on that task; the
    Codex-hosted run is not, because nothing fires that task's prompt into codex.
    Reading a durability answer out of a file this run's host never consults is the
    false-green this assertion exists to forbid."""
    claude_proj, claude_root = _mk_project(tmp_path, "claude", name="claude-proj")
    codex_proj, codex_root = _mk_project(tmp_path, "codex", name="codex-proj")
    stub_bin = _stub_crontab(tmp_path, [])
    cfg = _claude_config(
        tmp_path,
        tasks=[
            {"prompt": "/conductor:autodev", "cwd": claude_root},
            {"prompt": "/conductor:autodev", "cwd": codex_root},
        ],
    )

    ok = _status(claude_proj, stub_bin, cfg)
    assert ok.returncode == 0, (
        "the scheduled-task leg stopped counting on the host that owns it\n"
        + ok.stdout
        + ok.stderr
    )

    refused = _status(codex_proj, stub_bin, cfg)
    out = refused.stdout + refused.stderr
    assert refused.returncode != 0, (
        "driver status reported a codex run durable on a claude harness task\n" + out
    )
    # must-not: nor may it send a codex operator hunting for a file its host never reads
    assert "scheduled_tasks.json" not in out, out
