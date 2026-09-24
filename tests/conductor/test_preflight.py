import json
import os
import shutil
import subprocess
import sys

import pytest

from conductor import preflight
from conductor.hosts import base
from conductor.hosts import codex as codex_host
from tests.conductor import codex_stub
from tests.conductor.conftest import stale_version_siblings

_ALL = {
    "spec-craft:expectations",
    "spec-craft:executable-assertions",
    "conductor:assertions-to-tests",
    "superpowers:subagent-driven-development",
    "superpowers:requesting-code-review",
    "superpowers:receiving-code-review",
    "superpowers:writing-plans",
    "gstack:code-review",
    "gstack:codex",
    "gstack:document-release",
}


@pytest.fixture(autouse=True)
def _claude_by_default(monkeypatch):
    """Every legacy assertion below is about a Claude host; pin it rather than inherit the
    ambient shell's."""
    monkeypatch.setenv("CONDUCTOR_HOST", "claude")


def test_missing_command_fails_closed():
    out = preflight.check(
        available={"spec-craft:expectations", "superpowers:writing-plans"}
    )
    assert not out["ok"] and "/codex" in out["missing"]


def test_all_present_ok():
    assert preflight.check(available=_ALL)[
        "ok"
    ]  # bare /code-review matches gstack:code-review


def test_discovers_plugin_dir_install(tmp_path, monkeypatch):  # dogfood: --plugin-dir
    # a --plugin-dir-style plugin (manifest + skills) is found via CONDUCTOR_PLUGIN_DIRS,
    # not only the marketplace cache.
    plug = tmp_path / "spec-craft"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "spec-craft"}')
    skill = plug / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    avail = preflight.available_commands()
    assert "spec-craft:expectations" in avail


def test_discovers_conductor_own_root(monkeypatch, tmp_path):
    # dogfood: conductor's own skills always resolve — from ANY working directory. The suite
    # runs from inside a conductor checkout, so a root derived from `.` rather than from
    # `__file__` answers correctly here and nowhere else; the chdir is what tells them apart.
    # Under cron the cwd is not a checkout, and self-discovery is the leg that has no fallback.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    monkeypatch.chdir(tmp_path)
    avail = preflight.available_commands()
    assert "conductor:assertions-to-tests" in avail


# ------------------------------------------------------------------- A1: two host vocabularies
#
# The required set used to be ten Claude-form `/plugin:skill` literals, discovered under a
# `~/.claude` root through a Claude plugin-cache glob. Three separate things were wrong for a
# Codex user: the invocation form, the discovery root, and — least obviously — the identity of
# the opposite-host review wrapper, which is `/codex` only when the run is hosted on Claude.


#: The one marketplace and version these fixtures install from. `plugin list --json` does NOT
#: emit the install root, so it is derived from these plus the plugin name — which is why the
#: fixture has to place the tree at `installed_root()` and not wherever it likes.
_MARKET, _VERSION = "openai-curated", "d6169bef"


def _installed_root(home, name, market=_MARKET):
    """Where codex-cli 0.147.0 actually puts an installed plugin, as `codex plugin add` reports
    it (`Installed plugin root: …`). NOT `source.path`."""
    return home / "plugins" / "cache" / market / name / _VERSION


def _entries(sources):
    """`sources` -> `(name, marketplace, source.path)` triples.

    A key is either a bare plugin name (installed from `_MARKET`) or a `(name, marketplace)`
    pair. The pair form exists because a mapping keyed by name alone CANNOT hold
    `conductor@openai-curated` and `conductor@evil-market` at the same time — and that
    inexpressibility is exactly why nothing could observe two marketplaces collapsing onto one
    name.
    """
    return [
        (*(key if isinstance(key, tuple) else (key, _MARKET)), path)
        for key, path in sources.items()
    ]


def _plugin_list_json(sources, disabled=()):
    """What `codex plugin list --json` prints — the shape verified on codex-cli 0.147.0 by
    recording the CLI's own output; see `tests/conductor/fixtures/`.

    `sources` maps a plugin (see `_entries`) -> `source.path`, which names the MARKETPLACE tree
    the plugin was copied from and is a different directory from the installed copy. Every
    fixture here used to pass the installed root for it, which is why no test could catch
    discovery reading it.

    `disabled` names plugins the operator turned off. 0.147.0 keeps those in `installed[]` with
    `"enabled": false`; this argument exists because a fixture that hardcodes `True` cannot state
    the case, so adding the filter to production code would have left every test green.
    """
    return json.dumps(
        {
            "installed": [
                {
                    "pluginId": f"{name}@{market}",
                    "name": name,
                    "marketplaceName": market,
                    "version": _VERSION,
                    "installed": True,
                    "enabled": name not in disabled,
                    "source": {"source": "local", "path": str(path)},
                }
                for name, market, path in _entries(sources)
            ]
        }
    )


