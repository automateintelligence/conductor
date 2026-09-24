"""OPT-IN: the real `codex` against conductor's assumptions about it.

Skipped unless `CONDUCTOR_LIVE_CODEX=1`. With the variable set, a missing `codex` is a failure,
not a skip, because asking for this test means asking for a real Codex. The rest of the suite
is hermetic (`tests/conftest.py` refuses a real `codex`) and replays responses recorded from
codex-cli 0.155.0; this is where a newer Codex shows whether those recordings, and the fallback
rule in `conductor.hosts.codex.codex_skill_names`, still describe it.

Runs against a scratch `HOME` and `CODEX_HOME`, so it reads nothing of the operator's install.
Codex may start its own plugin-marketplace sync on a fresh home, which can reach the network.
"""

from __future__ import annotations

import os
import shutil

import pytest

from conductor.hosts import codex

pytestmark = [
    pytest.mark.live_codex,
    pytest.mark.skipif(
        os.environ.get("CONDUCTOR_LIVE_CODEX") != "1",
        reason="opt-in: set CONDUCTOR_LIVE_CODEX=1 to run against the real codex",
    ),
]

_SKILLS = {
    "gstack-claude": "---\nname: claude\ndescription: d\n---\n",
    "nameless": "---\ndescription: d\n---\n",
    "quoted": '---\nname: "beta"\ndescription: d\n---\n',
    "no-description": "---\nname: no-description\n---\n",
    "empty-frontmatter": "---\n---\n",
    "no-frontmatter": "description: d\n",
    "long-name": f"---\nname: {'n' * 65}\ndescription: d\n---\n",
}


def test_the_real_catalog_agrees_with_the_fallback_rule(tmp_path, monkeypatch):
    assert shutil.which("codex"), "CONDUCTOR_LIVE_CODEX=1 but no `codex` on PATH"
    home = tmp_path / "codex-home"
    for dirname, text in _SKILLS.items():
        d = home / "skills" / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(home))

    catalog = codex.skills_list(project_root=str(project))

    assert catalog is not None, "codex app-server gave no skills/list answer"
    listed = {
        s["name"]
        for s in catalog
        if str(s.get("path", "")).startswith(str(home / "skills"))
    }
    listed -= {s["name"] for s in catalog if "/.system/" in str(s.get("path", ""))}
    assert listed == {"claude", "nameless", "beta"}, listed
    assert codex.codex_skill_names(f"{home}/skills/*/SKILL.md") == listed
