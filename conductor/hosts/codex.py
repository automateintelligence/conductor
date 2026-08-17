"""The Codex CLI adapter. Every Codex-specific string in Conductor belongs here.

Verified against codex-cli 0.147.0 on 2026-08-12; see
``docs/reviews/2026-08-12-codex-host-ground-truth.md``. Every flag used below comes from
``codex exec --help`` at that version. Nothing is inferred from published documentation, and
nothing unverified is guessed at — ``codex exec resume`` and ``codex exec review`` exist but
their argument contracts were not established, so this module does not use them.

Written from scratch against that help output, NOT adapted from ``claude.py``. ``-p`` is
``--profile`` here: a prompt passed after ``-p`` would make Codex look for
``$CODEX_HOME/<prompt>.config.toml``, fail to find it, and present as a model-selection bug.
The token ``-p`` therefore does not appear in this file at all, and a test enforces that.
"""

from __future__ import annotations

import os

from conductor.hosts import discovery

#: ``--ignore-user-config`` skips ``config.toml`` but auth still uses ``CODEX_HOME`` (ground
#: truth §"Session and config isolation"), so this is the Codex config root unconditionally.
CONFIG_DIR_ENV = "CODEX_HOME"


class CodexAdapter:
    id: str = "codex"

    # ------------------------------------------------------------------ generated cron driver
    #
    # Shell fragments for the generated Tier-B driver. They may reference what the driver
    # defines before them: ``$LOG``, ``ts()``, ``$PROJECT``, ``$WORKTREE``, ``$CONDUCTOR``.

    #: The shell variable holding the resolved host executable.
    BIN_VAR: str = "CODEX_BIN"

    #: Codex's own owner-flag variable. Deliberately NOT shared with Claude's: the value is a
    #: sandbox vocabulary (``--sandbox``, ``--approve-for-me``) that has no Claude analogue,
    #: and one name for two vocabularies is how the wrong host's flags reach an argv.
    FLAGS_VAR: str = "CONDUCTOR_RESUME_CODEX_FLAGS"

    #: What the owner writes in ``resume-env.sh`` to choose an unattended posture, per posture.
    #: Codex's axis is the sandbox, so these are sandbox values, not permission modes.
    POSTURE_EXAMPLES: dict[str, str] = {
        "scoped": "--sandbox workspace-write",
        "full-bypass": "--dangerously-bypass-approvals-and-sandbox",
    }
    POSTURE_NOTES: dict[str, str] = {
        "scoped": "least privilege: writes confined to the workspace, approvals still apply",
        "full-bypass": "no sandbox and no approvals; your explicit call, never defaulted.",
    }

    def resume_bin_resolution(self) -> str:
        """Resolve `codex`, `conductor`, and conductor's skill tree at RUN time.

        The installed Codex plugin cache is deliberately NOT globbed. The only root ever
        observed was ``$CODEX_HOME/.tmp/plugins``, a ``.tmp`` path no documentation makes
        contractual, and baking an unverified layout is exactly the rot
        ``resume_script._ROT_PATTERNS`` exists to detect. ``CODEX_PLUGIN_ROOT`` is the escape
        hatch; otherwise the skill tree is derived from the resolved conductor bin
        (``<root>/bin/conductor`` -> ``<root>/skills/...``), which cannot go stale because it
        is computed on every fire.
        """
        return (
            'CODEX_BIN="$(command -v codex || true)"\n'
            'CONDUCTOR="$(command -v conductor || true)"\n'
            '[ -x "$CONDUCTOR" ] || [ -z "${CODEX_PLUGIN_ROOT:-}" ] || CONDUCTOR="$CODEX_PLUGIN_ROOT/bin/conductor"\n'
            "# Conductor's skill tree, derived from the bin at RUN time — never a baked path.\n"
            'CONDUCTOR_SOURCE="${CODEX_PLUGIN_ROOT:-$(cd "$(dirname "$(readlink -f "${CONDUCTOR:-.}" 2>/dev/null || printf \'%s\' "${CONDUCTOR:-.}")")/.." 2>/dev/null && pwd)}"'
        )

    def resume_unresolved_guard(self) -> str:
        """Fail LOUD if a bin or the skill file is unresolvable — silence is the real defect.

        The skill file is part of the check because Codex has no host-dispatched slash command:
        the fire IS an instruction to read that file, so a missing one does not fail fast, it
        burns a whole context discovering the path is wrong.
        """
        return (
            'if [ ! -x "$CODEX_BIN" ] || [ ! -x "${CONDUCTOR:-}" ] || '
            '[ ! -f "${CONDUCTOR_SOURCE:-}/skills/autodev/SKILL.md" ]; then\n'
            "    printf '%s driver-unresolved codex=%s conductor=%s skill=%s\\n' "
            '"$(ts)" "$CODEX_BIN" "${CONDUCTOR:-}" "${CONDUCTOR_SOURCE:-}/skills/autodev/SKILL.md" >> "$LOG"\n'
            "    exit 3\n"
            "fi"
        )

    def resume_posture_arms(self) -> str:
        """`case` arms mapping the owner's parsed flags to a posture LABEL — detection only.

        Codex's sandbox is a graded axis where Claude's posture is a mode plus a settings file;
        the two do not map onto each other, so this table is derived from ``codex exec --help``
        and not from Claude's. ``--approve-for-me`` counts as scoped because it auto-approves
        under a workspace-write sandbox — labelling that supervised would be the audit
        misrepresentation the posture line exists to prevent.

        The first arm is the ``--`` terminator, and ``break`` inside a ``case`` inside the
        driver's ``for`` loop ends the scan. Verified against codex-cli 0.147.0:
        ``codex exec --cd /tmp -- --dangerously-bypass-approvals-and-sandbox <prompt>`` does not
        apply that flag — it takes it as the PROMPT positional and then rejects the driver's own
        prompt as a second positional, exit 2. A token after ``--`` grants nothing, so labelling
        the fire from it is an audit line claiming a privilege Codex never gave.
        """
        return (
            "        --) break ;;\n"
            '        --dangerously-bypass-approvals-and-sandbox) POSTURE="full-bypass" ;;\n'
            '        --sandbox=danger-full-access) POSTURE="full-bypass" ;;\n'
            '        danger-full-access) case "$prev" in --sandbox|-s) POSTURE="full-bypass" ;; esac ;;\n'
            '        --sandbox=workspace-write|--approve-for-me) [ "$POSTURE" = "full-bypass" ] || POSTURE="scoped" ;;\n'
            '        workspace-write) case "$prev" in --sandbox|-s) [ "$POSTURE" = "full-bypass" ] || POSTURE="scoped" ;; esac ;;'
        )

    def resume_fire_command(self) -> str:
        """One headless phase: ``codex exec --cd <worktree> <owner flags> <prompt>``.

        ``--cd`` names the workspace explicitly because Codex otherwise infers it from cwd. The
        prompt is a trailing positional, so the owner's flags go BEFORE it — the opposite order
        from Claude, which is why this line is not shared with ``claude.py``.

        The prompt names the absolute SKILL.md path instead of ``$conductor:autodev``. That
        dollar form is a ``~/.codex/AGENTS.md`` prompting convention the MODEL expands, not a
        host dispatch primitive: on a machine without that table it resolves to nothing at all,
        and inside double quotes the shell would eat it first. Every entry in such a table
        expands to exactly the instruction below, so nothing is lost by writing it out.
        """
        return (
            '"$CODEX_BIN" exec --cd "$WORKTREE" "$@" '
            '"Read $CONDUCTOR_SOURCE/skills/autodev/SKILL.md and execute it."'
        )

    def posture_of(self, args: list[str]) -> str:
        """The Python mirror of ``resume_posture_arms``. Exact tokens, bypass wins.

        Stops at ``--`` for the same reason the shell arm does: Codex parses nothing after the
        terminator as a flag, so nothing after it can raise the posture.
        """
        posture = "supervised"
        prev = ""
        for arg in args:
            if arg == "--":
                break
            if arg in (
                "--dangerously-bypass-approvals-and-sandbox",
                "--sandbox=danger-full-access",
            ) or (arg == "danger-full-access" and prev in ("--sandbox", "-s")):
                posture = "full-bypass"
            elif (
                arg in ("--sandbox=workspace-write", "--approve-for-me")
                or (arg == "workspace-write" and prev in ("--sandbox", "-s"))
            ) and posture != "full-bypass":
                posture = "scoped"
            prev = arg
        return posture

    # ------------------------------------------------------------------------- session + host

    #: Codex's sandbox axis, which is what a Codex session's permission state actually is.
    #: Claude's mode names are absent on purpose: permissions do not transfer between hosts, so
    #: a stray ``bypassPermissions`` must resolve supervised here, not full-bypass.
    _MODE_POSTURE = {
        "read-only": "supervised",
        "workspace-write": "scoped",
        "danger-full-access": "full-bypass",
    }

    def session_posture(self, mode: str) -> str:
        """A detected Codex sandbox mode -> this run's posture. Fail-closed."""
        return self._MODE_POSTURE.get(mode, "supervised")

    def scheduled_tasks_file(self) -> str | None:
        """Codex has no verified harness scheduled-task file, so it has no such leg.

        Ground truth §"Things NOT determined" records no Codex analogue of Claude's
        ``scheduled_tasks.json``. Guessing a path would either false-green durability or read
        an unrelated file; returning None removes the leg for this host, and the crontab marker
        remains the durability evidence.
        """
        return None

    # ------------------------------------------------------------- discovery + command naming

    #: Codex does NOT resolve plugin-level dependencies. Verified against codex-cli 0.147.0:
    #: the 180 ``.codex-plugin/plugin.json`` manifests in the installed curated catalog use
    #: exactly twelve fields and ``dependencies`` is not among them, and Codex accepts unknown
    #: fields without complaint — so adding one would be silently inert and actively
    #: misleading. Preflight reads this to tell a Codex user to install spec-craft by hand
    #: instead of discovering mid-run that a conducted skill resolves to nothing.
    resolves_plugin_dependencies: bool = False

    def source_root(self) -> str:
        return os.environ.get(CONFIG_DIR_ENV) or os.path.expanduser("~/.codex")

    def native_invocation(self, skill: str) -> str:
        """``conductor:autodev`` -> ``$autodev``.

        Two things this is NOT. It is not a host dispatch primitive: ``$name`` is a prompting
        convention the *model* interprets by reading ``AGENTS.md``, and it expands to "read
        this SKILL.md path and execute it" (ground truth §"Skill invocation under Codex").
        That is why the driver's fire command writes the path out instead of using this form;
        this one names a skill to a reader who supplies the convention. And it is not
        namespaced: Codex skill directories are flat under ``$CODEX_HOME/skills/`` and
        ``./.codex/skills/``, with no counterpart to Claude's plugin qualifier, so the
        qualifier is dropped rather than transliterated into a name that resolves to nothing.
        """
        return skill if skill.startswith("$") else f"${skill.rsplit(':', 1)[-1]}"

    def discovered_commands(self, *, project_root: str | None = None) -> set[str]:
        """Invocable command names on this machine, all bare.

        Three verified roots: ``$CODEX_HOME/skills/``, ``$CODEX_HOME/prompts/`` (the analogue
        of Claude's slash commands), and the project-local ``./.codex/skills/`` the
        ``AGENTS.md`` dispatch table resolves.

        The installed Codex *marketplace* cache is deliberately not searched, for the same
        reason ``resume_bin_resolution`` does not glob it: the only observed root was
        ``$CODEX_HOME/.tmp/plugins``. A preflight that greens on a guessed layout is worse
        than one that reports the command missing. A3 is where Conductor gets a Codex catalog
        entry at all, and it can add the leg once the layout is contractual.
        """
        home = self.source_root()
        project = project_root or os.getcwd()
        cmds = discovery.skill_names(f"{home}/skills/*/SKILL.md")
        cmds |= discovery.command_names(f"{home}/prompts/*.md")
        cmds |= discovery.skill_names(f"{project}/.{self.id}/skills/*/SKILL.md")
        cmds |= discovery.scan_plugin_dir(
            discovery.CONDUCTOR_ROOT, discovery.ALL_MANIFEST_DIRS
        )
        for root in discovery.dev_plugin_roots():
            cmds |= discovery.scan_plugin_dir(root, (f".{self.id}-plugin",))
        return cmds
