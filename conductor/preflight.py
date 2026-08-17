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
    #: One actionable line per missing skill, plus a trailing note when this host does not
    #: resolve plugin dependencies. "Missing" alone is not actionable: `$expectations` does
    #: not tell a Codex user that the thing to install is called `spec-craft`.
    advice: list[str]


# Exact skills the recipe (T5) + setup (T6) invoke, written host-neutrally. `plugin:skill`
# where the skill ships inside a plugin; a bare name where it is environment-provided (which
# may be a user skill OR a plugin skill — the suffix rule in `_present` matches both).
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


def _present(cmd: str, avail: set[str]) -> bool:
    c = cmd.lstrip("/$")
    if ":" in c:
        return c in avail
    return c in avail or any(a.endswith(f":{c}") for a in avail)


def _advice(adapter: discovery.CommandDiscovery, unresolved: list[str]) -> list[str]:
    """One actionable line per missing skill, then the dependency note when it applies.

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
    avail = (
        available
        if available is not None
        else available_commands(host_id=host, project_root=project_root)
    )
    # Match on the name AS THIS HOST RESOLVES IT: `native_invocation` is the single place that
    # knows Claude keeps the plugin qualifier and Codex drops it, so matching and reporting
    # cannot drift apart into a preflight that greens on a name it then prints differently.
    unresolved = [
        name for name in names if not _present(adapter.native_invocation(name), avail)
    ]
    return {
        "ok": not unresolved,
        "missing": [adapter.native_invocation(name) for name in unresolved],
        "advice": _advice(adapter, unresolved),
    }


if __name__ == "__main__":
    host = runhost.resolve(os.getcwd())
    required = required_commands(host)
    result = check(host_id=host)
    ok: bool = result["ok"]
    for line in result["advice"]:
        print(
            f"MISSING: {line}" if not line.startswith("NOTE:") else line,
            file=sys.stderr,
        )
    if ok:  # the documented "verify" step must confirm success, not print a blank line
        print(
            f"preflight OK: {len(required)}/{len(required)} conducted skills resolved "
            f"on host {host}"
        )
    sys.exit(0 if ok else 1)
