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

import glob
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import textwrap
import time
from collections.abc import Mapping

from conductor.hosts import base, discovery, proc

#: ``--ignore-user-config`` skips ``config.toml`` but auth still uses ``CODEX_HOME`` (ground
#: truth §"Session and config isolation"), so this is the Codex config root unconditionally.
CONFIG_DIR_ENV = "CODEX_HOME"

#: The live session's thread id, exported into the environment of every shell Codex runs.
#:
#: CONFIRMED FOR THE INTERACTIVE TUI, not merely for ``codex exec``. The identity probe could
#: only establish the ``codex exec`` case — two pty attempts never reached a prompt — and left
#: the TUI as an explicit unknown, which mattered because the scenario this whole contract
#: exists for IS a human's interactive session. It was closed on 2026-08-27 by driving
#: ``codex --dangerously-bypass-approvals-and-sandbox <prompt>`` under a pty that answers the
#: terminal capability queries the TUI blocks on (DSR, primary DA, the kitty-keyboard query and
#: the OSC 10/11 colour queries) and then the directory-trust modal. The captured tool-shell
#: environment contained ``CODEX_THREAD_ID=01a0443d-186a-7902-800c-d7cebe222d2b``, and the lock
#: file that session created was named for exactly that value. So the Codex half of the
#: contract has an entry point on the host it needs one on, and the ancestry fallback below is
#: a backstop rather than the primary path.
#:
#: UNDOCUMENTED, and more fragile than Claude's ``CLAUDE_PID``: one literal in the binary, on a
#: 0.x CLI whose own documentation disclaims listing internal variables. Absence is a supported
#: outcome, never a crash.
SESSION_THREAD_ENV = "CODEX_THREAD_ID"

#: Directory under the Codex config root holding one zero-byte file per thread, on which the
#: live Codex process holds an exclusive ``flock`` for the session's lifetime.
#:
#: This is the strongest liveness primitive either host offers, and it is why the Codex identity
#: is NOT forced into Claude's ``<host>:<pid>:<ticks>`` shape. It gives two POSITIVE exit proofs
#: rather than one: a clean exit DELETES the file, and a ``SIGKILL`` leaves the file behind while
#: the kernel drops the lock — both verified, the second by killing a live session and watching
#: its ``/proc/locks`` entry disappear while the file remained.
#:
#: An implementation detail with no documentation whatsoever, hence
#: ``THREAD_LOCK_DIR_MISSING`` below: an absent directory is "cannot tell", never "not running".
THREAD_LOCK_DIR = "thread-writer-locks"

