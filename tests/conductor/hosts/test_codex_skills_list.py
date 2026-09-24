"""Codex's own skill catalog as the source of truth for Codex discovery.

`codex app-server` answers `skills/list` with every skill the session would load: every root
Codex scans (`$CODEX_HOME/skills`, `~/.agents/skills`, the system cache, admin and project
layers, repo `.agents/skills`, installed plugins), the `<namespace>:` Codex derives from the
nearest plugin manifest, the `enabled` flag, and the SKILL.md files it rejected. Measured on
codex-cli 0.155.0: no auth needed, about 1.5s against a populated `$CODEX_HOME`, and it lists
`~/.agents/skills/superpowers/<skill>` as `superpowers:<skill>`.

These tests drive the stub `codex` in `tests/conductor/codex_stub.py`, which replays JSON-RPC
recorded from codex-cli 0.155.0 (`tests/conductor/fixtures/`). When Codex cannot
answer — not installed, an error, output that is not the protocol — discovery falls back to
scanning the filesystem, and there it applies Codex's own SKILL.md validity rule.
"""

from __future__ import annotations

import json
import pathlib
import time
import pytest

from conductor import preflight
from conductor.hosts import base, codex
from tests.conductor import codex_stub

_REQUIRED_ON_CODEX = (
    "spec-craft:expectations",
    "spec-craft:executable-assertions",
    "conductor:assertions-to-tests",
    "superpowers:subagent-driven-development",
    "superpowers:requesting-code-review",
    "superpowers:receiving-code-review",
    "superpowers:writing-plans",
    "code-review",
    "claude",
    "document-release",
)


def _skill(name, path=None, enabled=True, plugin_id=None):
    return {
        "name": name,
        "description": "d",
        "path": path or f"/skills/{name.replace(':', '/')}/SKILL.md",
        "scope": "user",
        "enabled": enabled,
        "pluginId": plugin_id,
    }


def _stub(tmp_path, monkeypatch, *, skills=(), errors=(), answer=None, hang=False):
    """A stub `codex` whose `app-server` answers `skills/list` with `skills` (or `answer`).

    The `initialize` reply is the one recorded from codex-cli 0.155.0; the `skills/list` reply
    has the recorded shape with the entries each test needs.
    """
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    if answer is None:
        answer = {
            "id": 1,
            "result": {
                "data": [
                    {
                        "cwd": str(tmp_path),
                        "skills": list(skills),
                        "errors": list(errors),
                    }
                ]
            },
        }
    initialize = codex_stub.recorded_app_server(
        codex_home=str(home), cwd=str(tmp_path), home=str(tmp_path)
    )[0]
    codex_stub.put_on_path(
        monkeypatch,
        tmp_path / "stub-bin",
        app_server=[initialize, json.dumps(answer)],
        hang=hang,
    )
    return home


def test_skills_codex_lists_are_what_discovery_reports(tmp_path, monkeypatch):
    _stub(
        tmp_path,
        monkeypatch,
        skills=[_skill("superpowers:writing-plans"), _skill("claude")],
    )
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert {"superpowers:writing-plans", "claude"} <= found


def test_the_catalog_is_asked_about_the_project_being_preflighted(
    tmp_path, monkeypatch
):
    """Repo `.agents/skills` and project `.codex/skills` depend on the cwd Codex is asked for."""
    project = tmp_path / "project"
    request = json.loads(codex._skills_list_request(str(project)).splitlines()[-1])
    assert request["method"] == "skills/list"
    assert request["params"]["cwds"] == [str(project)]


def test_a_codex_machine_equipped_through_agents_skills_passes_preflight(
    tmp_path, monkeypatch
):
    """superpowers installed the documented Codex way — `~/.agents/skills/superpowers` linked
    to a checkout carrying a plugin manifest — is listed by Codex as `superpowers:<skill>`."""
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    _stub(
        tmp_path,
        monkeypatch,
        skills=[
            _skill(name, plugin_id="spec-craft@market")
            if name.startswith("spec-craft:")
            else _skill(name)
            for name in _REQUIRED_ON_CODEX
        ],
    )
    out = preflight.check(project_root=str(tmp_path / "project"))
    assert out["ok"], out


def test_a_skill_on_disk_that_codex_does_not_list_is_not_counted(tmp_path, monkeypatch):
    """Codex's answer wins over the directory: a SKILL.md Codex rejected or never scanned is not
    invocable, however it looks on disk."""
    home = _stub(tmp_path, monkeypatch, skills=[])
    d = home / "skills" / "claude"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: claude\ndescription: d\n---\n")
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert "claude" not in found


def test_a_disabled_skill_is_not_counted(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, skills=[_skill("claude", enabled=False)])
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert "claude" not in found