def _stub_codex_on_path(tmp_path, monkeypatch, sources, disabled=(), catalog=None):
    """A `codex` on PATH reporting `sources` (name -> `source.path`) as its installed plugins.

    `catalog` is what its `app-server` answers `skills/list` with: `(name, path, pluginId)`
    triples, the skills Codex itself would list for the tree the caller built. None means the
    server gives no catalog, and discovery then has only on-disk evidence.

    `git` is carried across because host resolution shells out to it (`runhost._common_root`);
    a PATH without it would silently degrade every derivation test to the literal-path branch.
    """
    bindir = tmp_path / "stub-bin"
    app_server = None
    if catalog is not None:
        home = os.environ.get("CODEX_HOME", str(tmp_path))
        initialize = codex_stub.recorded_app_server(
            codex_home=home, cwd=str(tmp_path), home=str(tmp_path)
        )[0]
        skills = [
            {
                "name": name,
                "description": "d",
                "path": str(path),
                "scope": "user",
                "enabled": True,
                "pluginId": plugin_id,
            }
            for name, path, plugin_id in catalog
        ]
        answer = {
            "id": 1,
            "result": {"data": [{"cwd": str(tmp_path), "skills": skills}]},
        }
        app_server = [initialize, json.dumps(answer)]
    codex_stub.install(
        bindir, plugin_list=_plugin_list_json(sources, disabled), app_server=app_server
    )
    _link_git(bindir)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def _link_git(bindir):
    git = bindir / "git"
    if not git.exists():
        git.symlink_to(shutil.which("git"))


#: Which plugin owns each conducted skill. Environment-provided skills (no plugin in the
#: requirement) are absent on purpose: nothing claims a plugin for them, so nothing has to be
#: verified for them either.
_PLUGIN_SKILLS = {
    "spec-craft": ["expectations", "executable-assertions"],
    "conductor": ["assertions-to-tests"],
    "superpowers": [
        "subagent-driven-development",
        "requesting-code-review",
        "receiving-code-review",
        "writing-plans",
    ],
}


def _codex_install(
    tmp_path,
    monkeypatch,
    *,
    review_wrapper="claude",
    flat_only=False,
    without=(),
    pin_host=True,
    roots_missing=(),
    env_dir_prefix="",
):
    """A Codex machine with the conducted stack installed the way Codex installs it.

    Plugin-owned skills live inside their plugin's root, which `codex plugin list --json`
    reports — that is what makes `spec-craft:expectations` attributable at all. Environment-
    provided ones are flat under `$CODEX_HOME/skills/`, which is where a user skill lives.

    `flat_only` is the machine where someone copied the skill directories in by hand: each is
    invocable under its bare name only, so none answers to the `<plugin>:<skill>` name Codex
    gives a plugin's skill.

    Each plugin gets TWO trees, as it does on a real Codex: the installed copy Codex loads, and
    the marketplace source `source.path` names. The source tree is left EMPTY here, so a
    discovery that reads it finds nothing and the difference is visible; the reverse case — a
    populated source and a gutted install — is `test_the_marketplace_source_copy_...` below.

    `pin_host=False` withholds `$CONDUCTOR_HOST` so the caller can make preflight DERIVE the
    host instead of being handed it; the Claude root is then pointed somewhere empty so a
    wrong derivation cannot quietly pass off this machine's real `~/.claude`.

    `roots_missing` names plugins codex still REPORTS as installed and enabled but whose
    install root is not on disk — the machine where the cache moved under a version bump, or an
    install landed half-written. It is a distinct state from both "installed" and "absent", and
    no fixture could express it while every listed plugin's root was unconditionally created.

    `env_dir_prefix` is the gstack layout: its Codex installer links each environment-provided
    skill in as `$CODEX_HOME/skills/gstack-<name>/` while the SKILL.md still declares
    `name: <name>` — so the directory name and the skill's name disagree.
    """
    if pin_host:
        monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    else:
        monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude-home"))
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    catalog = []  # what Codex's own `skills/list` lists for the tree built below
    for name in ("code-review", review_wrapper, "document-release"):
        d = home / "skills" / f"{env_dir_prefix}{name}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
        catalog.append((name, d / "SKILL.md", None))
    sources = {}
    for plugin, skills in _PLUGIN_SKILLS.items():
        root = _installed_root(home, plugin)
        source = tmp_path / "codex-marketplace" / "plugins" / plugin
        source.mkdir(parents=True)
        sources[plugin] = source
        if plugin in roots_missing:
            continue  # listed by `plugin list`, but nothing at the root that identity implies
        root.mkdir(parents=True)
        # A STALE version directory either side of the listed one, holding a DIFFERENT skill
        # set. Every fixture used to create exactly one version per plugin, so a discovery that
        # ignored the version codex reported and globbed any on-disk one resolved the same
        # names and nothing failed. Different contents, because same contents would keep such a
        # discovery green — the wrong tree has to answer with the wrong skills.
        for stale in stale_version_siblings(root.name):
            d = root.parent / stale / "skills" / "stale-decoy"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: stale-decoy\ndescription: d\n---\n")
        for name in skills:
            if name in without:
                continue
            d = (home / "skills" / name) if flat_only else (root / "skills" / name)
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
            if flat_only:
                catalog.append((name, d / "SKILL.md", None))
            else:
                catalog.append(
                    (f"{plugin}:{name}", d / "SKILL.md", f"{plugin}@{_MARKET}")
                )
    _stub_codex_on_path(
        tmp_path, monkeypatch, {} if flat_only else sources, catalog=catalog
    )
    return home


def test_preflight_succeeds_against_a_codex_install(tmp_path, monkeypatch):
    _codex_install(tmp_path, monkeypatch)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out


