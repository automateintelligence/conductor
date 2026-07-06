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


def test_render_shell_escapes_paths():
    """A worktree path with a space/quote must not break or inject into the emitted shell."""
    nasty = "/home/u/pro j'x"
    s = rs.render(PROJECT, nasty)
    assert (bash := _which("bash")) is None or subprocess.run(
        [bash, "-n"], input=s, text=True, capture_output=True
    ).returncode == 0
    # the raw unescaped literal must not appear as a bare assignment
    assert 'WORKTREE="/home/u/pro j\'x"' not in s
