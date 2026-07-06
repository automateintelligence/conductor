"""Tier-B resume-driver generator: the runtime-resolution contract that fixes the 2026-07-05
silent-stall (generation-time-pinned bins that rot on upgrade)."""

import os
import re
import stat
import subprocess

import pytest

from conductor import resume_script as rs

PROJECT = "/home/u/programming/proj"
WORKTREE = "/home/u/programming/proj-run-x"


def _render():
    return rs.render(PROJECT, WORKTREE)


# ---- the render bakes in NO version-pinned bin paths (the whole point) ----


def test_render_resolves_bins_at_runtime_not_generation_time():
    s = _render()
    # claude: resolve from PATH, fall back to the STABLE unversioned launcher.
    assert 'CLAUDE_BIN="$(command -v claude || true)"' in s
    assert 'CLAUDE_BIN="$HOME/.local/bin/claude"' in s
    # conductor: resolve from PATH, else glob the NEWEST installed plugin version.
    assert 'CONDUCTOR="$(command -v conductor || true)"' in s
    assert "conductor/*/bin/conductor" in s and "sort -V | tail -1" in s


def test_render_has_no_rot_antipatterns():
    """No node-version-pinned path and no version-pinned conductor path may appear — those are
    exactly the two rots that stalled the live run."""
    s = _render()
    for pat, why in rs._ROT_PATTERNS:
        assert not pat.search(s), f"render must not contain {why}: {pat.pattern}"


def test_render_repairs_cron_path():
    s = _render()
    assert 'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin' in s


def test_render_fails_loud_on_unresolvable_bin():
    s = _render()
    assert "driver-unresolved" in s
    assert "exit 3" in s  # non-launch failure is surfaced + non-zero, never silent


def test_render_does_not_export_run_branch():
    """CONDUCTOR_RUN_BRANCH as a literal would override .conductor/run_branch and pin a stale
    branch (secondary footgun #2). The file is the single source of truth. (An explanatory
    comment may mention it; an actual `export` statement must not exist.)"""
    s = _render()
    assert not re.search(r"(?m)^\s*export\s+CONDUCTOR_RUN_BRANCH\b", s)


def test_render_sources_owner_env_out_of_line():
    """Owner/machine config is sourced, never baked — so regeneration can't clobber it."""
    s = _render()
    assert ".conductor/resume-env.sh" in s
    assert (
        "CONDUCTOR_MERGE_VERIFY" in s
    )  # named in the header so owners know where it goes


def test_render_never_bakes_a_permission_bypass():
    """Owner decision: --dangerously-skip-permissions / bypassPermissions are NEVER defaulted into
    the driver. Unattended authority is an explicit opt-in via CONDUCTOR_RESUME_CLAUDE_FLAGS."""
    s = _render()
    # the flag must appear ONLY in comment guidance, never on the actual fire command line
    fire = [ln for ln in s.splitlines() if ln.strip().startswith('"$CLAUDE_BIN" -p')]
    assert len(fire) == 1
    assert "--dangerously-skip-permissions" not in fire[0]
    assert "bypassPermissions" not in fire[0]
    # the fire consumes the owner's flags as re-parsed positional args, quoting preserved
    assert '"$@"' in fire[0]
    # the opt-in hook IS present (empty default) on the eval/set-- line feeding the fire,
    # so an owner can enable it from resume-env.sh
    evals = [
        ln
        for ln in s.splitlines()
        if ln.strip().startswith("eval") and "${CONDUCTOR_RESUME_CLAUDE_FLAGS:-}" in ln
    ]
    assert len(evals) == 1
    # no bypass flag baked anywhere outside comments
    for ln in s.splitlines():
        if not ln.strip().startswith("#"):
            assert "--dangerously-skip-permissions" not in ln