def test_a_hand_copied_codex_stack_does_not_answer_to_the_plugin_qualified_names(
    tmp_path, monkeypatch
):
    """Codex exposes a plugin's skill only as `<plugin>:<skill>`, and a `$` mention matches the
    exact name (codex-cli 0.155.0: `$spec-craft:expectations` injected the skill, a bare
    `$expectations` injected nothing). A flat `$CODEX_HOME/skills/expectations/` copy is
    `$expectations` and nothing else, so every invocation the recipe writes resolves to nothing:
    missing, with the plugin to install named."""
    _codex_install(tmp_path, monkeypatch, flat_only=True)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"], out
    assert out["unverified"] == [], out
    assert "$spec-craft:expectations" in out["missing"]
    assert "$superpowers:writing-plans" in out["missing"]
    # environment-provided skills claim no plugin, and the flat copies are exactly them
    assert "$document-release" not in out["missing"]


def test_a_plugin_codex_lists_but_cannot_locate_is_unverified_never_missing(
    tmp_path, monkeypatch
):
    """The three-state gate exists for exactly this: codex REPORTS `spec-craft` installed and
    enabled, and the install root that identity implies is not there. Dropping the claim made it
    indistinguishable from an absent plugin, so the gate reported `missing` and advised
    reinstalling a plugin the owner already has — sending them to fix the one thing that is not
    broken while the real fault (a moved or half-written cache) goes unnamed."""
    _codex_install(tmp_path, monkeypatch, roots_missing=("spec-craft",))
    out = preflight.check(project_root=str(tmp_path / "project"))

    assert not out["ok"], out
    assert "$spec-craft:expectations" in out["unverified"], out
    assert "$spec-craft:executable-assertions" in out["unverified"], out
    assert out["missing"] == [], out
    # the other plugins are still fine — this is per-plugin, not a whole-machine degrade
    assert "$superpowers:writing-plans" not in out["unverified"], out
    advice = "\n".join(out["advice"]).lower()
    assert "install it" not in advice, advice
    assert "not on disk" in advice, advice
    assert "spec-craft" in advice


def test_a_hand_copied_stack_is_advised_as_missing_not_as_a_broken_install(
    tmp_path, monkeypatch
):
    """A flat copy answers to none of the qualified names, so its remedy is the missing one —
    install the plugin — and never the listed-but-unlocatable wording, which would send the
    owner to repair a plugin install that does not exist."""
    _codex_install(tmp_path, monkeypatch, flat_only=True)
    flat = "\n".join(
        preflight.check(project_root=str(tmp_path / "p"))["advice"]
    ).lower()
    assert "install it" in flat
    assert "not on disk" not in flat


_CLAUDE_CACHE_SKILLS = (
    ("spec-craft", ["expectations", "executable-assertions"]),
    ("conductor", ["assertions-to-tests"]),
    (
        "superpowers",
        [
            "subagent-driven-development",
            "requesting-code-review",
            "receiving-code-review",
            "writing-plans",
        ],
    ),
    ("gstack", ["code-review", "codex", "document-release"]),
)


def _claude_install(tmp_path, monkeypatch, *, without=()):
    """A Claude machine with the conducted stack in its marketplace plugin cache."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    cache = home / "plugins" / "cache" / "market"
    for plugin, skills in _CLAUDE_CACHE_SKILLS:
        for skill in skills:
            if skill in without:
                continue
            d = cache / plugin / "1.0" / "skills" / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\n---\n")
    return home


def test_preflight_still_succeeds_against_a_claude_install(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOST", "claude")
    _claude_install(tmp_path, monkeypatch)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out["missing"]


def test_a_codex_install_missing_the_claude_wrapper_fails_closed(tmp_path, monkeypatch):
    # The opposite-host reviewer is the ONE requirement that flips with the host. A Codex
    # machine that installed `codex` (itself) and not `claude` has no opposite-host reviewer,
    # and preflight must say so rather than green on the same-host tool.
    _codex_install(tmp_path, monkeypatch, review_wrapper="codex")
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"]
    assert "$claude" in out["missing"]


# ------------------------------------------- Codex names a skill by its frontmatter `name`
#
# These read `codex_skill_names`, the rule conductor applies to the trees Codex's catalog does not
# speak for (its own checkout, the dev roots) and to on-disk evidence.
#
# Measured against codex-cli 0.155.0's own `skills/list` (app-server): a SKILL.md that declares
# `name: alpha` in a directory called `dir-a` is listed as `alpha`; a quoted `name: "beta"` is
# `beta`; a SKILL.md with no `name` falls back to its directory name. gstack depends on the first
# rule — it installs `gstack-claude/` declaring `name: claude` — so a discovery that reads only
# directory names reports `$claude` missing on a machine where `$claude` is what Codex lists.


def test_a_gstack_style_codex_install_resolves_the_environment_provided_skills(
    tmp_path, monkeypatch
):
    _codex_install(tmp_path, monkeypatch, env_dir_prefix="gstack-")
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out


def _codex_home_with(tmp_path, monkeypatch, skills):
    """`$CODEX_HOME/skills/<dir>/SKILL.md` with the given text each, and no plugins."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    for dirname, text in skills.items():
        d = home / "skills" / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    _stub_codex_on_path(tmp_path, monkeypatch, {})
    return home


