"""The `HostAdapter` members A1 actually consumes.

A1 is a SUBSET of Plan 04's nineteen-member protocol, not a competitor: it implements only
`source_root`, `native_invocation`, and the per-host command discovery the preflight needs.
Everything else stays `...` until Plan 04 fills it in.

`native_invocation` is the load-bearing one. Ground truth
(`docs/reviews/2026-08-12-codex-host-ground-truth.md` §"Skill invocation under Codex") pins
`$name` as Codex's convention and `/plugin:skill` as Claude's, and it also records that Codex
skill directories are FLAT (`~/.codex/skills/<name>/`) with no plugin-namespace counterpart —
so the qualifier is a Claude-side concept that the Codex renderer drops.
"""

from __future__ import annotations

import os

import pytest

from conductor.hosts import base


@pytest.fixture
def adapters():
    return {host_id: base.load(host_id) for host_id in base.HOST_IDS}


def test_claude_source_root_defaults_to_the_dot_claude_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert base.load("claude").source_root() == str(tmp_path / ".claude")


def test_claude_source_root_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert base.load("claude").source_root() == str(tmp_path / "elsewhere")


def test_codex_source_root_defaults_to_the_dot_codex_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert base.load("codex").source_root() == str(tmp_path / ".codex")


def test_codex_source_root_honours_codex_home(monkeypatch, tmp_path):
    # Verified in ground truth §"Session and config isolation": auth always uses CODEX_HOME,
    # so it is the Codex config root even when `--ignore-user-config` is in play.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "scratch"))
    assert base.load("codex").source_root() == str(tmp_path / "scratch")


def test_the_two_source_roots_are_never_the_same_directory(adapters):
    assert adapters["claude"].source_root() != adapters["codex"].source_root()


def test_claude_renders_a_plugin_qualified_skill_as_a_slash_command(adapters):
    assert (
        adapters["claude"].native_invocation("conductor:autodev")
        == "/conductor:autodev"
    )


def test_claude_renders_an_unqualified_skill_as_a_bare_slash_command(adapters):
    assert adapters["claude"].native_invocation("code-review") == "/code-review"


def test_codex_renders_a_skill_with_the_dollar_convention(adapters):
    assert adapters["codex"].native_invocation("code-review") == "$code-review"


def test_codex_drops_the_plugin_qualifier_because_its_skill_dirs_are_flat(adapters):
    assert adapters["codex"].native_invocation("conductor:autodev") == "$autodev"


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_native_invocation_is_idempotent_over_an_already_rendered_name(
    adapters, host_id
):
    # The requirement list is written once in host-neutral form, but a caller that renders
    # twice (a message quoting a name that preflight already rendered) must not produce
    # `//code-review` or `$$code-review`.
    once = adapters[host_id].native_invocation("code-review")
    assert adapters[host_id].native_invocation(once) == once


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_discovered_commands_finds_a_bare_user_skill_under_the_host_source_root(
    monkeypatch, tmp_path, host_id
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    root = tmp_path / f".{host_id}"
    skill = root / "skills" / "document-release"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: document-release\n---\n")
    assert "document-release" in base.load(host_id).discovered_commands()


def test_claude_discovers_the_marketplace_plugin_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    cached = tmp_path / ".claude" / "plugins" / "cache" / "market" / "gstack" / "1.0"
    (cached / "skills" / "code-review").mkdir(parents=True)
    (cached / "skills" / "code-review" / "SKILL.md").write_text("---\n---\n")
    (cached / "commands").mkdir()
    (cached / "commands" / "browse.md").write_text("x")
    found = base.load("claude").discovered_commands()
    assert "gstack:code-review" in found
    assert "gstack:browse" in found


def test_codex_discovers_a_project_local_skill(monkeypatch, tmp_path):
    # Verified in ground truth: the AGENTS.md dispatch table resolves `./.codex/skills/`,
    # not only `~/.codex/skills/`. A repo-local conducted skill must therefore count.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    project = tmp_path / "project"
    skill = project / ".codex" / "skills" / "code-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: code-review\n---\n")
    assert "code-review" in base.load("codex").discovered_commands(
        project_root=str(project)
    )


def test_codex_discovers_a_prompt_as_a_command(monkeypatch, tmp_path):
    # Verified: `~/.codex/prompts/*.md` are the Codex analogue of Claude's slash commands.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    prompts = tmp_path / "codex-home" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "document-release.md").write_text("---\ndescription: x\n---\n")
    assert "document-release" in base.load("codex").discovered_commands()


@pytest.mark.parametrize(
    "host_id,manifest_dir", [("claude", ".claude-plugin"), ("codex", ".codex-plugin")]
)
def test_each_host_reads_its_own_plugin_manifest(
    monkeypatch, tmp_path, host_id, manifest_dir
):
    # Verified in ground truth §"Claude vs Codex": `.claude-plugin/plugin.json` vs
    # `.codex-plugin/plugin.json`. A shared reader would namespace a Codex plugin's skills
    # under nothing at all.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere-codex"))
    plug = tmp_path / "spec-craft"
    (plug / manifest_dir).mkdir(parents=True)
    (plug / manifest_dir / "plugin.json").write_text('{"name": "spec-craft"}')
    skill = plug / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: expectations\n---\n")
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    found = base.load(host_id).discovered_commands()
    assert "spec-craft:expectations" in found


def test_a_host_ignores_the_other_hosts_plugin_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere-codex"))
    plug = tmp_path / "spec-craft"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "spec-craft"}')
    skill = plug / "skills" / "expectations"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\n---\n")
    monkeypatch.setenv("CONDUCTOR_PLUGIN_DIRS", str(plug))
    assert "spec-craft:expectations" not in base.load("codex").discovered_commands()


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_every_host_discovers_conductors_own_skills_from_its_checkout(host_id):
    # Dogfood invariant, preserved from the Claude-only preflight: whatever else is or is not
    # installed, the copy of conductor that is RUNNING can always resolve its own skills.
    found = base.load(host_id).discovered_commands()
    assert "conductor:assertions-to-tests" in found


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_discovery_survives_a_missing_source_root(monkeypatch, tmp_path, host_id):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    assert isinstance(base.load(host_id).discovered_commands(), set)
    assert not os.path.exists(str(tmp_path / "absent"))
