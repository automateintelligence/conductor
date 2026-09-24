"""Filesystem mechanics shared by both adapters' skill discovery.

``base``'s standing rule is that **argv construction is never shared**. Discovery is not argv.
It is "glob a directory, read a JSON ``name``", and it is genuinely identical on the two hosts
because the ``SKILL.md`` format and the ``skills/<name>/`` layout are identical (ground truth
§"Skill file format is compatible across hosts"). What *does* differ — which roots are searched,
which manifest directory names the plugin, whether a skill is named by its directory or by the
``name`` its ``SKILL.md`` declares, and which SKILL.md files the host refuses to load — stays in
each adapter, where a wrong answer is visible rather than averaged away. This module supplies
the primitives (``skill_names``, ``frontmatter``) and chooses neither rule.

Nothing here raises. Discovery answers "what is installed", and a missing, unreadable, or
malformed directory is a legitimate answer to that question ("not this"), not an error. The
fail-closed decision belongs to ``preflight.check``, which sees the whole set.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Callable, NamedTuple, Protocol, cast

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


class HostSkills(NamedTuple):
    """ONE host snapshot: what is invocable, and which plugins the host cannot account for.

    Both halves come from a single question to the host, because on Codex they are a partition
    of one ``codex plugin list --json`` answer. Asking twice would let the halves come from
    different moments and disagree about a plugin installed in between — and disagreement here
    is precisely the state preflight exists to report.
    """

    commands: set[str]
    #: Plugins the host REPORTS as installed and enabled but whose contents it cannot locate.
    #: Not the same fact as "no skills of that plugin were discovered": a plugin that is simply
    #: absent contributes nothing here, and preflight tells those two apart because the remedy
    #: differs — install it, versus repair an install that is already there.
    unverifiable_plugins: frozenset[str]
    #: Plugins MORE THAN ONE installed root claims. Their skills are invocable under the
    #: qualified name, but which root answers is not something conductor can establish, so a
    #: requirement naming one of these plugins is ``unverified``, never a pass. Defaults empty
    #: because only a host that reports installed plugins can have a collision to report.
    contested_plugins: frozenset[str] = frozenset()


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
    #: Whether a bare invocation (``code-review``) reaches a plugin's skill of that name. False
    #: means only a skill named exactly ``code-review`` satisfies an unqualified requirement.
    resolves_unqualified_plugin_skills: bool

    def source_root(self) -> str: ...
    def native_invocation(self, skill: str) -> str: ...
    def discovered_commands(self, *, project_root: str | None = None) -> set[str]: ...
    def host_skills(self, *, project_root: str | None = None) -> HostSkills: ...


def adapter_for(host_id: str) -> CommandDiscovery:
    """``base.load`` narrowed to the members A1 uses. Same object, narrower contract."""
    return cast(CommandDiscovery, load(host_id))


#: How a host turns a ``.../skills/*/SKILL.md`` glob into the names it resolves. Each adapter
#: picks one of the two below and passes it to every helper that enumerates skills.
SkillNamer = Callable[[str], set[str]]


def skill_names(pattern: str) -> set[str]:
    """Bare skill names from a ``.../skills/*/SKILL.md`` glob — the *directory* names."""
    return {os.path.basename(os.path.dirname(p)) for p in glob.glob(pattern)}


def _yaml_scalar(raw: str) -> str:
    """The string value of a one-line plain or quoted YAML scalar.

    Enough YAML for a ``name:`` line and no more: a quoted value ends at its closing quote, a
    plain one at an inline `` #`` comment. Anything richer is not a skill name any host
    accepts, and the result is compared for equality against required names, so a misread
    value can only fail to match — never match something it should not.
    """
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else ""
    return value.split(" #", 1)[0].strip()


def frontmatter(skill_md: str) -> dict[str, str] | None:
    """The top-level scalar fields of a SKILL.md's leading ``---`` frontmatter block.

    None when there is no block: the first line is not ``---``, no closing ``---`` follows, the
    block is empty, or the file cannot be read. Nested keys are skipped, and a ``|`` or ``>``
    block scalar is folded into one line. A line in the body is never a field.

    This is a line reader, not a YAML parser — the package is stdlib-only. It covers the
    frontmatter skills are actually written in; a host that needs its own exact rule applies it
    to what this returns.
    """
    try:
        with open(skill_md, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    try:
        close = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    block = lines[1:close]
    if not block:
        return None
    fields: dict[str, str] = {}
    folding: str | None = None
    for line in block:
        indented = line[:1] in (" ", "\t")
        if folding is not None:
            if indented or not line.strip():
                fields[folding] = " ".join((fields[folding] + " " + line).split())
                continue
            folding = None
        if indented or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value[:1] in ("|", ">"):
            fields[key.strip()] = ""
            folding = key.strip()
            continue
        fields[key.strip()] = _yaml_scalar(value)
    return fields


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


def plugin_contents(root: str, namer: SkillNamer = skill_names) -> set[str]:
    """The bare skill and command names one plugin root provides."""
    return namer(f"{root}/skills/*/SKILL.md") | command_names(f"{root}/commands/*.md")


def qualified(name: str, root: str, namer: SkillNamer = skill_names) -> set[str]:
    """``<plugin>:<skill>`` for everything in ``root``, attributed to ``name``."""
    return {f"{name}:{n}" for n in plugin_contents(root, namer)}


def scan_plugin_dir(
    root: str, manifest_dirs: tuple[str, ...], namer: SkillNamer = skill_names
) -> set[str]:
    """``<plugin>:<name>`` for every skill and command in one plugin root.

    An unnamed root yields nothing rather than a bare name: an unattributed entry cannot be
    told apart from a user skill, and it would at best satisfy an unqualified requirement with
    a skill no host invokes under that name, and at worst suggest a plugin the host cannot
    actually load.
    """
    name = manifest_name(root, manifest_dirs)
    return qualified(name, root, namer) if name else set()


def dev_plugin_roots(*env_vars: str) -> list[str]:
    """Dev / ``--plugin-dir`` roots no marketplace cache can see.

    ``CONDUCTOR_PLUGIN_DIRS`` first because it is ours and host-neutral; then whatever
    plugin-root variable the calling host publishes, of which Codex has none that was
    verified — hence the varargs rather than a required argument.
    """
    roots = [d for d in os.environ.get(PLUGIN_DIRS_ENV, "").split(os.pathsep) if d]
    roots += [os.environ[v] for v in env_vars if os.environ.get(v)]
    return roots
