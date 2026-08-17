"""Per-spec done-gate resolution (multi-spec safety).

The done-gate (manifest.yaml, .frozen, run/results.json) is a tracked path. Flat at
``assertions/`` it is one per-repo slot two sibling-worktree specs contend for at the shared
base. These tests pin the namespacing that lets them coexist at ``assertions/<slug>/`` while
keeping the flat legacy gate — and a stale ``.conductor/`` — working untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conductor import branches, paths
from conductor.core import resolve as core_resolve
from conductor.core import runkey, runstate, schema

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")


# --- spec_slug: the single source shared with the run branch ---------------------------


def test_spec_slug_matches_run_branch_name():
    spec = "docs/specs/2026-07-05-self-enforcement.md"
    assert branches.run_branch_name(spec) == f"conductor/run-{paths.spec_slug(spec)}"


def test_spec_slug_is_deterministic_and_ref_safe():
    spec = "docs/specs/My Spec!!.md"
    slug = paths.spec_slug(spec)
    assert slug == paths.spec_slug(spec)  # deterministic
    assert slug and slug[0].isalnum() and " " not in slug and "!" not in slug


def test_distinct_specs_get_distinct_slugs():
    assert paths.spec_slug("docs/specs/alpha.md") != paths.spec_slug(
        "docs/specs/beta.md"
    )


# --- gate_slug: env > run_branch > goal.md ---------------------------------------------


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_gate_slug_none_without_any_source(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_GATE_SLUG", raising=False)
    assert paths.gate_slug(str(tmp_path)) is None


def test_gate_slug_from_run_branch_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_GATE_SLUG", raising=False)
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    assert paths.gate_slug(str(tmp_path)) == "alpha"


def test_gate_slug_from_goal_spec(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_GATE_SLUG", raising=False)
    _write(tmp_path, ".conductor/goal.md", "Implement docs/specs/beta.md until done\n")
    assert paths.gate_slug(str(tmp_path)) == paths.spec_slug("docs/specs/beta.md")


def test_run_branch_and_goal_agree_and_run_branch_wins(tmp_path, monkeypatch):
    # start writes both; for one spec they resolve to the SAME slug (run_branch is
    # conductor/run-<spec_slug>). run_branch is consulted first — pin that it wins even so.
    monkeypatch.delenv("CONDUCTOR_GATE_SLUG", raising=False)
    spec = "docs/specs/gamma.md"
    rb = branches.run_branch_name(spec)
    _write(tmp_path, ".conductor/run_branch", rb + "\n")
    _write(tmp_path, ".conductor/goal.md", f"Implement {spec} until done\n")
    assert paths.gate_slug(str(tmp_path)) == paths.spec_slug(spec)


def test_gate_slug_env_overrides_files(tmp_path, monkeypatch):
    _write(tmp_path, ".conductor/run_branch", "conductor/run-fromfile\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "fromenv")
    assert paths.gate_slug(str(tmp_path)) == "fromenv"


# --- spec_from_goal: THE shared goal -> spec resolver ----------------------------------
#
# `paths._goal_slug` and `freeze._assertions_source` each used to carry their own copy of the
# `docs/specs/...md` regex, and each took the LEFTMOST match. Because both took the leftmost
# they agreed with each other, so a spec merely mentioned in passing above the intended one
# repointed the gate slug AND the frozen assertions source together with nothing left to
# disagree and catch it. One resolver now serves both, an explicit `spec:` line beats prose,
# and two prose candidates fail closed naming both instead of silently picking one.


def test_spec_from_goal_text_single_prose_match_is_unchanged(tmp_path):
    assert (
        paths.spec_from_goal_text("Implement docs/specs/beta.md until done\n")
        == "docs/specs/beta.md"
    )


def test_spec_from_goal_text_without_any_spec_is_none():
    assert paths.spec_from_goal_text("Do the thing\n") is None


def test_spec_from_goal_text_repeating_one_spec_is_not_ambiguous():
    text = "Implement docs/specs/beta.md.\nSee docs/specs/beta.md for detail.\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


def test_explicit_spec_field_beats_a_prose_mention():
    text = "Context: docs/specs/alpha.md was the old one.\nspec: docs/specs/beta.md\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


def test_two_prose_specs_without_a_spec_field_fail_closed_naming_both():
    text = "Port docs/specs/alpha.md ideas into docs/specs/beta.md until done\n"
    with pytest.raises(paths.AmbiguousSpecReference) as excinfo:
        paths.spec_from_goal_text(text)
    message = str(excinfo.value)
    assert "docs/specs/alpha.md" in message and "docs/specs/beta.md" in message
    assert "spec:" in message  # tells the user how to disambiguate


def test_explicit_spec_field_silences_two_prose_candidates():
    text = (
        "Port docs/specs/alpha.md ideas into docs/specs/beta.md until done\n"
        "spec: docs/specs/beta.md\n"
    )
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


# Two `spec:` FIELDS are the same ambiguity as two prose paths, one layer in: `search` took
# the leftmost and re-created the very leftmost-wins defect the resolver exists to kill, this
# time in the explicit field that is supposed to be the way OUT of it.


def test_two_distinct_spec_fields_fail_closed_naming_both():
    text = "spec: docs/specs/alpha.md\nspec: docs/specs/beta.md\n"
    with pytest.raises(paths.AmbiguousSpecReference) as excinfo:
        paths.spec_from_goal_text(text)
    message = str(excinfo.value)
    assert "docs/specs/alpha.md" in message and "docs/specs/beta.md" in message


def test_the_same_spec_field_twice_is_not_ambiguous():
    # mirrors the prose rule: a repeated declaration still declares ONE spec
    text = "spec: docs/specs/beta.md\nspec: docs/specs/beta.md\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


def test_spec_fields_differing_only_in_quoting_are_the_same_declaration():
    text = "spec: docs/specs/beta.md\nspec: `docs/specs/beta.md`\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


def test_two_distinct_spec_fields_fail_closed_even_outside_docs_specs():
    # the field is root-agnostic, so the ambiguity check cannot lean on the prose regex
    text = "spec: spec/alpha.md\nspec: spec/beta.md\n"
    with pytest.raises(paths.AmbiguousSpecReference) as excinfo:
        paths.spec_from_goal_text(text)
    assert "spec/alpha.md" in str(excinfo.value)


# The `spec:` field is the ONLY way a repo that does not keep specs under `docs/specs/` can
# name one at all — the prose fallback hardcodes that root. Constraining the field to the
# `docs/specs/*.md` shape would close the sole escape hatch, so it is deliberately not.


def test_explicit_spec_field_outside_docs_specs_resolves(tmp_path):
    assert paths.spec_from_goal_text("spec: spec/payments.md\n") == "spec/payments.md"
    assert (
        paths.spec_from_goal_text("spec: docs/requirements/payments.md\n")
        == "docs/requirements/payments.md"
    )


# An `.assertions.md` sibling is the spec's DONE-DEFINITION, not a second spec. Once
# `freeze._source_candidates` made `<stem>.assertions.md` canonical, that sibling started
# matching the `docs/specs/*.md` prose regex, so the ordinary goal shape "implement X and keep
# X's assertions green" read as two candidates and failed closed on a run that used to work.
# The exclusion is on the FALLBACK scan only — an explicit `spec:` field naming one is the
# user's business, and a louder kind of mistake.


def test_assertions_sibling_is_not_a_second_prose_spec_candidate():
    text = (
        "Implement docs/specs/payments.md and keep "
        "docs/specs/payments.assertions.md green\n"
    )
    assert paths.spec_from_goal_text(text) == "docs/specs/payments.md"


def test_legacy_dotmd_assertions_sibling_is_not_a_second_prose_candidate():
    text = (
        "Implement docs/specs/payments.md and keep "
        "docs/specs/payments.md.assertions.md green\n"
    )
    assert paths.spec_from_goal_text(text) == "docs/specs/payments.md"


def test_an_assertions_path_alone_is_still_no_spec():
    # the goal must name the SPEC; a done-definition on its own declares no subject
    assert (
        paths.spec_from_goal_text("keep docs/specs/payments.assertions.md green\n")
        is None
    )


def test_two_real_specs_still_fail_closed_alongside_an_assertions_sibling():
    text = (
        "Port docs/specs/alpha.md into docs/specs/beta.md, keeping "
        "docs/specs/beta.assertions.md green\n"
    )
    with pytest.raises(paths.AmbiguousSpecReference) as excinfo:
        paths.spec_from_goal_text(text)
    message = str(excinfo.value)
    assert "docs/specs/alpha.md" in message and "docs/specs/beta.md" in message
    assert "assertions.md" not in message  # the sibling is not offered as a candidate


def test_an_explicit_spec_field_naming_an_assertions_file_is_honoured():
    assert (
        paths.spec_from_goal_text("spec: docs/specs/payments.assertions.md\n")
        == "docs/specs/payments.assertions.md"
    )


# codex round 2, finding 2: the exclusion above tested the LAZY match, not the path. A path
# whose name merely CONTAINS `.assertions.md` — `docs/specs/foo.assertions.md.md` — matched
# only as far as its first `.md`, and the suffix check then discarded that prefix as if it
# were the sibling. The real path vanished, so a goal that also named another spec silently
# resolved to the other one instead of failing closed. The candidate must be matched to its
# token boundary BEFORE the suffix decides anything.


def test_a_path_merely_prefixed_by_an_assertions_name_resolves_to_itself():
    text = "Implement docs/specs/foo.assertions.md.md until done\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/foo.assertions.md.md"


def test_a_path_merely_prefixed_by_an_assertions_name_is_not_silently_dropped():
    text = "Implement docs/specs/foo.assertions.md.md alongside docs/specs/bar.md\n"
    with pytest.raises(paths.AmbiguousSpecReference) as excinfo:
        paths.spec_from_goal_text(text)
    message = str(excinfo.value)
    assert "docs/specs/foo.assertions.md.md" in message
    assert "docs/specs/bar.md" in message


def test_a_markdown_linked_spec_resolves_to_the_path_not_the_link_run():
    # The fence around the fix: matching greedily over an unrestricted character class would
    # swallow `](` and yield `docs/specs/beta.md](docs/specs/beta.md` as the "spec". Markdown
    # link punctuation ends a path token.
    text = "Implement [docs/specs/beta.md](docs/specs/beta.md) until done\n"
    assert paths.spec_from_goal_text(text) == "docs/specs/beta.md"


def test_spec_from_goal_reads_the_goal_file_and_is_none_without_one(tmp_path):
    assert paths.spec_from_goal(str(tmp_path)) is None
    _write(tmp_path, ".conductor/goal.md", "spec: docs/specs/beta.md\n")
    assert paths.spec_from_goal(str(tmp_path)) == "docs/specs/beta.md"


def test_gate_slug_from_explicit_spec_field(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_GATE_SLUG", raising=False)
    _write(
        tmp_path,
        ".conductor/goal.md",
        "Follow the pattern in docs/specs/alpha.md.\nspec: docs/specs/beta.md\n",
    )
    assert paths.gate_slug(str(tmp_path)) == paths.spec_slug("docs/specs/beta.md")


def test_ambiguous_goal_fails_closed_rather_than_repointing_the_gate(
    tmp_path, monkeypatch
):
    _clear_env(monkeypatch)
    _write(
        tmp_path,
        ".conductor/goal.md",
        "Port docs/specs/alpha.md ideas into docs/specs/beta.md until done\n",
    )
    assert paths.gate_slug(str(tmp_path)) is None  # never guesses the leftmost
    g = paths.resolve_gate(str(tmp_path))
    assert g.fail_closed is not None
    assert "docs/specs/alpha.md" in g.fail_closed
    assert "docs/specs/beta.md" in g.fail_closed


def test_explicit_spec_field_keeps_an_otherwise_ambiguous_goal_running(
    tmp_path, monkeypatch
):
    _clear_env(monkeypatch)
    _write(
        tmp_path,
        ".conductor/goal.md",
        "Port docs/specs/alpha.md ideas into docs/specs/beta.md until done\n"
        "spec: docs/specs/beta.md\n",
    )
    g = paths.resolve_gate(str(tmp_path))
    assert g.fail_closed is None
    assert paths.gate_slug(str(tmp_path)) == paths.spec_slug("docs/specs/beta.md")


# --- gate_dir: explicit slug forces namespaced; ambient slug falls back until built -------


def _clear_env(monkeypatch):
    for k in (
        "CONDUCTOR_GATE_SLUG",
        "CONDUCTOR_GATE_DIR",
        "CONDUCTOR_MANIFEST",
        "CONDUCTOR_FREEZE_BASELINE",
    ):
        monkeypatch.delenv(k, raising=False)


def test_gate_dir_flat_when_no_slug(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions")


def test_explicit_slug_forces_namespaced_no_flat_fallback(tmp_path, monkeypatch):
    # codex P2: an explicit CONDUCTOR_GATE_SLUG (start's "this run is namespaced" signal)
    # forces assertions/<slug>/ even before its manifest exists and even with a legacy flat
    # manifest present — so setup can't silently freeze/validate the old flat gate.
    _clear_env(monkeypatch)
    _write(tmp_path, "assertions/manifest.yaml", "assertions: []\n")  # legacy flat gate
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "alpha")


def test_ambient_slug_falls_back_to_flat_until_built(tmp_path, monkeypatch):
    # An AMBIENT slug (.conductor/run_branch, not the explicit env) with no per-slug gate yet
    # keeps the flat legacy gate — protects an in-place flat gate and a stale .conductor/.
    _clear_env(monkeypatch)
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions")


def test_ambient_slug_uses_namespaced_once_manifest_exists(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    _write(tmp_path, "assertions/alpha/manifest.yaml", "assertions: []\n")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "alpha")


def test_ambient_slug_stays_namespaced_when_only_frozen_exists(tmp_path, monkeypatch):
    # Integrity (codex P1): once a namespaced gate is FROZEN, deleting its manifest must NOT
    # downgrade to the flat gate — the .frozen baseline keeps the dir so the missing manifest
    # fails closed under it. Exercised on the ambient (run-time) path.
    _clear_env(monkeypatch)
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    _write(tmp_path, "assertions/alpha/.frozen", "{}\n")  # frozen, manifest gone
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "alpha")


def test_gate_dir_env_override_wins(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", "/somewhere/else")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    assert paths.gate_dir(str(tmp_path)) == "/somewhere/else"


def test_has_namespaced_frozen_gate(tmp_path):
    assert paths.has_namespaced_frozen_gate(str(tmp_path)) is False
    _write(tmp_path, "assertions/.frozen", "{}\n")  # the FLAT baseline never counts
    assert paths.has_namespaced_frozen_gate(str(tmp_path)) is False
    _write(tmp_path, "assertions/alpha/.frozen", "{}\n")  # a namespaced one does
    assert paths.has_namespaced_frozen_gate(str(tmp_path)) is True


def test_corrupt_ambient_slug_fails_closed_when_repo_has_frozen_gate(
    tmp_path, monkeypatch
):
    # codex P1: an ambient slug (run_branch) edited to an UNBUILT slug must NOT fall back to
    # the flat gate when the repo holds a frozen namespaced gate — resolve to the (empty)
    # nsdir so the missing manifest fails closed, instead of dodging onto the flat slot.
    _clear_env(monkeypatch)
    _write(tmp_path, "assertions/alpha/.frozen", "{}\n")  # a frozen namespaced gate
    _write(
        tmp_path, "assertions/manifest.yaml", "assertions: []\n"
    )  # a flat gate to dodge to
    _write(tmp_path, ".conductor/run_branch", "conductor/run-junk\n")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "junk")


def test_run_branch_slug_rejects_unsafe_path_component(tmp_path, monkeypatch):
    # codex P2: .conductor/run_branch is worker-editable and its suffix is now a filesystem
    # component. A suffix with separators / .. must be rejected, never joined into a gate path.
    _clear_env(monkeypatch)
    for bad in (
        "conductor/run-../../evil",
        "conductor/run-a/b",
        "conductor/run-..",
        "conductor/run-.lock",
    ):
        _write(tmp_path, ".conductor/run_branch", bad + "\n")
        assert paths._run_branch_slug(str(tmp_path)) is None, bad
        assert paths.gate_slug(str(tmp_path)) is None, bad
        g = paths.resolve_gate(str(tmp_path))
        assert g.directory == str(tmp_path / "assertions"), bad  # flat, never traversed
    _write(
        tmp_path, ".conductor/run_branch", "conductor/run-good-1.2\n"
    )  # a safe one still works
    assert paths._run_branch_slug(str(tmp_path)) == "good-1.2"


def test_unresolved_frozen_gate(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    assert (
        paths.unresolved_frozen_gate(str(tmp_path)) is False
    )  # no frozen gates at all
    # frozen alpha exists; run_branch points at an UNFROZEN planted alternate -> dodging
    _write(tmp_path, "assertions/alpha/.frozen", "{}\n")
    _write(tmp_path, "assertions/other/manifest.yaml", "assertions: []\n")
    _write(tmp_path, ".conductor/run_branch", "conductor/run-other\n")
    assert paths.unresolved_frozen_gate(str(tmp_path)) is True
    # once the resolved gate is itself frozen, it is not dodging
    _write(tmp_path, "assertions/other/.frozen", "{}\n")
    assert paths.unresolved_frozen_gate(str(tmp_path)) is False
    # an explicit slug is deliberate setup selection, never flagged
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "brandnew")
    assert paths.unresolved_frozen_gate(str(tmp_path)) is False


def test_ambient_switch_away_from_flat_frozen_gate_fails_closed(tmp_path, monkeypatch):
    # codex P2: a LEGACY flat-frozen repo (assertions/.frozen) must not be bypassed by planting
    # a namespaced manifest + ambient run_branch — resolving to the unfrozen namespace while the
    # flat baseline exists is dodging it, even though no namespaced .frozen exists.
    _clear_env(monkeypatch)
    _write(tmp_path, "assertions/.frozen", "{}\n")  # legacy flat frozen gate
    _write(tmp_path, "assertions/manifest.yaml", "assertions: []\n")
    _write(
        tmp_path, "assertions/other/manifest.yaml", "assertions: []\n"
    )  # planted namespace
    _write(tmp_path, ".conductor/run_branch", "conductor/run-other\n")
    # gate_dir resolves to the planted (unfrozen) namespace...
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "other")
    # ...but the guard flags it, because the flat frozen gate is being dodged.
    assert paths.unresolved_frozen_gate(str(tmp_path)) is True
    # selecting the flat gate directly (no ambient namespace) is fine — its baseline exists.
    (tmp_path / "assertions" / "other" / "manifest.yaml").unlink()
    (tmp_path / ".conductor" / "run_branch").unlink()
    assert paths.unresolved_frozen_gate(str(tmp_path)) is False


def test_unresolved_frozen_gate_exempts_explicit_path_overrides(tmp_path, monkeypatch):
    # codex P2: documented explicit overrides (CONDUCTOR_MANIFEST / CONDUCTOR_GATE_DIR) are
    # deliberate gate selections — the ambient-dodge guard must stand down, not fail closed.
    _clear_env(monkeypatch)
    _write(
        tmp_path, "assertions/alpha/.frozen", "{}\n"
    )  # repo has a frozen per-spec gate
    _write(tmp_path, ".conductor/run_branch", "conductor/run-other\n")  # ambient dodge
    assert (
        paths.unresolved_frozen_gate(str(tmp_path)) is True
    )  # fires on ambient resolution
    monkeypatch.setenv("CONDUCTOR_MANIFEST", str(tmp_path / "custom" / "manifest.yaml"))
    assert (
        paths.unresolved_frozen_gate(str(tmp_path)) is False
    )  # explicit manifest -> exempt
    monkeypatch.delenv("CONDUCTOR_MANIFEST", raising=False)
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", str(tmp_path / "custom"))
    assert (
        paths.unresolved_frozen_gate(str(tmp_path)) is False
    )  # explicit gate dir -> exempt


# --- manifest_path / baseline_path / run_dir ------------------------------------------


def test_paths_derive_from_gate_dir(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    _write(tmp_path, "assertions/alpha/manifest.yaml", "assertions: []\n")
    nsdir = tmp_path / "assertions" / "alpha"
    assert paths.manifest_path(str(tmp_path)) == str(nsdir / "manifest.yaml")
    assert paths.baseline_path(str(tmp_path)) == str(nsdir / ".frozen")
    assert paths.run_dir(str(tmp_path)) == str(nsdir / "run")


def test_explicit_env_overrides_win_for_paths(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_MANIFEST", "/m/manifest.yaml")
    monkeypatch.setenv("CONDUCTOR_FREEZE_BASELINE", "/b/.frozen")
    assert paths.manifest_path(str(tmp_path)) == "/m/manifest.yaml"
    assert paths.baseline_path(str(tmp_path)) == "/b/.frozen"
    # run_dir sits beside the (overridden) manifest
    assert paths.run_dir(str(tmp_path)) == os.path.join("/m", "run")


# --- CLI coexistence: two specs, one repo, no collision (the flaw, end to end) ---------

_PINNED = (
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest "
    "-p no:cacheprovider"
)


def _build_per_slug_gate(proj: Path, slug: str, marker: str) -> None:
    """A namespaced gate under assertions/<slug>/ whose single assertion passes only when
    its own test file (unique per slug via `marker`) is present."""
    d = proj / "assertions" / slug
    (d / "tests").mkdir(parents=True)
    (d / "tests" / "test_it.py").write_text(
        f"def test_it():\n    assert {marker!r} == {marker!r}\n"
    )
    (d / "manifest.yaml").write_text(
        textwrap.dedent(f"""\
            assertions:
              - id: {slug}-ok
                claim: "{slug} holds"
                command: "{_PINNED} assertions/{slug}/tests/test_it.py"
                level: spec
                kind: example
            """)
    )
    (proj / ".conductor").mkdir(exist_ok=True)
    (proj / ".conductor" / "run_branch").write_text(f"conductor/run-{slug}\n")


def _conductor(proj: Path, slug: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    # Select which run's gate this invocation targets (as .conductor/run_branch would at
    # run time); no CONDUCTOR_MANIFEST plumbing.
    env["CONDUCTOR_GATE_SLUG"] = slug
    for k in ("CONDUCTOR_MANIFEST", "CONDUCTOR_FREEZE_BASELINE", "CONDUCTOR_GATE_DIR"):
        env.pop(k, None)
    return subprocess.run(
        [CONDUCTOR, *args],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_two_specs_coexist_without_collision(tmp_path):
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    _build_per_slug_gate(proj, "alpha", "A")
    _build_per_slug_gate(proj, "beta", "B")

    # Each run freezes and runs its OWN gate.
    for slug in ("alpha", "beta"):
        frozen = _conductor(proj, slug, "gate", "freeze")
        assert frozen.returncode == 0, frozen.stdout + frozen.stderr
        assert (proj / "assertions" / slug / ".frozen").is_file()
        run = _conductor(proj, slug, "assert", "run", "--level", "spec")
        assert run.returncode == 0, run.stdout + run.stderr

    # Isolation: each gate has its own baseline + results; neither writes the flat slot.
    assert (proj / "assertions" / "alpha" / "run" / "results.json").is_file()
    assert (proj / "assertions" / "beta" / "run" / "results.json").is_file()
    assert not (proj / "assertions" / "manifest.yaml").exists()
    assert not (proj / "assertions" / ".frozen").exists()
    assert not (proj / "assertions" / "run").exists()

    # alpha's frozen gate is unaffected by beta existing: verify stays green.
    v = _conductor(proj, "alpha", "gate", "verify")
    assert v.returncode == 0, v.stdout + v.stderr


def test_freeze_cli_writes_the_per_slug_baseline_not_flat(tmp_path):
    # Directly pins the fixed bug: `conductor gate freeze` used to ignore the gate override
    # and always write flat assertions/.frozen. It must now write assertions/<slug>/.frozen.
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    _build_per_slug_gate(proj, "alpha", "A")
    frozen = _conductor(proj, "alpha", "gate", "freeze")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (proj / "assertions" / "alpha" / ".frozen").is_file()
    assert not (proj / "assertions" / ".frozen").exists()


def test_deleting_namespaced_manifest_fails_closed_under_its_baseline(tmp_path):
    # codex P1: after a namespaced gate is frozen, dropping its manifest must fail closed
    # under assertions/<slug>/.frozen — NOT silently fall back to a (green) flat gate.
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    _build_per_slug_gate(proj, "alpha", "A")
    # A green flat gate exists too; the resolver must not use it as an escape hatch.
    (proj / "assertions" / "manifest.yaml").write_text(
        'assertions:\n  - id: flat-ok\n    command: "true"\n    level: spec\n'
    )
    frozen = _conductor(proj, "alpha", "gate", "freeze")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (proj / "assertions" / "alpha" / ".frozen").is_file()

    # Tamper: remove the namespaced manifest while its frozen baseline remains.
    (proj / "assertions" / "alpha" / "manifest.yaml").unlink()

    v = _conductor(proj, "alpha", "gate", "verify")
    assert v.returncode != 0, (
        "gate verify fell back to the flat gate instead of failing closed:\n"
        + v.stdout
        + v.stderr
    )
    r = _conductor(proj, "alpha", "assert", "run", "--level", "spec")
    assert r.returncode != 0, (
        "assert run fell back to the flat gate instead of failing closed:\n"
        + r.stdout
        + r.stderr
    )


def test_gate_dir_cli_matches_resolver_from_any_cwd(tmp_path):
    # codex P2: the CLI verb must print the PROJECT-ROOTED path paths.gate_dir() resolves —
    # $CONDUCTOR_GATE_DIR verbatim, else $CONDUCTOR_HOME/assertions/<slug> — even when invoked
    # from OUTSIDE the project root (CONDUCTOR_HOME != cwd). A cwd-relative path would write
    # the manifest where lint/freeze/run never read it.
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "elsewhere"  # invoke from a DIFFERENT cwd than the project
    elsewhere.mkdir()
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    for k in ("CONDUCTOR_GATE_SLUG", "CONDUCTOR_MANIFEST", "CONDUCTOR_FREEZE_BASELINE"):
        env.pop(k, None)

    def _gate_dir(e):
        return subprocess.run(
            [CONDUCTOR, "gate-dir", "docs/specs/alpha.md"],
            cwd=str(elsewhere),
            env=e,
            capture_output=True,
            text=True,
            timeout=30,
        )

    env.pop("CONDUCTOR_GATE_DIR", None)
    default = _gate_dir(env)
    assert default.returncode == 0, default.stderr
    assert default.stdout.strip() == str(proj / "assertions" / "alpha")

    env["CONDUCTOR_GATE_DIR"] = "/tmp/custom-gate"
    override = _gate_dir(env)
    assert override.returncode == 0, override.stderr
    assert override.stdout.strip() == "/tmp/custom-gate"

    # An already-set CONDUCTOR_GATE_SLUG must win over the spec-derived slug, project-rooted.
    env.pop("CONDUCTOR_GATE_DIR", None)
    env["CONDUCTOR_GATE_SLUG"] = "explicitslug"
    slug_case = _gate_dir(env)
    assert slug_case.returncode == 0, slug_case.stderr
    assert slug_case.stdout.strip() == str(proj / "assertions" / "explicitslug")


def test_corrupt_run_branch_cannot_bypass_a_frozen_namespaced_gate(tmp_path):
    # codex P1, end to end: freeze a namespaced gate via the ambient run_branch, then corrupt
    # run_branch to an unbuilt slug. `assert run` and `gate verify` must fail closed, NOT
    # resolve onto the (green) flat gate the worker planted.
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    _build_per_slug_gate(proj, "alpha", "A")  # writes .conductor/run_branch = run-alpha
    (proj / "assertions" / "manifest.yaml").write_text(
        'assertions:\n  - id: flat-ok\n    command: "true"\n    level: spec\n'
    )

    def _ambient(*args):
        env = dict(os.environ)
        env["CONDUCTOR_HOME"] = str(proj)
        for k in (
            "CONDUCTOR_GATE_SLUG",
            "CONDUCTOR_MANIFEST",
            "CONDUCTOR_FREEZE_BASELINE",
            "CONDUCTOR_GATE_DIR",
        ):
            env.pop(k, None)
        return subprocess.run(
            [CONDUCTOR, *args],
            cwd=str(proj),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    frozen = _ambient("gate", "freeze")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (proj / "assertions" / "alpha" / ".frozen").is_file()

    (proj / ".conductor" / "run_branch").write_text("conductor/run-junk\n")

    # assert run reports the TAMPER exit 6 (guard runs before manifest loading), not a
    # manifest-missing 2 — and agrees with gate verify.
    r = _ambient("assert", "run", "--level", "spec")
    assert r.returncode == 6, (
        "assert run dodged onto the flat gate:\n" + r.stdout + r.stderr
    )
    v = _ambient("gate", "verify")
    assert v.returncode != 0, (
        "gate verify read green by dodging the frozen gate:\n" + v.stdout + v.stderr
    )
    # gate freeze must REFUSE too — freezing the dodged (junk) selection would launder it.
    f = _ambient("gate", "freeze")
    assert f.returncode != 0, (
        "gate freeze laundered a dodged gate:\n" + f.stdout + f.stderr
    )
    assert not (proj / "assertions" / "junk" / ".frozen").exists()


def test_repointed_frozen_alternate_fails_closed_in_assert_run(tmp_path):
    # final codex P1: `assert run` must refuse a run_branch repointed to a DIFFERENT, already
    # FROZEN gate (clause ii) even though that alternate baseline EXISTS — the fail_closed
    # verdict is checked BEFORE the baseline branch, same as `gate verify`. Reachable with a
    # sources-less (pre-upgrade) .frozen that the _assertions_source check would let slide.
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    other = proj / "assertions" / "other"
    other.mkdir(parents=True)
    (other / "manifest.yaml").write_text(
        'assertions:\n  - id: green\n    command: "true"\n    level: spec\n'
    )
    (other / ".frozen").write_text(
        '{"version": 1, "ids": {}}\n'
    )  # sources-less baseline
    dot = proj / ".conductor"
    dot.mkdir()
    (dot / "goal.md").write_text(
        "Implement docs/specs/alpha.md until done\n"
    )  # declares alpha
    (dot / "run_branch").write_text(
        "conductor/run-other\n"
    )  # ...but repointed to other

    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    for k in (
        "CONDUCTOR_GATE_SLUG",
        "CONDUCTOR_MANIFEST",
        "CONDUCTOR_FREEZE_BASELINE",
        "CONDUCTOR_GATE_DIR",
    ):
        env.pop(k, None)
    r = subprocess.run(
        [CONDUCTOR, "assert", "run", "--level", "spec"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 6, (
        "assert run validated a repointed frozen alternate:\n" + r.stdout + r.stderr
    )


def test_planted_unfrozen_alternate_manifest_cannot_report_done(tmp_path):
    # codex P1: freeze alpha, then plant a trivially-green UNFROZEN assertions/other/
    # manifest.yaml and point run_branch at it. `assert run` must fail closed (exit 6), not
    # DONE — the runner can't be dodged onto an unfrozen alternate gate.
    proj = tmp_path / "repo"
    (proj / "docs" / "specs").mkdir(parents=True)
    _build_per_slug_gate(proj, "alpha", "A")

    def _ambient(*args):
        env = dict(os.environ)
        env["CONDUCTOR_HOME"] = str(proj)
        for k in (
            "CONDUCTOR_GATE_SLUG",
            "CONDUCTOR_MANIFEST",
            "CONDUCTOR_FREEZE_BASELINE",
            "CONDUCTOR_GATE_DIR",
        ):
            env.pop(k, None)
        return subprocess.run(
            [CONDUCTOR, *args],
            cwd=str(proj),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert _ambient("gate", "freeze").returncode == 0
    assert (proj / "assertions" / "alpha" / ".frozen").is_file()

    other = proj / "assertions" / "other"
    other.mkdir()
    (other / "manifest.yaml").write_text(
        'assertions:\n  - id: planted\n    command: "true"\n    level: spec\n'
    )
    (proj / ".conductor" / "run_branch").write_text("conductor/run-other\n")

    r = _ambient("assert", "run", "--level", "spec")
    assert r.returncode == 6, (
        "planted unfrozen gate reported DONE:\n" + r.stdout + r.stderr
    )


def test_freeze_binds_assertions_source_to_selected_spec(tmp_path):
    # codex P1: /conductor:start freezes at step 3, BEFORE the goal is recorded. In a repo
    # holding >1 docs/specs/*.assertions.md the glob is ambiguous; CONDUCTOR_ASSERTIONS_SOURCE
    # binds the freeze to THIS spec's source instead of failing / freezing the wrong one.
    proj = tmp_path / "repo"
    specs = proj / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / "alpha.md.assertions.md").write_text(
        "# A\n\n## alpha-ok\n- **Claim:** holds\n"
    )
    (specs / "beta.md.assertions.md").write_text(
        "# B\n\n## beta-ok\n- **Claim:** holds\n"
    )
    _build_per_slug_gate(proj, "alpha", "A")  # writes .conductor/run_branch = run-alpha

    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    env["CONDUCTOR_GATE_SLUG"] = "alpha"
    for k in (
        "CONDUCTOR_MANIFEST",
        "CONDUCTOR_FREEZE_BASELINE",
        "CONDUCTOR_GATE_DIR",
        "CONDUCTOR_ASSERTIONS_SOURCE",
    ):
        env.pop(k, None)

    def _freeze(e):
        return subprocess.run(
            [CONDUCTOR, "gate", "freeze"],
            cwd=str(proj),
            env=e,
            capture_output=True,
            text=True,
            timeout=60,
        )

    # No goal yet + two candidate sources -> ambiguous, fail closed.
    amb = _freeze(env)
    assert amb.returncode != 0
    assert "ambiguous" in (amb.stdout + amb.stderr).lower(), amb.stdout + amb.stderr

    # Bind to THIS spec -> clean freeze against alpha's source only.
    env["CONDUCTOR_ASSERTIONS_SOURCE"] = "docs/specs/alpha.md"
    ok = _freeze(env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    frozen = proj / "assertions" / "alpha" / ".frozen"
    assert frozen.is_file()
    doc = json.loads(frozen.read_text())
    assert doc.get("sources_via") == "env"
    assert any("alpha.md.assertions.md" in k for k in doc.get("sources", {}))
    assert not any("beta" in k for k in doc.get("sources", {}))


# --- resolve_gate: the exhaustive policy matrix ----------------------------------------
#
# ONE row per meaningful cell of (selection source x namespace build-state x frozen-state
# elsewhere x flat layout). Each row pins the full GateResolution: directory, slug, source,
# and the §5 fail_closed verdict. The adversarial rows (corrupt run_branch, planted manifest,
# flat-frozen dodge) are folded in alongside the happy paths so the whole policy is covered
# in one place instead of one review round at a time.
#
# Fields: (id, env, files, run_branch, goal, exp_source, exp_dir, exp_slug, exp_fail)
#   env/exp_dir may use "{root}" -> str(tmp_path); a plain exp_dir is relative to root.
#   files: manifest.yaml -> "assertions: []"; .frozen -> "{}".
_MATRIX = [
    # --- no selection: flat, unless a frozen gate elsewhere is being abandoned ---
    ("none-empty", {}, [], None, None, "flat", "assertions", None, False),
    (
        "none-flat-frozen",
        {},
        ["assertions/manifest.yaml", "assertions/.frozen"],
        None,
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    (
        "none-flat-manifest-only",
        {},
        ["assertions/manifest.yaml"],
        None,
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    (
        "none-but-namespaced-frozen-exists",
        {},
        ["assertions/alpha/.frozen"],
        None,
        None,
        "flat",
        "assertions",
        None,
        True,
    ),
    # --- CONDUCTOR_GATE_DIR: explicit, exempt ---
    (
        "env-dir",
        {"CONDUCTOR_GATE_DIR": "{root}/custom-gate"},
        [],
        None,
        None,
        "gate_dir_env",
        "{root}/custom-gate",
        None,
        False,
    ),
    (
        "env-dir-with-frozen-namespace",
        {"CONDUCTOR_GATE_DIR": "{root}/custom-gate"},
        ["assertions/alpha/.frozen"],
        None,
        None,
        "gate_dir_env",
        "{root}/custom-gate",
        None,
        False,
    ),
    # --- CONDUCTOR_GATE_SLUG: explicit, forced, no flat fallback, exempt ---
    (
        "env-slug-unbuilt",
        {"CONDUCTOR_GATE_SLUG": "alpha"},
        [],
        None,
        None,
        "explicit_slug",
        "assertions/alpha",
        "alpha",
        False,
    ),
    (
        "env-slug-over-flat-frozen",
        {"CONDUCTOR_GATE_SLUG": "alpha"},
        ["assertions/manifest.yaml", "assertions/.frozen"],
        None,
        None,
        "explicit_slug",
        "assertions/alpha",
        "alpha",
        False,
    ),
    (
        "env-slug-second-spec-setup",
        {"CONDUCTOR_GATE_SLUG": "alpha"},
        ["assertions/beta/.frozen"],
        None,
        None,
        "explicit_slug",
        "assertions/alpha",
        "alpha",
        False,
    ),
    # --- CONDUCTOR_MANIFEST / FREEZE_BASELINE: explicit path overrides, exempt ---
    (
        "env-manifest-exempt",
        {"CONDUCTOR_MANIFEST": "{root}/custom/manifest.yaml"},
        ["assertions/alpha/.frozen"],
        None,
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    (
        "env-baseline-exempt",
        {"CONDUCTOR_FREEZE_BASELINE": "{root}/b/.frozen"},
        ["assertions/alpha/.frozen"],
        "conductor/run-junk",
        None,
        "run_branch",
        "assertions/junk",
        "junk",
        False,
    ),
    # --- ambient run_branch: build-state x frozen-state ---
    (
        "rb-unbuilt-no-frozen",
        {},
        [],
        "conductor/run-alpha",
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    (
        "rb-manifest-only",
        {},
        ["assertions/alpha/manifest.yaml"],
        "conductor/run-alpha",
        None,
        "run_branch",
        "assertions/alpha",
        "alpha",
        False,
    ),
    (
        "rb-frozen-only",
        {},
        ["assertions/alpha/.frozen"],
        "conductor/run-alpha",
        None,
        "run_branch",
        "assertions/alpha",
        "alpha",
        False,
    ),
    (
        "rb-manifest-and-frozen",
        {},
        ["assertions/alpha/manifest.yaml", "assertions/alpha/.frozen"],
        "conductor/run-alpha",
        None,
        "run_branch",
        "assertions/alpha",
        "alpha",
        False,
    ),
    # --- adversarial: corrupt run_branch / planted manifest dodging a frozen gate ---
    (
        "rb-corrupt-vs-namespaced-frozen",
        {},
        ["assertions/alpha/.frozen"],
        "conductor/run-junk",
        None,
        "run_branch",
        "assertions/junk",
        "junk",
        True,
    ),
    (
        "rb-planted-vs-namespaced-frozen",
        {},
        ["assertions/alpha/.frozen", "assertions/other/manifest.yaml"],
        "conductor/run-other",
        None,
        "run_branch",
        "assertions/other",
        "other",
        True,
    ),
    (
        "rb-planted-vs-flat-frozen",
        {},
        [
            "assertions/.frozen",
            "assertions/manifest.yaml",
            "assertions/other/manifest.yaml",
        ],
        "conductor/run-other",
        None,
        "run_branch",
        "assertions/other",
        "other",
        True,
    ),
    (
        # a malformed/edited run_branch suffix that is not a safe path component (path
        # separators / ..) is rejected -> no slug -> flat; never joined into a gate path.
        "run_branch-path-traversal-rejected",
        {},
        [],
        "conductor/run-../../evil",
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    (
        "rb-corrupt-vs-flat-frozen-only",
        {},
        ["assertions/.frozen", "assertions/manifest.yaml"],
        "conductor/run-junk",
        None,
        "flat",
        "assertions",
        None,
        False,
    ),
    # --- ambient goal + run_branch<->goal agreement (§5 clause ii) ---
    (
        "goal-built",
        {},
        ["assertions/beta/manifest.yaml"],
        None,
        "Implement docs/specs/beta.md until done",
        "goal",
        "assertions/beta",
        "beta",
        False,
    ),
    (
        "run_branch-and-goal-agree",
        {},
        ["assertions/beta/manifest.yaml"],
        "conductor/run-beta",
        "Implement docs/specs/beta.md until done",
        "run_branch",
        "assertions/beta",
        "beta",
        False,
    ),
    (
        # disagreement alone is not §5: with nothing frozen there is no frozen gate to dodge,
        # so clause (ii) — which only guards a FROZEN resolved gate — does not fire.
        "run_branch-goal-disagree-but-unfrozen-ok",
        {},
        ["assertions/alpha/manifest.yaml"],
        "conductor/run-alpha",
        "Implement docs/specs/beta.md until done",
        "run_branch",
        "assertions/alpha",
        "alpha",
        False,
    ),
    (
        "run_branch-repointed-to-frozen-alternate",
        {},
        [
            "assertions/alpha/manifest.yaml",
            "assertions/alpha/.frozen",
            "assertions/other/manifest.yaml",
            "assertions/other/.frozen",
        ],
        "conductor/run-other",
        "Implement docs/specs/alpha.md until done",
        "run_branch",
        "assertions/other",
        "other",
        True,
    ),
]


def _subst(value, root):
    return value.replace("{root}", str(root)) if "{root}" in value else value


@pytest.mark.parametrize("row", _MATRIX, ids=[r[0] for r in _MATRIX])
def test_resolve_gate_policy_matrix(tmp_path, monkeypatch, row):
    _id, env, files, run_branch, goal, exp_source, exp_dir, exp_slug, exp_fail = row
    _clear_env(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, _subst(v, tmp_path))
    for f in files:
        _write(
            tmp_path, f, "assertions: []\n" if f.endswith("manifest.yaml") else "{}\n"
        )
    if run_branch:
        _write(tmp_path, ".conductor/run_branch", run_branch + "\n")
    if goal:
        _write(tmp_path, ".conductor/goal.md", goal + "\n")

    g = paths.resolve_gate(str(tmp_path))

    want_dir = _subst(exp_dir, tmp_path)
    if "{root}" not in exp_dir and not os.path.isabs(exp_dir):
        want_dir = str(tmp_path / exp_dir)
    assert g.directory == want_dir, f"{_id}: directory -> {g}"
    assert g.slug == exp_slug, f"{_id}: slug -> {g}"
    assert g.source == exp_source, f"{_id}: source -> {g}"
    assert (g.fail_closed is not None) is exp_fail, f"{_id}: fail_closed -> {g}"

    # the derived paths are consistent with the resolution + honor explicit overrides
    exp_manifest = env.get("CONDUCTOR_MANIFEST")
    exp_manifest = (
        _subst(exp_manifest, tmp_path)
        if exp_manifest
        else os.path.join(g.directory, "manifest.yaml")
    )
    exp_baseline = env.get("CONDUCTOR_FREEZE_BASELINE")
    exp_baseline = (
        _subst(exp_baseline, tmp_path)
        if exp_baseline
        else os.path.join(g.directory, ".frozen")
    )
    assert g.manifest == exp_manifest, f"{_id}: manifest -> {g}"
    assert g.baseline == exp_baseline, f"{_id}: baseline -> {g}"
    assert g.run_dir == os.path.join(os.path.dirname(g.manifest), "run"), (
        f"{_id}: run_dir -> {g}"
    )
    # the thin wrappers agree with the one resolution
    assert paths.gate_dir(str(tmp_path)) == g.directory
    assert paths.manifest_path(str(tmp_path)) == g.manifest
    assert paths.baseline_path(str(tmp_path)) == g.baseline
    assert paths.run_dir(str(tmp_path)) == g.run_dir
    assert paths.unresolved_frozen_gate(str(tmp_path)) is (g.fail_closed is not None)


# --- run-key mode: the key alone governs (design §"Project and run identity") --------------
#
# `pytest`, `os` and `subprocess` are imported at the top of this file, as are the
# `conductor.core` modules these tests use; the `git_repo` fixture comes from
# tests/conductor/conftest.py. The expected names are restated as literals on purpose — a test
# that computed them with `derived_names` (the helper under test) would assert nothing.

_NOW = "2026-08-10T12:00:00+00:00"


def _run_doc(spec, *, generation=1, identity_scheme="path-hash-v2", **overrides):
    key = runkey.run_key(spec, generation)
    doc = schema.new_run_doc(
        run_key=key,
        generation=generation,
        spec_path=spec,
        workstation_id="0123456789abcdef0123456789abcdef",
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=_NOW,
        identity_scheme=identity_scheme,
    )
    doc.update(overrides)
    return doc


def test_run_gate_dir_is_assertions_slash_run_key(tmp_path):
    key = runkey.run_key("docs/specs/alpha.md")
    assert paths.run_gate_dir(str(tmp_path), key) == str(tmp_path / "assertions" / key)


def test_run_gate_dir_refuses_an_unsafe_run_key(tmp_path):
    # A helper that composes a filesystem path from a key must refuse a key that escapes the
    # gate root, exactly as runstate._checked does before building a state path — otherwise the
    # traversal is latent until the first caller passes a tampered key.
    for bad in ("../x", "a/b", "..", ".lock", "x.lock", "", "-leading", "/abs"):
        with pytest.raises(ValueError) as excinfo:
            paths.run_gate_dir(str(tmp_path), bad)
        assert repr(bad) in str(excinfo.value), bad


def test_run_key_mode_ignores_ambient_files_and_environment(tmp_path, monkeypatch):
    spec = "docs/specs/alpha.md"
    doc = _run_doc(spec)
    key = doc["run_key"]
    _write(tmp_path, ".conductor/run_branch", "conductor/run-hijacked\n")
    _write(tmp_path, ".conductor/goal.md", "docs/specs/gamma.md\n")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked")
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv(
        "CONDUCTOR_MANIFEST", str(tmp_path / "elsewhere" / "manifest.yaml")
    )
    monkeypatch.setenv(
        "CONDUCTOR_FREEZE_BASELINE", str(tmp_path / "elsewhere" / ".frozen")
    )
    res = paths.resolve_gate(str(tmp_path), run_key=key, run=doc)
    assert res.source == "run_key"
    assert res.slug == key
    assert res.directory == str(tmp_path / "assertions" / key)
    assert res.manifest == str(tmp_path / "assertions" / key / "manifest.yaml")
    assert res.baseline == str(tmp_path / "assertions" / key / ".frozen")
    assert res.run_dir == str(tmp_path / "assertions" / key / "run")
    assert res.fail_closed is None


def test_two_run_keys_resolve_to_distinct_gates(tmp_path):
    alpha, beta = _run_doc("docs/specs/alpha.md"), _run_doc("docs/specs/beta.md")
    first = paths.resolve_gate(str(tmp_path), run_key=alpha["run_key"], run=alpha)
    second = paths.resolve_gate(str(tmp_path), run_key=beta["run_key"], run=beta)
    assert first.directory != second.directory
    assert first.manifest != second.manifest
    assert first.baseline != second.baseline
    assert first.run_dir != second.run_dir


def test_a_later_generation_gets_its_own_gate(tmp_path):
    first = _run_doc("docs/specs/alpha.md", generation=1)
    second = _run_doc("docs/specs/alpha.md", generation=2)
    assert paths.resolve_gate(
        str(tmp_path), run_key=first["run_key"], run=first
    ).directory != (
        paths.resolve_gate(
            str(tmp_path), run_key=second["run_key"], run=second
        ).directory
    )


def test_gate_dir_disagreeing_with_the_run_key_fails_closed(tmp_path):
    doc = _run_doc("docs/specs/alpha.md", gate_dir="assertions/some-other-run")
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "gate_dir" in res.fail_closed
    assert "run.json" in res.fail_closed


def test_integration_branch_disagreeing_with_the_run_key_fails_closed(tmp_path):
    doc = _run_doc(
        "docs/specs/alpha.md", integration_branch="conductor/run-something-else"
    )
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "integration_branch" in res.fail_closed


def test_an_unsafe_recorded_gate_dir_never_becomes_a_path(tmp_path):
    doc = _run_doc("docs/specs/alpha.md")
    doc["gate_dir"] = "assertions/../../outside"
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed
    assert "outside" not in res.directory


def test_an_unknown_identity_scheme_fails_closed(tmp_path):
    # The scheme decides WHICH verification applies, so an unrecognised one means no
    # verification ran at all. It must refuse rather than resolve unchecked.
    doc = _run_doc("docs/specs/alpha.md", identity_scheme="path-hash-v3")
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "identity_scheme" in res.fail_closed
    # A refusal resolves to the inert directory, never to the segment the unverified record
    # claimed. That segment is well-formed here, and trusting a well-formed segment is what let a
    # mis-paired record resolve onto ANOTHER run's real, frozen, green gate.
    assert res.directory == str(tmp_path / "assertions" / "__unresolved__")


def test_a_run_document_for_another_run_fails_closed(tmp_path):
    # A legacy-slug-v1 record is exempt from the derived-name cross-check and so its recorded
    # names are trusted verbatim. If the resolver did not also check that the document IS this
    # run's, a mis-paired load would hand this key the OTHER run's gate — precisely the
    # "validate some other run's already-green gate" outcome run-key mode exists to prevent.
    other = _run_doc("docs/specs/beta.md", identity_scheme="legacy-slug-v1")
    other["gate_dir"] = "assertions/self-enforcement"
    other["integration_branch"] = "conductor/run-self-enforcement"
    mine = runkey.run_key("docs/specs/alpha.md")
    res = paths.resolve_gate(str(tmp_path), run_key=mine, run=other)
    assert res.fail_closed and "run_key" in res.fail_closed
    # the verdict names BOTH keys, so the operator can see which record was loaded
    assert mine in res.fail_closed and other["run_key"] in res.fail_closed
    # And the resolved directory is the inert one. `assertions/self-enforcement` is a real,
    # frozen, green gate in this repository: a caller that read `directory` without checking
    # `fail_closed` would validate against it and pass. Setting fail_closed while still handing
    # back the other run's gate is the whole bug, so the directory is asserted, not just the
    # verdict.
    assert res.directory == str(tmp_path / "assertions" / "__unresolved__")
    assert res.manifest.startswith(res.directory)
    assert res.baseline.startswith(res.directory)


def _failing_run_key_cases():
    """Every way run-key mode refuses, as (label, run_key, run document) rows."""
    wrong_record = _run_doc("docs/specs/beta.md", identity_scheme="legacy-slug-v1")
    wrong_record["gate_dir"] = "assertions/self-enforcement"
    wrong_record["integration_branch"] = "conductor/run-self-enforcement"
    unsafe_dir = _run_doc("docs/specs/alpha.md")
    unsafe_dir["gate_dir"] = "assertions/../../outside"
    absent_dir = _run_doc("docs/specs/alpha.md", gate_dir="somewhere/else")
    bad_gate_dir = _run_doc("docs/specs/alpha.md", gate_dir="assertions/some-other-run")
    bad_branch = _run_doc(
        "docs/specs/alpha.md", integration_branch="conductor/run-something-else"
    )
    unknown_scheme = _run_doc("docs/specs/alpha.md", identity_scheme="path-hash-v3")
    return [
        ("wrong run's record", runkey.run_key("docs/specs/alpha.md"), wrong_record),
        ("unsafe gate_dir", unsafe_dir["run_key"], unsafe_dir),
        ("gate_dir outside assertions/", absent_dir["run_key"], absent_dir),
        ("gate_dir mismatch", bad_gate_dir["run_key"], bad_gate_dir),
        ("integration_branch mismatch", bad_branch["run_key"], bad_branch),
        ("unknown identity_scheme", unknown_scheme["run_key"], unknown_scheme),
    ]


def test_no_run_key_refusal_collapses_onto_the_flat_gate(tmp_path):
    # THE point of run-key mode. In a repo that has one — this one does — flat `assertions/` is
    # a real, FROZEN, green gate, so a refusal that redirected there would hand a caller that
    # read .directory without checking .fail_closed the repo's own passing gate to validate and
    # to write results into. Legacy mode never redirects on failure (it keeps whatever it
    # resolved, and reaches flat only when fail_closed is None); run-key mode must not either.
    _write(tmp_path, "assertions/manifest.yaml", "assertions: []\n")
    _write(tmp_path, "assertions/.frozen", "{}\n")
    flat = str(tmp_path / "assertions")
    for label, key, doc in _failing_run_key_cases():
        res = paths.resolve_gate(str(tmp_path), run_key=key, run=doc)
        assert res.fail_closed, label
        assert res.directory != flat, label
        assert res.manifest != os.path.join(flat, "manifest.yaml"), label
        assert res.baseline != os.path.join(flat, ".frozen"), label
        assert res.run_dir != os.path.join(flat, "run"), label
        assert not os.path.exists(res.manifest), label
        assert not os.path.exists(res.baseline), label


def test_an_unusable_gate_dir_refusal_lands_on_an_inert_directory(tmp_path):
    # With no safe segment to name, the refusal needs a path that cannot be any run's gate.
    # A run key must start with [a-z0-9], so `__unresolved__` can never collide with one.
    for gate_dir in ("assertions/../../outside", "somewhere/else", "assertions/"):
        doc = _run_doc("docs/specs/alpha.md")
        doc["gate_dir"] = gate_dir
        res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
        assert res.fail_closed, gate_dir
        assert res.directory == str(tmp_path / "assertions" / "__unresolved__"), (
            gate_dir
        )


def test_a_legacy_slug_run_keeps_its_recorded_names(tmp_path):
    doc = _run_doc("docs/specs/alpha.md", identity_scheme="legacy-slug-v1")
    doc["gate_dir"] = "assertions/self-enforcement"
    doc["integration_branch"] = "conductor/run-self-enforcement"
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed is None
    assert res.directory == str(tmp_path / "assertions" / "self-enforcement")


_UNSAFE_LEGACY_GATE_DIRS = (
    "assertions/a..b",  # matches the old schema regex; traverses; refused by the resolver
    "assertions/../../outside",
    "assertions/x/y",
    "assertions/x.lock",
    "assertions/",
    "assertions/.hidden",
    "elsewhere/alpha",
)


@pytest.mark.parametrize("gate_dir", _UNSAFE_LEGACY_GATE_DIRS)
def test_the_writer_and_the_resolver_agree_on_an_unsafe_legacy_gate_dir(
    tmp_path, gate_dir
):
    """``legacy-slug-v1`` is EXEMPT from the derived-name cross-check, so ``schema``'s gate_dir
    guard and ``paths``'s segment guard are the only structural validation its ``gate_dir`` ever
    gets — and they disagreed: ``assertions/a..b`` was accepted by ``validate_run`` and refused by
    ``resolve_gate``, a record legal to write and impossible to use. Every existing bad-gate_dir
    test uses ``path-hash-v2``, where the derived-name check fires and masks both guards, so this
    is the only place either one is pinned. Both now delegate to ``names.is_safe_segment``."""
    doc = _run_doc("docs/specs/alpha.md", identity_scheme="legacy-slug-v1")
    doc["gate_dir"] = gate_dir
    doc["integration_branch"] = "conductor/run-self-enforcement"

    with pytest.raises(schema.SchemaError) as excinfo:
        schema.validate_run(doc)
    assert "gate_dir" in str(excinfo.value)

    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed and "gate_dir" in res.fail_closed
    assert res.directory == str(tmp_path / "assertions" / "__unresolved__")


def test_the_writer_and_the_resolver_agree_on_a_safe_legacy_gate_dir(tmp_path):
    """The control for the refusals above: a migrated run's non-derived-but-safe segment is
    accepted by BOTH, so neither guard is simply refusing everything ``legacy-slug-v1``."""
    doc = _run_doc("docs/specs/alpha.md", identity_scheme="legacy-slug-v1")
    doc["gate_dir"] = "assertions/self-enforcement"
    doc["integration_branch"] = "conductor/run-self-enforcement"
    assert schema.validate_run(doc) is doc
    res = paths.resolve_gate(str(tmp_path), run_key=doc["run_key"], run=doc)
    assert res.fail_closed is None
    assert res.directory == str(tmp_path / "assertions" / "self-enforcement")


def test_run_key_mode_requires_the_run_document(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        paths.resolve_gate(str(tmp_path), run_key="alpha-1a2b3c4d")
    assert "runstate.load" in str(excinfo.value)


def test_gate_for_run_loads_the_document_and_resolves(git_repo):
    root = str(git_repo)
    doc = _run_doc("docs/specs/alpha.md")
    runstate.create(core_resolve.state_root(root), doc["run_key"], doc)
    res = core_resolve.resolve(run_key=doc["run_key"], start=root)
    gate = core_resolve.gate_for_run(res)
    assert gate.source == "run_key"
    assert gate.directory == os.path.join(res.repo_root, "assertions", doc["run_key"])


def test_legacy_mode_is_unchanged_when_no_run_key_is_given(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _write(tmp_path, ".conductor/run_branch", "conductor/run-alpha\n")
    _write(tmp_path, ".conductor/goal.md", "docs/specs/alpha.md\n")
    _write(tmp_path, "assertions/alpha/manifest.yaml", "assertions: []\n")
    res = paths.resolve_gate(str(tmp_path))
    assert res.source == "run_branch"
    assert res.directory == str(tmp_path / "assertions" / "alpha")


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
