"""`codex plugin list --json`, read against an artefact RECORDED from codex-cli 0.147.0.

Every previous fixture for this parser was hand-written, and each one encoded the same
assumption the parser made — `enabled: true` always, `pluginId == name@openai-curated` always,
`source.path == the installed root` always. A test built from such a fixture cannot contradict
the code it checks. The artefact these tests read is verbatim CLI output from a sandbox install
that is deliberately none of those things; see ``../fixtures/README.md`` for how it was
recorded and how to re-record it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conductor.hosts import codex

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "codex-plugin-list-0.147.0.json",
)

#: What the 0.147.0 binary printed as `Installed plugin root:` for each entry of the artefact,
#: relative to the sandbox `$CODEX_HOME`. Recorded alongside the JSON — NOT computed by the code
#: under test, or the derivation would be checking itself.
_RECORDED_INSTALLED_ROOTS = {
    "conductor@evil-market": "plugins/cache/evil-market/conductor/1.0.9",
    "conductor@trusted-market": "plugins/cache/trusted-market/conductor/1.0.0",
    "spec-craft@trusted-market": "plugins/cache/trusted-market/spec-craft/1.0.0",
    "superpowers@trusted-market": "plugins/cache/trusted-market/superpowers/2.3.1",
}


def _recorded(codex_home, *, keep=None, on_disk=None):
    """The recorded payload with its placeholders bound to `codex_home`.

    `keep` selects which recorded entries appear in `installed[]`, so one artefact can pose the
    single-plugin question and the collision question separately. `on_disk` (default: everything
    kept) selects which installed roots are actually materialised, because a derived root that
    does not exist must not be reported.
    """
    with open(_FIXTURE, encoding="utf-8") as f:
        raw = f.read()
    raw = raw.replace("__CODEX_HOME__", str(codex_home)).replace(
        "__FIXTURE_ROOT__", str(codex_home / "fixture-root")
    )
    data = json.loads(raw)
    if keep is not None:
        data["installed"] = [p for p in data["installed"] if p["pluginId"] in keep]
    for entry in data["installed"]:
        # The marketplace SOURCE tree, which is what `source.path` names. Always materialised,
        # and populated, so a parser that reads it gets a real WRONG directory rather than
        # nothing — an empty one would let the bug pass for the right reason.
        src = os.path.join(entry["source"]["path"], "skills", "start")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "SKILL.md"), "w") as f:
            f.write("---\nname: start\n---\n")
        if on_disk is not None and entry["pluginId"] not in on_disk:
            continue
        root = codex_home / _RECORDED_INSTALLED_ROOTS[entry["pluginId"]]
        (root / "skills" / "start").mkdir(parents=True, exist_ok=True)
        (root / "skills" / "start" / "SKILL.md").write_text("---\nname: start\n---\n")
    return json.dumps(data)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv(codex.CONFIG_DIR_ENV, str(home))
    return home


# --------------------------------------------------------- the installed root is not source.path


def test_a_plugin_root_is_the_installed_copy_never_the_marketplace_source(codex_home):
    """`source.path` is marketplace metadata: the tree the plugin was copied FROM. The copy Codex
    actually loads lives under `$CODEX_HOME/plugins/cache/<marketplace>/<name>/<version>`, and no
    field of `plugin list --json` names it. Reading `source.path` discovers skills from a tree the
    host never loads — so a source copy left behind by an upgrade reports the stack healthy while
    the installed copy has nothing in it."""
    payload = _recorded(codex_home, keep={"superpowers@trusted-market"})

    roots = codex.plugin_roots_from_json(payload)

    expected = str(codex_home / _RECORDED_INSTALLED_ROOTS["superpowers@trusted-market"])
    assert roots == {"superpowers": expected}


def test_a_root_that_is_not_on_disk_is_reported_as_no_root_at_all(codex_home):
    """The derivation is a layout, so it is CHECKED rather than trusted: if a future Codex moves
    the cache, the derived path stops existing and this parser must answer "no root" — degrading
    to `unverified` at the gate — instead of naming a directory that is not there."""
    payload = _recorded(codex_home, keep={"superpowers@trusted-market"}, on_disk=set())

    assert codex.plugin_roots_from_json(payload) == {}


# ------------------------------------------------------------ disabled plugins are not usable


def test_a_disabled_plugin_contributes_no_root(codex_home):
    """0.147.0 leaves a disabled plugin in `installed[]` with `"enabled": false`, and its loader
    stops before loading capabilities when it is. Counting one as installed greens preflight on
    skills Codex will never load — and lets the cron driver exec a disabled plugin's
    `bin/conductor`, which is the same fire the operator turned off."""
    payload = _recorded(codex_home, keep={"spec-craft@trusted-market"})

    assert codex.plugin_roots_from_json(payload) == {}


def test_an_enabled_plugin_alongside_a_disabled_one_still_resolves(codex_home):
    """The filter drops the disabled entry, not the whole answer."""
    payload = _recorded(
        codex_home, keep={"spec-craft@trusted-market", "superpowers@trusted-market"}
    )

    assert set(codex.plugin_roots_from_json(payload)) == {"superpowers"}


# ------------------------------------------------------- a bare name does not identify a plugin


def test_one_name_claimed_by_two_marketplaces_resolves_to_neither(codex_home):
    """`conductor@trusted-market` and `conductor@evil-market` are two DIFFERENT plugins that
    collapse onto one bare `name`. Keying on the name attributes whichever the host happened to
    list first — here the evil one, because 0.147.0 lists it first — so a copied `start` skill
    becomes `conductor:start` and the gate greens on it while the real conductor is installed
    right beside it. An ambiguous name is not evidence of identity, so it yields none."""
    payload = _recorded(
        codex_home, keep={"conductor@evil-market", "conductor@trusted-market"}
    )

    assert codex.plugin_roots_from_json(payload) == {}


def test_an_unambiguous_name_alongside_a_collision_still_resolves(codex_home):
    """Ambiguity is refused per NAME, not for the whole machine."""
    payload = _recorded(codex_home)  # all four recorded entries

    assert set(codex.plugin_roots_from_json(payload)) == {"superpowers"}


# ------------------------------------------------- the driver's own parser answers identically


@pytest.mark.parametrize(
    "keep",
    [
        {"superpowers@trusted-market"},
        {"spec-craft@trusted-market"},
        {"spec-craft@trusted-market", "superpowers@trusted-market"},
        {"conductor@evil-market", "conductor@trusted-market"},
        None,
    ],
)
def test_the_cron_snippet_agrees_with_the_python_parser_on_every_recorded_state(
    codex_home, keep
):
    """The driver cannot import conductor — that is the problem the snippet solves — so the only
    defence against the two parsers drifting is running both over the same recorded payload."""
    payload = _recorded(codex_home, keep=keep)
    from_python = codex.plugin_roots_from_json(payload)

    for name in ("superpowers", "spec-craft", "conductor", "absent"):
        proc = subprocess.run(
            [sys.executable, "-c", codex.PLUGIN_ROOT_SNIPPET, name],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, codex.CONFIG_DIR_ENV: str(codex_home)},
        )
        assert proc.stdout.strip() == from_python.get(name, ""), (name, proc.stderr)
