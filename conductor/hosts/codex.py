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

import json
import os
import shutil
import subprocess
import textwrap

from conductor.hosts import discovery

#: ``--ignore-user-config`` skips ``config.toml`` but auth still uses ``CODEX_HOME`` (ground
#: truth §"Session and config isolation"), so this is the Codex config root unconditionally.
CONFIG_DIR_ENV = "CODEX_HOME"

#: Seconds ``codex plugin list --json`` gets before it is killed. A healthy 0.147.0 answers it
#: from local config and cache in well under a second, so this is generous by two orders of
#: magnitude; what it exists to bound is the pathological case. One constant for both callers —
#: the in-process one and the shell the cron driver runs — because a bound that holds in only
#: one of them is the unbounded case wherever it does not.
PLUGIN_LIST_TIMEOUT_S = 20

#: Seconds between the TERM that asks the lookup to stop and the KILL that makes it. A bound
#: expressed only as a TERM is not a bound: a child that traps or ignores the signal keeps
#: running and its supervisor keeps waiting, which is exactly what `timeout <n>` with no
#: `--kill-after` does — probed here, `timeout 1` around a TERM-ignoring process returned 124
#: only after the child's full three seconds. Both bounded paths below therefore escalate.
PLUGIN_LIST_KILL_GRACE_S = 5

#: Where an installed plugin's copy lives, under the Codex config root. NOT emitted by
#: ``codex plugin list --json`` — see ``plugin_roots_from_json`` — so it is derived from three
#: fields that ARE, and then checked on disk.
_INSTALL_CACHE = ("plugins", "cache")

#: The plugin-root lookup as a self-contained program the generated cron
#: driver can run before any conductor code is importable — that is the whole problem it solves,
#: so it cannot import from here. Reads ``codex plugin list --json`` on stdin, takes a plugin
#: name in argv, prints that plugin's INSTALLED root (or nothing). Single quotes are forbidden
#: inside it: the driver wraps it in shell single quotes.
PLUGIN_ROOT_SNIPPET = (
    "import json,os,sys;"
    'h=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex");'
    'e=[p for p in json.load(sys.stdin).get("installed") or [] if p.get("name")==sys.argv[1]'
    ' and p.get("enabled") is True and p.get("marketplaceName") and p.get("version")];'
    'd=[os.path.join(h,"plugins","cache",p["marketplaceName"],p["name"],p["version"])'
    " for p in e];"
    "d=sorted(set(x for x in d if os.path.isdir(x)));"
    'print(d[0] if len(d)==1 else "")'
)


def config_root() -> str:
    """The Codex config root: ``$CODEX_HOME``, else ``~/.codex``."""
    return os.environ.get(CONFIG_DIR_ENV) or os.path.expanduser("~/.codex")


def _installed_root(entry: dict, home: str) -> str | None:
    """One ``installed[]`` entry -> the directory Codex actually LOADS, or None.

    ``source.path`` is not it. Verified on codex-cli 0.147.0: ``source`` is copied straight out
    of the marketplace manifest, so it names the tree the plugin was fetched FROM
    (``<marketplace root>/plugins/<name>``), while installing copies that tree somewhere else
    and the loader reads the copy. Nothing in the ``--json`` output names the copy — the fields
    are ``pluginId``, ``name``, ``marketplaceName``, ``version``, ``installed``, ``enabled``,
    ``source``, ``marketplaceSource``, ``installPolicy``, ``authPolicy``, and the closest of
    them, ``marketplaceSource.source``, is the marketplace root rather than the install root.

    So the root is DERIVED from three emitted fields and then required to exist. The layout
    ``$CODEX_HOME/plugins/cache/<marketplaceName>/<name>/<version>`` is what ``codex plugin add``
    itself reports as ``Installed plugin root:`` for every entry of the recorded artefact in
    ``tests/conductor/fixtures/``. Deriving a layout is exactly what this module otherwise
    refuses to do, and the ``isdir`` check is the reason it is admissible here: a Codex that
    moves the cache makes the derived path stop existing, so this answers "no root" — which
    degrades the gate to ``unverified`` — instead of silently naming a wrong directory the way
    ``source.path`` did.
    """
    # DISABLED is not installed, for every purpose Conductor has. 0.147.0 keeps a disabled
    # plugin in ``installed[]`` with ``"enabled": false`` and its tree on disk, but its loader
    # returns before loading any capability — so its skills resolve to nothing at run time, and
    # the cron driver pointing ``$CONDUCTOR`` at its ``bin/conductor`` execs the very plugin the
    # operator turned off. ``is True`` rather than truthiness: a version that stops emitting the
    # field leaves us unable to tell, and "cannot tell" has to read as "do not use it".
    if entry.get("enabled") is not True:
        return None
    name = entry.get("name")
    market = entry.get("marketplaceName")
    version = entry.get("version")
    if not all(isinstance(v, str) and v for v in (name, market, version)):
        return None
    root = os.path.join(home, *_INSTALL_CACHE, str(market), str(name), str(version))
    return root if os.path.isdir(root) else None


