"""Everything the generated cron driver needs that differs between hosts (A1).

The driver is shell, not argv, so what the adapters hand it is shell text. The rule from
``base``'s module docstring still holds and is enforced here structurally: **no launch text is
built by shared code.** ``-p`` is ``--print`` to Claude and ``--profile`` to Codex, so one
templated string is wrong exactly once, silently, and presents as a model-selection bug. Each
adapter writes its own fire line in its own module.

The Claude fragments are pinned byte-for-byte against the shipped v4 driver. That is the whole
"do not regress Claude" guarantee expressed as an assertion: if a refactor prettifies the
Claude text, these fail before anyone's live run does.
"""

from __future__ import annotations

import inspect
import os
import pathlib

import pytest

from conductor.hosts import base

# Literal, never `base.HOST_IDS`: parametrizing over the value under test would let a falsifier
# that shrinks HOST_IDS silently DELETE cases instead of failing them.
HOSTS = ("claude", "codex")


def test_the_host_matrix_covers_exactly_the_supported_hosts():
    assert HOSTS == base.HOST_IDS


# --- the structural rule: no shared launch text ---------------------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize(
    "member",
    [
        "resume_bin_resolution",
        "resume_unresolved_guard",
        "resume_posture_arms",
        "resume_fire_command",
        "posture_of",
        "session_posture",
        "scheduled_tasks_file",
    ],
)
def test_each_adapter_writes_its_own_host_text(host_id, member):
    """No shared base class, no mixin, no format string with the host id in it. Someone
    "removing the duplication" between the two fire lines fails this."""
    fn = getattr(type(base.load(host_id)), member)
    assert inspect.isfunction(fn), member
    assert fn.__module__ == f"conductor.hosts.{host_id}", (member, fn.__module__)


def test_the_codex_module_contains_no_dash_p_token_at_all():
    """A tripwire, not a style rule: `-p` in codex.py means `--profile`, and the only reason to
    write it there is to have copied a Claude vector."""
    from conductor.hosts import codex

    text = pathlib.Path(codex.__file__).read_text(encoding="utf-8")
    assert '"-p"' not in text and "'-p'" not in text and " -p " not in text


# --- Claude's fragments are the shipped v4 driver, byte for byte -----------------------------

CLAUDE_BIN_RESOLUTION = (
    'CLAUDE_BIN="$(command -v claude || true)"\n'
    '[ -x "$CLAUDE_BIN" ] || CLAUDE_BIN="$HOME/.local/bin/claude"   '
    "# claude 2.x standalone launcher (stable, unversioned)\n"
    'CONDUCTOR="$(command -v conductor || true)"\n'
    '[ -x "$CONDUCTOR" ] || CONDUCTOR="$(ls -d "$HOME"/.claude/plugins/cache/*/conductor/*/bin/conductor 2>/dev/null | sort -V | tail -1)"'
)

CLAUDE_GUARD = (
    'if [ ! -x "$CLAUDE_BIN" ] || [ ! -x "${CONDUCTOR:-}" ]; then\n'
    '    printf \'%s driver-unresolved claude=%s conductor=%s\\n\' "$(ts)" "$CLAUDE_BIN" "${CONDUCTOR:-}" >> "$LOG"\n'
    "    exit 3\n"
    "fi"
)

CLAUDE_FIRE = '"$CLAUDE_BIN" -p "/conductor:autodev" "$@"'


def test_claude_bin_resolution_is_the_shipped_text():
    assert base.load("claude").resume_bin_resolution() == CLAUDE_BIN_RESOLUTION


def test_claude_unresolved_guard_is_the_shipped_text():
    assert base.load("claude").resume_unresolved_guard() == CLAUDE_GUARD


def test_claude_fire_command_is_the_proven_production_invocation():
    assert base.load("claude").resume_fire_command() == CLAUDE_FIRE


def test_claude_posture_arms_are_the_shipped_detection_table():
    arms = base.load("claude").resume_posture_arms()
    assert (
        arms
        == """        --dangerously-skip-permissions) POSTURE="full-bypass" ;;
        --permission-mode=bypassPermissions) POSTURE="full-bypass" ;;
        bypassPermissions) [ "$prev" = "--permission-mode" ] && POSTURE="full-bypass" ;;
        --settings|--settings=*) [ "$POSTURE" = "full-bypass" ] || POSTURE="scoped" ;;"""
    )


# --- Codex's fragments spawn codex, and say so ----------------------------------------------


def test_codex_bin_resolution_resolves_the_codex_binary_and_never_claude():
    text = base.load("codex").resume_bin_resolution()
    assert 'CODEX_BIN="$(command -v codex || true)"' in text
    assert "claude" not in text


def test_codex_fire_command_uses_exec_and_never_dash_p():
    fire = base.load("codex").resume_fire_command()
    assert fire.startswith('"$CODEX_BIN" exec ')
    assert " -p " not in fire and "--profile" not in fire
    assert "--cd" in fire
    assert "claude" not in fire


