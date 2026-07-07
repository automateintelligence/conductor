"""Tier-B resume-driver generator: the runtime-resolution contract that fixes the 2026-07-05
silent-stall (generation-time-pinned bins that rot on upgrade)."""

import os
import re
import shlex
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
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
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


def test_install_cron_on_a_non_repo_fails_with_a_named_reason(tmp_path, monkeypatch, capsys):
    _stub_crontab(tmp_path, monkeypatch)
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    assert rs.main(["install-cron", "--project", str(not_repo)]) == 1
    assert "cannot resolve main root" in capsys.readouterr().err
