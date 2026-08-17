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
from tests.conductor.conftest import stale_version_siblings

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


#: Elements `installed[]` is not supposed to contain. Nothing says a future codex-cli, a
#: partially-written cache, or a `--json` bug cannot emit one, and the two parsers disagreed on
#: every one of them: the Python mirror skipped them and the shell comprehension called `.get()`
#: on them, so `{"installed":[null, <a valid conductor entry>]}` made preflight resolve the
#: plugin while the cron driver crashed and resolved nothing. Preflight green, cron stopped.
_JUNK_ENTRIES = (None, "conductor", 123, [], {"name": None}, {})


def _recorded(codex_home, *, keep=None, on_disk=None, junk=()):
    """The recorded payload with its placeholders bound to `codex_home`.

    `keep` selects which recorded entries appear in `installed[]`, so one artefact can pose the
    single-plugin question and the collision question separately. `on_disk` (default: everything
    kept) selects which installed roots are actually materialised, because a derived root that
    does not exist must not be reported. `junk` prepends elements that are not entries at all.
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
        # A STALE version directory either side of the listed one, both populated. An upgrade or
        # a half-cleaned cache leaves these behind, and with only ever one version directory per
        # plugin the version segment was unobservable: a parser that ignored the reported
        # version and globbed any on-disk one answered identically. Populated for the same
        # reason `source.path` is: an empty decoy lets a wrong answer pass for the right reason.
        for stale in stale_version_siblings(root.name):
            (root.parent / stale / "skills" / "start").mkdir(
                parents=True, exist_ok=True
            )
            (root.parent / stale / "skills" / "start" / "SKILL.md").write_text(
                "---\nname: start\n---\n"
            )
    # FIRST, so an entry that is not an entry cannot be skipped by luck of ordering: a parser
    # that stops at the first surprise never reaches the valid entries behind it.
    data["installed"] = list(junk) + data["installed"]
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


def test_a_root_that_is_not_on_disk_is_still_a_plugin_codex_says_is_installed(
    codex_home,
):
    """ "No root" and "no such plugin" are different answers and were the same value. Codex
    REPORTED superpowers installed and enabled; only the tree that claim implies is absent. The
    gate needs the difference to tell an owner "your install moved" instead of "install this",
    so the claim survives the failed check as its own fact."""
    payload = _recorded(codex_home, keep={"superpowers@trusted-market"}, on_disk=set())

    assert codex.unverifiable_plugins_from_json(payload) == frozenset({"superpowers"})
    # a disabled plugin is not installed for any purpose here, so it is not unverifiable either
    assert (
        codex.unverifiable_plugins_from_json(
            _recorded(codex_home, keep={"spec-craft@trusted-market"}, on_disk=set())
        )
        == frozenset()
    )


def test_a_stale_version_beside_the_listed_one_is_never_the_answer(codex_home):
    """The version segment is EVIDENCE, not a wildcard. `codex plugin list --json` reports which
    version is installed, and the tree beside it is whatever an upgrade or a failed cleanup left
    behind — an old skill set, a partial copy, an uninstalled plugin's leftovers. Both parsers
    were mutated to ignore the reported version and take any on-disk one, and 374 of the 375
    tests over them still passed, because no fixture had ever put a second version there."""
    payload = _recorded(codex_home, keep={"superpowers@trusted-market"})
    listed = codex_home / _RECORDED_INSTALLED_ROOTS["superpowers@trusted-market"]
    older, newer = stale_version_siblings(listed.name)
    assert (listed.parent / older).is_dir() and (listed.parent / newer).is_dir()

    assert codex.plugin_roots_from_json(payload) == {"superpowers": str(listed)}
    assert _snippet_state(payload, "superpowers", codex_home) == ("root", str(listed))


def test_only_a_stale_version_on_disk_is_unverifiable_not_installed(codex_home):
    """The version bump that moves the cache, exactly. Codex reports 2.3.1; what is on disk is
    the neighbouring stale copy and nothing else. Answering with that tree loads skills Codex
    will not load, so the honest answer is "the reported root is not there" — `unverified`."""
    payload = _recorded(codex_home, keep={"superpowers@trusted-market"}, on_disk=set())
    listed = codex_home / _RECORDED_INSTALLED_ROOTS["superpowers@trusted-market"]
    older, _newer = stale_version_siblings(listed.name)
    (listed.parent / older / "skills" / "start").mkdir(parents=True)
    (listed.parent / older / "skills" / "start" / "SKILL.md").write_text("---\n---\n")

    assert codex.plugin_roots_from_json(payload) == {}
    assert codex.unverifiable_plugins_from_json(payload) == frozenset({"superpowers"})
    assert _snippet_state(payload, "superpowers", codex_home) == ("unverifiable", "")


def test_a_plugin_with_one_real_root_is_never_also_unverifiable(codex_home):
    """Two listings of which one is really installed resolve normally; the failed derivation of
    the other is not a second, contradictory answer about the same name."""
    payload = _recorded(
        codex_home,
        keep={"conductor@evil-market", "conductor@trusted-market"},
        on_disk={"conductor@trusted-market"},
    )

    assert set(codex.plugin_roots_from_json(payload)) == {"conductor"}
    assert codex.unverifiable_plugins_from_json(payload) == frozenset()


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


def _snippet(payload, name, codex_home):
    return subprocess.run(
        [sys.executable, "-c", codex.PLUGIN_ROOT_SNIPPET, name],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, codex.CONFIG_DIR_ENV: str(codex_home)},
    )


#: The snippet's exit codes, as the three states it can be in. It cannot print a third answer —
#: its stdout is a path the driver execs — so the state that is neither "here is the root" nor
#: "nothing to say" has to leave by the exit status.
_SNIPPET_STATE = {0: "root", 3: "unverifiable", 2: "none"}


def _python_state(payload, name):
    """The Python parser's three-state answer for one plugin name."""
    roots = codex.plugin_roots_from_json(payload)
    if name in roots:
        return "root", roots[name]
    if name in codex.unverifiable_plugins_from_json(payload):
        return "unverifiable", ""
    return "none", ""