def test_a_qualified_name_codex_lists_twice_is_unverified(tmp_path, monkeypatch):
    """Two roots answering to `spec-craft:expectations` — say an installed plugin and a
    manifest-namespaced copy under `~/.agents/skills` — is the contested case: invocable, but
    conductor cannot say which one Codex runs."""
    _stub(
        tmp_path,
        monkeypatch,
        skills=[
            _skill("spec-craft:expectations", path="/a/expectations/SKILL.md"),
            _skill("spec-craft:expectations", path="/b/expectations/SKILL.md"),
        ],
    )
    out = preflight.check(
        required=["spec-craft:expectations"],
        host_id="codex",
        project_root=str(tmp_path / "project"),
    )
    assert out["unverified"] == ["$spec-craft:expectations"], out


def _flat_claude(home):
    d = home / "skills" / "gstack-claude"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: claude\ndescription: d\n---\n")


def _assert_fails_closed(out, reason):
    """Codex did not supply its catalog, so a skill that is merely on disk is not a pass."""
    assert not out["ok"], out
    assert out["unverified"] == ["$claude"], out
    assert out["missing"] == [], out
    advice = "\n".join(out["advice"])
    assert reason in advice, advice
    assert "on disk" in advice, advice


@pytest.mark.parametrize(
    "answer",
    [
        {"id": 1, "error": {"code": -32601, "message": "method not found"}},
        {"id": 1, "result": {"unexpected": True}},
    ],
)
def test_an_unusable_answer_fails_closed(tmp_path, monkeypatch, answer):
    home = _stub(tmp_path, monkeypatch, answer=answer)
    _flat_claude(home)
    out = preflight.check(
        required=["claude"], host_id="codex", project_root=str(tmp_path / "project")
    )
    _assert_fails_closed(out, "skills/list")


def test_a_codex_that_exits_without_answering_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    codex_stub.put_on_path(monkeypatch, tmp_path / "stub-bin", app_server=None)
    _flat_claude(home)
    out = preflight.check(
        required=["claude"], host_id="codex", project_root=str(tmp_path / "project")
    )
    _assert_fails_closed(out, "skills/list")


def test_no_codex_on_path_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    bindir = tmp_path / "no-codex"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    _flat_claude(home)
    out = preflight.check(
        required=["claude"], host_id="codex", project_root=str(tmp_path / "project")
    )
    _assert_fails_closed(out, "not on PATH")


def test_a_timeout_never_counts_a_skill_found_only_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "SKILLS_LIST_TIMEOUT_S", 0.5)
    home = _stub(tmp_path, monkeypatch, hang=True)
    _flat_claude(home)
    out = preflight.check(
        required=["claude"], host_id="codex", project_root=str(tmp_path / "project")
    )
    _assert_fails_closed(out, "did not answer within")


def test_conductors_own_checkout_still_resolves_without_a_catalog(
    tmp_path, monkeypatch
):
    """The running checkout is evidence of itself; it is not read out of Codex's catalog."""
    _stub(tmp_path, monkeypatch, answer={"id": 1, "error": {}})
    out = preflight.check(
        required=["conductor:assertions-to-tests"],
        host_id="codex",
        project_root=str(tmp_path / "project"),
    )
    assert out["ok"], out


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "claude", "path": "/s/claude/SKILL.md"},
        {"name": "claude", "path": "/s/claude/SKILL.md", "enabled": "yes"},
        {"name": "claude", "path": "/s/claude/SKILL.md", "enabled": None},
    ],
)
def test_only_an_enabled_true_skill_is_counted(tmp_path, monkeypatch, entry):
    """`enabled` is a required `bool` in 0.155.0's `SkillMetadata`
    (`app-server-protocol/src/protocol/v2/plugin.rs`); an entry without a literal `true` is
    not one Codex marked usable."""
    _stub(tmp_path, monkeypatch, skills=[entry])
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert "claude" not in found


def test_the_catalog_probe_leaves_no_helper_process_behind(tmp_path, monkeypatch):
    """A Codex server may start helpers in its process group; they must not outlive the probe
    even when the server itself exits cleanly."""
    pidfile = tmp_path / "helper.pid"
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    project = tmp_path / "project"
    codex_stub.put_on_path(
        monkeypatch,
        tmp_path / "stub-bin",
        app_server=codex_stub.recorded_app_server(
            codex_home=str(home), cwd=str(project), home=str(tmp_path)
        ),
        helper_pidfile=pidfile,
    )
    assert codex.skills_list(project_root=str(project)) is not None
    pid = int(pidfile.read_text())
    assert not _running(pid), f"helper {pid} outlived the probe"


