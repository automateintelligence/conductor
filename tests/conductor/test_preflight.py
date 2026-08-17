import json
import os

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


def _plugin_list_json(roots):
    """What `codex plugin list --json` prints — the shape verified on codex-cli 0.147.0."""
    return json.dumps(
        {
            "installed": [
                {
                    "pluginId": f"{name}@openai-curated",
                    "name": name,
                    "marketplaceName": "openai-curated",
                    "version": "d6169bef",
                    "installed": True,
                    "enabled": True,
                    "source": {"source": "local", "path": str(path)},
                }
                for name, path in roots.items()
            ]
        }
    )


def _stub_codex_on_path(tmp_path, monkeypatch, roots):
    """A `codex` on PATH that reports `roots` as its installed plugins."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    codex = bindir / "codex"
    codex.write_text(
        f"#!/bin/sh\nprintf '%s' '{_plugin_list_json(roots)}'\nexit 0\n",
    )
    os.chmod(codex, 0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


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
    tmp_path, monkeypatch, *, review_wrapper="claude", flat_only=False, without=()
):
    """A Codex machine with the conducted stack installed the way Codex installs it.

    Plugin-owned skills live inside their plugin's root, which `codex plugin list --json`
    reports — that is what makes `spec-craft:expectations` attributable at all. Environment-
    provided ones are flat under `$CODEX_HOME/skills/`, which is where a user skill lives.

    `flat_only` is the machine where someone copied the skill directories in by hand: every
    name resolves, and not one of them can be attributed to the plugin that is supposed to
    own it.
    """
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    for name in ("code-review", review_wrapper, "document-release"):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    roots = {}
    for plugin, skills in _PLUGIN_SKILLS.items():
        root = tmp_path / "codex-cache" / plugin
        for name in skills:
            if name in without:
                continue
            d = (home / "skills" / name) if flat_only else (root / "skills" / name)
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        roots[plugin] = root
    _stub_codex_on_path(tmp_path, monkeypatch, {} if flat_only else roots)
    return home


def test_preflight_succeeds_against_a_codex_install(tmp_path, monkeypatch):
    _codex_install(tmp_path, monkeypatch)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out


def test_a_hand_copied_codex_stack_is_reported_unverifiable_not_healthy(
    tmp_path, monkeypatch
):
    """Every skill is present and invocable, so nothing is missing — but no plugin claims any
    of them, so preflight cannot say the conducted stack is the conducted stack."""
    _codex_install(tmp_path, monkeypatch, flat_only=True)
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"], out
    assert out["missing"] == []
    assert "$expectations" in out["unverified"]
    assert "$writing-plans" in out["unverified"]
    # environment-provided skills claim no plugin, so they are not in question
    assert "$document-release" not in out["unverified"]


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
    assert on_codex["missing"] == ["$expectations"]


def test_a_flat_codex_skill_is_reported_unverifiable_never_as_a_pass():
    """Codex skill dirs are flat, so a bare `expectations` under $CODEX_HOME/skills/ carries no
    plugin identity at all. It IS invocable as `$expectations`, so it is not missing — but
    preflight cannot justify calling it spec-craft's, and a gate that greens on what it cannot
    check is worth nothing."""
    out = preflight.check(
        required=["spec-craft:expectations"],
        available={"expectations"},
        host_id="codex",
    )
    assert not out["ok"], out
    assert out["unverified"] == ["$expectations"]
    assert out["missing"] == []
    line = next(a for a in out["advice"] if a.startswith("$expectations"))
    assert "cannot verify" in line and "spec-craft" in line


def test_a_codex_skill_attributed_to_the_required_plugin_is_a_clean_pass():
    out = preflight.check(
        required=["spec-craft:expectations"],
        available={"spec-craft:expectations"},
        host_id="codex",
    )
    assert out["ok"], out
    assert out["unverified"] == []


def test_an_unqualified_requirement_is_satisfied_by_any_plugins_copy_on_codex():
    """`code-review` is environment-provided: the requirement names no plugin, so no plugin
    identity is being claimed and there is nothing to verify."""
    out = preflight.check(
        required=["code-review"], available={"gstack:code-review"}, host_id="codex"
    )
    assert out["ok"] and out["unverified"] == []


def test_codex_recovers_plugin_identity_from_the_installed_plugin_list(
    tmp_path, monkeypatch
):
    """Identity IS recoverable for a plugin-installed skill: `codex plugin list --json` reports
    each installed plugin's name and root, and its skills live under that root. Discovery must
    use it rather than flattening every skill into an unattributable bare name."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    root = tmp_path / "cache" / "spec-craft"
    skill = root / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\n---\n")
    _stub_codex_on_path(tmp_path, monkeypatch, {"spec-craft": root})
    avail = preflight.available_commands(host_id="codex")
    assert "spec-craft:expectations" in avail
    assert "expectations" not in avail


def test_the_plugin_lookup_agrees_with_the_program_the_cron_driver_runs(tmp_path):
    """Two parsers of one JSON shape drift. The driver cannot import conductor — that is the
    problem it solves — so the only defence is checking them against each other."""
    import subprocess
    import sys

    from conductor.hosts import codex

    payload = _plugin_list_json(
        {"spec-craft": tmp_path / "sc", "other": tmp_path / "o"}
    )
    from_python = codex.plugin_roots_from_json(payload)
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
    _codex_install(
        tmp_path, monkeypatch, without=("expectations", "executable-assertions")
    )
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert not out["ok"]
    assert "$expectations" in out["missing"]
    assert "$executable-assertions" in out["missing"]


def test_the_advice_for_a_missing_plugin_skill_names_the_plugin(tmp_path, monkeypatch):
    _codex_install(tmp_path, monkeypatch, without=("expectations",))
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