def test_codex_discovery_reports_the_declared_name_not_the_directory(
    tmp_path, monkeypatch
):
    home = _codex_home_with(
        tmp_path,
        monkeypatch,
        {
            "gstack-claude": "---\nname: claude\ndescription: x\n---\nbody\n",
            "quoted": '---\nname: "beta"\ndescription: x\n---\n',
            "single-quoted": "---\nname: 'gamma'\ndescription: x\n---\n",
            "nameless": "---\ndescription: no name\n---\n",  # outside the subset
        },
    )
    found = codex_host.codex_skill_names(f"{home}/skills/*/SKILL.md")
    assert {"claude", "beta", "gamma"} <= found
    assert not {"gstack-claude", "quoted", "single-quoted", "nameless"} & found


def test_a_codex_directory_named_after_a_requirement_does_not_satisfy_it(
    tmp_path, monkeypatch
):
    """Name, not directory, is what Codex resolves `$claude` against. A directory called
    `claude` that declares another name is not `$claude`, and a `gstack-claude` directory that
    declares `gstack-claude` is not either: there is no prefix or suffix matching."""
    home = _codex_home_with(
        tmp_path,
        monkeypatch,
        {
            "claude": "---\nname: something-else\ndescription: d\n---\n",
            "gstack-claude": "---\nname: gstack-claude\ndescription: d\n---\n",
        },
    )
    assert "claude" not in codex_host.codex_skill_names(f"{home}/skills/*/SKILL.md")


def test_a_name_line_outside_the_frontmatter_is_not_a_declared_name(
    tmp_path, monkeypatch
):
    home = _codex_home_with(
        tmp_path,
        monkeypatch,
        {
            "no-frontmatter": "name: claude\n",
            "body-only": "---\ndescription: x\n---\nname: claude\n",
            "nested": "---\ndescription: x\nmetadata:\n  name: claude\n---\n",
            "empty-name": '---\nname: ""\ndescription: x\n---\n',
        },
    )
    found = codex_host.codex_skill_names(f"{home}/skills/*/SKILL.md")
    assert "claude" not in found
    # codex-cli 0.155.0 lists each of these under its directory name
    # Each of these is also outside conductor's strict subset, so none is read at all.
    assert not {"body-only", "nested", "empty-name"} & found


def test_a_codex_name_outside_the_flat_subset_is_not_read(tmp_path, monkeypatch):
    """A trailing comment is valid YAML that Codex loads, but it is outside the subset
    conductor reads exactly, so the file reads as not loadable: the safe direction."""
    home = _codex_home_with(
        tmp_path,
        monkeypatch,
        {
            "commented": "---\nname: delta # trailing\ndescription: x\n---\n",
            "crlf": "---\r\nname: eps\r\ndescription: x\r\n---\r\n",
        },
    )
    found = codex_host.codex_skill_names(f"{home}/skills/*/SKILL.md")
    assert "eps" in found
    assert not {"delta", "commented"} & found


def test_a_codex_plugin_skill_is_qualified_by_its_declared_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    plug = tmp_path / "spec-craft"
    (plug / ".codex-plugin").mkdir(parents=True)
    (plug / ".codex-plugin" / "plugin.json").write_text('{"name": "spec-craft"}')
    skill = plug / "skills" / "spec-craft-expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    _stub_codex_on_path(tmp_path, monkeypatch, {})
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert "spec-craft:expectations" in found
    assert "spec-craft:spec-craft-expectations" not in found