def plugin_roots_from_json(text: str) -> dict[str, str]:
    """``codex plugin list --json`` output -> ``{plugin name: INSTALLED root}``.

    Shape verified against codex-cli 0.147.0 by recording the CLI's own output; see
    ``tests/conductor/fixtures/README.md``. Nothing here raises — malformed or unexpected output
    means "this machine reports no plugin identities", which is a legitimate answer that
    ``preflight`` degrades on, not an error.

    A name claimed by MORE THAN ONE installed root is dropped. ``name`` is a string a plugin
    declares about itself; ``pluginId``/``marketplaceName`` are the identity Codex actually keys
    on, and two marketplaces may ship ``conductor`` at once. Keeping the first — which is what
    listing order decides, and 0.147.0 lists the rival first — attributes a stranger's copied
    skill as the required plugin's and points the cron driver's ``$CONDUCTOR`` at its ``bin/``.
    Conductor has no marketplace trust list and inventing one is not this function's job, so the
    honest answer for an ambiguous name is none: preflight reports the requirement
    ``unverified`` and the driver stops at its guard. Roots that are not on disk are excluded
    BEFORE the count, so two listings of which only one is really installed still resolve.
    """
    return {
        name: next(iter(roots))
        for name, roots in _claims_from_json(text).items()
        if len(roots) == 1
    }


def contested_roots_from_json(text: str) -> list[str]:
    """The installed roots of every name MORE THAN ONE of them claims, sorted.

    Their contents are still invocable — Codex's skill namespace is flat, so both plugins' skills
    resolve by bare name — but no plugin claim survives the collision. Discovery contributes them
    unqualified, which is what makes ``preflight`` report the requirement ``unverified`` rather
    than ``missing``: the skill IS there, and telling an owner to install a plugin that is
    already installed twice is advice that teaches them to ignore the gate.
    """
    return sorted(
        root
        for roots in _claims_from_json(text).values()
        if len(roots) > 1
        for root in roots
    )


