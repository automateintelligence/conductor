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
    avail = preflight.available_commands(claude_home=str(tmp_path / "empty"))
    assert "spec-craft:expectations" in avail


def test_discovers_conductor_own_root():  # dogfood: conductor's own skills always resolve
    avail = preflight.available_commands(claude_home="/nonexistent")
    assert "conductor:assertions-to-tests" in avail