def test_claude_discovery_still_names_a_user_skill_by_its_directory(
    tmp_path, monkeypatch
):
    """Claude Code's rule is the other one: a user skill is invoked by its directory name
    (`~/.claude/skills/connect-chrome/` declaring `name: open-gstack-browser` is
    `/connect-chrome`). Codex's rule must not leak into the Claude adapter."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    d = home / "skills" / "connect-chrome"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: open-gstack-browser\ndescription: d\n---\n")
    found = preflight.available_commands(host_id="claude")
    assert "connect-chrome" in found
    assert "open-gstack-browser" not in found


def test_required_commands_swap_only_the_opposite_host_wrapper():
    claude = preflight.required_commands("claude")
    codex = preflight.required_commands("codex")
    assert len(claude) == len(codex)
    differ = [(a, b) for a, b in zip(claude, codex) if a != b]
    assert differ == [("codex", "claude")]


def test_missing_names_are_rendered_in_the_hosts_own_invocation_form():
    on_claude = preflight.check(available=set(), host_id="claude")["missing"]
    on_codex = preflight.check(available=set(), host_id="codex")["missing"]
    assert "/spec-craft:expectations" in on_claude
    assert "/document-release" in on_claude
    # Codex names an installed plugin's skill `<plugin>:<skill>` and matches a `$` mention
    # exactly, so the qualifier is kept; a bare `$expectations` resolves to nothing.
    assert "$spec-craft:expectations" in on_codex
    assert "$document-release" in on_codex


def test_a_codex_run_does_not_resolve_a_plugin_qualified_skill_from_a_flat_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    d = tmp_path / "codex-home" / "skills" / "expectations"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    _stub_codex_on_path(
        tmp_path, monkeypatch, {}, catalog=[("expectations", d / "SKILL.md", None)]
    )
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert "$spec-craft:expectations" in out["missing"]


def test_a_same_named_skill_from_another_plugin_is_not_the_required_one_on_either_host():
    """The false pass: `unrelated:expectations` is attributable, and it is attributed to a
    plugin that is NOT spec-craft. Accepting it reports the stack healthy while spec-craft is
    absent — and a hostile plugin only has to ship a same-named skill to be invoked in its
    place. The two hosts must agree, because the fact ("spec-craft is not installed") is not a
    host-specific fact."""
    avail = {"unrelated:expectations"}
    on_claude = preflight.check(
        required=["spec-craft:expectations"], available=avail, host_id="claude"
    )
    on_codex = preflight.check(
        required=["spec-craft:expectations"], available=avail, host_id="codex"
    )
    assert not on_claude["ok"]
    assert not on_codex["ok"], on_codex
    assert on_codex["missing"] == ["$spec-craft:expectations"]


@pytest.mark.parametrize("host_id", ["claude", "codex"])
def test_a_bare_same_named_skill_is_not_the_plugin_skill_on_either_host(host_id):
    """A bare `expectations` answers to `/expectations` or `$expectations`, never to the
    qualified name the recipe invokes. Both hosts now agree: the requirement is missing, and the
    advice names the plugin that ships it."""
    out = preflight.check(
        required=["spec-craft:expectations"],
        available={"expectations"},
        host_id=host_id,
    )
    rendered = base.load(host_id).native_invocation("spec-craft:expectations")
    assert not out["ok"], out
    assert out["missing"] == [rendered]
    assert out["unverified"] == []
    line = next(a for a in out["advice"] if a.startswith(rendered))
    assert "`spec-craft` plugin" in line


def test_a_codex_skill_attributed_to_the_required_plugin_is_a_clean_pass():
    out = preflight.check(
        required=["spec-craft:expectations"],
        available={"spec-craft:expectations"},
        host_id="codex",
    )
    assert out["ok"], out
    assert out["unverified"] == []


def test_an_unqualified_requirement_is_satisfied_by_any_plugins_copy_on_claude():
    """`code-review` is environment-provided: the requirement names no plugin, so no plugin
    identity is being claimed and there is nothing to verify. Claude resolves a bare slash
    command to a plugin's skill of that name."""
    out = preflight.check(
        required=["code-review"], available={"gstack:code-review"}, host_id="claude"
    )
    assert out["ok"] and out["unverified"] == []


def test_an_unqualified_requirement_needs_an_exact_name_on_codex():
    """Codex matches a `$` mention against the exact skill name, and a plugin's skill is named
    `<plugin>:<skill>`, so `$code-review` does not reach `gstack:code-review`."""
    out = preflight.check(
        required=["code-review"], available={"gstack:code-review"}, host_id="codex"
    )
    assert not out["ok"]
    assert out["missing"] == ["$code-review"]


def test_codex_recovers_plugin_identity_from_the_installed_plugin_list(
    tmp_path, monkeypatch
):
    """Identity IS recoverable for a plugin-installed skill: `codex plugin list --json` reports
    each installed plugin's identity, the install root derives from it, and its skills live
    under that root. The on-disk evidence discovery reports when Codex's catalog is missing
    must use it rather than flattening every skill into an unattributable bare name — and it
    stays evidence: nothing found this way is counted as invocable."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    skill = _installed_root(home, "spec-craft") / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    _stub_codex_on_path(
        tmp_path, monkeypatch, {"spec-craft": tmp_path / "marketplace" / "spec-craft"}
    )
    snapshot = codex_host.CodexAdapter().host_skills(project_root=str(tmp_path / "p"))
    assert "spec-craft:expectations" in snapshot.on_disk
    assert "expectations" not in snapshot.on_disk
    assert "spec-craft:expectations" not in snapshot.commands


def test_a_second_marketplace_shipping_a_same_named_plugin_greens_nothing(
    tmp_path, monkeypatch
):
    """The false pass the three-state result was supposed to close, one level down. `pluginId`
    and `marketplaceName` are the identity Codex uses; `name` is a self-declared string, so
    `conductor@evil-market` shipping a copied `start` skill is attributed as `conductor:start`
    exactly like the real one — and `installed[]` lists it FIRST, so it wins. Conductor has no
    trust list and cannot rank the two, so an ambiguous name must claim nothing at all."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    catalog = []
    for market in ("evil-market", _MARKET):
        skill = _installed_root(home, "spec-craft", market) / "skills" / "expectations"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: expectations\ndescription: d\n---\n"
        )
        catalog.append(
            ("spec-craft:expectations", skill / "SKILL.md", f"spec-craft@{market}")
        )
    _stub_codex_on_path(
        tmp_path,
        monkeypatch,
        {
            ("spec-craft", "evil-market"): tmp_path / "evil" / "spec-craft",
            ("spec-craft", _MARKET): tmp_path / "curated" / "spec-craft",
        },
        catalog=catalog,
    )

    out = preflight.check(
        required=["spec-craft:expectations"],
        host_id="codex",
        project_root=str(tmp_path / "project"),
    )

    assert not out["ok"], out
    assert out["unverified"] == ["$spec-craft:expectations"], out
    line = next(a for a in out["advice"] if a.startswith("$spec-craft:expectations"))
    assert "more than one installed plugin" in line, line
    assert "install it" not in line.lower(), line