def test_codex_fire_command_names_the_skill_file_it_reads():
    """`$conductor:autodev` is a ~/.codex/AGENTS.md prompting convention (third-party), not a
    Codex host dispatch primitive: on a machine without that table it resolves to nothing. The
    prompt is the expansion every such table entry has anyway."""
    fire = base.load("codex").resume_fire_command()
    assert "skills/autodev/SKILL.md" in fire
    assert "$conductor:autodev" not in fire


def test_codex_resolves_its_skill_root_at_run_time_never_at_generation_time():
    """A generation-time absolute path is the exact rot `_ROT_PATTERNS` exists to detect: the
    next plugin upgrade moves the directory and every headless fire dies silently."""
    text = base.load("codex").resume_bin_resolution()
    fire = base.load("codex").resume_fire_command()
    assert "CONDUCTOR_SOURCE=" in text
    assert "$CONDUCTOR_SOURCE/skills/autodev/SKILL.md" in fire
    assert os.path.isabs(fire.split("$CONDUCTOR_SOURCE")[0].split()[-1]) is False


def test_codex_finds_a_plugin_installed_conductor_by_asking_codex_not_by_guessing():
    """An installed plugin's bin is not on PATH, so PATH + CODEX_PLUGIN_ROOT resolve nothing on
    a normal install. The third leg asks the host — a documented CLI surface — and specifically
    does NOT encode the one cache root ever observed, which no documentation makes contractual
    and which carries a version segment that would rot on the next upgrade."""
    text = base.load("codex").resume_bin_resolution()
    assert "plugin list --json" in text
    assert "</dev/null" in text  # codex subcommands hang on an unredirected stdin
    assert ".tmp/plugins" not in text
    assert "$CODEX_HOME" not in text


def test_codex_never_derives_its_skill_root_from_the_current_directory():
    """`readlink -f .` answers with whatever directory the fire started in. Inside any conductor
    checkout that answer looks valid, which is how an unresolved driver reports a resolved
    tree — and how a test suite that runs from such a checkout cannot tell the two apart."""
    text = base.load("codex").resume_bin_resolution()
    assert "CONDUCTOR:-." not in text
    assert '[ ! -x "${CONDUCTOR:-}" ] || CONDUCTOR_SOURCE=' in text


def test_the_plugin_lookup_snippet_is_self_contained_and_shell_quotable():
    """It runs BEFORE any conductor code is importable — that is the problem it solves — and the
    driver wraps it in shell single quotes, so it must contain none."""
    from conductor.hosts import codex

    assert "'" not in codex.PLUGIN_ROOT_SNIPPET
    assert "conductor" not in codex.PLUGIN_ROOT_SNIPPET  # the name comes from argv
    assert f"'{codex.PLUGIN_ROOT_SNIPPET}' conductor" in codex.CodexAdapter().resume_bin_resolution()


def test_codex_guard_fails_loud_when_the_skill_file_is_missing():
    guard = base.load("codex").resume_unresolved_guard()
    assert "driver-unresolved codex=" in guard
    assert "skills/autodev/SKILL.md" in guard
    assert "exit 3" in guard
    assert "claude" not in guard


# --- no host's text ever carries the other host's vocabulary --------------------------------


@pytest.mark.parametrize("host_id", HOSTS)
def test_no_hosts_driver_text_leaks_the_other_hosts_flags(host_id):
    adapter = base.load(host_id)
    text = "\n".join(
        [
            adapter.resume_bin_resolution(),
            adapter.resume_unresolved_guard(),
            adapter.resume_posture_arms(),
            adapter.resume_fire_command(),
        ]
    )
    foreign = (
        ["--sandbox", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me"]
        if host_id == "claude"
        else ["--dangerously-skip-permissions", "--permission-mode", "--settings"]
    )
    for token in foreign:
        assert token not in text, (host_id, token)


@pytest.mark.parametrize(
    "host_id,bin_var,flags_var",
    [
        ("claude", "CLAUDE_BIN", "CONDUCTOR_RESUME_CLAUDE_FLAGS"),
        ("codex", "CODEX_BIN", "CONDUCTOR_RESUME_CODEX_FLAGS"),
    ],
)
def test_each_host_names_its_own_shell_and_owner_env_variables(
    host_id, bin_var, flags_var
):
    """CONDUCTOR_RESUME_CLAUDE_FLAGS keeps its name: it holds CLAUDE permission flags, and one
    shared name for two incompatible flag vocabularies is how an owner's
    --dangerously-skip-permissions ends up in a `codex exec` argv."""
    adapter = base.load(host_id)
    assert adapter.BIN_VAR == bin_var
    assert adapter.FLAGS_VAR == flags_var
    assert bin_var in adapter.resume_fire_command()


# --- posture derivation, per host, and non-transferable --------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], "supervised"),
        (["--dangerously-skip-permissions"], "full-bypass"),
        (["--permission-mode=bypassPermissions"], "full-bypass"),
        (["--permission-mode", "bypassPermissions"], "full-bypass"),
        (["--settings", "/tmp/s.json"], "scoped"),
        (["--settings=/tmp/s.json"], "scoped"),
        (
            ["--settings", "/tmp/s.json", "--dangerously-skip-permissions"],
            "full-bypass",
        ),
        (["--settings", "/tmp/x--dangerously-skip-permissions.json"], "scoped"),
        (["bypassPermissions"], "supervised"),
    ],
)
def test_claude_posture_derivation(args, expected):
    assert base.load("claude").posture_of(args) == expected


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], "supervised"),
        (["--sandbox", "read-only"], "supervised"),
        (["--dangerously-bypass-approvals-and-sandbox"], "full-bypass"),
        (["--sandbox", "danger-full-access"], "full-bypass"),
        (["--sandbox=danger-full-access"], "full-bypass"),
        (["-s", "danger-full-access"], "full-bypass"),
        (["--sandbox", "workspace-write"], "scoped"),
        (["--sandbox=workspace-write"], "scoped"),
        (["-s", "workspace-write"], "scoped"),
        (["--approve-for-me"], "scoped"),
        (
            [
                "--sandbox",
                "workspace-write",
                "--dangerously-bypass-approvals-and-sandbox",
            ],
            "full-bypass",
        ),
        (["--cd", "/tmp/danger-full-access"], "supervised"),
        (["workspace-write"], "supervised"),
        # `--` ends option parsing: everything after it is a POSITIONAL, never a flag.
        (["--", "--dangerously-bypass-approvals-and-sandbox"], "supervised"),
        (["--", "--sandbox", "workspace-write"], "supervised"),
        (["--", "--approve-for-me"], "supervised"),
        # tokens BEFORE the terminator are real flags and still count
        (
            ["--approve-for-me", "--", "--dangerously-bypass-approvals-and-sandbox"],
            "scoped",
        ),
    ],
)
def test_codex_posture_derivation(args, expected):
    assert base.load("codex").posture_of(args) == expected