#: Thread ids are UUID-shaped. The pattern is a SAFETY boundary, not validation politeness: the
#: value becomes a path segment under ``THREAD_LOCK_DIR``, so anything containing a separator or
#: a traversal component must be refused rather than joined. It also excludes the non-thread
#: bookkeeping files that share the directory — a ``.coordination.lock`` sits beside the thread
#: locks and would otherwise be read as a thread id by the ancestry walk.
_THREAD_ID_RE = re.compile(r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")

#: This module's host id, declared once. The module-level probe below reports it in its failure
#: report (design §"Failure handling": a pre-run capability probe names the host id and the
#: project root in place of a run identity it cannot have), and ``CodexAdapter.id`` is the same
#: fact — so they are the same declaration rather than two literals that can drift apart.
HOST_ID = "codex"

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

#: Longest skill name Codex accepts (``MAX_NAME_LEN``, ``codex-rs/skills/src/parser.rs`` at
#: rust-v0.155.0).
SKILL_NAME_MAX_CHARS = 64


def codex_skill_names(pattern: str) -> set[str]:
    """The names Codex would load a ``.../skills/*/SKILL.md`` glob under — the FALLBACK rule.

    ``parse_skill_frontmatter_metadata`` at rust-v0.155.0, restated: a ``---`` frontmatter block
    with a closing ``---`` is required; ``name`` is the declared one (whitespace collapsed) or
    the directory name when none is declared, and at most 64 characters; ``description`` is
    required and non-empty. Anything else Codex reports as an error and does not load, so it is
    not counted. gstack depends on the declared-name half: its Codex installer links
    ``$CODEX_HOME/skills/gstack-claude/`` declaring ``name: claude``. Claude Code names user
    skills by directory, so none of this applies to the Claude adapter.

    Used only when Codex's own catalog (``skills_list``) cannot be had. Codex parses the
    frontmatter as YAML; ``discovery.frontmatter`` is a stdlib line reader, which agrees on the
    frontmatter skills are written in and is the reason this is the fallback, not the source.
    """
    names = set()
    for path in glob.glob(pattern):
        fields = discovery.frontmatter(path)
        if fields is None:
            continue
        name = " ".join(fields.get("name", "").split()) or os.path.basename(
            os.path.dirname(path)
        )
        description = " ".join(fields.get("description", "").split())
        if description and len(name) <= SKILL_NAME_MAX_CHARS:
            names.add(name)
    return names


_SKILL_NAMER: discovery.SkillNamer = codex_skill_names

#: Seconds ``codex app-server`` gets to answer ``skills/list``. Measured on 0.155.0: about 1.5s
#: against a populated ``$CODEX_HOME`` and 0.9s against an empty one, so this bounds the
#: pathological case, as ``PLUGIN_LIST_TIMEOUT_S`` does for its probe.
SKILLS_LIST_TIMEOUT_S = 20

#: The ``skills/list`` request id, and the one response line that answers it.
_SKILLS_LIST_ID = 1


def _skills_list_request(cwd: str) -> bytes:
    """The JSON-RPC exchange ``codex app-server`` needs before it answers ``skills/list``."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"clientInfo": {"name": "conductor-preflight", "version": "0"}},
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {
            "jsonrpc": "2.0",
            "id": _SKILLS_LIST_ID,
            "method": "skills/list",
            "params": {"cwds": [cwd]},
        },
    ]
    return "".join(json.dumps(m) + "\n" for m in messages).encode()


def skills_from_response(message: object) -> list[dict] | None:
    """The skill entries of a ``skills/list`` response, or None when it is not one.

    None — never an empty list — for an error response or an unexpected shape, because "Codex
    lists no skills" is an answer and "this is not an answer" is a different fact: the caller
    falls back to the filesystem on the second and trusts the first.
    """
    if not isinstance(message, dict) or "error" in message:
        return None
    result = message.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        return None
    skills: list[dict] = []
    for entry in data:
        listed = entry.get("skills") if isinstance(entry, dict) else None
        if not isinstance(listed, list):
            return None
        skills += [
            s for s in listed if isinstance(s, dict) and isinstance(s.get("name"), str)
        ]
    return skills


def skills_list(project_root: str | None = None) -> list[dict] | None:
    """Codex's own skill catalog for ``project_root``, from ``codex app-server``'s
    ``skills/list``; None when Codex cannot give one; ``HostProbeTimeout`` when it never answers.

    WHY ASK CODEX RATHER THAN SCAN. The catalog is what a ``$`` mention resolves against, and
    reproducing it means reproducing ``resolve_skill_roots`` and ``SkillNamespaceResolver``
    (``codex-rs/ext/skills/src`` at rust-v0.155.0): user roots ``$CODEX_HOME/skills`` and
    ``~/.agents/skills``, the system cache, admin and trusted-project config layers, repo
    ``.agents/skills`` between the project root and cwd, installed plugin roots, a recursive
    walk that follows directory symlinks, a ``<namespace>:`` taken from the nearest valid plugin
    manifest above each skill (which is how ``~/.agents/skills/superpowers`` becomes
    ``superpowers:<skill>``), per-skill ``enabled`` config, and a YAML validity rule. Every piece
    of that restated here is a piece that drifts silently with the next release. The catalog
    carries all of it by construction.

    WHY IT IS SAFE FOR A BARE CRON. No auth is needed; it answers from local config and disk.
    It runs as a child of this process with stdin, stdout and a deadline under our control; the
    run is killed — the whole process group, since a Codex server may have spawned helpers — if
    no answer arrives in ``SKILLS_LIST_TIMEOUT_S``. ``HOME`` must be set, as it must for Codex to
    find ``~/.agents`` at all.

    The server does not answer a request it has already seen EOF behind, so the exchange is
    written, the answer read, and only then stdin closed.
    """
    project = project_root or os.getcwd()
    exe = shutil.which(HOST_ID)
    if not exe:
        return None
    try:
        child = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    deadline = time.monotonic() + SKILLS_LIST_TIMEOUT_S
    expired = False
    try:
        assert child.stdin is not None and child.stdout is not None
        try:
            child.stdin.write(_skills_list_request(project))
            child.stdin.flush()
        except OSError:
            return None
        pending = b""
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    expired = True
                    raise base.HostProbeTimeout(
                        f"`codex app-server` did not answer within {SKILLS_LIST_TIMEOUT_S}s when "
                        f"asked for `skills/list`, and was killed; conductor wrote nothing. "
                        f"host={HOST_ID} project_root={project} — this is a pre-run capability "
                        f"probe, so it names the host it could not ask and the project it was "
                        f"asked about in place of a run key. Codex could not be asked which "
                        f"skills it loads, so every one is unknown rather than absent."
                    )
                if not selector.select(remaining):
                    continue
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    return None  # exited without answering: not a catalog
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(message, dict)
                        and message.get("id") == _SKILLS_LIST_ID
                    ):
                        return skills_from_response(message)
    finally:
        _end(child, grace=0 if expired else PLUGIN_LIST_KILL_GRACE_S)


def _end(child: subprocess.Popen, *, grace: float) -> None:
    """Close the exchange and make sure nothing of it outlives this call.

    Closing stdin is how a server that answered is asked to exit; ``grace`` is how long it gets
    to do so. An expired probe gets none — it already had its bound.
    """
    for stream in (child.stdin, child.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        child.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        child.kill()
    child.wait()


def catalog_names(skills: list[dict]) -> tuple[set[str], frozenset[str]]:
    """``(invocable names, contested namespaces)`` from a ``skills/list`` catalog.

    A disabled skill is not invocable. A qualified name listed at more than one path is
    contested: both answer to ``$<namespace>:<skill>`` and which one Codex runs is not something
    conductor can establish, so its namespace travels with the plugin-list collisions.
    """
    names: set[str] = set()
    paths: dict[str, set[str]] = {}
    for skill in skills:
        if skill.get("enabled") is False:
            continue
        name = skill["name"]
        names.add(name)
        paths.setdefault(name, set()).add(str(skill.get("path")))
    contested = frozenset(
        name.split(":", 1)[0]
        for name, where in paths.items()
        if ":" in name and len(where) > 1
    )
    return names, contested


#: Characters that may follow the first one in a plugin-identity segment, beyond ASCII
#: alphanumerics. Declared beside the predicate because ``PLUGIN_ROOT_SNIPPET`` spells the same
#: set and the two are compared by a test that RUNS both.
_ID_EXTRA_CHARS = "._-"


def is_plugin_id_segment(value: object) -> bool:
    """Whether ``value`` may be joined into the install cache as ONE path component.

    ``marketplaceName``, ``name`` and ``version`` arrive as JSON over a pipe, from a binary this
    machine merely happens to have, and ``_derived_root`` joins all three into a path under
    ``$CODEX_HOME/plugins/cache``. ``os.path.join`` drops everything before an ABSOLUTE
    component, ``..`` climbs out, and a value carrying a separator spends two levels of the
    layout at once — after which ``isdir`` blesses whatever is really at the escaped path and the
    driver's shell mirror points ``$CONDUCTOR`` at its ``bin/conductor``. Reproduced with an
    absolute ``marketplaceName``.

    Codex validates these segments itself (``plugin/src/plugin_id.rs`` at rust-v0.147.0), and
    that is exactly why this exists rather than why it does not: an upstream check Conductor
    cannot enforce is not a check Conductor may rely on, and the answer that matters here is what
    the string is allowed to MEAN as a path, which is a question about this side.

    ASCII alphanumeric first, then alphanumerics and ``._-``. Absolute paths, ``..``, ``.``,
    anything with a separator, the empty string and every non-string are refused, and so is every
    dotfile and option-shaped name — none of which is an install root. Mirrored verbatim in
    ``PLUGIN_ROOT_SNIPPET``.
    """
    return (
        isinstance(value, str)
        and value != ""
        and value[0].isascii()
        and value[0].isalnum()
        and all((c.isascii() and c.isalnum()) or c in _ID_EXTRA_CHARS for c in value)
    )


#: The plugin-root lookup as a self-contained program the generated cron
#: driver can run before any conductor code is importable — that is the whole problem it solves,
#: so it cannot import from here. Reads ``codex plugin list --json`` on stdin, takes a plugin
#: name in argv, prints that plugin's INSTALLED root (or nothing). Single quotes are forbidden
#: inside it: the driver wraps it in shell single quotes.
#:
#: The two ``isinstance`` guards mirror ``_claims_from_json`` exactly, and they are the whole
#: reason the mirror is stated twice rather than assumed: without them this comprehension called
#: ``.get()`` on every element of ``installed[]`` and on the parsed document itself, so a single
#: unexpected element — ``{"installed":[null, <a valid conductor entry>]}`` — made this program
#: die with ``AttributeError`` and resolve nothing while the Python parser returned the valid
#: root. Preflight greened and the cron driver stopped, which is the one failure shape neither
#: side can see from where it stands.
PLUGIN_ROOT_SNIPPET = (
    "import json,os,sys;"
    'h=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex");'
    "j=json.load(sys.stdin);"
    # `is_plugin_id_segment`, verbatim. Written without a regex so it carries no backslash: the
    # driver wraps this whole program in shell single quotes.
    'ok=lambda v: isinstance(v,str) and v!="" and v[0].isascii() and v[0].isalnum()'
    ' and all((c.isascii() and c.isalnum()) or c in "'
    + _ID_EXTRA_CHARS
    + '" for c in v);'
    'e=[p for p in ((j.get("installed") if isinstance(j,dict) else None) or [])'
    ' if isinstance(p,dict) and p.get("name")==sys.argv[1]'
    ' and p.get("enabled") is True and ok(p.get("name"))'
    ' and ok(p.get("marketplaceName")) and ok(p.get("version"))];'
    'a=sorted(set(os.path.join(h,"plugins","cache",p["marketplaceName"],p["name"],p["version"])'
    " for p in e));"
    "d=[x for x in a if os.path.isdir(x)];"
    "print(d[0]) if len(d)==1 else sys.exit(3 if a and not d else 2)"
)

#: What ``PLUGIN_ROOT_SNIPPET`` exits with, and the only way it can report its third state.
#: Its stdout is a path the driver is about to exec, so it cannot also carry "codex listed this
#: plugin and its root is gone" — but the driver must be able to say that instead of reporting
#: the same bare `driver-unresolved` a genuinely uninstalled plugin gets. Mirrors
#: ``unverifiable_plugins_from_json``.
PLUGIN_ROOT_RC_UNVERIFIABLE = 3
PLUGIN_ROOT_RC_NONE = 2


def config_root() -> str:
    """The Codex config root: ``$CODEX_HOME``, else ``~/.codex``."""
    return os.environ.get(CONFIG_DIR_ENV) or os.path.expanduser("~/.codex")


def _derived_root(entry: dict, home: str) -> str | None:
    """One ``installed[]`` entry -> the directory its IDENTITY implies, unchecked, or None.

    Split from ``_installed_root`` because "codex reports this plugin installed and enabled" and
    "the tree that claim implies is there" are two different facts, and collapsing them into one
    None discarded the difference between a plugin that is absent and one whose install root
    moved. Preflight needs both to tell an owner which of those it is.
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
    # VALIDATED BEFORE JOINING, never after: an absolute component makes `os.path.join` discard
    # the cache root entirely, and the `isdir` check below would then bless the escaped path.
    if not all(is_plugin_id_segment(v) for v in (name, market, version)):
        return None
    return os.path.join(home, *_INSTALL_CACHE, str(market), str(name), str(version))


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
    root = _derived_root(entry, home)
    return root if root and os.path.isdir(root) else None


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


def contested_claims_from_json(text: str) -> dict[str, list[str]]:
    """``{plugin name: its installed roots, sorted}`` for every name MORE THAN ONE root claims.

    Their contents are still invocable — Codex names each plugin's skills ``<name>:<skill>``, so
    both roots answer to the same qualified name — but no plugin claim survives the collision.
    Discovery reports the name as contested, which is what makes ``preflight`` report the
    requirement ``unverified`` rather than ``missing``: the skill IS there, and telling an owner
    to install a plugin that is already installed twice is advice that teaches them to ignore
    the gate.
    """
    return {
        name: sorted(roots)
        for name, roots in _claims_from_json(text).items()
        if len(roots) > 1
    }


def unverifiable_plugins_from_json(text: str) -> frozenset[str]:
    """Names Codex REPORTS as installed and enabled but whose install root is not on disk.

    The third state, and the reason the gate has three. Codex's answer carries an identity, and
    the layout that identity implies is checked rather than trusted (``_installed_root``) — but
    when the check fails the claim was simply dropped, which made a moved cache and an
    uninstalled plugin the same answer. It is not: preflight then reported ``missing`` and told
    the owner to install ``spec-craft``, a plugin they already have, while the actual fault went
    unnamed. Reported here so ``unverified`` can say which of the two it is.

    A name with ANY on-disk root is excluded: two listings of which one is really installed
    resolve normally, and a name whose roots merely collide is ``contested``, not this.
    """
    entries = _entries_from_json(text)
    home = config_root()
    reported = {
        str(e["name"]) for e in entries if _derived_root(e, home) and e.get("name")
    }
    return frozenset(reported - set(_claims_from_json(text)))


def _entries_from_json(text: str) -> list[dict]:
    """The dict elements of ``installed[]``, from output that may be neither.

    Nothing here raises — malformed or unexpected output means "this machine reports no plugin
    identities", which is a legitimate answer preflight degrades on, not an error. The two
    ``isinstance`` filters are mirrored verbatim in ``PLUGIN_ROOT_SNIPPET``.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    listed = (data.get("installed") if isinstance(data, dict) else None) or []
    return [e for e in listed if isinstance(e, dict)]


def _claims_from_json(text: str) -> dict[str, set[str]]:
    """``{plugin name: every enabled, on-disk install root claiming it}``."""
    home = config_root()
    claims: dict[str, set[str]] = {}
    for entry in _entries_from_json(text):
        root = _installed_root(entry, home)
        if root:
            claims.setdefault(str(entry["name"]), set()).add(root)
    return claims


def installed_plugins(
    project_root: str | None = None,
) -> tuple[dict[str, str], dict[str, list[str]], frozenset[str]]:
    """``({attributable name: root}, {contested name: roots}, {unverifiable names})``, three empties
    when Codex answers nothing, or ``HostProbeTimeout`` when Codex never answers at all.

    Those last two are DIFFERENT ANSWERS and this function must not merge them. Three empties
    means "Codex was asked and reports no plugin identities" — a legitimate answer preflight
    degrades on. An expired probe means the question was never answered, so nothing at all is
    known about what is installed. Returning empties for both would tell the caller a machine
    with a hung Codex has no plugins, and preflight would then report every plugin-qualified
    skill as MISSING — advice to install what the owner may already have. The expiry is raised
    instead, and ``preflight.check`` maps it to ``unverified``, which is what it means.

    Asking the host is the only way to attribute a skill to a plugin on Codex: an installed
    plugin's skill directory carries no qualifier of its own, so nothing on disk says whose a
    skill is — Codex supplies ``<plugin>:`` from the install record. What comes
    back is an IDENTITY, which ``_installed_root`` turns into a path and then checks — the host
    is asked the question only it can answer, and the layout that answer implies is verified
    rather than trusted. ``</dev/null`` because Codex subcommands hang on an unredirected stdin
    (ground truth §"Codex help hangs"); a timeout because a preflight that hangs is worse than
    one that reports less.

    ONE invocation answers all three. They are a PARTITION of a single answer — attributable,
    ambiguous, unlocatable — so splitting them into separate functions would mean shelling out
    to the CLI once per part and, worse, letting the parts come from different moments: a plugin
    installed in between would be attributable to one and absent from another.

    ``project_root`` names WHICH PROJECT is being probed for, and exists for the expiry report
    rather than for the lookup: design §"Failure handling" requires a pre-run capability probe
    to report the host id and the project root in place of a run key, because it runs BEFORE any
    run exists to have one — ``skills/start/SKILL.md`` fires preflight as step 0, ahead of the
    gate, ``.conductor/run_branch`` and ``conductor run new``. It is threaded from the caller
    rather than re-derived here so a report cannot name a different project from the one the
    caller asked about; a caller with none of its own falls back to this process's own working
    directory, which is the same default ``host_skills`` and ``preflight.check`` already use.
    """
    project = project_root or os.getcwd()
    exe = shutil.which(HOST_ID)
    if not exe:
        return {}, {}, frozenset()
    try:
        proc = subprocess.run(
            [exe, "plugin", "list", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PLUGIN_LIST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as expiry:
        # BEFORE the broad handler below, because TimeoutExpired IS a SubprocessError: the
        # order is what keeps "never answered" out of the "answered nothing" bucket.
        raise base.HostProbeTimeout(
            f"`codex plugin list --json` did not answer within {PLUGIN_LIST_TIMEOUT_S}s and was "
            f"killed; no write occurred. host={HOST_ID} project_root={project} — this is a "
            f"pre-run capability probe, so it names the host it could not ask and the project "
            f"it was asked about in place of a run key, which no probe that decides whether a "
            f"run may start can have. Codex could not be asked which plugins are installed, "
            f"so plugin attribution is unknown rather than empty. Reproduce with: "
            f"codex plugin list --json </dev/null"
        ) from expiry
    except (OSError, subprocess.SubprocessError):
        return {}, {}, frozenset()
    if proc.returncode != 0:
        return {}, {}, frozenset()
    return (
        plugin_roots_from_json(proc.stdout),
        contested_claims_from_json(proc.stdout),
        unverifiable_plugins_from_json(proc.stdout),
    )


class CodexAdapter:
    id: str = HOST_ID

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
            f"| python3 -c '{PLUGIN_ROOT_SNIPPET}' conductor 2>/dev/null)\"\n"
            "    CODEX_ROOT_RC=$?\n"
            "    # Codex LISTED conductor and its install root is not there. Distinct from\n"
            "    # `driver-unresolved`, which is also what an uninstalled plugin produces: this\n"
            "    # says the plugin is installed and its tree moved, so an owner reading the log\n"
            "    # goes and looks at the cache instead of reinstalling what they already have.\n"
            f'    [ "$CODEX_ROOT_RC" -ne {PLUGIN_ROOT_RC_UNVERIFIABLE} ] || '
            "printf '%s plugin-root-unverified plugin=conductor home=%s\\n' "
            '"$(ts)" "${CODEX_HOME:-$HOME/.codex}" >> "$LOG"\n'
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

    # --------------------------------------------------------------------- session identity
    #
    # NO PROCESS NAME IS READ HERE, on either path. The chain is
    # ``CODEX_THREAD_ID`` -> lock file path -> ``(device, inode)`` -> ``/proc/locks`` -> the
    # holder pid, which never scans the process table and never compares a `comm` or an `exe`
    # basename to anything. The fallback below substitutes PARENTAGE for the environment
    # variable, not a name.
    #
    # THE LOCK IS ONLY EVER READ, NEVER TAKEN. Acquiring it would either block behind the live
    # session or, worse, succeed against a session that had exited and leave Conductor holding
    # a lock Codex expects to own.

    def thread_lock_path(
        self, thread_id: str, *, home: str | None = None
    ) -> str | None:
        """The lock file for ``thread_id``, or ``None`` if the id is not one this may join."""
        if not _THREAD_ID_RE.match(thread_id):
            return None
        return os.path.join(home or config_root(), THREAD_LOCK_DIR, f"{thread_id}.lock")

    def _thread_id_from_ancestry(self, home: str) -> str | None:
        """The thread id of the Codex process this call is running under, via parentage.

        The documented-nowhere fallback for a host build that stops exporting
        ``SESSION_THREAD_ENV``. It walks this process's own ancestor pids and asks the kernel's
        lock table which of them holds one of this ``CODEX_HOME``'s thread locks. That is
        parentage plus a kernel fact — never a name — so it stays inside the ban the deleted
        ``pgrep`` guard is under.

        VERIFIED against a real session on 2026-08-27, not merely reasoned about: a
        ``codex exec`` was made to run this code with ``CODEX_THREAD_ID`` stripped from the
        child's environment, and the ancestry walk returned
        ``codex:01a044cf-a85d-7fa0-bda5-f0f43bdbc985:<boot>`` — byte-identical to what the
        variable produces, and to the session id in the banner.

        ``None`` when nothing in the ancestry holds a thread lock, and also when an ancestor
        holds MORE THAN ONE. One Codex process owning several threads is real and was observed
        (one pid, two locks), and in that case parentage cannot say which thread this call
        belongs to. Answering anyway would register ownership under a thread id that can outlive
        or predecease the session doing the work, so the honest answer is that there is none.
        """
        directory = os.path.join(home, THREAD_LOCK_DIR)
        try:
            names = os.listdir(directory)
        except OSError:
            return None
        table = proc.lock_holders()
        if table is None:
            return None
        ancestry = set(proc.ancestor_pids(os.getpid()))
        matched: set[str] = set()
        for name in names:
            if not name.endswith(".lock"):
                continue
            thread_id = name[: -len(".lock")]
            if not _THREAD_ID_RE.match(thread_id):
                continue
            try:
                st = os.stat(os.path.join(directory, name))
            except OSError:
                continue
            key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
            if ancestry.intersection(table.get(key, ())):
                matched.add(thread_id)
        if len(matched) != 1:
            return None
        return matched.pop()

    def session_identity(self, env: Mapping[str, str]) -> str | None:
        """``codex:<CODEX_THREAD_ID>:<boot-id>`` for the session that owns ``env``.

        The thread id is the identity and a pid never is: one Codex process can hold several
        thread locks, so keying ownership on the process would exclude threads that are not
        working on this run.

        ``None`` when no thread id can be established, when it is not a well-formed id, or when
        the boot id is unreadable — all of which mean the caller must refuse to claim ownership
        rather than write a record nothing can later verify.
        """
        home = env.get(CONFIG_DIR_ENV) or config_root()
        thread_id = env.get(SESSION_THREAD_ENV) or ""
        if not _THREAD_ID_RE.match(thread_id.strip()):
            thread_id = self._thread_id_from_ancestry(home) or ""
        else:
            thread_id = thread_id.strip()
        if not thread_id:
            return None
        boot = proc.boot_id()
        if boot is None:
            return None
        return f"{self.id}:{thread_id}:{boot}"

    def process_alive(self, identity: str) -> bool | None:
        """Tri-state liveness for an identity recorded against this host.

        ``codex:<thread>:<boot>`` resolves through the kernel's lock table. ``proc:<pid>:…`` is
        the host-agnostic plain-process scheme a ``conductor heartbeat`` wrapper registers, and
        it must be answerable on a Codex-recorded run exactly as on a Claude-recorded one. Any
        other scheme, including Claude's, is ``None``.

        The exit proofs, all positive and all kernel-backed:

        * the recorded boot id differs — the machine restarted;
        * the lock file is GONE — Codex deletes it on a clean exit;
        * the lock file is present but the kernel holds no lock on it — the session was killed
          and the kernel released the lock on its behalf.

        A MISSING LOCK DIRECTORY IS ``None``, NOT ``False``. The directory is an undocumented
        implementation detail of a 0.x CLI; if a future version moves or renames it, every
        record would otherwise read as provably exited at once and every live session would stop
        excluding anything. "The mechanism is not where I expect it" must present as "cannot
        tell".
        """
        scheme = identity.split(":", 1)[0] if ":" in identity else ""
        if scheme == proc.LOCAL_SCHEME:
            return proc.pid_identity_liveness(identity, scheme=proc.LOCAL_SCHEME)
        if scheme != self.id:
            return None
        parts = identity.split(":")
        if len(parts) != 3 or not parts[2]:
            return None
        thread_id, recorded_boot = parts[1], parts[2]
        current_boot = proc.boot_id()
        if current_boot is None:
            return None
        if current_boot != recorded_boot:
            return False
        path = self.thread_lock_path(thread_id)
        if path is None:
            return None
        if not os.path.isdir(os.path.dirname(path)):
            return None
        state, _ = proc.path_lock_state(path)
        if state == "held":
            return True
        if state in ("absent", "unheld"):
            return False
        return None

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

    #: A ``$`` mention matches the exact skill name, and an installed plugin's skill is named
    #: ``<plugin>:<skill>`` — so ``$code-review`` never reaches ``gstack:code-review``. Measured on
    #: codex-cli 0.155.0: with spec-craft installed, ``$expectations`` injected no skill.
    resolves_unqualified_plugin_skills: bool = False

    def source_root(self) -> str:
        return config_root()

    def native_invocation(self, skill: str) -> str:
        """``conductor:autodev`` -> ``$conductor:autodev``; ``code-review`` -> ``$code-review``.

        The plugin qualifier is KEPT. Codex lists an installed plugin's skill as
        ``<plugin>:<skill>`` (``skills/list`` on codex-cli 0.155.0 shows ``spec-craft:expectations``,
        ``conductor:start``) and a ``$`` mention matches the exact name: in a live ``codex exec``
        with spec-craft installed, ``$spec-craft:expectations`` injected the skill and a bare
        ``$expectations`` injected nothing. Dropping the qualifier names a skill that does not
        exist. A flat user skill under ``$CODEX_HOME/skills/`` has no qualifier to keep, so an
        unqualified name stays bare.

        It is still not a host dispatch primitive: the driver's fire command writes the SKILL.md
        path out instead of using this form, so a launch never depends on it resolving.
        """
        return skill if skill.startswith("$") else f"${skill}"

    def discovered_commands(self, *, project_root: str | None = None) -> set[str]:
        return self.host_skills(project_root=project_root).commands

    def host_skills(self, *, project_root: str | None = None) -> discovery.HostSkills:
        """Invocable command names on this machine, plus the plugins Codex lists but cannot be
        shown to hold and the plugin names more than one source answers to.

        THE SOURCE IS CODEX'S OWN CATALOG (``skills_list``): every skill a ``$`` mention can
        reach, named exactly as Codex names it — plugin skills as ``<plugin>:<skill>``,
        manifest-namespaced user skills such as ``~/.agents/skills/superpowers`` as
        ``superpowers:<skill>``, user skills bare — minus what it disabled or refused to load.
        ``$CODEX_HOME/prompts/`` (the analogue of Claude's slash commands) and conductor's own
        checkout and dev roots are added to it, as they always were.

        When Codex cannot give a catalog — no executable, an error, output that is not the
        protocol — the answer falls back to scanning: ``$CODEX_HOME/skills/``, the project-local
        ``./.codex/skills/``, and each installed plugin's root, named and filtered by
        ``codex_skill_names``. That scan does not reach ``~/.agents/skills`` or the other roots
        only Codex's resolver knows, so it can under-report; it is the answer of last resort.

        ``codex plugin list --json`` is asked either way, first. It is what knows a plugin Codex
        lists but whose root is NOT ON DISK (``unverifiable_plugins``) and a plugin name two
        installed roots claim (``contested_plugins``, joined by any qualified name the catalog
        lists at two paths). Without the first, "codex says spec-craft is installed and its tree
        is gone" and "spec-craft was never installed" arrive at preflight as the same empty
        contribution; without the second, a second marketplace's same-named plugin passes as
        the required one.

        An expired probe — either one — PROPAGATES rather than degrading here. This method's
        answer is "what is invocable", and after an unanswered probe that is not known;
        returning what was scanned would be an answer of the same shape as a healthy one. The
        expiry carries those legs as ``partial`` instead, so the policy layer degrades with
        every fact that WAS established and none that were not.
        """
        home = self.source_root()
        project = project_root or os.getcwd()
        named = _SKILL_NAMER
        cmds = discovery.command_names(f"{home}/prompts/*.md")
        cmds |= discovery.scan_plugin_dir(
            discovery.CONDUCTOR_ROOT, discovery.ALL_MANIFEST_DIRS, named
        )
        for root in discovery.dev_plugin_roots():
            cmds |= discovery.scan_plugin_dir(root, (f".{self.id}-plugin",), named)
        scanned = named(f"{home}/skills/*/SKILL.md")
        scanned |= named(f"{project}/.{self.id}/skills/*/SKILL.md")
        # The host is asked only after everything above is established, so it can travel with
        # an expiry.
        try:
            attributed, contested, unverifiable = installed_plugins(
                project_root=project
            )
        except base.HostProbeTimeout as expiry:
            raise base.HostProbeTimeout(
                str(expiry), partial=discovery.HostSkills(cmds | scanned, frozenset())
            ) from expiry
        for name, root in attributed.items():
            scanned |= discovery.qualified(name, root, named)
        for name, roots in contested.items():
            for root in roots:
                scanned |= discovery.qualified(name, root, named)
        colliding = frozenset(contested)
        try:
            catalog = skills_list(project_root=project)
        except base.HostProbeTimeout as expiry:
            raise base.HostProbeTimeout(
                str(expiry),
                partial=discovery.HostSkills(cmds | scanned, unverifiable, colliding),
            ) from expiry
        if catalog is None:
            return discovery.HostSkills(cmds | scanned, unverifiable, colliding)
        listed, listed_twice = catalog_names(catalog)
        return discovery.HostSkills(
            cmds | listed, unverifiable, colliding | listed_twice
        )
