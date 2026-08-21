"""Static availability gate: does every conducted skill resolve on this run's host?

The required set is stated ONCE, host-neutrally, as skill names. Three host-specific things
are derived rather than written down:

* **Where skills live.** ``~/.claude`` with a marketplace plugin cache, or ``$CODEX_HOME`` with
  flat skill dirs, ``prompts/``, and a project-local ``./.codex/skills/``. Each adapter's
  ``discovered_commands`` owns its own roots.
* **How a name is written.** ``/spec-craft:expectations`` under Claude, ``$expectations`` under
  Codex — the plugin qualifier has no Codex counterpart, so it is dropped for both matching and
  reporting by the one function that knows the rule, ``native_invocation``.
* **Who reviews.** The opposite-host review wrapper is ``codex`` on a Claude-hosted run and
  ``claude`` on a Codex-hosted one. This is the requirement that actually flips, and getting it
  wrong greens a machine that can only ever produce a same-host review.

One genuine asymmetry, stated rather than papered over: on Claude the opposite-host wrapper is
gstack's ``/codex`` skill, which exists. On Codex the mirror image is a ``claude`` wrapper,
which nothing in the conducted stack ships today. A Codex machine without one therefore fails
this gate — correctly. The run cannot get an opposite-host review, and reporting ``$claude``
missing is the honest answer; silently accepting ``$codex`` would set up the same-host review
the policy exists to forbid.
"""

import os
import sys
from typing import TypedDict

from conductor.hosts import discovery, runhost
from conductor.hosts.base import opposite


class CheckResult(TypedDict):
    ok: bool
    missing: list[str]
    #: Required, present under the required NAME, and impossible to attribute to the plugin the
    #: requirement names. A third outcome exists because the two-valued one forced a lie: on a
    #: host that drops the plugin qualifier, "present under this name" and "this is the required
    #: plugin's skill" are different facts, and reporting the second when only the first was
    #: checked is how preflight greens a machine whose spec-craft is absent — or hostile.
    unverified: list[str]
    #: One actionable line per missing or unverifiable skill, plus a trailing note when this
    #: host does not resolve plugin dependencies. "Missing" alone is not actionable:
    #: `$expectations` does not tell a Codex user that the thing to install is called
    #: `spec-craft`.
    advice: list[str]


# Exact skills the recipe (T5) + setup (T6) invoke, written host-neutrally. `plugin:skill`
# where the skill ships inside a plugin; a bare name where it is environment-provided (which
# may be a user skill OR a plugin skill — the unqualified rule in `_resolve` matches both).
REQUIRED_SKILLS: tuple[str, ...] = (
    "spec-craft:expectations",
    "spec-craft:executable-assertions",
    "conductor:assertions-to-tests",
    "superpowers:subagent-driven-development",
    "superpowers:requesting-code-review",
    "superpowers:receiving-code-review",
    "superpowers:writing-plans",
    "code-review",
    "document-release",
)

# Where the opposite-host review wrapper sits in the reported order. Kept at the index the
# Claude-form list used so a familiar `MISSING:` block reads the same way it always has.
_REVIEW_WRAPPER_INDEX = 8


def required_commands(host_id: str) -> list[str]:
    """Every conducted skill a run on ``host_id`` needs, host-neutral names."""
    names = list(REQUIRED_SKILLS)
    names.insert(_REVIEW_WRAPPER_INDEX, opposite(host_id))
    return names


def available_commands(
    host_id: str | None = None, project_root: str | None = None
) -> set[str]:
    """Discover invocable command names from disk for ``host_id``.

    User skills come back bare, plugin skills and commands as ``<plugin>:<name>``. Runtime
    invocability is confirmed by the T7 smoke; this is the static availability gate.
    """
    host = host_id or runhost.resolve(project_root or os.getcwd())
    return discovery.adapter_for(host).discovered_commands(project_root=project_root)


def _resolve(
    name: str,
    adapter: discovery.CommandDiscovery,
    avail: set[str],
    unlocatable: frozenset[str],
) -> str:
    """One required skill -> ``ok`` | ``unverified`` | ``missing``.

    The name is matched AS THIS HOST RESOLVES IT, and the plugin qualifier is evidence that is
    checked wherever it survives:

    * Claude keeps the qualifier, so ``spec-craft:expectations`` is an exact match or nothing.
    * Codex drops it, so ``$expectations`` may be satisfied by ``spec-craft:expectations`` —
      a plugin skill Codex itself attributed. That is a clean pass.
    * A skill of the required name attributed to some OTHER plugin is not the required skill.
      It is missing, on either host: "spec-craft is not installed" is not a host-specific fact,
      and accepting a same-named skill is an invitation to ship one.
    * A skill of the required name with NO attribution — a flat ``$CODEX_HOME/skills/`` dir —
      is invocable, so it is not missing, but its identity is not recoverable from a directory
      name. That is ``unverified``: reported, never counted as a pass.
    * A skill whose plugin the host LISTS as installed but cannot locate on disk is also
      ``unverified``, and for the same reason the state exists: the host has made a claim that
      could not be checked. Calling it ``missing`` says "this plugin is not installed", which is
      not what the host reported, and sends the owner to reinstall something they already have.

    A requirement that names no plugin claims no identity, so a plugin's copy satisfies it.
    """
    rendered = adapter.native_invocation(name).lstrip("/$")
    if ":" in rendered:  # this host keeps the qualifier: exact match or nothing
        return "ok" if rendered in avail else "missing"
    if ":" in name:
        plugin = name.split(":", 1)[0]
        if f"{plugin}:{rendered}" in avail:
            return "ok"
        if rendered in avail or plugin in unlocatable:
            return "unverified"
        return "missing"
    if rendered in avail or any(a.endswith(f":{rendered}") for a in avail):
        return "ok"
    return "missing"