def _snippet_state(payload, name, codex_home):
    proc = _snippet(payload, name, codex_home)
    assert proc.stderr == "", (name, proc.stderr)
    assert proc.returncode in _SNIPPET_STATE, (name, proc.returncode, proc.stderr)
    return _SNIPPET_STATE[proc.returncode], proc.stdout.strip()


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
@pytest.mark.parametrize(
    "junk",
    [(), _JUNK_ENTRIES[:1], _JUNK_ENTRIES],
    ids=["clean", "one-null", "all-junk"],
)
@pytest.mark.parametrize("all_on_disk", [True, False], ids=["on-disk", "roots-gone"])
def test_the_cron_snippet_agrees_with_the_python_parser_on_every_recorded_state(
    codex_home, keep, junk, all_on_disk
):
    """The driver cannot import conductor — that is the problem the snippet solves — so the only
    defence against the two parsers drifting is running both over the same recorded payload.

    The matrix used to be all-object arrays with every root materialised, which is the shape the
    two parsers could not disagree on. Both omissions hid a real state: the shell comprehension
    called `.get()` on every element while the Python mirror skipped non-dicts, and neither side
    could express "codex lists this plugin but its root is not there" — the state that made
    preflight say `missing` and advise reinstalling a plugin the owner already had. STDERR is
    asserted empty for the same reason the answer is: a parser that answers "" by raising is not
    agreeing, it is failing in a way this comparison would call agreement everywhere the right
    answer happens to be "" too."""
    payload = _recorded(
        codex_home, keep=keep, junk=junk, on_disk=None if all_on_disk else set()
    )

    for name in ("superpowers", "spec-craft", "conductor", "absent"):
        assert _snippet_state(payload, name, codex_home) == _python_state(
            payload, name
        ), name


def test_neither_parser_treats_an_unexpected_top_level_shape_as_an_installed_plugin():
    """`installed[]` missing, or the whole document being something other than an object, is a
    legitimate answer ("this machine reports no plugin identities") and both parsers already
    agreed on it — except that the shell one reached it by raising `AttributeError`, which is
    only indistinguishable from the right answer while the right answer is empty."""
    for payload in ("[]", '"nope"', "null", "42", '{"installed": null}', "{}"):
        proc = _snippet(payload, "conductor", "/nonexistent-codex-home")
        assert proc.stderr == "", (payload, proc.stderr)
        assert proc.stdout.strip() == ""
        assert codex.plugin_roots_from_json(payload) == {}
