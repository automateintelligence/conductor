"""Tier-B resume-driver generator: the runtime-resolution contract that fixes the 2026-07-05
silent-stall (generation-time-pinned bins that rot on upgrade)."""

import json
import os
import re
import shlex
import stat
import subprocess
import time

import pytest

from conductor import resume_script as rs
from conductor.hosts import codex as codex_host

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
    # no bypass flag baked anywhere outside comments — the ONLY permitted non-comment
    # occurrences are the posture-detection `case` arms (detection, not enablement).
    # Exact whole-line equality: a tail appended to an arm (e.g. `set -- <flag> "$@"`)
    # would be enablement and must fail here.
    detection_arms = {
        '--dangerously-skip-permissions) POSTURE="full-bypass" ;;',
        '--permission-mode=bypassPermissions) POSTURE="full-bypass" ;;',
        'bypassPermissions) [ "$prev" = "--permission-mode" ] && POSTURE="full-bypass" ;;',
    }
    for ln in s.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if "--dangerously-skip-permissions" in ln or "bypassPermissions" in ln:
            assert stripped in detection_arms, (
                f"bypass flag outside the posture-detection case arms: {ln!r}"
            )


def test_write_nudges_owner_about_unattended_permissions(tmp_path, capsys):
    """When no resume-env.sh exists, `write` reminds the owner that unattended fires need
    pre-authorized permissions — without choosing the bypass for them."""
    out = tmp_path / "resume-autodev.sh"
    rs.main(["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)])
    err = capsys.readouterr().err
    assert "unattended" in err and "resume-env.sh" in err


def _write_and_read_err(tmp_path, capsys):
    out = tmp_path / "resume-autodev.sh"
    rc = rs.main(
        ["write", "--project", PROJECT, "--worktree", WORKTREE, "--out", str(out)]
    )
    assert rc == 0
    return capsys.readouterr().err


def test_write_nudge_fires_when_env_file_exists_but_posture_undecided(tmp_path, capsys):
    """The gate is 'permission posture undecided', NOT 'resume-env.sh absent': a file that
    exists but sets no posture (empty FLAGS, unrelated exports) still gets the nudge."""
    env = tmp_path / "resume-env.sh"
    env.write_text(
        'export CONDUCTOR_MERGE_VERIFY="pytest -q"\nCONDUCTOR_RESUME_CLAUDE_FLAGS=""\n'
    )
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err and "resume-env.sh" in err


def test_write_nudge_names_both_posture_branches(tmp_path, capsys):
    """The nudge is split into two concrete named branches — scoped (--settings, least
    privilege) and full (--dangerously-skip-permissions, owner's explicit call) — with
    BOTH flag spellings present so the owner can copy either."""
    err = _write_and_read_err(tmp_path, capsys)
    assert "--settings" in err
    assert "--dangerously-skip-permissions" in err
    assert "scoped" in err
    assert "full" in err
    assert "CONDUCTOR_RESUME_CLAUDE_FLAGS" in err


@pytest.mark.parametrize(
    "flags_line",
    [
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings /home/u/scoped-settings.json"',
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"',
        'export CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"',
        # the other full-bypass spelling the driver labels posture=full-bypass —
        # probe and driver must agree or the owner is re-nudged after deciding
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--permission-mode bypassPermissions"',
    ],
)
def test_write_nudge_silent_when_posture_decided(tmp_path, capsys, flags_line):
    """Either posture in the resume-env.sh FLAGS line silences the nudge — the owner
    already made the call; repeating the prompt would train them to ignore it."""
    env = tmp_path / "resume-env.sh"
    env.write_text(flags_line + "\n")
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" not in err


def test_write_nudge_ignores_commented_out_posture_lines(tmp_path, capsys):
    """A commented-out example FLAGS line is NOT a decision — silencing the nudge on it
    leaves the owner posture-less and the unattended fire stalling silently."""
    env = tmp_path / "resume-env.sh"
    env.write_text(
        '# CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"  # uncomment for full\n'
        'CONDUCTOR_RESUME_CLAUDE_FLAGS=""\n'
    )
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err


@pytest.mark.parametrize(
    "override_line",
    [
        'CONDUCTOR_RESUME_CLAUDE_FLAGS=""',
        "unset CONDUCTOR_RESUME_CLAUDE_FLAGS",
    ],
)
def test_write_nudge_fires_when_later_override_clears_posture(
    tmp_path, capsys, override_line
):
    """Shell semantics: the FINAL effective assignment wins. A posture followed by an
    empty reassignment (or unset) is undecided at runtime — the nudge must still fire."""
    env = tmp_path / "resume-env.sh"
    env.write_text(
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings /tmp/settings.json"\n'
        + override_line
        + "\n"
    )
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err


def test_write_nudge_ignores_comment_tail_on_empty_assignment(tmp_path, capsys):
    """An inline comment AFTER an empty active assignment is guidance, not a decision:
    `FLAGS="" # use --settings /path` must still nudge (the active value is empty)."""
    env = tmp_path / "resume-env.sh"
    env.write_text(
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="" # use --settings /path for scoped\n'
    )
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err


@pytest.mark.parametrize(
    "flags_line",
    [
        # posture-token SUBSTRINGS inside other tokens are not a decision — the probe
        # mirrors the driver's exact-token derivation (which labels these supervised)
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--model foo--settings-bar"',
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--permission-mode=bypassPermissions-disabled"',
    ],
)
def test_write_nudge_fires_on_posture_lookalike_tokens(tmp_path, capsys, flags_line):
    """Probe/driver agreement: values the driver would label supervised (lookalike
    substrings, not exact posture tokens) must NOT silence the nudge."""
    env = tmp_path / "resume-env.sh"
    env.write_text(flags_line + "\n")
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err


def test_write_nudge_fires_on_command_prefix_temp_env(tmp_path, capsys):
    """An UNQUOTED `FLAGS=--settings /path` is a temporary command env in shell — it does
    not persist for the driver after sourcing, so the posture is NOT decided and the
    nudge must still fire."""
    env = tmp_path / "resume-env.sh"
    env.write_text("CONDUCTOR_RESUME_CLAUDE_FLAGS=--settings /tmp/settings.json\n")
    err = _write_and_read_err(tmp_path, capsys)
    assert "unattended" in err


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


def _fire_driver(driver, home, extra_env=None):
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        **(extra_env or {}),
    }
    # cwd is the temp HOME, never the suite's own checkout. A driver that resolves anything
    # relative to `.` would otherwise find THIS conductor tree and pass on ambient luck — the
    # suite runs from inside a valid install, so a wrong answer and a right one look alike.
    return subprocess.run(
        ["bash", str(driver)],
        env=env,
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=30,
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


# ---- posture visibility: fire-start carries a DERIVED posture label (Phase 3, A5) ----


def _posture_lines(log):
    return [ln for ln in log.splitlines() if "posture=" in ln]


def _fire_with_env_line(tmp, name, env_line):
    """One harness per case (fresh log): optionally write resume-env.sh (0600), fire,
    return the log text."""
    base = tmp / name
    base.mkdir()
    project, driver, home, _fired = _mk_env_harness(base)
    if env_line is not None:
        env_file = project / ".conductor" / "resume-env.sh"
        env_file.write_text(env_line + "\n")
        os.chmod(env_file, 0o600)
    proc = _fire_driver(driver, home)
    log_file = project / ".conductor" / "resume-autodev.log"
    log = log_file.read_text() if log_file.is_file() else ""
    # harness sanity: the stub fire must have happened, else the case proves nothing
    assert "fire-start" in log, (name, proc.returncode, proc.stdout, proc.stderr, log)
    return log


def test_posture_label_derived_from_flags_three_inputs(tmp_path):
    """Bypass flags -> full-bypass; --settings <path> -> scoped (path leaked nowhere);
    empty -> supervised. Three pairwise-distinct labels prove the label is DERIVED,
    never a constant."""
    secret_settings = str(tmp_path / "scoped-secret-settings.json")

    log_bypass = _fire_with_env_line(
        tmp_path,
        "bypass",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"',
    )
    log_scoped = _fire_with_env_line(
        tmp_path,
        "scoped",
        f'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings {secret_settings}"',
    )
    log_supervised = _fire_with_env_line(tmp_path, "supervised", None)

    for log, label in (
        (log_bypass, "posture=full-bypass"),
        (log_scoped, "posture=scoped"),
        (log_supervised, "posture=supervised"),
    ):
        lines = _posture_lines(log)
        assert lines, f"no posture= line logged; expected {label}\n{log}"
        assert any(label in ln for ln in lines), (label, lines)

    # must-not: the posture line carries the BARE label — never the raw flag value
    for ln in _posture_lines(log_bypass):
        assert "--dangerously-skip-permissions" not in ln, ln
    # must-not: the settings path appears NOWHERE in the whole log
    assert secret_settings not in log_scoped, log_scoped
    # scoped is a bare label, not full-bypass mislabeled
    assert not any("posture=full-bypass" in ln for ln in _posture_lines(log_scoped))


def test_posture_bypass_wins_when_both_flags_present(tmp_path):
    """--dangerously-skip-permissions AND --settings together -> full-bypass (the more
    privileged posture is the honest label)."""
    log = _fire_with_env_line(
        tmp_path,
        "both",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings /tmp/s.json --dangerously-skip-permissions"',
    )
    lines = _posture_lines(log)
    assert any("posture=full-bypass" in ln for ln in lines), lines
    assert not any("posture=scoped" in ln for ln in lines), lines


def test_posture_recognizes_permission_mode_bypass_spelling(tmp_path):
    """`--permission-mode bypassPermissions` is the other full-bypass spelling — labeling
    it supervised would be exactly the audit misrepresentation A5 exists to prevent."""
    log = _fire_with_env_line(
        tmp_path,
        "permmode",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--permission-mode bypassPermissions"',
    )
    assert any("posture=full-bypass" in ln for ln in _posture_lines(log)), log


def test_posture_not_fooled_by_flag_substring_inside_a_value(tmp_path):
    """A settings PATH that merely contains the bypass substring must stay scoped — the
    patterns are space-anchored, matching flag words, not arbitrary value substrings."""
    log = _fire_with_env_line(
        tmp_path,
        "substr",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings /tmp/x--dangerously-skip-permissions.json"',
    )
    lines = _posture_lines(log)
    assert any("posture=scoped" in ln for ln in lines), lines
    assert not any("posture=full-bypass" in ln for ln in lines), lines


def test_posture_not_fooled_by_flag_token_inside_spaced_value(tmp_path):
    """EXACT argv-token derivation: a single settings-path ARGUMENT containing a space
    plus a flag-looking token must stay scoped — argv boundaries are honored, so the
    embedded ` --dangerously-skip-permissions.json` never reads as a real flag."""
    log = _fire_with_env_line(
        tmp_path,
        "spacedval",
        "CONDUCTOR_RESUME_CLAUDE_FLAGS=\"--settings '/tmp/a --dangerously-skip-permissions.json'\"",
    )
    lines = _posture_lines(log)
    assert any("posture=scoped" in ln for ln in lines), lines
    assert not any("posture=full-bypass" in ln for ln in lines), lines


def test_posture_derived_from_executed_argv_not_raw_string(tmp_path):
    """The label is derived from the SAME parsed argv the fire executes with: a QUOTED
    'bypassPermissions' value executes as full bypass and must log full-bypass, not
    supervised (a divergent raw-string parse would misrepresent the audit trail)."""
    log = _fire_with_env_line(
        tmp_path,
        "quotedbp",
        "CONDUCTOR_RESUME_CLAUDE_FLAGS=\"--permission-mode 'bypassPermissions'\"",
    )
    assert any("posture=full-bypass" in ln for ln in _posture_lines(log)), log


def test_posture_not_fooled_by_bypasspermissions_substring_in_a_path(tmp_path):
    """The bypassPermissions spelling is anchored to the --permission-mode flag+value
    shape — a settings PATH containing the bare substring must stay scoped."""
    log = _fire_with_env_line(
        tmp_path,
        "bpsubstr",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--settings /tmp/bypassPermissions.json"',
    )
    lines = _posture_lines(log)
    assert any("posture=scoped" in ln for ln in lines), lines
    assert not any("posture=full-bypass" in ln for ln in lines), lines


def test_render_posture_line_never_interpolates_raw_flags():
    """Static must-not: the printf that logs the posture label must interpolate the derived
    $POSTURE variable, never $CONDUCTOR_RESUME_CLAUDE_FLAGS (which would leak the raw flag
    value or the settings path into the log)."""
    s = _render()
    posture_printfs = [
        ln for ln in s.splitlines() if "printf" in ln and "posture=" in ln
    ]
    assert len(posture_printfs) == 1, posture_printfs
    line = posture_printfs[0]
    assert "CONDUCTOR_RESUME_CLAUDE_FLAGS" not in line, line
    assert '"$POSTURE"' in line, line
    # the label is DERIVED: a case statement maps flags -> label before the fire
    assert 'POSTURE="supervised"' in s  # least-privileged default
    assert s.index('POSTURE="supervised"') < s.index(line)


def test_render_shell_escapes_paths():
    """A worktree path with a space/quote must not break or inject into the emitted shell."""
    nasty = "/home/u/pro j'x"
    s = rs.render(PROJECT, nasty)
    assert (bash := _which("bash")) is None or subprocess.run(
        [bash, "-n"], input=s, text=True, capture_output=True
    ).returncode == 0
    # the raw unescaped literal must not appear as a bare assignment
    assert 'WORKTREE="/home/u/pro j\'x"' not in s


# ---- shared cron marker + install-cron/uninstall-cron (Phase 6, task 6.1) ----
#
# Every test here uses a STUB `crontab` executable prepended to PATH (state recorded in
# a temp file) — the machine's real crontab is NEVER read or written.


def _mk_git_project(tmp):
    proj = tmp / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
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
    return proj, os.path.dirname(common)


def _stub_crontab(tmp, monkeypatch, initial=None):
    """Stub `crontab` on PATH: `-l` prints the recorded state (exit 1 when absent, like a
    user with no crontab); `-` records stdin as the new state. Returns the state file."""
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    state = tmp / "crontab-state"
    stub = stub_bin / "crontab"
    stub.write_text(
        "#!/bin/sh\n"
        f'STATE="{state}"\n'
        'case "${1:-}" in\n'
        '  -l) [ -f "$STATE" ] || { echo "no crontab for user" >&2; exit 1; }; cat "$STATE" ;;\n'
        '  -) cat > "$STATE" ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    os.chmod(stub, 0o755)
    if initial is not None:
        state.write_text(initial)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    return state


def test_main_root_is_dirname_of_git_common_dir(tmp_path):
    proj, main_root = _mk_git_project(tmp_path)
    assert rs.main_root(str(proj)) == main_root


def test_main_root_identical_from_linked_worktree(tmp_path):
    """The whole point of --git-common-dir over --show-toplevel: install (from the owner
    checkout) and removal (from the run worktree) must compute the SAME root."""
    proj, main_root = _mk_git_project(tmp_path)
    subprocess.run(
        ["git", "-C", str(proj), "commit", "--allow-empty", "-q", "-m", "x"],
        check=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", "-q", str(wt)],
        check=True,
        timeout=30,
    )
    assert rs.main_root(str(wt)) == rs.main_root(str(proj)) == main_root


def test_cron_marker_is_the_literal_autodev_tag():
    assert rs.cron_marker("/home/u/proj") == "# conductor-autodev /home/u/proj"


def test_install_cron_appends_both_legs_with_marker(tmp_path, monkeypatch):
    proj, main_root = _mk_git_project(tmp_path)
    state = _stub_crontab(tmp_path, monkeypatch)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    lines = state.read_text().splitlines()
    marker = rs.cron_marker(main_root)
    marked = [ln for ln in lines if marker in ln]
    assert len(marked) == 2, lines
    assert any(
        ln.startswith("@reboot sleep 30 && ")
        and f"{main_root}/.conductor/resume-autodev.sh" in ln
        for ln in marked
    ), lines
    assert any(
        ln.startswith("*/20 * * * * ")
        and f"{main_root}/.conductor/resume-autodev.sh" in ln
        for ln in marked
    ), lines


def test_install_cron_is_idempotent(tmp_path, monkeypatch):
    proj, main_root = _mk_git_project(tmp_path)
    state = _stub_crontab(tmp_path, monkeypatch)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    first = state.read_text()
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    assert state.read_text() == first
    marker = rs.cron_marker(main_root)
    assert sum(marker in ln for ln in first.splitlines()) == 2


def test_install_cron_preserves_unrelated_lines(tmp_path, monkeypatch):
    proj, _ = _mk_git_project(tmp_path)
    unrelated = "0 5 * * * /usr/local/bin/backup.sh\n# a comment the owner wrote\n"
    state = _stub_crontab(tmp_path, monkeypatch, initial=unrelated)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    body = state.read_text()
    assert "0 5 * * * /usr/local/bin/backup.sh" in body
    assert "# a comment the owner wrote" in body


def test_install_then_uninstall_round_trips_to_original(tmp_path, monkeypatch):
    """Removal matches install BECAUSE both derive cron_marker(main_root(...)) — one
    implementation, no drift."""
    proj, _ = _mk_git_project(tmp_path)
    original = "0 5 * * * /usr/local/bin/backup.sh\n*/10 * * * * /opt/other/job\n"
    state = _stub_crontab(tmp_path, monkeypatch, initial=original)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    assert state.read_text() != original
    assert rs.main(["uninstall-cron", "--project", str(proj)]) == 0
    assert state.read_text() == original


def test_uninstall_cron_only_removes_this_projects_marker(tmp_path, monkeypatch):
    """grep -F -v -- semantics: ONLY lines carrying THIS project's exact marker go; another
    project's conductor-autodev lines survive."""
    proj, _ = _mk_git_project(tmp_path)
    other = "*/20 * * * * /elsewhere/.conductor/resume-autodev.sh # conductor-autodev /elsewhere\n"
    state = _stub_crontab(tmp_path, monkeypatch, initial=other)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    assert rs.main(["uninstall-cron", "--project", str(proj)]) == 0
    assert state.read_text() == other


def test_uninstall_cron_with_no_crontab_is_a_clean_noop(tmp_path, monkeypatch):
    """No crontab and nothing to remove = a TRUE no-op: exit 0, nothing written (the
    'no crontab' state is not converted into an existing empty crontab)."""
    proj, _ = _mk_git_project(tmp_path)
    state = _stub_crontab(tmp_path, monkeypatch)  # no state file = no crontab
    assert rs.main(["uninstall-cron", "--project", str(proj)]) == 0
    assert not state.exists()


def test_cron_read_failure_refuses_instead_of_wiping(tmp_path, monkeypatch):
    """A `crontab -l` failure that is NOT 'no crontab for user' (spool unreadable, cron
    misconfigured) must refuse loudly — treating it as empty would make the write-back
    destroy every pre-existing job."""
    proj, _ = _mk_git_project(tmp_path)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "crontab"
    written = tmp_path / "written"
    stub.write_text(
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        '  -l) echo "crontab: /var/spool/cron: Permission denied" >&2; exit 1 ;;\n'
        f'  -) cat > "{written}" ;;\n'
        "esac\n"
    )
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    assert rs.main(["install-cron", "--project", str(proj)]) == 1
    assert not written.exists()  # never wrote a table built from a failed read


def test_install_cron_quotes_a_root_with_spaces(tmp_path, monkeypatch):
    """The cron COMMAND is shell-quoted (a space in the root must not word-split the
    script path); the marker comment stays the literal unquoted fixed string."""
    base = tmp_path / "has space"
    base.mkdir()
    proj, main_root = _mk_git_project(base)
    state = _stub_crontab(tmp_path, monkeypatch)
    assert rs.main(["install-cron", "--project", str(proj)]) == 0
    marked = [
        ln for ln in state.read_text().splitlines() if rs.cron_marker(main_root) in ln
    ]
    assert len(marked) == 2
    quoted = shlex.quote(f"{main_root}/.conductor/resume-autodev.sh")
    for ln in marked:
        assert quoted in ln, ln
        assert ln.endswith(rs.cron_marker(main_root)), ln


def test_install_cron_on_a_non_repo_fails_with_a_named_reason(
    tmp_path, monkeypatch, capsys
):
    _stub_crontab(tmp_path, monkeypatch)
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    assert rs.main(["install-cron", "--project", str(not_repo)]) == 1
    assert "cannot resolve main root" in capsys.readouterr().err


# ---- A1: the driver spawns the RUN'S host, not always claude ----------------------------
#
# The measurement that motivated A1: 18 of the 36 host-coupled Python lines in conductor/ are
# in this file, and every one of them is on the path between "cron fires" and "an agent runs".
# A Codex user installing conductor today gets a driver that resolves `claude`.

# Literal, never `base.HOST_IDS`: parametrizing over the value under test would let a
# falsifier that shrinks HOST_IDS DELETE cases instead of failing them.
A1_HOSTS = ("claude", "codex")


def test_the_host_matrix_covers_exactly_the_supported_hosts():
    from conductor.hosts import base

    assert A1_HOSTS == base.HOST_IDS


def _project_recorded_as(tmp, host_id):
    """A project whose run is recorded as `host_id`, plus its worktree."""
    from conductor.hosts import runhost

    project = tmp / "proj"
    worktree = tmp / "wt"
    (project / ".conductor").mkdir(parents=True)
    worktree.mkdir()
    runhost.record(str(project), host_id)
    return str(project), str(worktree)


def test_render_for_a_claude_recorded_run_is_the_claude_driver(tmp_path):
    project, worktree = _project_recorded_as(tmp_path, "claude")
    s = rs.render(project, worktree)
    assert 'CLAUDE_BIN="$(command -v claude || true)"' in s
    assert '"$CLAUDE_BIN" -p "/conductor:autodev" "$@"' in s


def test_render_for_an_unrecorded_run_is_byte_identical_to_the_recorded_claude_one(
    tmp_path, monkeypatch
):
    """Every run installed before A1 has no `.conductor/host`. Those must not change host on
    the next regeneration — that would silently switch which agent drives a live run.

    `$CONDUCTOR_HOST` is cleared rather than assumed absent: with it set to `codex` both renders
    are codex and this compares two identical wrong answers. It is the only test in this file
    that supplies the resolver NOTHING, so it is the only one that can catch a broken
    derivation, and it has to actually be unsupplied to do that."""
    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    project, worktree = _project_recorded_as(tmp_path, "claude")
    recorded = rs.render(project, worktree)
    os.remove(os.path.join(project, ".conductor", "host"))
    unrecorded = rs.render(project, worktree)
    assert unrecorded == recorded
    assert 'CLAUDE_BIN="$(command -v claude || true)"' in unrecorded


def test_render_for_a_codex_recorded_run_resolves_and_launches_codex(tmp_path):
    """The A1 goal in one assertion: the cron fire spawns `codex`, not `claude`."""
    project, worktree = _project_recorded_as(tmp_path, "codex")
    s = rs.render(project, worktree)
    assert 'CODEX_BIN="$(command -v codex || true)"' in s
    assert "command -v claude" not in s
    fire = [ln for ln in s.splitlines() if ln.strip().startswith('"$CODEX_BIN"')]
    assert len(fire) == 1, fire
    assert fire[0].strip().startswith('"$CODEX_BIN" exec --cd "$WORKTREE"')
    assert "skills/autodev/SKILL.md" in fire[0]


def test_a_codex_driver_never_names_the_claude_binary_or_its_plugin_cache(tmp_path):
    project, worktree = _project_recorded_as(tmp_path, "codex")
    s = rs.render(project, worktree)
    for token in (
        "CLAUDE_BIN",
        "$HOME/.local/bin/claude",
        '$HOME"/.claude/plugins/cache',
        "--dangerously-skip-permissions",
        "--permission-mode",
        "CONDUCTOR_RESUME_CLAUDE_FLAGS",
    ):
        assert token not in s, token


def test_a_claude_driver_never_names_codex_flags(tmp_path):
    project, worktree = _project_recorded_as(tmp_path, "claude")
    s = rs.render(project, worktree)
    for token in (
        "CODEX_BIN",
        "--sandbox",
        "--dangerously-bypass-approvals-and-sandbox",
        "CONDUCTOR_RESUME_CODEX_FLAGS",
    ):
        assert token not in s, token


@pytest.mark.parametrize("host_id", A1_HOSTS)
def test_every_hosts_render_is_valid_bash(host_id, tmp_path):
    if not (bash := _which("bash")):
        pytest.skip("bash not available")
    project, worktree = _project_recorded_as(tmp_path, host_id)
    proc = subprocess.run(
        [bash, "-n"],
        input=rs.render(project, worktree),
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("host_id", A1_HOSTS)
def test_every_hosts_render_shell_escapes_paths(host_id, tmp_path):
    if not (bash := _which("bash")):
        pytest.skip("bash not available")
    project, _ = _project_recorded_as(tmp_path, host_id)
    s = rs.render(project, "/home/u/pro j'x")
    assert (
        subprocess.run([bash, "-n"], input=s, text=True, capture_output=True).returncode
        == 0
    )
    assert 'WORKTREE="/home/u/pro j\'x"' not in s


@pytest.mark.parametrize("host_id", A1_HOSTS)
def test_every_hosts_render_keeps_the_three_guards_and_the_rot_ban(host_id, tmp_path):
    project, worktree = _project_recorded_as(tmp_path, host_id)
    s = rs.render(project, worktree)
    assert "flock -n 9" in s
    assert "assert run --level spec" in s
    assert 'CONDUCTOR_HOME="$WORKTREE"' in s
    assert "driver-unresolved" in s and "exit 3" in s
    assert "env-unsafe" in s and "exit 5" in s
    for pat, why in rs._ROT_PATTERNS:
        assert not pat.search(s), f"{host_id}: {why}"


def test_verify_flags_an_installed_driver_from_the_previous_template(tmp_path):
    """TEMPLATE_VERSION must move whenever the generated text does, or an already-installed
    driver never regenerates and the whole change ships inert."""
    project, worktree = _project_recorded_as(tmp_path, "claude")
    installed = tmp_path / "resume-autodev.sh"
    installed.write_text(
        rs.render(project, worktree).replace(
            rs._MARKER, "# conductor-resume-template: v4"
        )
    )
    ok, reasons = rs.verify(project, worktree, str(installed))
    assert not ok
    assert any("regenerate" in r for r in reasons), reasons


def test_verify_regenerates_when_the_recorded_host_changes(tmp_path):
    """Re-recording a run onto the other host must make the installed driver stale, or the
    run keeps firing the old agent forever."""
    from conductor.hosts import runhost

    project, worktree = _project_recorded_as(tmp_path, "claude")
    installed = tmp_path / "resume-autodev.sh"
    installed.write_text(rs.render(project, worktree))
    assert rs.verify(project, worktree, str(installed))[0]
    runhost.record(project, "codex")
    ok, reasons = rs.verify(project, worktree, str(installed))
    assert not ok, reasons


# ---- A1: the owner-env allowlist ----------------------------------------------------------


@pytest.mark.parametrize(
    "var",
    [
        "CONDUCTOR_MERGE_VERIFY",
        "CONDUCTOR_PLUGIN_DIRS",
        "DOCKER_HOST",
        "CONDUCTOR_RESUME_CLAUDE_FLAGS",
        "CONDUCTOR_RESUME_CODEX_FLAGS",
        "CONDUCTOR_SPEC_ROOTS",
    ],
)
def test_owner_env_migration_allowlist_covers_every_declared_variable(var):
    """An owner variable missing from this list is silently DROPPED when a driver carrying it
    inline is regenerated. CONDUCTOR_SPEC_ROOTS is the live example: a project whose specs are
    not under docs/specs resolves its gate interactively and then fails across the cron loop."""
    assert var in rs.OWNER_ENV_VARS
    assert rs._OWNER_ENV_RE.findall(f"export {var}=x\n") == [var]


def test_the_allowlist_regex_is_derived_from_the_declared_set_not_hand_written():
    """A hand-maintained alternation drifts from the declaration the day someone adds a
    variable to one and not the other."""
    for var in rs.OWNER_ENV_VARS:
        assert rs._OWNER_ENV_RE.findall(f"export {var}=x\n") == [var]
    assert rs._OWNER_ENV_RE.findall("export CONDUCTOR_NOT_DECLARED=x\n") == []


# ---- A1: the codex driver actually fires codex (end to end, real bash) --------------------


#: The one marketplace and version every harness entry below is installed from. The install
#: root is NOT emitted by `plugin list --json`; it is
#: `$CODEX_HOME/plugins/cache/<marketplaceName>/<name>/<version>`, so these three strings are
#: what decides where the fixture must put the plugin tree.
_MARKET, _VERSION = "openai-curated", "d6169bef"


def _codex_plugin_list_json(plugins, disabled=()):
    """What `codex plugin list --json` prints — the shape verified on codex-cli 0.147.0 by
    recording the CLI's own output (`tests/conductor/fixtures/`).

    `source.path` is the MARKETPLACE SOURCE tree, which is not where the plugin is installed.
    Callers pass that source path here and put the actual plugin under the derived install root;
    a fixture that passes one path for both cannot fail when the driver reads the wrong one.

    `disabled` names plugins the operator turned off — 0.147.0 keeps those in `installed[]` with
    `"enabled": false`. Hardcoding `True` is why nothing could tell an operator's disabled
    conductor from a live one before the driver went ahead and exec'd its `bin/conductor`.

    Each entry is `(name, source_path)` or `(name, source_path, marketplace)`. The third element
    exists so two marketplaces can ship one plugin NAME at once — a state a fixture pinned to a
    single marketplace cannot describe, and therefore cannot fail on.
    """
    return json.dumps(
        {
            "installed": [
                {
                    "pluginId": f"{name}@{market}",
                    "name": name,
                    "marketplaceName": market,
                    "version": _VERSION,
                    "installed": True,
                    "enabled": name not in disabled,
                    "source": {"source": "local", "path": str(source_path)},
                    "installPolicy": "AVAILABLE",
                    "authPolicy": "NEVER",
                }
                for name, source_path, market in (
                    (*p, _MARKET) if len(p) == 2 else p for p in plugins
                )
            ]
        }
    )


def _mk_codex_harness(
    tmp,
    *,
    install_conductor_plugin=True,
    on_path=False,
    plugin_list_hangs=False,
    conductor_plugin_disabled=False,
    second_marketplace=None,
):
    """A Codex-recorded run on a machine that installed conductor the way a Codex user does:
    as a PLUGIN, whose bin is therefore NOT on PATH (skills/start/SKILL.md says exactly this).

    `conductor` is deliberately absent from the temp HOME's .local/bin unless `on_path` — a
    fixture that plants it there proves only that `command -v` works. The stub `codex` answers
    `plugin list --json`, which is how the driver is expected to find the plugin root.

    The two roots are DIFFERENT, because on a real Codex they are: the installed copy sits at
    `$CODEX_HOME/plugins/cache/<marketplace>/<name>/<version>`, while `source.path` names the
    marketplace tree it was copied from. Both are populated, and the source one is a complete
    decoy — its own `bin/conductor` and `skills/autodev/SKILL.md` — so a driver that resolves
    off `source.path` fires successfully against the wrong tree instead of failing to resolve.
    That is the state the previous fixture could not express, because it passed one path twice.
    """
    from conductor.hosts import runhost

    project = tmp / "proj"
    worktree = tmp / "wt"
    home = tmp / "home"
    bindir = home / ".local" / "bin"
    plugin_root = (
        home / ".codex" / "plugins" / "cache" / _MARKET / "conductor" / _VERSION
    )
    source_root = tmp / "codex-marketplace" / "plugins" / "conductor"
    skill = plugin_root / "skills" / "autodev"
    for d in (
        project / ".conductor",
        worktree,
        bindir,
        skill,
        plugin_root / "bin",
        source_root / "skills" / "autodev",
        source_root / "bin",
    ):
        d.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: autodev\n---\n")
    (source_root / "skills" / "autodev" / "SKILL.md").write_text(
        "---\nname: autodev\n---\n"
    )
    decoy = source_root / "bin" / "conductor"
    decoy.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(decoy, 0o755)
    argv_file = tmp / "argv"
    entries = [("conductor", source_root)] if install_conductor_plugin else []
    if second_marketplace:
        # A SECOND plugin, also called `conductor`, from another marketplace — complete, with
        # its own bin and autodev skill, and listed FIRST the way 0.147.0 orders it. Nothing on
        # disk distinguishes it from the real one except the identity `plugin list` reports.
        rival = (
            home
            / ".codex"
            / "plugins"
            / "cache"
            / second_marketplace
            / "conductor"
            / _VERSION
        )
        (rival / "skills" / "autodev").mkdir(parents=True)
        (rival / "skills" / "autodev" / "SKILL.md").write_text("---\n---\n")
        (rival / "bin").mkdir()
        rival_bin = rival / "bin" / "conductor"
        rival_bin.write_text("#!/bin/sh\nexit 1\n")
        os.chmod(rival_bin, 0o755)
        entries.insert(
            0,
            (
                "conductor",
                tmp / "rival-marketplace" / "conductor",
                second_marketplace,
            ),
        )
    listed = _codex_plugin_list_json(
        entries,
        disabled=("conductor",) if conductor_plugin_disabled else (),
    )
    # `plugin_list_hangs` is the CLI that never returns. It sleeps far longer than any bound the
    # driver could reasonably impose, so a driver that does not bound the call is measurably
    # stuck rather than merely slow.
    plugin_arm = (
        "sleep 8; exit 0" if plugin_list_hangs else f"printf '%s' '{listed}'; exit 0"
    )
    codex = bindir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = plugin ]; then {plugin_arm}; fi\n'
        f'for a in "$@"; do printf \'%s\\n\' "$a"; done > "{argv_file}"\nexit 0\n'
    )
    os.chmod(codex, 0o755)
    stub_conductor = (bindir if on_path else plugin_root / "bin") / "conductor"
    stub_conductor.write_text("#!/bin/sh\nexit 1\n")  # gate not green -> proceed
    os.chmod(stub_conductor, 0o755)
    runhost.record(str(project), "codex")
    driver = project / ".conductor" / "resume-autodev.sh"
    driver.write_text(rs.render(str(project), str(worktree)))
    os.chmod(driver, 0o755)
    return project, driver, home, argv_file, stub_conductor


def _fire_codex(tmp, name, env_line=None):
    base = tmp / name
    base.mkdir()
    project, driver, home, argv_file, _conductor = _mk_codex_harness(base)
    if env_line is not None:
        env_file = project / ".conductor" / "resume-env.sh"
        env_file.write_text(env_line + "\n")
        os.chmod(env_file, 0o600)
    proc = _fire_driver(driver, home)
    log_file = project / ".conductor" / "resume-autodev.log"
    log = log_file.read_text() if log_file.is_file() else ""
    argv = argv_file.read_text().splitlines() if argv_file.is_file() else []
    return proc, log, argv


def test_a_codex_run_fires_the_codex_binary_with_an_exec_invocation(tmp_path):
    if not _which("bash"):
        pytest.skip("bash not available")
    proc, log, argv = _fire_codex(tmp_path, "fire")
    assert "fire-start" in log, (proc.returncode, proc.stdout, proc.stderr, log)
    assert proc.returncode == 0
    assert argv[:3] == ["exec", "--cd", str(tmp_path / "fire" / "wt")], argv
    assert argv[-1].startswith("Read ")
    assert argv[-1].endswith("/skills/autodev/SKILL.md and execute it.")
    # -p is --profile to codex: the prompt must never be its value
    assert "-p" not in argv, argv
    assert "--profile" not in argv, argv
    assert "/conductor:autodev" not in argv, argv


def test_a_plugin_installed_conductor_survives_a_cron_fire_with_nothing_on_path(
    tmp_path,
):
    """The install a Codex user actually has. `skills/start/SKILL.md` states that installed
    plugin binaries are NOT on PATH, nothing installs a shim, and nothing persists
    CODEX_PLUGIN_ROOT — so `command -v conductor` is empty at fire time and the whole run
    stops at the unresolved guard before Codex is ever spawned.

    The prompt must name the SKILL.md inside the INSTALLED plugin root. Asserting only that it
    ends in `/skills/autodev/SKILL.md` would pass on any tree the driver happened to find,
    including this suite's own checkout."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "plugin-install"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(base)
    assert not (
        home / ".local" / "bin" / "conductor"
    ).exists()  # the fixture's whole point
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 0, (proc.stdout, proc.stderr, log)
    assert "driver-unresolved" not in log
    assert "fire-start" in log
    argv = argv_file.read_text().splitlines()
    root = conductor.parent.parent
    assert argv[-1] == f"Read {root}/skills/autodev/SKILL.md and execute it.", argv


def test_a_hanging_plugin_lookup_is_bounded_and_names_itself_in_the_log(
    tmp_path, monkeypatch
):
    """The lookup runs BEFORE `fire-start` is logged and BEFORE the flock is taken, so an
    unbounded one leaves a live process with no log line at all — and the next cron tick, twenty
    minutes later, starts another. Nothing about that is distinguishable from a healthy quiet
    run. Bound it, and give expiry its own line so the stall is attributable to this call rather
    than inferred from silence."""
    if not _which("bash"):
        pytest.skip("bash not available")
    if not _which("timeout"):
        pytest.skip("coreutils timeout not available")
    monkeypatch.setattr(codex_host, "PLUGIN_LIST_TIMEOUT_S", 1)
    base = tmp_path / "hang"
    base.mkdir()
    project, driver, home, argv_file, _c = _mk_codex_harness(
        base, plugin_list_hangs=True
    )

    started = time.monotonic()
    proc = _fire_driver(driver, home)
    elapsed = time.monotonic() - started

    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert "plugin-list-timeout" in log, log
    assert elapsed < 6, (elapsed, log)
    # ...and it still fails CLOSED: an unresolvable conductor is exit 3, never a fire.
    assert proc.returncode == 3, (proc.returncode, log)
    assert not argv_file.exists()


def test_a_rival_marketplaces_conductor_is_never_exec_d_by_the_driver(tmp_path):
    """Two installed plugins both called `conductor`, from two marketplaces. Keyed on the bare
    name the driver takes whichever `plugin list` printed first — an attacker only has to
    publish a plugin by that name to have cron exec its `bin/conductor` every twenty minutes,
    unattended, with the owner's posture flags. Conductor cannot rank marketplaces, so an
    ambiguous name must resolve to nothing and the guard must fire."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "rival"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(
        base, second_marketplace="evil-market"
    )

    proc = _fire_driver(driver, home)

    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 3, (proc.returncode, log)
    assert "driver-unresolved codex=" in log
    assert not argv_file.exists()


def test_a_disabled_conductor_plugin_is_never_exec_d_by_the_driver(tmp_path):
    """The operator disabled conductor. 0.147.0 still lists it, its tree is still on disk and
    its `bin/conductor` is still executable — so a driver that reads `installed[]` without the
    `enabled` bit fires the very plugin the operator turned off, every twenty minutes, from
    cron. Stop at the guard instead."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "disabled"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(
        base, conductor_plugin_disabled=True
    )
    assert conductor.exists()  # the fixture's point: present, executable, and OFF

    proc = _fire_driver(driver, home)

    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 3, (proc.returncode, log)
    assert "driver-unresolved codex=" in log
    assert not argv_file.exists()


def test_a_codex_fire_stops_loud_when_codex_knows_of_no_conductor_plugin(tmp_path):
    """Nothing on PATH, no CODEX_PLUGIN_ROOT, and codex reports no such plugin: the run must
    stop at the guard with the reason logged, never spawn codex with an unresolvable prompt."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "no-plugin"
    base.mkdir()
    project, driver, home, argv_file, _c = _mk_codex_harness(
        base, install_conductor_plugin=False
    )
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 3
    assert "driver-unresolved codex=" in log
    assert not argv_file.exists()
    assert "fire-start" not in log
    # ...and the skill path it reports must be EMPTY, not a tree derived from whatever cwd
    # happened to be. A cwd-derived answer is how this suite — which runs from inside a real
    # conductor checkout — makes an unresolved driver look resolved.
    assert "skill=/skills/autodev/SKILL.md" in log, log


def test_a_conductor_on_path_still_wins_over_the_plugin_lookup(tmp_path):
    """A dev checkout on PATH is a supported install too, and it must not be shadowed by
    whatever `codex plugin list` reports."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "on-path"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(base, on_path=True)
    skill = home / ".local" / "skills" / "autodev"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: autodev\n---\n")
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 0, (proc.stdout, proc.stderr, log)
    argv = argv_file.read_text().splitlines()
    assert argv[-1] == f"Read {skill}/SKILL.md and execute it.", argv


def test_a_stale_codex_plugin_root_never_overrides_the_resolved_bins_own_tree(tmp_path):
    """`CODEX_PLUGIN_ROOT` is an owner/environment variable nothing keeps current — a plugin
    uninstall or an upgrade leaves it naming a directory that is gone or empty. Taking it AHEAD
    of the resolved bin means a machine with a perfectly good `conductor` on PATH checks the
    stale tree and exits 3 on every fire, which contradicts this driver's own stated rule that
    the skill tree derives ONLY from a resolved bin."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "stale-root"
    base.mkdir()
    project, driver, home, argv_file, _c = _mk_codex_harness(base, on_path=True)
    skill = home / ".local" / "skills" / "autodev"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: autodev\n---\n")
    stale = base / "uninstalled-plugin"  # named, and not there
    assert not stale.exists()

    proc = _fire_driver(driver, home, extra_env={"CODEX_PLUGIN_ROOT": str(stale)})

    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 0, (proc.returncode, log)
    argv = argv_file.read_text().splitlines()
    assert argv[-1] == f"Read {skill}/SKILL.md and execute it.", argv


def test_a_codex_run_fails_loud_when_the_skill_tree_is_missing(tmp_path):
    """A prompt pointing at a nonexistent SKILL.md does not fail fast — it burns a whole
    context discovering the path is wrong. Fail before the fire, like an unresolved bin."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "noskill"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(base)
    os.remove(conductor.parent.parent / "skills" / "autodev" / "SKILL.md")
    proc = _fire_driver(driver, home)
    log = (project / ".conductor" / "resume-autodev.log").read_text()
    assert proc.returncode == 3
    assert "driver-unresolved codex=" in log
    assert not argv_file.exists()
    assert "fire-start" not in log


@pytest.mark.parametrize(
    "env_line,expected",
    [
        (None, "supervised"),
        ('CONDUCTOR_RESUME_CODEX_FLAGS="--sandbox read-only"', "supervised"),
        ('CONDUCTOR_RESUME_CODEX_FLAGS="--sandbox workspace-write"', "scoped"),
        ('CONDUCTOR_RESUME_CODEX_FLAGS="--approve-for-me"', "scoped"),
        (
            'CONDUCTOR_RESUME_CODEX_FLAGS="--dangerously-bypass-approvals-and-sandbox"',
            "full-bypass",
        ),
        (
            'CONDUCTOR_RESUME_CODEX_FLAGS="--sandbox workspace-write --dangerously-bypass-approvals-and-sandbox"',
            "full-bypass",
        ),
        (
            "CONDUCTOR_RESUME_CODEX_FLAGS=\"--cd '/tmp/a danger-full-access'\"",
            "supervised",
        ),
        # `--` ends codex's option parsing (verified against codex-cli 0.147.0): the flag
        # after it becomes the PROMPT positional and grants nothing.
        (
            'CONDUCTOR_RESUME_CODEX_FLAGS="-- --dangerously-bypass-approvals-and-sandbox"',
            "supervised",
        ),
        (
            'CONDUCTOR_RESUME_CODEX_FLAGS="--approve-for-me -- --dangerously-bypass-approvals-and-sandbox"',
            "scoped",
        ),
    ],
)
def test_a_codex_fire_labels_its_posture_from_codex_flags(tmp_path, env_line, expected):
    if not _which("bash"):
        pytest.skip("bash not available")
    name = f"posture{abs(hash((env_line, expected)))}"
    _proc, log, _argv = _fire_codex(tmp_path, name, env_line)
    lines = _posture_lines(log)
    assert lines, log
    assert any(f"posture={expected}" in ln for ln in lines), (expected, lines)


def test_the_shell_posture_derivation_agrees_with_its_python_mirror(tmp_path):
    """Two derivations of the same label drift; the log then misrepresents the fire. Every
    case above is checked against the adapter that renders the shell."""
    from conductor.hosts import base

    adapter = base.load("codex")
    for args, expected in (
        ([], "supervised"),
        (["--sandbox", "workspace-write"], "scoped"),
        (["--approve-for-me"], "scoped"),
        (["--dangerously-bypass-approvals-and-sandbox"], "full-bypass"),
        (["--", "--dangerously-bypass-approvals-and-sandbox"], "supervised"),
        (
            ["--approve-for-me", "--", "--dangerously-bypass-approvals-and-sandbox"],
            "scoped",
        ),
    ):
        assert adapter.posture_of(args) == expected


def test_a_codex_run_ignores_the_claude_flag_variable_entirely(tmp_path):
    """An owner who set CONDUCTOR_RESUME_CLAUDE_FLAGS on a machine that later runs Codex must
    not have those flags smuggled into a `codex exec` argv, and must not be labelled
    full-bypass for a posture Codex never granted."""
    if not _which("bash"):
        pytest.skip("bash not available")
    _proc, log, argv = _fire_codex(
        tmp_path,
        "leak",
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"',
    )
    assert "--dangerously-skip-permissions" not in argv, argv
    assert any("posture=supervised" in ln for ln in _posture_lines(log)), log


def test_a_codex_driver_holds_the_same_flock_and_gate_guards(tmp_path):
    """A second fire while one holds the lock must exit 0 without launching — the guard is
    host-neutral and must not have been lost in the rewrite."""
    if not _which("bash"):
        pytest.skip("bash not available")
    base = tmp_path / "gate"
    base.mkdir()
    project, driver, home, argv_file, conductor = _mk_codex_harness(base)
    conductor.write_text("#!/bin/sh\nexit 0\n")  # done-gate green -> no-op fire
    os.chmod(conductor, 0o755)
    proc = _fire_driver(driver, home)
    assert proc.returncode == 0
    assert not argv_file.exists()


# ---- A1: the write-time posture nudge follows the host -----------------------------------


@pytest.mark.parametrize(
    "host_id,flags_var",
    [
        ("claude", "CONDUCTOR_RESUME_CLAUDE_FLAGS"),
        ("codex", "CONDUCTOR_RESUME_CODEX_FLAGS"),
    ],
)
def test_the_write_nudge_names_the_hosts_own_flag_variable(
    host_id, flags_var, tmp_path, capsys
):
    from conductor.hosts import base

    project, worktree = _project_recorded_as(tmp_path, host_id)
    out = tmp_path / "resume-autodev.sh"
    rs.main(["write", "--project", project, "--worktree", worktree, "--out", str(out)])
    err = capsys.readouterr().err
    assert flags_var in err
    # and never the other host's variable, which would tell the owner to set something the
    # driver for this run does not read
    other = base.load(base.opposite(host_id)).FLAGS_VAR
    assert other not in err, err


@pytest.mark.parametrize(
    "host_id,decided_line",
    [
        ("claude", 'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"'),
        (
            "codex",
            'CONDUCTOR_RESUME_CODEX_FLAGS="--dangerously-bypass-approvals-and-sandbox"',
        ),
    ],
)
def test_a_decided_posture_silences_the_nudge_per_host(
    host_id, decided_line, tmp_path, capsys
):
    project, worktree = _project_recorded_as(tmp_path, host_id)
    out = tmp_path / "out" / "resume-autodev.sh"
    out.parent.mkdir()
    (out.parent / "resume-env.sh").write_text(decided_line + "\n")
    rs.main(["write", "--project", project, "--worktree", worktree, "--out", str(out)])
    assert "Pick a posture" not in capsys.readouterr().err


def test_the_other_hosts_decided_posture_does_not_silence_the_nudge(tmp_path, capsys):
    """A Claude posture in resume-env.sh decides nothing for a Codex run: that run still
    fires supervised and its owner still needs telling."""
    project, worktree = _project_recorded_as(tmp_path, "codex")
    out = tmp_path / "out" / "resume-autodev.sh"
    out.parent.mkdir()
    (out.parent / "resume-env.sh").write_text(
        'CONDUCTOR_RESUME_CLAUDE_FLAGS="--dangerously-skip-permissions"\n'
    )
    rs.main(["write", "--project", project, "--worktree", worktree, "--out", str(out)])
    assert "Pick a posture" in capsys.readouterr().err


@pytest.mark.parametrize(
    "host_id,own,foreign",
    [
        ("claude", "CONDUCTOR_RESUME_CLAUDE_FLAGS", "CONDUCTOR_RESUME_CODEX_FLAGS"),
        ("codex", "CONDUCTOR_RESUME_CODEX_FLAGS", "CONDUCTOR_RESUME_CLAUDE_FLAGS"),
    ],
)
def test_a_driver_documents_only_the_variables_its_own_fire_reads(
    host_id, own, foreign
):
    """The MIGRATION allowlist is the union — a driver being regenerated may carry either
    host's inline config and neither may be dropped. What a driver DOCUMENTS is narrower:
    naming a variable this run never reads is how an owner sets Claude flags on a Codex run
    and wonders why nothing changed."""
    from conductor.hosts import base

    vars_for = rs.owner_env_vars_for(base.load(host_id))
    assert own in vars_for
    assert foreign not in vars_for
    assert "CONDUCTOR_SPEC_ROOTS" in vars_for
    assert set(vars_for) < set(rs.OWNER_ENV_VARS)
