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


def test_gate_dir_cli_honors_gate_dir_override(tmp_path):
    # codex P2: the CLI verb must match paths.gate_dir() — $CONDUCTOR_GATE_DIR overrides
    # outright, else assertions/<slug>. Divergence writes one dir and reads another.
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(tmp_path)
    for k in ("CONDUCTOR_GATE_SLUG", "CONDUCTOR_MANIFEST", "CONDUCTOR_FREEZE_BASELINE"):
        env.pop(k, None)

    def _gate_dir(e):
        return subprocess.run(
            [CONDUCTOR, "gate-dir", "docs/specs/alpha.md"],
            cwd=str(tmp_path),
            env=e,
            capture_output=True,
            text=True,
            timeout=30,
        )

    env.pop("CONDUCTOR_GATE_DIR", None)
    default = _gate_dir(env)
    assert default.returncode == 0, default.stderr
    assert default.stdout.strip() == "assertions/alpha"

    env["CONDUCTOR_GATE_DIR"] = "/tmp/custom-gate"
    override = _gate_dir(env)
    assert override.returncode == 0, override.stderr
    assert override.stdout.strip() == "/tmp/custom-gate"

    # An already-set CONDUCTOR_GATE_SLUG must win over the spec-derived slug, matching
    # paths.gate_dir() (codex P2) — else gate-dir writes one dir and lint/freeze read another.
    env.pop("CONDUCTOR_GATE_DIR", None)
    env["CONDUCTOR_GATE_SLUG"] = "explicitslug"
    slug_case = _gate_dir(env)
    assert slug_case.returncode == 0, slug_case.stderr
    assert slug_case.stdout.strip() == "assertions/explicitslug"


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


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
