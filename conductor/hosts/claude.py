"""The Claude Code adapter. Every Claude-specific string in Conductor belongs here.

Everything below is transcribed from the shipped Tier-B driver rather than rewritten, and the
transcription is pinned byte-for-byte by ``tests/conductor/hosts/test_driver_text.py``. A
Claude-recorded run must render the same driver after A1 as before it — the current text is not
merely working, it is the text many live fires have proven, including the ones that caught the
2026-07-05 silent stall.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Mapping

from conductor.hosts import discovery, proc

#: Claude Code publishes the root of the plugin whose skill is executing. Codex has no
#: verified counterpart, which is why ``discovery.dev_plugin_roots`` takes the variable name
#: as an argument rather than reading one shared constant.
PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"

#: Overrides ``~/.claude``.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

#: The pid of the top-level Claude Code CLI process, exported into the environment of every
#: subprocess Claude spawns for a tool call. It is the ONLY durable identity this host offers:
#: Claude holds no advisory locks, and its messaging socket is not one-per-session (seven live
#: CLI processes, two socket files, measured).
#:
#: Three properties were established empirically before anything was built on it
#: (docs/reviews/2026-08-27-interactive-identity-probe.md §1):
#:
#: * it is IDENTICAL across separate tool calls and at every subagent depth — subagents are not
#:   separate OS processes, so there is exactly one identity per session, which is the right
#:   granularity for "do not fire while a human has a session open";
#: * it names a process that OUTLIVES a skill turn by a wide margin (16 days, in the session
#:   that measured it);
#: * a detached third-party observer, given only the resulting tuple, correctly answered LIVE
#:   for a running session and PROVABLY EXITED for both a dead pid and a simulated pid reuse.
#:
#: UNDOCUMENTED. Present as a literal in the shipped binary but absent from
#: code.claude.com/docs/en/env-vars, so it can be removed or repurposed in any release. Absence
#: is therefore a supported outcome, not a crash: ``session_identity`` returns ``None`` and the
#: caller refuses to claim ownership.
SESSION_PID_ENV = "CLAUDE_PID"

#: Recorded as a diagnostic breadcrumb only, never as identity. It is the same one-per-session
#: value as ``CLAUDE_PID`` with no liveness semantics of its own, and its transcript file
#: outlives the session by design — so treating it as identity would make a finished session
#: look like a live owner forever.
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"


class ClaudeAdapter:
    id: str = "claude"

    # ------------------------------------------------------------------ generated cron driver
    #
    # The fragments below are shell, not argv, because the thing that consumes them is a
    # generated bash script fired by cron. They may reference exactly what the driver defines
    # before them: ``$LOG``, ``ts()``, ``$PROJECT``, ``$WORKTREE``, ``$CONDUCTOR``, and this
    # adapter's own ``BIN_VAR``.

    #: The shell variable holding the resolved host executable.
    BIN_VAR: str = "CLAUDE_BIN"

    #: Where the owner opts into an unattended permission posture. The name keeps saying
    #: CLAUDE because the VALUE is Claude's flag vocabulary — one shared name across two hosts
    #: is how an owner's ``--dangerously-skip-permissions`` would end up in a ``codex exec``
    #: argv. Renaming it would also silently disarm every resume-env.sh already on disk.
    FLAGS_VAR: str = "CONDUCTOR_RESUME_CLAUDE_FLAGS"

    #: What the owner writes in ``resume-env.sh`` to choose an unattended posture, per posture.
    #: Used by the generated driver's opt-in comment AND by `resume-script write`'s nudge, so
    #: the two cannot suggest different flags for the same host.
    POSTURE_EXAMPLES: dict[str, str] = {
        "scoped": "--settings <path-to-scoped-settings.json>",
        "full-bypass": "--dangerously-skip-permissions",
    }
    POSTURE_NOTES: dict[str, str] = {
        "scoped": "least privilege: allowlist git/gh/pytest/ruff/pyright/conductor/docker",
        "full-bypass": "standing full-access posture; your explicit call, never defaulted.",
    }

    def resume_bin_resolution(self) -> str:
        """Resolve `claude` and `conductor` at RUN time. Never pin generation-time paths.

        The `~/.local/bin/claude` fallback is the claude 2.x standalone launcher: stable and
        unversioned, where a node-versioned path rots on the next upgrade and every headless
        fire then dies silently.
        """
        return (
            'CLAUDE_BIN="$(command -v claude || true)"\n'
            '[ -x "$CLAUDE_BIN" ] || CLAUDE_BIN="$HOME/.local/bin/claude"   '
            "# claude 2.x standalone launcher (stable, unversioned)\n"
            'CONDUCTOR="$(command -v conductor || true)"\n'
            '[ -x "$CONDUCTOR" ] || CONDUCTOR="$(ls -d "$HOME"/.claude/plugins/cache/*/conductor/*/bin/conductor 2>/dev/null | sort -V | tail -1)"'
        )

    def resume_unresolved_guard(self) -> str:
        """Fail LOUD if a bin is unresolvable — silence is the real defect."""
        return (
            'if [ ! -x "$CLAUDE_BIN" ] || [ ! -x "${CONDUCTOR:-}" ]; then\n'
            "    printf '%s driver-unresolved claude=%s conductor=%s\\n' "
            '"$(ts)" "$CLAUDE_BIN" "${CONDUCTOR:-}" >> "$LOG"\n'
            "    exit 3\n"
            "fi"
        )

    def resume_posture_arms(self) -> str:
        """`case` arms mapping the owner's parsed flags to a posture LABEL — detection only.

        Exact argv tokens, never substrings, so a flag VALUE that merely contains a flag-looking
        token cannot mislabel the fire. Bypass wins when both appear: the more privileged
        posture is the honest label. ``bypassPermissions`` counts only as the VALUE of a
        preceding ``--permission-mode``.
        """
        return (
            '        --dangerously-skip-permissions) POSTURE="full-bypass" ;;\n'
            '        --permission-mode=bypassPermissions) POSTURE="full-bypass" ;;\n'
            '        bypassPermissions) [ "$prev" = "--permission-mode" ] && POSTURE="full-bypass" ;;\n'
            '        --settings|--settings=*) [ "$POSTURE" = "full-bypass" ] || POSTURE="scoped" ;;'
        )

    def resume_fire_command(self) -> str:
        """One headless phase: ``claude -p "/conductor:autodev"`` plus the owner's flags.

        Byte-identical to the invocation live fires have proven. The owner's re-parsed flags
        follow the prompt because Claude takes the prompt as ``-p``'s value, not as a trailing
        positional — the opposite of Codex, which is why this line is not shared.
        """
        return '"$CLAUDE_BIN" -p "/conductor:autodev" "$@"'

    def posture_of(self, args: list[str]) -> str:
        """The Python mirror of ``resume_posture_arms``.

        Probe and driver must agree, or `resume-script write` re-nudges an owner who already
        decided — training them to ignore the nudge.
        """
        posture = "supervised"
        prev = ""
        for arg in args:
            if arg in (
                "--dangerously-skip-permissions",
                "--permission-mode=bypassPermissions",
            ) or (arg == "bypassPermissions" and prev == "--permission-mode"):
                posture = "full-bypass"
            elif (
                arg == "--settings" or arg.startswith("--settings=")
            ) and posture != "full-bypass":
                posture = "scoped"
            prev = arg
        return posture

    # --------------------------------------------------------------------- session identity
    #
    # NO PROCESS NAME IS READ HERE, and the omission is the point rather than an oversight.
    # Plan 04 originally specified that each adapter "matches a live process by its own
    # executable basename, which is ``adapter.id``". For Claude that predicate is FALSE FOR
    # EVERY REAL SESSION — the binary is named after its version, so `comm` and the `exe`
    # basename read `2.1.227`, never `claude` — and a `process_alive` built on it would answer
    # False for a live human session, letting the driver fire into an occupied worktree. The
    # specification was deleted rather than worked around; see that plan's §Task 7.
    #
    # `(pid, starttime, boot_id)` needs no name predicate. `starttime` answers "is this still
    # the process I recorded", which is the only question ownership asks, and "is it one of MY
    # host's processes" is answered by the record's `host` field before this method is reached.

    def session_identity(self, env: Mapping[str, str]) -> str | None:
        """``claude:<CLAUDE_PID>:<starttime-ticks>:<boot-id>`` for the session that owns ``env``.

        ``None`` when ``CLAUDE_PID`` is absent, unparseable, or names nothing readable. Absence
        is the fail-SAFE direction for registration: no identity means the caller must refuse to
        claim ownership, because a record whose liveness nothing can later check blocks the run
        forever or clears it wrongly.

        The pid comes from ``env``, NEVER from ``os.getpid()``. The process running this code is
        a ``conductor`` CLI call that will have exited within the second; the session it belongs
        to is what has to be excluded.
        """
        raw = env.get(SESSION_PID_ENV)
        if not raw:
            return None
        try:
            pid = int(raw.strip())
        except ValueError:
            return None
        return proc.pid_identity(self.id, pid)

    def process_alive(self, identity: str) -> bool | None:
        """Tri-state liveness for an identity recorded against this host.

        Two schemes are accepted and they are not interchangeable. ``claude:…`` is an
        interactive session, minted by ``session_identity``. ``proc:…`` is a plain local process
        this machine forked — the ``conductor heartbeat`` wrapper that supervises a fire for its
        whole life — and it is host-agnostic by construction, so it must be answerable on a run
        recorded against either host.

        Anything else, including a well-formed identity in the OTHER host's scheme, is ``None``.
        A codex thread id says nothing about a pid, and guessing would answer a question about a
        process this adapter cannot identify.
        """
        scheme = identity.split(":", 1)[0] if ":" in identity else ""
        if scheme in (self.id, proc.LOCAL_SCHEME):
            return proc.pid_identity_liveness(identity, scheme=scheme)
        return None

    # ------------------------------------------------------------------------- session + host

    #: Affirmative EXACT matches only — substring or prefix matching would let an ambiguous or
    #: token-embedded mode string over-grant ("bypassPermissions extra" MUST stay supervised).
    _BYPASS_MODES = frozenset({"bypassPermissions"})
    _MODE_POSTURE = {
        "default": "supervised",
        "plan": "supervised",
        "acceptEdits": "scoped",
    }

    def session_posture(self, mode: str) -> str:
        """A detected Claude session permission mode -> this run's posture. Fail-closed:
        anything not affirmatively recognized resolves to supervised."""
        if mode in self._BYPASS_MODES:
            return "full-bypass"
        return self._MODE_POSTURE.get(mode, "supervised")

    def scheduled_tasks_file(self) -> str | None:
        """The Claude harness's scheduled-task file — the second durability leg for
        `conductor driver status`, alongside the crontab marker."""
        cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        return os.path.join(cfg, "scheduled_tasks.json")

    # ------------------------------------------------------------- discovery + command naming

    #: Claude resolves plugin-level dependencies: ``.claude-plugin/plugin.json`` carries a
    #: ``dependencies`` array, so installing conductor pulls spec-craft with it.
    resolves_plugin_dependencies: bool = True

    def source_root(self) -> str:
        return os.environ.get(CONFIG_DIR_ENV) or os.path.expanduser("~/.claude")

    def native_invocation(self, skill: str) -> str:
        """``conductor:autodev`` -> ``/conductor:autodev``.

        Claude dispatches the slash form itself, and the plugin qualifier is load-bearing:
        two plugins may ship a skill of the same name.
        """
        return skill if skill.startswith("/") else f"/{skill}"

    def discovered_commands(self, *, project_root: str | None = None) -> set[str]:
        return self.host_skills(project_root=project_root).commands

    def host_skills(self, *, project_root: str | None = None) -> discovery.HostSkills:
        """Invocable command names on this machine. User skills bare, plugin skills and
        commands as ``<plugin>:<name>``.

        ``unverifiable_plugins`` is always empty here, and that is a property of Claude rather
        than a stub: nothing on this host REPORTS an installed plugin separately from the tree
        that holds it. The cache glob below IS the report, so a plugin whose directory is gone
        is indistinguishable from one that was never installed — there is no third state to
        surface. Codex has one because its CLI answers with an identity and the tree that
        identity implies is a separate fact that can be false.

        The marketplace-cache leg derives the plugin name from the cache PATH segment
        (``plugins/cache/<marketplace>/<plugin>/<version>/``) rather than from the manifest,
        which is what Conductor's preflight has always done. The two can disagree, and
        changing which one wins is a behaviour change A1 has no business making.
        """
        home = self.source_root()
        cmds = discovery.skill_names(f"{home}/skills/*/SKILL.md")
        for path in glob.glob(f"{home}/plugins/cache/*/*/*/skills/*/SKILL.md"):
            parts = path.split(os.sep)
            plugin = parts[parts.index("cache") + 2]
            cmds.add(f"{plugin}:{os.path.basename(os.path.dirname(path))}")
        for path in glob.glob(f"{home}/plugins/cache/*/*/*/commands/*.md"):
            parts = path.split(os.sep)
            plugin = parts[parts.index("cache") + 2]
            cmds.add(f"{plugin}:{os.path.basename(path)[:-3]}")
        cmds |= discovery.scan_plugin_dir(
            discovery.CONDUCTOR_ROOT, discovery.ALL_MANIFEST_DIRS
        )
        for root in discovery.dev_plugin_roots(PLUGIN_ROOT_ENV):
            cmds |= discovery.scan_plugin_dir(root, (f".{self.id}-plugin",))
        return discovery.HostSkills(cmds, frozenset())