def test_write_nudges_owner_about_unattended_permissions(tmp_path, capsys):
    """When no resume-env.sh exists, `write` reminds the owner that unattended fires need
    pre-authorized permissions — without choosing the bypass for them."""
    out = tmp_path / "resume-autodev.sh"
    rs.main(["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)])
    err = capsys.readouterr().err
    assert "unattended" in err and "resume-env.sh" in err


def test_render_preserves_the_three_guards():
    s = _render()
    assert "flock -n 9" in s  # (c) one fire at a time
    assert "/proc/$pid/cwd" in s  # (a) no double-drive (cwd detection)
    assert "assert run --level spec" in s  # (b) done-gate-green no-op
    assert (
        'CONDUCTOR_HOME="$WORKTREE"' in s
    )  # resumes in the worktree, not owner checkout


def test_render_is_deterministic():
    assert rs.render(PROJECT, WORKTREE) == rs.render(PROJECT, WORKTREE)
    assert rs.render(PROJECT, WORKTREE) != rs.render(PROJECT, "/other/worktree")


def test_render_carries_template_version_marker():
    assert rs._MARKER in _render()
    assert f"v{rs.TEMPLATE_VERSION}" in rs._MARKER


def test_render_is_valid_bash():
    """`bash -n` must parse the emitted script — a broken heredoc/quote would ship a dead driver."""
    if not (bash := _which("bash")):
        pytest.skip("bash not available")
    proc = subprocess.run([bash, "-n"], input=_render(), text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def _which(b):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, b)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ---- verify: the reconcile self-heal signal ----


def test_verify_ok_on_freshly_written(tmp_path):
    script = tmp_path / "resume-autodev.sh"
    script.write_text(_render())
    ok, reasons = rs.verify(PROJECT, WORKTREE, str(script))
    assert ok, reasons


def test_verify_flags_missing(tmp_path):
    ok, reasons = rs.verify(PROJECT, WORKTREE, str(tmp_path / "nope.sh"))
    assert not ok
    assert any("missing" in r for r in reasons)


def test_verify_flags_rotted_pre_v2_script(tmp_path):
    """The actual failure mode: an old script with version-pinned bins must be flagged stale so
    reconcile regenerates it."""
    old = (
        "#!/usr/bin/env bash\n"
        'CONDUCTOR="/home/u/.claude/plugins/cache/automateintelligence/conductor/0.4.1/bin/conductor"\n'
        'CLAUDE_BIN="$(command -v claude || echo /home/u/.nvm/versions/node/v20.19.5/bin/claude)"\n'
        '"$CLAUDE_BIN" -p "/conductor:autodev"\n'
    )
    script = tmp_path / "resume-autodev.sh"
    script.write_text(old)
    ok, reasons = rs.verify(PROJECT, WORKTREE, str(script))
    assert not ok
    joined = " ".join(reasons)
    assert "rot" in joined  # both version-pinned paths caught
    assert f"v{rs.TEMPLATE_VERSION}" in joined  # no template marker


def test_verify_surfaces_inline_owner_env_for_safe_migration(tmp_path):
    """A regeneration must not silently drop the owner's inline CONDUCTOR_MERGE_VERIFY etc. —
    verify surfaces them so start can migrate them to resume-env.sh."""
    old = (
        "#!/usr/bin/env bash\n"
        "export CONDUCTOR_MERGE_VERIFY='cd backend && pytest -q'\n"
        "export DOCKER_HOST='unix:///var/run/docker.sock'\n"
    )
    script = tmp_path / "resume-autodev.sh"
    script.write_text(old)
    ok, reasons = rs.verify(PROJECT, WORKTREE, str(script))
    assert not ok
    joined = " ".join(reasons)
    assert "resume-env.sh" in joined
    assert "CONDUCTOR_MERGE_VERIFY" in joined and "DOCKER_HOST" in joined


def test_verify_flags_hand_edit(tmp_path):
    script = tmp_path / "resume-autodev.sh"
    script.write_text(_render() + "\necho tampered\n")
    ok, reasons = rs.verify(PROJECT, WORKTREE, str(script))
    assert not ok
    assert any("stale or hand-edited" in r for r in reasons)


# ---- CLI ----


def test_cli_write_to_stdout(capsys):
    rc = rs.main(["write", "--project", PROJECT, "--worktree", WORKTREE])
    assert rc == 0
    assert rs._MARKER in capsys.readouterr().out


def test_cli_write_to_file_is_executable(tmp_path):
    out = tmp_path / "resume-autodev.sh"
    rc = rs.main(
        ["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)]
    )
    assert rc == 0
    assert out.read_text() == _render()
    assert os.stat(out).st_mode & stat.S_IXUSR  # chmod +x so cron can run it


def test_cli_verify_exit_codes(tmp_path):
    out = tmp_path / "resume-autodev.sh"
    rs.main(["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)])
    assert (
        rs.main(
            [
                "verify",
                "--project",
                PROJECT,
                "--worktree",
                WORKTREE,
                "--script",
                str(out),
            ]
        )
        == 0
    )
    out.write_text("#!/usr/bin/env bash\necho stale\n")
    assert (
        rs.main(
            [
                "verify",
                "--project",
                PROJECT,
                "--worktree",
                WORKTREE,
                "--script",
                str(out),
            ]
        )
        == 1
    )


# ---- no-clobber guard: regeneration must never silently drop inline owner env ----


def test_write_refuses_to_clobber_inline_owner_env(tmp_path, capsys):
    """The exact P1 risk: mechanical 'verify fails -> write' must NOT overwrite a driver whose
    owner baked CONDUCTOR_MERGE_VERIFY inline. Refuse (exit 2) with migration guidance."""
    out = tmp_path / "resume-autodev.sh"
    original = (
        "#!/usr/bin/env bash\nexport CONDUCTOR_MERGE_VERIFY='cd backend && pytest -q'\n"
    )
    out.write_text(original)
    rc = rs.main(
        ["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)]
    )
    assert rc == 2
    assert out.read_text() == original  # untouched — owner env preserved
    err = capsys.readouterr().err
    assert (
        "refusing to overwrite" in err
        and "resume-env.sh" in err
        and "CONDUCTOR_MERGE_VERIFY" in err
    )


def test_write_force_overwrites_after_migration(tmp_path):
    out = tmp_path / "resume-autodev.sh"
    out.write_text("#!/usr/bin/env bash\nexport CONDUCTOR_MERGE_VERIFY='x'\n")
    rc = rs.main(
        [
            "write",
            "--project",
            PROJECT,
            "--worktree",
            WORKTREE,
            "--out",
            str(out),
            "--force",
        ]
    )
    assert rc == 0
    assert out.read_text() == _render()


def test_write_regenerates_clean_driver_without_force(tmp_path):
    """The common self-heal case: a current/older driver with NO inline owner env (env lives in
    resume-env.sh) regenerates freely."""
    out = tmp_path / "resume-autodev.sh"
    out.write_text("#!/usr/bin/env bash\n# old clean driver, no inline exports\n")
    assert (
        rs.main(
            ["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)]
        )
        == 0
    )
    assert out.read_text() == _render()


# ---- env-file safety guard: never source a group- or world-writable resume-env.sh ----


def test_render_guards_env_file_permissions_before_sourcing():
    """Static contract: the guard (env-unsafe + exit 5) appears BEFORE the sourcing line,
    and the sourcing is inside the guarded block, not a bare `[ -f ... ] && .`."""
    s = _render()
    assert "env-unsafe" in s
    assert "exit 5" in s
    guard_at = s.index("env-unsafe")
    source_at = s.index('. "$ENV_FILE"')
    assert guard_at < source_at
    assert '[ -f "$PROJECT/.conductor/resume-env.sh" ] && .' not in s


def _mk_env_harness(tmp):
    """Mirror of the frozen A4 harness: stub claude/conductor in a temp HOME's .local/bin
    (the driver's PATH repair puts it first, so the real bins can never fire)."""
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
    stub_conductor = bindir / "conductor"
    stub_conductor.write_text("#!/bin/sh\nexit 1\n")  # gate not green -> proceed
    os.chmod(stub_conductor, 0o755)
    driver = project / ".conductor" / "resume-autodev.sh"
    driver.write_text(rs.render(str(project), str(worktree)))
    os.chmod(driver, 0o755)
    return project, driver, home, fired


def _fire_driver(driver, home):
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    return subprocess.run(
        ["bash", str(driver)], env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize("mode", [0o660, 0o606, 0o666])
def test_driver_refuses_writable_env_file_loud_and_never_fires(tmp_path, mode):
    project, driver, home, fired = _mk_env_harness(tmp_path)
    env_file = project / ".conductor" / "resume-env.sh"
    env_file.write_text('CONDUCTOR_RESUME_CLAUDE_FLAGS=""\n')
    os.chmod(env_file, mode)
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode != 0
    assert "env-unsafe" in log
    assert f"mode={mode:o}" in log
    assert not fired.exists()
    assert "fire-start" not in log


def test_driver_proceeds_on_0600_env_file(tmp_path):
    project, driver, home, fired = _mk_env_harness(tmp_path)
    env_file = project / ".conductor" / "resume-env.sh"
    env_file.write_text('CONDUCTOR_RESUME_CLAUDE_FLAGS=""\n')
    os.chmod(env_file, 0o600)
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert "env-unsafe" not in log
    assert fired.exists()
    assert "fire-start" in log
    assert proc.returncode == 0


def test_driver_proceeds_when_env_file_absent(tmp_path):
    project, driver, home, fired = _mk_env_harness(tmp_path)
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert "env-unsafe" not in log
    assert fired.exists()
    assert proc.returncode == 0


def test_driver_preserves_quoted_flag_values_with_spaces(tmp_path):
    """A quoted `--settings '/path with space'` in CONDUCTOR_RESUME_CLAUDE_FLAGS must reach
    claude as exactly TWO argv words (the owner's own quoting re-parsed), never word-split
    into four fragments by a bare unquoted expansion."""
    project, driver, home, _fired = _mk_env_harness(tmp_path)
    argv_file = tmp_path / "argv"
    claude = home / ".local" / "bin" / "claude"
    claude.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "{argv_file}"\n'
    )
    os.chmod(claude, 0o755)
    env_file = project / ".conductor" / "resume-env.sh"
    env_file.write_text(
        "CONDUCTOR_RESUME_CLAUDE_FLAGS=\"--settings '/tmp/space path/settings.json'\"\n"
    )
    os.chmod(env_file, 0o600)
    proc = _fire_driver(driver, home)
    assert proc.returncode == 0
    argv = argv_file.read_text().splitlines()
    assert argv == [
        "-p",
        "/conductor:autodev",
        "--settings",
        "/tmp/space path/settings.json",
    ]
    # negative: the word-split fragments must not appear as argv entries
    assert "'/tmp/space" not in argv
    assert "path/settings.json'" not in argv


def test_render_shell_escapes_paths():
    """A worktree path with a space/quote must not break or inject into the emitted shell."""
    nasty = "/home/u/pro j'x"
    s = rs.render(PROJECT, nasty)
    assert (bash := _which("bash")) is None or subprocess.run(
        [bash, "-n"], input=s, text=True, capture_output=True
    ).returncode == 0
    # the raw unescaped literal must not appear as a bare assignment
    assert 'WORKTREE="/home/u/pro j\'x"' not in s