def _running(pid):
    """Alive and not a zombie awaiting its reaper."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def test_a_server_that_never_reads_cannot_block_the_request(tmp_path, monkeypatch):
    """The request is written inside the deadline: a server that never reads stdin fills the
    pipe, and a blocking write would wait forever before the bound was ever checked."""
    monkeypatch.setattr(codex, "SKILLS_LIST_TIMEOUT_S", 1)
    big = b"x" * (4 * 1024 * 1024)
    monkeypatch.setattr(codex, "_skills_list_request", lambda cwd: big)
    _stub(tmp_path, monkeypatch, hang=True)
    started = time.monotonic()
    with pytest.raises(base.HostProbeTimeout):
        codex.skills_list(project_root=str(tmp_path / "project"))
    assert time.monotonic() - started < 10


# ------------------------------------------------ fallback namer: Codex's SKILL.md validity rule
#
# `parse_skill_frontmatter_metadata` (codex-rs/skills/src/parser.rs, rust-v0.155.0): a `---`
# block with a closing `---`, parsed as YAML — and when that fails, re-parsed after quoting
# scalars that contain `: ` or start like a broken flow value. `name` defaults to the directory
# and is at most 64 characters; `description` is required and non-empty. Every case below was
# checked against a live 0.155.0 `skills/list` (see `test_codex_live.py`).

ACCEPTED = {
    "plain": ("---\ndescription: d\n---\n", "plain"),
    "block": ("---\nname: block\ndescription: |\n  spans\n  lines\n---\n", "block"),
    "max-name": (f"---\nname: {'m' * 64}\ndescription: d\n---\n", "m" * 64),
    "bracket": ("---\nname: bracket\ndescription: [\n---\n", "bracket"),
    "brace": ("---\nname: brace\ndescription: {\n---\n", "brace"),
    "colon": ("---\nname: colon\ndescription: Build for AWS: ECS\n---\n", "colon"),
    "mapdesc": ("---\nname: mapdesc\ndescription: {a: 1}\n---\n", "mapdesc"),
    "number": ("---\nname: number\ndescription: 42\n---\n", "number"),
    "tab": ("---\nname: tab\ndescription:\td\n---\n", "tab"),
    "multi": ("---\nname: multi\ndescription: first\n  continued\n---\n", "multi"),
    "quoted": ('---\nname: "beta"\ndescription: d\n---\n', "beta"),
}
REJECTED = {
    "no-description": "---\nname: no-description\n---\n",
    "empty-description": '---\nname: empty-description\ndescription: ""\n---\n',
    "null-description": "---\nname: null-description\ndescription: null\n---\n",
    "list-description": "---\nname: list-description\ndescription: [a, b]\n---\n",
    "list-name": "---\nname: [x]\ndescription: d\n---\n",
    "not-a-mapping": "---\n- a\n- b\n---\n",
    "empty-frontmatter": "---\n---\n",
    "no-frontmatter": "description: d\n",
    "unclosed": "---\nname: unclosed\ndescription: d\n",
    "long-name": f"---\nname: {'n' * 65}\ndescription: d\n---\n",
}


def _names(tmp_path, skills):
    for dirname, text in skills.items():
        d = tmp_path / "skills" / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    return codex.codex_skill_names(f"{tmp_path}/skills/*/SKILL.md")


def test_the_fallback_namer_accepts_what_codex_accepts(tmp_path):
    found = _names(tmp_path, {d: text for d, (text, _) in ACCEPTED.items()})
    assert found == {name for _, name in ACCEPTED.values()}


@pytest.mark.parametrize("dirname", sorted(REJECTED))
def test_the_fallback_namer_rejects_what_codex_rejects(tmp_path, dirname):
    assert _names(tmp_path, {dirname: REJECTED[dirname]}) == set()


# ------------------------------------------------------- recorded codex-cli 0.155.0 responses


def test_the_recorded_catalog_resolves_plugin_and_agents_skills(tmp_path, monkeypatch):
    """Replayed verbatim from a scratch `$CODEX_HOME` with spec-craft installed from its
    marketplace and superpowers reachable through `~/.agents/skills/superpowers`: Codex lists
    the plugin's skills and the manifest-namespaced ones both qualified."""
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_PLUGIN_DIRS", raising=False)
    codex_stub.put_on_path(
        monkeypatch,
        tmp_path / "stub-bin",
        plugin_list=codex_stub.recorded_plugin_list(codex_home=str(home)),
        app_server=codex_stub.recorded_app_server(
            codex_home=str(home), cwd=str(project), home=str(tmp_path)
        ),
    )
    found = preflight.available_commands(host_id="codex", project_root=str(project))
    assert {
        "spec-craft:expectations",
        "spec-craft:executable-assertions",
        "superpowers:writing-plans",
        "superpowers:subagent-driven-development",
    } <= found
    assert "expectations" not in found and "writing-plans" not in found

    out = preflight.check(project_root=str(project))
    assert out["unverified"] == [], out
    # That scratch home had no gstack install, so the environment-provided skills are absent.
    assert out["missing"] == ["$code-review", "$claude", "$document-release"], out


@pytest.mark.parametrize("dirname", sorted(REJECTED))
def test_without_pyyaml_the_namer_still_never_accepts_what_codex_rejects(
    tmp_path, monkeypatch, dirname
):
    monkeypatch.setattr(codex, "_yaml", None)
    assert _names(tmp_path, {dirname: REJECTED[dirname]}) == set()


def test_without_pyyaml_conductors_own_skills_still_resolve(monkeypatch):
    monkeypatch.setattr(codex, "_yaml", None)
    root = pathlib.Path(codex.__file__).resolve().parents[2]
    assert {"autodev", "start", "assertions-to-tests"} <= codex.codex_skill_names(
        f"{root}/skills/*/SKILL.md"
    )