def _claims_from_json(text: str) -> dict[str, set[str]]:
    """``{plugin name: every enabled, on-disk install root claiming it}``."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    home = config_root()
    claims: dict[str, set[str]] = {}
    for entry in (data.get("installed") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        root = _installed_root(entry, home)
        if root:
            claims.setdefault(str(entry["name"]), set()).add(root)
    return claims


def installed_plugins() -> tuple[dict[str, str], list[str]]:
    """``({attributable name: root}, [contested roots])``, or two empties if Codex cannot be
    asked.

    Asking the host is the only way to attribute a skill to a plugin on Codex: skill directories
    are flat and carry no plugin qualifier, so nothing on disk says whose a skill is. What comes
    back is an IDENTITY, which ``_installed_root`` turns into a path and then checks — the host
    is asked the question only it can answer, and the layout that answer implies is verified
    rather than trusted. ``</dev/null`` because Codex subcommands hang on an unredirected stdin
    (ground truth §"Codex help hangs"); a timeout because a preflight that hangs is worse than
    one that reports less.

    ONE invocation answers both halves. Splitting them into two functions would mean shelling
    out to the CLI twice per preflight and, worse, letting the two answers come from different
    moments — a plugin installed in between would be attributable to one and contested to the
    other.
    """
    exe = shutil.which("codex")
    if not exe:
        return {}, []
    try:
        proc = subprocess.run(
            [exe, "plugin", "list", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PLUGIN_LIST_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, []
    if proc.returncode != 0:
        return {}, []
    return plugin_roots_from_json(proc.stdout), contested_roots_from_json(proc.stdout)


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

        A Codex user installs conductor as a PLUGIN, and an installed plugin's bin is not on
        PATH (``skills/start/SKILL.md`` says so). Nothing installs a shim and nothing persists
        ``CODEX_PLUGIN_ROOT``, so PATH plus that variable resolve nothing on a normal install
        and the first cron fire dies at the guard before Codex is ever spawned.

        The third leg therefore ASKS CODEX which plugins are installed, via
        ``codex plugin list --json`` (verified against codex-cli 0.147.0 — ``--json`` is a
        documented flag of that subcommand), and derives the install root from the identity
        fields that answer carries. It is NOT read off ``source.path``: that field is
        marketplace metadata naming the tree the plugin was fetched from, and pointing
        ``$CONDUCTOR`` at it execs a copy Codex does not load — see ``_installed_root``, which
        is the single place that rule lives and the single place the derived root is checked to
        exist. ``</dev/null`` because Codex subcommands hang on an unredirected stdin (ground
        truth §"Codex help hangs"), and under cron a hang is a stuck worker rather than a failed
        one. ``python3`` is not a new dependency: ``bin/conductor`` execs it for every
        subcommand, so a machine that cannot run it cannot run conductor.

        The lookup is BOUNDED (``plugin_list_lookup``) and stays BEFORE the lock. Bounded
        because it runs before the
        first log line and before ``flock``, so a hung CLI is a live fire that has written
        nothing at all and every subsequent tick starts another — the silent-block class this
        driver otherwise exists to close. Before the lock because ``$CONDUCTOR`` is what
        ``resume_unresolved_guard`` reads: moving the lookup after ``flock -n`` would put the
        exit-3 guard inside the critical section, so a machine that cannot resolve conductor
        would take the project lock to say so, and every following tick would hit ``flock -n``
        and exit 0 SILENTLY instead of logging ``driver-unresolved`` — trading a loud per-tick
        failure for no output at all. Position only mattered while the call was unbounded: what
        made a pre-lock hang accumulate was that it could outlive the twenty-minute tick, and
        a bound of ``PLUGIN_LIST_TIMEOUT_S`` removes that outright.

        The skill tree is derived from whichever bin won (``<root>/bin/conductor`` ->
        ``<root>/skills/...``), which cannot go stale because it is computed on every fire. It
        is derived ONLY from a resolved bin — never from ``.``, and never from
        ``$CODEX_PLUGIN_ROOT`` — so an unresolved fire reports an empty path instead of
        inventing one, and a resolved fire checks the tree belonging to the bin it is about to
        name. ``$CODEX_PLUGIN_ROOT`` participates one line earlier, as a candidate BIN that must
        pass ``-x``; letting it also name the tree let a stale value beat a bin that resolved,
        so every fire exited 3 against a directory the bin had nothing to do with.
        """
        return (
            'CODEX_BIN="$(command -v codex || true)"\n'
            'CONDUCTOR="$(command -v conductor || true)"\n'
            '[ -x "$CONDUCTOR" ] || [ -z "${CODEX_PLUGIN_ROOT:-}" ] || CONDUCTOR="$CODEX_PLUGIN_ROOT/bin/conductor"\n'
            "# Installed as a plugin? Ask codex which plugins are installed — a CLI contract,\n"
            "# not a guessed cache layout, so a cache move or a version bump cannot rot it.\n"
            "# BOUNDED: this runs before `fire-start` is logged and before the flock is taken,\n"
            "# so an unbounded call leaves a live fire that has written NOTHING, and the next\n"
            "# tick starts another. Expiry gets its own log line — a stall nobody can see is\n"
            "# the failure mode, not the stall itself.\n"
            'if [ ! -x "${CONDUCTOR:-}" ] && [ -x "${CODEX_BIN:-}" ]; then\n'
            + textwrap.indent(self.plugin_list_lookup(), "    ")
            + "\n"
            '    CODEX_CONDUCTOR_DIR="$(printf \'%s\' "$CODEX_PLUGIN_JSON" '
            f"| python3 -c '{PLUGIN_ROOT_SNIPPET}' conductor 2>/dev/null || true)\"\n"
            '    [ -z "$CODEX_CONDUCTOR_DIR" ] || CONDUCTOR="$CODEX_CONDUCTOR_DIR/bin/conductor"\n'
            "fi\n"
            "# Conductor's skill tree, derived from the RESOLVED bin at RUN time — never a\n"
            "# baked path, never from `.` (a cwd-derived tree is a wrong answer that looks like\n"
            "# a right one whenever the fire happens to start inside some checkout), and never\n"
            "# from $CODEX_PLUGIN_ROOT: nothing keeps that variable current, so an uninstall or\n"
            "# an upgrade leaves it naming a tree that is gone while a perfectly good bin sits\n"
            "# on PATH. It gets a say in WHICH BIN wins, above; the tree then follows the bin.\n"
            'CONDUCTOR_SOURCE=""\n'
            '[ ! -x "${CONDUCTOR:-}" ] || '
            'CONDUCTOR_SOURCE="$(cd "$(dirname "$(readlink -f "$CONDUCTOR" 2>/dev/null || printf \'%s\' "$CONDUCTOR")")/.." 2>/dev/null && pwd)"'
        )

    def plugin_list_lookup(self) -> str:
        """The BOUNDED ``codex plugin list --json`` call, as a standalone shell fragment.

        Reads ``$CODEX_BIN``, ``$PROJECT``, ``$LOG`` and ``ts()``; sets ``CODEX_PLUGIN_JSON``
        and ``CODEX_PLUGIN_RC``. Its own method because the branch below — the machine with no
        ``timeout`` binary — is unreachable from a test that fires the whole driver: the driver
        re-adds ``/usr/bin:/bin`` to ``PATH`` before resolving anything, so no test PATH can
        hide coreutils from it. That is precisely how the branch came to be the one that ran
        with no ceiling at all. Extracted, it can be run under a PATH a test fully controls,
        verbatim, rather than asserted about as text.

        Two bounds, one ceiling. ``timeout`` gets ``-k``: a bound expressed only as a TERM is
        not a bound, because a child that traps or ignores the signal keeps running and
        ``timeout`` keeps waiting for it — probed at ``timeout 1`` around a TERM-ignoring
        process, which returned 124 only after the child's full three seconds. Without
        coreutils the SAME bound is built out of the shell's own parts (background, poll to the
        deadline, TERM, then KILL) rather than left to run forever behind a log line no
        consumer reads. Refusing the lookup outright was the third option and is the wrong one:
        it would break every plugin-installed conductor on a bare macOS, which is a working
        configuration.
        """
        return (
            'CODEX_TIMEOUT="$(command -v timeout || command -v gtimeout || true)"\n'
            'if [ -n "$CODEX_TIMEOUT" ]; then\n'
            f'    CODEX_PLUGIN_JSON="$("$CODEX_TIMEOUT" -k {PLUGIN_LIST_KILL_GRACE_S} '
            f"{PLUGIN_LIST_TIMEOUT_S} "
            '"$CODEX_BIN" plugin list --json </dev/null 2>/dev/null)"\n'
            "    CODEX_PLUGIN_RC=$?\n"
            "else\n"
            "    # No coreutils timeout (a bare macOS). The same ceiling, out of the shell's own\n"
            "    # parts. The scratch file sits beside this script in the owner's `.conductor`,\n"
            "    # never at a predictable name under a world-writable /tmp that another user\n"
            "    # could pre-symlink and have this write through.\n"
            '    CODEX_PLUGIN_OUT="$PROJECT/.conductor/plugin-list.$$"\n'
            '    "$CODEX_BIN" plugin list --json </dev/null >"$CODEX_PLUGIN_OUT" 2>/dev/null &\n'
            "    CODEX_PLUGIN_PID=$!\n"
            "    CODEX_PLUGIN_WAITED=0\n"
            f'    while [ "$CODEX_PLUGIN_WAITED" -lt {PLUGIN_LIST_TIMEOUT_S} ] && '
            'kill -0 "$CODEX_PLUGIN_PID" 2>/dev/null; do\n'
            "        sleep 1\n"
            "        CODEX_PLUGIN_WAITED=$((CODEX_PLUGIN_WAITED + 1))\n"
            "    done\n"
            '    if kill -0 "$CODEX_PLUGIN_PID" 2>/dev/null; then\n'
            '        kill -TERM "$CODEX_PLUGIN_PID" 2>/dev/null || true\n'
            f"        sleep {PLUGIN_LIST_KILL_GRACE_S}\n"
            '        kill -KILL "$CODEX_PLUGIN_PID" 2>/dev/null || true\n'
            '        wait "$CODEX_PLUGIN_PID" 2>/dev/null || true\n'
            "        CODEX_PLUGIN_RC=124\n"
            "    else\n"
            '        wait "$CODEX_PLUGIN_PID"\n'
            "        CODEX_PLUGIN_RC=$?\n"
            "    fi\n"
            '    CODEX_PLUGIN_JSON="$(cat "$CODEX_PLUGIN_OUT" 2>/dev/null || true)"\n'
            '    rm -f "$CODEX_PLUGIN_OUT"\n'
            "fi\n"
            "# 124 is an expiry `timeout` reported; 137 is one it had to escalate to KILL. Both\n"
            "# are the lookup being cut off, and `driver status` reads this line.\n"
            'case "$CODEX_PLUGIN_RC" in\n'
            "    124|137) printf '%s plugin-list-timeout bin=%s limit=%ss rc=%s\\n' "
            f'"$(ts)" "$CODEX_BIN" {PLUGIN_LIST_TIMEOUT_S} "$CODEX_PLUGIN_RC" >> "$LOG" ;;\n'
            "esac"
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
        return config_root()

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

        Installed PLUGIN skills come back qualified, because Codex itself supplies the
        attribution: ``codex plugin list --json`` reports each installed plugin's identity, and
        its skills live under the root that identity implies. Without it no Codex machine can
        distinguish spec-craft's ``expectations`` from any other plugin's.

        A plugin NAME that two installed roots claim is the exception, and it comes back BARE.
        Both roots' skills really are invocable — the Codex skill namespace is flat — so
        dropping them would report a present skill missing; but neither root's claim on the name
        survives the other, so qualifying them would hand a stranger's copy the required
        plugin's identity. Bare is the third answer: present, unattributed, ``unverified``.
        """
        home = self.source_root()
        project = project_root or os.getcwd()
        cmds = discovery.skill_names(f"{home}/skills/*/SKILL.md")
        cmds |= discovery.command_names(f"{home}/prompts/*.md")
        cmds |= discovery.skill_names(f"{project}/.{self.id}/skills/*/SKILL.md")
        attributed, contested = installed_plugins()
        for name, root in attributed.items():
            cmds |= discovery.qualified(name, root)
        for root in contested:
            cmds |= discovery.plugin_contents(root)
        cmds |= discovery.scan_plugin_dir(
            discovery.CONDUCTOR_ROOT, discovery.ALL_MANIFEST_DIRS
        )
        for root in discovery.dev_plugin_roots():
            cmds |= discovery.scan_plugin_dir(root, (f".{self.id}-plugin",))
        return cmds