def test_a_disabled_spec_craft_is_not_a_healthy_conducted_stack(tmp_path, monkeypatch):
    """`codex plugin list` still reports a disabled plugin as installed, and its tree is still
    on disk — but the loader stops before its skills, so every `spec-craft:expectations` call
    resolves to nothing. Preflight must report that, not green on a directory listing."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    skill = _installed_root(home, "spec-craft") / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\n---\n")
    _stub_codex_on_path(
        tmp_path,
        monkeypatch,
        {"spec-craft": tmp_path / "marketplace" / "spec-craft"},
        disabled=("spec-craft",),
    )

    avail = preflight.available_commands(host_id="codex")

    assert "spec-craft:expectations" not in avail, avail


def test_the_marketplace_source_copy_is_not_the_plugin_codex_loads(
    tmp_path, monkeypatch
):
    """`source.path` names the tree the plugin was fetched FROM; installing copies it elsewhere
    and the loader reads the copy. This machine is what an interrupted upgrade leaves behind: a
    complete marketplace source and an installed root with nothing in it. `plugin list` still
    reports the plugin, so preflight must NOT report the stack healthy off the source copy."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    _installed_root(home, "spec-craft").mkdir(parents=True)  # installed, and gutted
    source = tmp_path / "marketplace" / "plugins" / "spec-craft"
    (source / "skills" / "expectations").mkdir(parents=True)
    (source / "skills" / "expectations" / "SKILL.md").write_text("---\n---\n")
    _stub_codex_on_path(tmp_path, monkeypatch, {"spec-craft": source})

    avail = preflight.available_commands(host_id="codex")

    assert "spec-craft:expectations" not in avail, avail
    assert "expectations" not in avail, avail


