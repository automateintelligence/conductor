"""Per-spec done-gate resolution (multi-spec safety).

The done-gate (manifest.yaml, .frozen, run/results.json) is a tracked path. Flat at
``assertions/`` it is one per-repo slot two sibling-worktree specs contend for at the shared
base. These tests pin the namespacing that lets them coexist at ``assertions/<slug>/`` while
keeping the flat legacy gate — and a stale ``.conductor/`` — working untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


# --- gate_dir vs setup_gate_dir: read prefers built per-slug; setup always namespaces ----


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


def test_gate_dir_falls_back_to_flat_until_per_slug_gate_built(tmp_path, monkeypatch):
    # A slug resolves but the per-slug manifest is not there yet (e.g. a stale .conductor/
    # left by a finished run, or before setup writes it): the flat legacy gate is used.
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions")


def test_gate_dir_uses_per_slug_once_manifest_exists(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    _write(tmp_path, "assertions/alpha/manifest.yaml", "assertions: []\n")
    assert paths.gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "alpha")


def test_setup_gate_dir_namespaces_before_manifest_exists(tmp_path, monkeypatch):
    # setup is what CREATES the manifest — it must point at the per-slug dir up front.
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    assert paths.setup_gate_dir(str(tmp_path)) == str(tmp_path / "assertions" / "alpha")


def test_gate_dir_env_override_wins(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", "/somewhere/else")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "alpha")
    assert paths.gate_dir(str(tmp_path)) == "/somewhere/else"


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


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