def test_a_flag_after_the_terminator_grants_nothing_and_must_not_be_labelled():
    """Verified against codex-cli 0.147.0: `codex exec --cd /tmp -- <flag> <prompt>` does not
    apply <flag>. It takes it as the PROMPT positional, then rejects the driver's own generated
    prompt as a second positional and exits 2. Labelling that fire `full-bypass` is an audit log
    that claims a privilege Codex never granted — and `_posture_decided` then suppresses the
    permissions nudge on the strength of the lie."""
    adapter = base.load("codex")
    assert (
        adapter.posture_of(["--", "--dangerously-bypass-approvals-and-sandbox"])
        == "supervised"
    )
    assert "--) break ;;" in adapter.resume_posture_arms()


def test_a_bypass_flag_from_the_other_host_never_grants_bypass():
    """Permissions do not transfer between hosts. A Claude bypass flag reaching a Codex fire
    must label — and behave as — supervised, not full-bypass."""
    assert (
        base.load("codex").posture_of(["--dangerously-skip-permissions"])
        == "supervised"
    )
    assert (
        base.load("claude").posture_of(["--dangerously-bypass-approvals-and-sandbox"])
        == "supervised"
    )
    assert (
        base.load("claude").posture_of(["--sandbox", "danger-full-access"])
        == "supervised"
    )


# --- session permission mode -> posture, per host --------------------------------------------


@pytest.mark.parametrize(
    "host_id,mode,expected",
    [
        ("claude", "bypassPermissions", "full-bypass"),
        ("claude", "acceptEdits", "scoped"),
        ("claude", "default", "supervised"),
        ("claude", "plan", "supervised"),
        ("claude", "danger-full-access", "supervised"),
        ("codex", "danger-full-access", "full-bypass"),
        ("codex", "workspace-write", "scoped"),
        ("codex", "read-only", "supervised"),
        ("codex", "bypassPermissions", "supervised"),
        ("codex", "acceptEdits", "supervised"),
    ],
)
def test_session_posture_is_each_hosts_own_permission_vocabulary(
    host_id, mode, expected
):
    assert base.load(host_id).session_posture(mode) == expected


@pytest.mark.parametrize("host_id", HOSTS)
@pytest.mark.parametrize("mode", ["", "  ", "bypassPermissions extra", "nonsense"])
def test_session_posture_fails_closed_on_anything_unrecognized(host_id, mode):
    assert base.load(host_id).session_posture(mode) == "supervised"


# --- the harness scheduled-task durability leg ----------------------------------------------


def test_claude_names_its_harness_scheduled_tasks_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert base.load("claude").scheduled_tasks_file() == str(
        tmp_path / "scheduled_tasks.json"
    )


def test_claude_scheduled_tasks_file_defaults_under_the_claude_config_home(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert base.load("claude").scheduled_tasks_file() == str(
        tmp_path / ".claude" / "scheduled_tasks.json"
    )


def test_codex_has_no_scheduled_tasks_file_rather_than_a_guessed_one():
    """No Codex harness scheduled-task file was verified (ground truth §"Things NOT
    determined"). Guessing a path would either false-green durability or read a stranger's
    file; returning None removes the leg for this host."""
    assert base.load("codex").scheduled_tasks_file() is None