def _advice(
    adapter: discovery.CommandDiscovery,
    unresolved: list[str],
    unverified: list[str],
    unlocatable: frozenset[str] = frozenset(),
) -> list[str]:
    """One actionable line per missing skill, one per unverifiable one, then the dependency
    note when it applies.

    An unverifiable skill gets its own wording because the remedy is different: the skill IS
    there, so "install it" is wrong advice and an owner who follows it learns to ignore the
    gate. What is missing is the plugin's claim on it.

    ``unverified`` has TWO causes and they do not share a remedy, so they do not share a
    sentence. A flat, hand-copied skill is installed and merely unattributable — install the
    plugin properly. A plugin the host lists but cannot locate is a BROKEN install: the owner
    has it, the tree the host named is not there, and telling them to install it points at the
    one thing that is not wrong.

    The note is the Track A answer to a packaging fact A3 verified against codex-cli 0.147.0:
    ``.codex-plugin/plugin.json`` has no ``dependencies`` field — the 180 manifests in the
    installed curated catalog use exactly twelve fields and that is not one — and Codex accepts
    unknown fields silently, so it cannot be added to make it work. Under Claude,
    ``.claude-plugin/plugin.json`` declares ``dependencies: ["spec-craft"]`` and installing
    conductor pulls spec-craft with it. Under Codex nothing does, and a spec-craft skill that
    resolves to nothing surfaces mid-run rather than at install time. Nothing in the manifest
    can fix that, so preflight names it instead — loudly, at the gate, before the loop starts.
    """
    lines: list[str] = []
    plugins: list[str] = []
    for name in unresolved:
        rendered = adapter.native_invocation(name)
        if ":" in name:
            plugin = name.split(":", 1)[0]
            if plugin not in plugins:
                plugins.append(plugin)
            lines.append(f"{rendered} — ships in the `{plugin}` plugin; install it")
        else:
            lines.append(
                f"{rendered} — environment-provided; install a `{name}` skill on this host"
            )
    for name in unverified:
        plugin = name.split(":", 1)[0]
        if plugin in unlocatable:
            lines.append(
                f"{adapter.native_invocation(name)} — {adapter.id} lists the `{plugin}` plugin "
                f"as installed and enabled, but the install root that identity implies is NOT "
                f"ON DISK, so nothing of it can be loaded. That is a broken or relocated "
                f"install, not a missing plugin: check "
                f"`$CODEX_HOME/plugins/cache/<marketplace>/{plugin}/<version>` against "
                f"`codex plugin list --json` before reinstalling."
            )
        else:
            lines.append(
                f"{adapter.native_invocation(name)} — present, but {adapter.id} cannot verify it "
                f"is the `{plugin}` plugin's: it resolves as an unattributed skill. Install "
                f"`{plugin}` as a {adapter.id} plugin so its identity is recoverable."
            )
    if plugins and not adapter.resolves_plugin_dependencies:
        lines.append(
            f"NOTE: {adapter.id} does not resolve plugin dependencies, so installing conductor "
            f"does not pull these in. Install each explicitly: {', '.join(plugins)}."
        )
    return lines


def check(
    required: list[str] | None = None,
    available: set[str] | None = None,
    host_id: str | None = None,
    project_root: str | None = None,
) -> CheckResult:
    """Which required skills this host cannot resolve, named the way this host names them."""
    host = host_id or runhost.resolve(project_root or os.getcwd())
    adapter = discovery.adapter_for(host)
    names = required if required is not None else required_commands(host)
    # ONE snapshot: which names are invocable and which plugins the host cannot account for are
    # two halves of a single answer, and asking for them separately would let them describe
    # different moments. A caller that supplies `available` has supplied a set of names and
    # nothing else, so it knows of no unlocatable plugin — never a leftover from another host.
    if available is not None:
        avail, unlocatable = available, frozenset()
    else:
        snapshot = adapter.host_skills(project_root=project_root)
        avail, unlocatable = snapshot.commands, snapshot.unverifiable_plugins
    # Match on the name AS THIS HOST RESOLVES IT: `native_invocation` is the single place that
    # knows Claude keeps the plugin qualifier and Codex drops it, so matching and reporting
    # cannot drift apart into a preflight that greens on a name it then prints differently.
    outcomes = [(name, _resolve(name, adapter, avail, unlocatable)) for name in names]
    unresolved = [name for name, out in outcomes if out == "missing"]
    unverified = [name for name, out in outcomes if out == "unverified"]
    # `ok` is a PASS, not an absence of hard failures: a skill whose plugin identity could not
    # be established has not been checked, and counting it as checked is the false pass.
    return {
        "ok": not unresolved and not unverified,
        "missing": [adapter.native_invocation(name) for name in unresolved],
        "unverified": [adapter.native_invocation(name) for name in unverified],
        "advice": _advice(adapter, unresolved, unverified, unlocatable),
    }


if __name__ == "__main__":
    host = runhost.resolve(os.getcwd())
    required = required_commands(host)
    result = check(host_id=host)
    ok: bool = result["ok"]
    unverifiable = set(result["unverified"])
    for line in result["advice"]:
        if line.startswith("NOTE:"):
            prefix = ""
        elif line.split(" ", 1)[0] in unverifiable:
            prefix = "UNVERIFIED: "
        else:
            prefix = "MISSING: "
        print(f"{prefix}{line}", file=sys.stderr)
    if ok:  # the documented "verify" step must confirm success, not print a blank line
        print(
            f"preflight OK: {len(required)}/{len(required)} conducted skills resolved "
            f"on host {host}"
        )
    sys.exit(0 if ok else 1)