def test_the_plugin_lookup_agrees_with_the_program_the_cron_driver_runs(
    tmp_path, monkeypatch
):
    """Two parsers of one JSON shape drift. The driver cannot import conductor — that is the
    problem it solves — so the only defence is checking them against each other. The wider
    version of this, run over a payload RECORDED from the real binary in every non-happy state,
    lives in `tests/conductor/hosts/test_codex_plugin_list.py`."""
    import subprocess
    import sys

    from conductor.hosts import codex

    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _installed_root(home, "spec-craft").mkdir(parents=True)  # `other` stays uninstalled
    payload = _plugin_list_json(
        {"spec-craft": tmp_path / "market" / "sc", "other": tmp_path / "market" / "o"}
    )
    from_python = codex.plugin_roots_from_json(payload)
    assert from_python == {"spec-craft": str(_installed_root(home, "spec-craft"))}
    for name in ("spec-craft", "other", "absent"):
        proc = subprocess.run(
            [sys.executable, "-c", codex.PLUGIN_ROOT_SNIPPET, name],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.stdout.strip() == from_python.get(name, ""), (name, proc.stderr)


def test_a_claude_run_does_not_accept_an_unnamespaced_plugin_skill():
    # The regression floor for the qualifier: under Claude, a bare `expectations` is NOT
    # `/spec-craft:expectations`, and loosening that would green a machine where the plugin
    # is not installed at all.
    out = preflight.check(available={"expectations"}, host_id="claude")
    assert "/spec-craft:expectations" in out["missing"]


def test_available_commands_uses_the_requested_hosts_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    d = tmp_path / "codex-home" / "skills" / "only-on-codex"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: d\n---\n")
    _stub_codex_on_path(
        tmp_path, monkeypatch, {}, catalog=[("only-on-codex", d / "SKILL.md", None)]
    )
    assert "only-on-codex" in preflight.available_commands(host_id="codex")
    assert "only-on-codex" not in preflight.available_commands(host_id="claude")


# ------------------------------------------------- A1: a missing skill must name its installer
#
# Verified by the A3 packaging probe against codex-cli 0.147.0: `.codex-plugin/plugin.json` has
# no `dependencies` field — the 180 manifests in the installed curated catalog use exactly
# twelve fields and that is not one of them — and Codex silently accepts unknown fields, so it
# cannot be added to make it work. Under Claude, `.claude-plugin/plugin.json` declares
# `dependencies: ["spec-craft"]` and installing conductor pulls spec-craft with it. Under Codex
# NOTHING does. Conductor's own skills then invoke a spec-craft skill that resolves to nothing,
# and the failure surfaces mid-run instead of at install time. Preflight is the Track A answer,
# so "missing" is not enough: it has to name the thing to install.


def test_a_codex_install_without_spec_craft_fails_closed(tmp_path, monkeypatch):
    _codex_install(
        tmp_path, monkeypatch, without=("expectations", "executable-assertions")
    )
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"]
    assert "$spec-craft:expectations" in out["missing"]
    assert "$spec-craft:executable-assertions" in out["missing"]


def test_the_advice_for_a_missing_plugin_skill_names_the_plugin(tmp_path, monkeypatch):
    _codex_install(tmp_path, monkeypatch, without=("expectations",))
    advice = preflight.check(project_root=str(tmp_path / "project"))["advice"]
    # The SKILL'S OWN line must name it, not merely the trailing NOTE. Asserting against the
    # joined text passes with the per-skill half deleted, because the NOTE repeats the plugin
    # names — a needle that survives its own fix is the hole this repo keeps falling into.
    line = next(a for a in advice if a.startswith("$spec-craft:expectations"))
    assert "spec-craft" in line


def test_a_host_that_resolves_dependencies_does_not_claim_manual_installation():
    on_claude = "\n".join(preflight.check(available=set(), host_id="claude")["advice"])
    assert "does not resolve plugin dependencies" not in on_claude


def test_a_host_that_does_not_resolve_dependencies_says_so_once_and_actionably():
    on_codex = "\n".join(preflight.check(available=set(), host_id="codex")["advice"])
    assert "does not resolve plugin dependencies" in on_codex
    # the actionable half: which plugin, not merely that dependencies are unresolved
    assert "spec-craft" in on_codex


def test_advice_is_empty_when_nothing_is_missing():
    assert preflight.check(available=_ALL, host_id="claude")["advice"] == []


def test_every_missing_skill_gets_exactly_one_advice_line():
    out = preflight.check(available=set(), host_id="codex")
    named = [a for a in out["advice"] if a.startswith("$")]
    assert len(named) == len(out["missing"])


def test_an_environment_provided_skill_is_not_advertised_as_a_plugin():
    advice = "\n".join(preflight.check(available=set(), host_id="claude")["advice"])
    line = next(a for a in advice.splitlines() if a.startswith("/document-release"))
    assert "environment-provided" in line


# ----------------------------------------------------------- A1: preflight DERIVES its own host
#
# Every host case above hands `check` its answer — as `host_id=` or through `$CONDUCTOR_HOST`.
# The one caller that matters does neither: `python -m conductor.preflight` resolves the host
# from the project, and the whole point of that resolution is choosing WHICH ROOT to look in.
# The two below supply nothing but the project, so the derivation is what is under test.


def _git_repo(tmp_path, name):
    proj = tmp_path / name
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    return proj


def _bin_dir_without_codex(tmp_path, monkeypatch):
    """A PATH carrying `git` and deliberately no `codex`.

    Without this the machine running the suite lends a wrongly-derived Codex check its own real
    `codex plugin list`, and a wrong root becomes indistinguishable from a right one.
    """
    bindir = tmp_path / "no-codex-bin"
    bindir.mkdir(exist_ok=True)
    _link_git(bindir)
    monkeypatch.setenv("PATH", str(bindir))


def test_a_project_with_no_recorded_host_is_preflighted_against_the_claude_root(
    tmp_path, monkeypatch
):
    """No `host_id=`, no `$CONDUCTOR_HOST`, no `.conductor/host` — the pre-A1 state every
    existing run is in. Nine of the ten requirements resolve out of the Claude plugin cache and
    the withheld one is reported in Claude's own slash form, which no Codex-rooted check could
    produce."""
    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    proj = _git_repo(tmp_path, "proj")
    _claude_install(tmp_path, monkeypatch, without=("expectations",))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    _bin_dir_without_codex(tmp_path, monkeypatch)

    out = preflight.check(project_root=str(proj))

    assert out["missing"] == ["/spec-craft:expectations"], out


def test_check_discovers_the_projects_own_codex_skills_not_the_current_directorys(
    tmp_path, monkeypatch
):
    """``./.codex/skills/`` is one of the three verified Codex roots, and ``check`` is asked
    about a PROJECT — never about wherever the process happens to be standing. Dropping
    ``project_root`` on the way to discovery substitutes the cwd for it, which is some other
    tree entirely under cron and in this suite alike."""
    proj = tmp_path / "proj"
    skill = proj / ".codex" / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    _bin_dir_without_codex(tmp_path, monkeypatch)

    out = preflight.check(
        required=["expectations"], host_id="codex", project_root=str(proj)
    )

    # No Codex to confirm it, so not a pass — but the project's own tree was the one scanned.
    assert out["unverified"] == ["$expectations"], out
    assert any("is on disk" in line for line in out["advice"]), out


def test_a_cached_plugin_is_named_by_its_cache_path_not_by_its_manifest(
    tmp_path, monkeypatch
):
    """Claude's marketplace-cache leg takes the plugin name from the
    ``plugins/cache/<marketplace>/<plugin>/<version>/`` segment, which is what conductor's
    preflight has always done. Every other fixture here builds a cache whose path segment and
    manifest agree, so nothing could tell the two sources apart — and switching to the manifest
    would silently re-namespace every installed plugin's skills out from under the required
    set. This is the one fixture where they disagree."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    root = home / "plugins" / "cache" / "market" / "spec-craft" / "1.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "renamed-upstream"}')
    skill = root / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\n---\n")

    avail = preflight.available_commands(host_id="claude")

    assert "spec-craft:expectations" in avail
    assert "renamed-upstream:expectations" not in avail


def test_a_manifestless_plugin_dir_contributes_nothing_on_either_host(
    tmp_path, monkeypatch
):
    """A ``--plugin-dir`` root is named by its manifest, so a root without one is not "the
    plugin its directory is called" — it is unattributable. Yielding its bare skill names would
    satisfy ``spec-craft:expectations`` on a machine where spec-craft is not installed at all:
    as a straight pass on Claude, and by downgrading the requirement to ``unverified`` on
    Codex, which is the same false green one report line further down."""
    plug = tmp_path / "spec-craft"
    skill = plug / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\ndescription: d\n---\n")
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    _bin_dir_without_codex(tmp_path, monkeypatch)

    for host in ("claude", "codex"):
        avail = preflight.available_commands(
            host_id=host, project_root=str(tmp_path / "project")
        )
        assert "expectations" not in avail, host
        assert "spec-craft:expectations" not in avail, host


def test_a_project_recorded_as_codex_is_preflighted_against_the_codex_root(
    tmp_path, monkeypatch
):
    """The same derivation with the durable recording as its only input. The Claude root is
    empty here, so resolving to the legacy default would report all ten missing in slash form
    rather than the one withheld skill in Codex's."""
    from conductor.hosts import runhost

    proj = _git_repo(tmp_path, "proj")
    _codex_install(tmp_path, monkeypatch, pin_host=False, without=("expectations",))
    runhost.record(str(proj), "codex")

    out = preflight.check(project_root=str(proj))

    assert out["missing"] == ["$spec-craft:expectations"], out


# ------------------------------------------- a host that cannot be asked is not a host with nothing

#: `sleep`, resolved off the DEFAULT path rather than the ambient one. The fixtures below run
#: after `$PATH` has already been narrowed to a stub directory, so `shutil.which("sleep")` there
#: answers None and the "hanging" fake exits 127 instantly — an answering fake wearing a hanging
#: fake's name, which passes the very code it exists to falsify.
_SLEEP = shutil.which("sleep", path=os.defpath)
assert _SLEEP, (
    "no `sleep` on the default path; the hanging-host fixtures cannot be built"
)


def _codex_that_never_answers(tmp_path, monkeypatch, *, timeout=0.5):
    """Replace the stub `codex` with one that outlives its own bound.

    Written into the SAME `stub-bin` `_codex_install` put on PATH, so the machine underneath is
    a fully installed one: every plugin root is on disk and every skill is really there. The
    only thing wrong with it is that Codex will not answer, which is the whole point — a
    degrade that reports this machine as missing its stack is reporting a fact that is false.
    """
    bindir = tmp_path / "stub-bin"
    exe = bindir / "codex"
    # `sleep` by ABSOLUTE path: PATH here is `stub-bin` plus nothing, so a bare `sleep` would
    # not resolve and the fake would answer instantly instead of hanging.
    exe.write_text(f"#!/bin/sh\nexec {_SLEEP} 60\n")
    os.chmod(exe, 0o755)
    _link_git(bindir)
    monkeypatch.setattr(codex_host, "PLUGIN_LIST_TIMEOUT_S", timeout)
    return bindir


def test_an_unanswerable_plugin_probe_degrades_to_unverified_never_missing(
    tmp_path, monkeypatch
):
    """The machine is fully installed and Codex will not say so. `missing` is then a false
    statement about the owner's disk, and it comes with the one instruction that cannot help:
    install what is already there. `unverified` is the honest verdict — and it is still not a
    pass, so the run stops either way."""
    _codex_install(tmp_path, monkeypatch)
    _codex_that_never_answers(tmp_path, monkeypatch)

    out = preflight.check(project_root=str(tmp_path / "project"))

    assert not out["ok"], out
    assert out["missing"] == [], out
    assert "$spec-craft:expectations" in out["unverified"], out
    assert "$superpowers:writing-plans" in out["unverified"], out


def test_the_expiry_advice_names_the_probe_instead_of_prescribing_a_reinstall(
    tmp_path, monkeypatch
):
    """`unverified` has three causes and an expiry shares a remedy with neither of the others:
    nothing is known about any skill, so "install the plugin" and "repair a broken install" are
    both guesses. The only actionable thing is the probe that expired and how to re-run it."""
    _codex_install(tmp_path, monkeypatch)
    _codex_that_never_answers(tmp_path, monkeypatch)

    advice = "\n".join(
        preflight.check(project_root=str(tmp_path / "project"))["advice"]
    )

    assert "install it" not in advice.lower(), advice
    assert "unattributed skill" not in advice.lower(), advice
    assert "not on disk" not in advice.lower(), advice
    assert "plugin list --json" in advice, advice  # the operation that expired
    assert "no write occurred" in advice, advice  # a read-only probe, and it says so


def test_the_expiry_keeps_the_names_that_were_established_before_the_probe(
    tmp_path, monkeypatch
):
    """Anti-over-correction: degrading must not also discard what discovery already knew.
    `conductor:autodev` resolves from conductor's own checkout and never depended on the probe, so a
    degrade that reports it unresolved has thrown away a fact that was never in doubt."""
    _codex_install(tmp_path, monkeypatch)
    _codex_that_never_answers(tmp_path, monkeypatch)

    out = preflight.check(
        required=["conductor:autodev"], project_root=str(tmp_path / "project")
    )

    assert out["ok"], out


def test_a_probe_expiry_does_not_crash_the_preflight_entry_point(tmp_path, monkeypatch):
    """The user-visible contract under a hung host: a report and a non-zero exit, never a
    traceback. An unhandled `HostProbeTimeout` reaching `__main__` would replace the advice an
    owner needs with a stack trace, and `conductor preflight` is step 0 of every start."""
    _codex_install(tmp_path, monkeypatch)
    bindir = _codex_that_never_answers(tmp_path, monkeypatch, timeout=1.0)

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "conductor.preflight"],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.defpath}",
            "PYTHONPATH": os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
            "CONDUCTOR_HOST": "codex",
            "CODEX_HOME": str(tmp_path / "codex-home"),
        },
    )

    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "UNVERIFIED:" in proc.stderr, proc.stderr
    assert "MISSING:" not in proc.stderr, proc.stderr
