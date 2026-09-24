"""Codex's own skill catalog as the source of truth for Codex discovery.

`codex app-server` answers `skills/list` with every skill the session would load: every root
Codex scans (`$CODEX_HOME/skills`, `~/.agents/skills`, the system cache, admin and project
layers, repo `.agents/skills`, installed plugins), the `<namespace>:` Codex derives from the
nearest plugin manifest, the `enabled` flag, and the SKILL.md files it rejected. Measured on
codex-cli 0.155.0: no auth needed, about 1.5s against a populated `$CODEX_HOME`, and it lists
`~/.agents/skills/superpowers/<skill>` as `superpowers:<skill>`.

These tests drive a stub `codex` that speaks the same JSON-RPC over stdio. When Codex cannot
answer — not installed, an error, output that is not the protocol — discovery falls back to
scanning the filesystem, and there it applies Codex's own SKILL.md validity rule.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from conductor import preflight
from conductor.hosts import codex

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

_STUB = """#!{python}
import json, sys, time
args = sys.argv[1:]
if args[:3] == ["plugin", "list", "--json"]:
    print(json.dumps({plugins}))
    sys.exit(0)
if args[:1] != ["app-server"]:
    sys.exit(0)
if {hang}:
    time.sleep(60)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") == 0:
        print(json.dumps({{"id": 0, "result": {{}}}}), flush=True)
    elif msg.get("id") == 1:
        with open({record!r}, "w") as f:
            json.dump(msg, f)
        print(json.dumps({{"method": "remoteControl/status/changed", "params": {{}}}}), flush=True)
        print(json.dumps({answer}), flush=True)
"""


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
    """A `codex` on PATH whose `app-server` answers `skills/list` with `skills`."""
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
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "codex"
    record = tmp_path / "skills-list-request.json"
    exe.write_text(
        _STUB.format(
            python=shutil.which("python3"),
            plugins=repr({"installed": []}),
            hang=hang,
            record=str(record),
            answer=repr(answer),
        )
    )
    os.chmod(exe, 0o755)
    git = shutil.which("git")
    assert git
    (bindir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bindir))
    return home, record


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
    _, record = _stub(tmp_path, monkeypatch, skills=[_skill("claude")])
    project = tmp_path / "project"
    preflight.available_commands(host_id="codex", project_root=str(project))
    request = json.loads(record.read_text())
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
    home, _ = _stub(tmp_path, monkeypatch, skills=[])
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


@pytest.mark.parametrize(
    "answer",
    [
        {"id": 1, "error": {"code": -32601, "message": "method not found"}},
        {"id": 1, "result": {"unexpected": True}},
    ],
)
def test_an_unusable_answer_falls_back_to_the_filesystem(tmp_path, monkeypatch, answer):
    home, _ = _stub(tmp_path, monkeypatch, answer=answer)
    d = home / "skills" / "gstack-claude"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: claude\ndescription: d\n---\n")
    found = preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )
    assert "claude" in found


def test_a_catalog_that_never_answers_is_unverified_and_says_which_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    monkeypatch.setattr(codex, "SKILLS_LIST_TIMEOUT_S", 0.5)
    _stub(tmp_path, monkeypatch, hang=True)
    project = tmp_path / "project"
    out = preflight.check(project_root=str(project))
    assert not out["ok"]
    assert out["missing"] == [], out
    assert "$superpowers:writing-plans" in out["unverified"], out
    advice = "\n".join(out["advice"])
    assert "skills/list" in advice, advice
    assert "did not answer within" in advice, advice
    assert str(project) in advice, advice


# ------------------------------------------------ fallback: Codex's SKILL.md validity rule
#
# codex-rs/skills/src/parser.rs at rust-v0.155.0: frontmatter delimited by `---` is required;
# `name` defaults to the directory name and may be at most 64 characters; `description` is
# required and must be non-empty after whitespace is collapsed.


def _fallback_home(tmp_path, monkeypatch, skills):
    home, _ = _stub(tmp_path, monkeypatch, answer={"id": 1, "error": {}})
    for dirname, text in skills.items():
        d = home / "skills" / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    return preflight.available_commands(
        host_id="codex", project_root=str(tmp_path / "project")
    )


def test_the_fallback_does_not_count_a_skill_codex_would_reject(tmp_path, monkeypatch):
    found = _fallback_home(
        tmp_path,
        monkeypatch,
        {
            "no-description": "---\nname: no-description\n---\n",
            "empty-description": '---\nname: empty-description\ndescription: ""\n---\n',
            "empty-frontmatter": "---\n---\n",
            "no-frontmatter": "description: d\n",
            "long-name": f"---\nname: {'n' * 65}\ndescription: d\n---\n",
        },
    )
    rejected = {"no-description", "empty-description", "empty-frontmatter"}
    assert not rejected & found, found
    assert "no-frontmatter" not in found
    assert "n" * 65 not in found and "long-name" not in found


def test_the_fallback_counts_what_codex_accepts(tmp_path, monkeypatch):
    found = _fallback_home(
        tmp_path,
        monkeypatch,
        {
            "plain": "---\ndescription: d\n---\n",
            "block": "---\nname: block\ndescription: |\n  spans\n  lines\n---\n",
            "max-name": f"---\nname: {'m' * 64}\ndescription: d\n---\n",
        },
    )
    assert {"plain", "block", "m" * 64} <= found, found
