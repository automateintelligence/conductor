import pytest

from conductor import preflight

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
    (skill / "SKILL.md").write_text("---\nname: expectations\n---\n")
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    avail = preflight.available_commands()
    assert "spec-craft:expectations" in avail


def test_discovers_conductor_own_root(monkeypatch, tmp_path):
    # dogfood: conductor's own skills always resolve
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent"))
    avail = preflight.available_commands()
    assert "conductor:assertions-to-tests" in avail


# ------------------------------------------------------------------- A1: two host vocabularies
#
# The required set used to be ten Claude-form `/plugin:skill` literals, discovered under a
# `~/.claude` root through a Claude plugin-cache glob. Three separate things were wrong for a
# Codex user: the invocation form, the discovery root, and — least obviously — the identity of
# the opposite-host review wrapper, which is `/codex` only when the run is hosted on Claude.


def _codex_install(tmp_path, monkeypatch, *, review_wrapper="claude"):
    """A Codex machine with every conducted skill installed the way Codex installs them:
    flat directories under `$CODEX_HOME/skills/`, no plugin namespace anywhere."""
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    names = [
        "expectations",
        "executable-assertions",
        "assertions-to-tests",
        "subagent-driven-development",
        "requesting-code-review",
        "receiving-code-review",
        "writing-plans",
        "code-review",
        review_wrapper,
        "document-release",
    ]
    for name in names:
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    return home


def test_preflight_succeeds_against_a_codex_install(tmp_path, monkeypatch):
    _codex_install(tmp_path, monkeypatch)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out["missing"]


def test_preflight_still_succeeds_against_a_claude_install(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOST", "claude")
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    cache = home / "plugins" / "cache" / "market"
    for plugin, skills in (
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
    ):
        for skill in skills:
            d = cache / plugin / "1.0" / "skills" / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\n---\n")
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
    # Codex skill dirs are flat, so the plugin qualifier is dropped, not transliterated into
    # a `$spec-craft:expectations` that resolves to nothing.
    assert "$expectations" in on_codex
    assert "$document-release" in on_codex
    assert not any(":" in name for name in on_codex)


def test_a_codex_run_resolves_a_plugin_qualified_skill_from_a_flat_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    d = tmp_path / "codex-home" / "skills" / "expectations"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\n")
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert "$expectations" not in out["missing"]


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
    (d / "SKILL.md").write_text("---\n---\n")
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
    home = _codex_install(tmp_path, monkeypatch)
    for name in ("expectations", "executable-assertions"):
        (home / "skills" / name / "SKILL.md").unlink()
        (home / "skills" / name).rmdir()
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"]
    assert "$expectations" in out["missing"]
    assert "$executable-assertions" in out["missing"]


def test_the_advice_for_a_missing_plugin_skill_names_the_plugin(tmp_path, monkeypatch):
    home = _codex_install(tmp_path, monkeypatch)
    (home / "skills" / "expectations" / "SKILL.md").unlink()
    (home / "skills" / "expectations").rmdir()
    advice = preflight.check(project_root=str(tmp_path / "project"))["advice"]
    # The SKILL'S OWN line must name it, not merely the trailing NOTE. Asserting against the
    # joined text passes with the per-skill half deleted, because the NOTE repeats the plugin
    # names — a needle that survives its own fix is the hole this repo keeps falling into.
    line = next(a for a in advice if a.startswith("$expectations"))
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
