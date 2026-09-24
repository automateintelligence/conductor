"""OPT-IN: the real `codex` against conductor's assumptions about it.

Skipped unless `CONDUCTOR_LIVE_CODEX=1`. With the variable set, a missing `codex` is a failure,
not a skip, because asking for this test means asking for a real Codex. The rest of the suite
is hermetic (`tests/conftest.py` refuses a real `codex`) and replays responses recorded from
codex-cli 0.155.0; this is where a newer Codex shows whether those recordings still describe
it, and whether `conductor.hosts.codex.codex_skill_names` still accepts nothing Codex rejects
(checked over the generated corpus in `tests/conductor/skill_corpus.py`).

Runs against a scratch `HOME` and `CODEX_HOME`, so it reads nothing of the operator's install.
Codex may start its own plugin-marketplace sync on a fresh home, which can reach the network.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from conductor.hosts import codex
from tests.conductor import skill_corpus

pytestmark = [
    pytest.mark.live_codex,
    pytest.mark.skipif(
        os.environ.get("CONDUCTOR_LIVE_CODEX") != "1",
        reason="opt-in: set CONDUCTOR_LIVE_CODEX=1 to run against the real codex",
    ),
]


def _catalog_for(tmp_path, monkeypatch, files):
    assert shutil.which("codex"), "CONDUCTOR_LIVE_CODEX=1 but no `codex` on PATH"
    home = tmp_path / "codex-home"
    for dirname, text in files.items():
        d = home / "skills" / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(home))
    catalog = codex.skills_list(project_root=str(project))
    return home, {
        pathlib.Path(s["path"]).parent.name: s["name"]
        for s in catalog
        if pathlib.Path(s["path"]).parent.parent == home / "skills"
    }


def test_everything_the_subset_accepts_codex_loads_under_the_same_name(
    tmp_path, monkeypatch
):
    """The namer's one promise, checked against the real parser over a generated corpus:
    whatever it accepts, Codex loads, and under the name the namer reported. (The converse is
    deliberately not required: an exotic SKILL.md Codex loads may read as not loadable.)"""
    files = skill_corpus.corpus()
    home, loaded = _catalog_for(tmp_path, monkeypatch, files)
    wrong = {}
    for dirname in files:
        accepted = codex.codex_skill_names(f"{home}/skills/{dirname}/SKILL.md")
        if accepted and loaded.get(dirname) not in accepted:
            wrong[dirname] = (files[dirname], sorted(accepted), loaded.get(dirname))
    assert not wrong, wrong


def test_the_hand_picked_cases_hold_against_the_real_codex(tmp_path, monkeypatch):
    from tests.conductor.hosts.test_codex_skills_list import ACCEPTED, REJECTED

    files = {d: text for d, (text, _) in ACCEPTED.items()}
    files.update({f"rejected-{d}": text for d, text in REJECTED.items()})
    _, loaded = _catalog_for(tmp_path, monkeypatch, files)
    for dirname, (_, name) in ACCEPTED.items():
        assert loaded.get(dirname) == name, (dirname, loaded.get(dirname))
