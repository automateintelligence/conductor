"""Filesystem mechanics shared by both adapters' skill discovery.

``base``'s standing rule is that **argv construction is never shared**. Discovery is not argv.
It is "glob a directory, read a JSON ``name``", and it is genuinely identical on the two hosts
because the ``SKILL.md`` format and the ``skills/<name>/`` layout are identical (ground truth
§"Skill file format is compatible across hosts"). What *does* differ — which roots are searched
and which manifest directory names the plugin — stays in each adapter, where a wrong answer is
visible rather than averaged away.

Nothing here raises. Discovery answers "what is installed", and a missing, unreadable, or
malformed directory is a legitimate answer to that question ("not this"), not an error. The
fail-closed decision belongs to ``preflight.check``, which sees the whole set.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Protocol, cast

from conductor.hosts.base import HOST_IDS, load

#: The conductor checkout this process is running out of — ``conductor/hosts/discovery.py``
#: up three levels. Self-discovery: whatever else is or is not installed, the copy of
#: conductor that is executing can always resolve its own skills.
CONDUCTOR_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

#: Every host's per-plugin manifest directory, derived from the host ids rather than spelled
#: out (``.claude-plugin`` / ``.codex-plugin``, ground truth §"Claude vs Codex"). Used ONLY
#: for ``CONDUCTOR_ROOT``: a plugin directory that carries the other host's manifest is not
#: installable on this host and must not be counted, but the checkout we are *running from*
#: is present by definition and only needs its manifest to learn its own namespace.
ALL_MANIFEST_DIRS = tuple(f".{host_id}-plugin" for host_id in HOST_IDS)

#: Host-neutral escape hatch for dev / uninstalled trees, honoured by every host.
PLUGIN_DIRS_ENV = "CONDUCTOR_PLUGIN_DIRS"


class CommandDiscovery(Protocol):
    """The slice of an adapter that the preflight consumes.

    Declared here rather than added to ``base.HostAdapter`` on purpose. ``HostAdapter`` is
    Plan 04's nineteen-member interface and A1 is a **subset** of Plan 04, not a competitor;
    growing the shared Protocol from a subset would make Plan 04's own contract test — which
    asserts the declared set — a moving target. ``source_root`` and ``native_invocation`` are
    already Plan 04 members and are restated here only so this Protocol stands alone;
    ``discovered_commands`` and ``resolves_plugin_dependencies`` are A1's, and Plan 04 folds
    them in when it lands.
    """

    id: str
    #: Whether installing a plugin on this host also installs the plugins it declares as
    #: dependencies. False means preflight has to tell the user to do it by hand.
    resolves_plugin_dependencies: bool

    def source_root(self) -> str: ...
    def native_invocation(self, skill: str) -> str: ...
    def discovered_commands(self, *, project_root: str | None = None) -> set[str]: ...


def adapter_for(host_id: str) -> CommandDiscovery:
    """``base.load`` narrowed to the members A1 uses. Same object, narrower contract."""
    return cast(CommandDiscovery, load(host_id))


def skill_names(pattern: str) -> set[str]:
    """Bare skill names from a ``.../skills/*/SKILL.md`` glob — the *directory* names."""
    return {os.path.basename(os.path.dirname(p)) for p in glob.glob(pattern)}


def command_names(pattern: str) -> set[str]:
    """Bare command names from a ``*.md`` glob — the file stems."""
    return {os.path.basename(p)[:-3] for p in glob.glob(pattern)}


def manifest_name(root: str, manifest_dirs: tuple[str, ...]) -> str | None:
    """The plugin ``name`` from the first readable manifest, or None when there is none."""
    for manifest_dir in manifest_dirs:
        try:
            with open(os.path.join(root, manifest_dir, "plugin.json")) as f:
                name = json.load(f).get("name")
        except (OSError, ValueError):
            continue
        if name:
            return name
    return None


def scan_plugin_dir(root: str, manifest_dirs: tuple[str, ...]) -> set[str]:
    """``<plugin>:<name>`` for every skill and command in one plugin root.

    An unnamed root yields nothing rather than a bare name: an unnamespaced entry would
    match a required ``plugin:skill`` by the suffix rule in ``preflight._present`` and
    green-light a plugin the host cannot actually load.
    """
    name = manifest_name(root, manifest_dirs)
    if not name:
        return set()
    found = skill_names(f"{root}/skills/*/SKILL.md") | command_names(
        f"{root}/commands/*.md"
    )
    return {f"{name}:{n}" for n in found}


def dev_plugin_roots(*env_vars: str) -> list[str]:
    """Dev / ``--plugin-dir`` roots no marketplace cache can see.

    ``CONDUCTOR_PLUGIN_DIRS`` first because it is ours and host-neutral; then whatever
    plugin-root variable the calling host publishes, of which Codex has none that was
    verified — hence the varargs rather than a required argument.
    """
    roots = [d for d in os.environ.get(PLUGIN_DIRS_ENV, "").split(os.pathsep) if d]
    roots += [os.environ[v] for v in env_vars if os.environ.get(v)]
    return roots
