"""The `conductor run` verb group.

Every scheduled or non-interactive invocation carries an explicit run key; a bare command is
allowed only when exactly one active run exists, and otherwise fails with the available keys and
the exact commands (design §"Project and run identity")."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conductor import run_cmd
from conductor.core import (
    atomic,
    hygiene,
    names,
    registry,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
)

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")

# The git_env / git / git_repo fixtures come from tests/conductor/conftest.py (Task 9), which
# covers this directory. Do not add `pytest_plugins` — pytest only honours it in the rootdir
# conftest, and a nested reference is deprecated.


@pytest.fixture(autouse=True)
def isolated_conductor_env(tmp_path, monkeypatch):
    """Keep the installation ID out of the developer's real config directory, clear the ambient
    gate overrides so the wrapper's legacy path is deterministic, and point the ambient project
    at a directory that is NOT a git repository.

    That last one is the important one: every verb falls back to ``$CONDUCTOR_HOME`` when
    ``--project`` does not reach it, so a bug that drops ``--project`` would otherwise write run
    state into whatever repository the test suite happens to run from. Pointing it at a non-repo
    turns that into a loud, harmless failure."""
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    ambient = tmp_path / "not-a-repo"
    ambient.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(ambient))
    for name in (
        "CONDUCTOR_GATE_DIR",
        "CONDUCTOR_GATE_SLUG",
        "CONDUCTOR_MANIFEST",
        "CONDUCTOR_FREEZE_BASELINE",
    ):
        monkeypatch.delenv(name, raising=False)


def _run(root, *args):
    return run_cmd.main([*args, "--project", str(root)])


def test_new_creates_the_registry_the_run_dir_and_run_json(git_repo, capsys):
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 0
    key = capsys.readouterr().out.strip()
    assert key == runkey.run_key("docs/specs/alpha.md")
    state_root = resolve.state_root(str(git_repo))
    doc = runstate.load(state_root, key)
    assert doc is not None
    assert doc["spec_path"] == "docs/specs/alpha.md"
    assert doc["status"] == "active"
    assert doc["integration_branch"] == f"conductor/run-{key}"
    assert doc["gate_dir"] == f"assertions/{key}"
    assert doc["spec_digest"] == run_cmd.spec_digest(
        str(git_repo), "docs/specs/alpha.md"
    )
    project_doc = registry.load(state_root)
    assert project_doc is not None
    assert registry.current_run_key(project_doc, "docs/specs/alpha.md") == key


def test_new_writes_both_files_through_one_journalled_transaction(git_repo, capsys):
    """``run new`` writes project.json and run.json, so it must not be able to land one without
    the other. The journal is gone on success; the crash case is covered below."""
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 0
    capsys.readouterr()
    assert transaction.pending(resolve.state_root(str(git_repo))) == []


def test_new_establishes_the_local_exclude(git_repo):
    _run(git_repo, "new", "docs/specs/alpha.md")
    assert hygiene.is_ignored(str(git_repo), ".conductor/project.json")


def test_new_refuses_when_run_state_is_already_tracked(git_repo, git, capsys):
    (git_repo / ".conductor").mkdir()
    (git_repo / ".conductor" / "goal.md").write_text("goal\n")
    git(git_repo, "add", "-f", ".conductor/goal.md")
    git(git_repo, "commit", "-qm", "oops")
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    assert "rm -r --cached" in capsys.readouterr().err


def test_new_refuses_a_spec_outside_the_repository(git_repo, capsys):
    assert _run(git_repo, "new", "../outside.md") == 1
    assert "outside the repository" in capsys.readouterr().err
    assert registry.load(resolve.state_root(str(git_repo))) is None


def test_new_refuses_a_spec_that_does_not_exist(git_repo, capsys):
    assert _run(git_repo, "new", "docs/specs/missing.md") == 1
    err = capsys.readouterr().err
    assert "does not exist" in err
    # hygiene.ensure_local_exclude has already written .git/info/exclude by this point, so the
    # house "no write occurred" phrase would be false here; the message must scope its claim to
    # run state and name the scaffolding it did write.
    assert "no run state was written" in err
    assert "git exclude" in err
    assert "no write occurred" not in err
    assert registry.load(resolve.state_root(str(git_repo))) is None


def test_new_twice_for_the_same_spec_refuses_and_names_the_existing_run(
    git_repo, capsys
):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    err = capsys.readouterr().err
    assert runkey.run_key("docs/specs/alpha.md") in err
    assert "--new-run" in err


def test_new_without_new_run_refuses_a_spec_whose_generations_all_ended(
    git_repo, capsys
):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    first = runkey.run_key("docs/specs/alpha.md")
    runstate.set_status(state_root, first, "awaiting-team-merge")
    runstate.set_status(state_root, first, "terminal")
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    assert "--new-run" in capsys.readouterr().err


def _mirror(state_root, spec):
    """(generation statuses by run key, current) as project.json records them."""
    project_doc = registry.load(state_root)
    assert project_doc is not None
    entry = registry.mapping(project_doc, spec)
    assert entry is not None
    return {g["run_key"]: g["status"] for g in entry["generations"]}, entry["current"]


def test_new_run_after_a_terminal_record_creates_generation_two_and_fixes_the_mirror(
    git_repo, capsys
):
    """No test-side registry.mirror_status: this is exactly the state a finished run leaves in
    production, because runstate.set_status holds only state.lock and nothing else writes the
    mirror. cmd_new folds each generation's authoritative status into the project.json after-image
    it is already journalling, so --new-run works and the mirror converges on the records."""
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    first = runkey.run_key("docs/specs/alpha.md")
    runstate.set_status(state_root, first, "awaiting-team-merge")
    runstate.set_status(state_root, first, "terminal")
    assert _mirror(state_root, "docs/specs/alpha.md") == ({first: "active"}, first)

    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 0
    assert capsys.readouterr().out.strip() == f"{first}-g2"
    statuses, current = _mirror(state_root, "docs/specs/alpha.md")
    assert statuses == {first: "terminal", f"{first}-g2": "active"}
    assert current == f"{first}-g2"


def test_new_run_after_a_failed_record_creates_generation_two_and_fixes_the_mirror(
    git_repo, capsys
):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    first = runkey.run_key("docs/specs/alpha.md")
    runstate.set_status(state_root, first, "failed")
    assert _mirror(state_root, "docs/specs/alpha.md") == ({first: "active"}, first)

    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 0
    assert capsys.readouterr().out.strip() == f"{first}-g2"
    statuses, current = _mirror(state_root, "docs/specs/alpha.md")
    assert statuses == {first: "failed", f"{first}-g2": "active"}
    assert current == f"{first}-g2"


def test_new_refuses_a_spec_whose_content_already_belongs_to_a_run(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    (git_repo / "docs" / "specs" / "archive" / "alpha.md").write_bytes(
        (git_repo / "docs" / "specs" / "alpha.md").read_bytes()
    )
    assert _run(git_repo, "new", "docs/specs/archive/alpha.md") == 1
    err = capsys.readouterr().err
    assert "conductor run repoint-spec" in err
    assert runkey.run_key("docs/specs/alpha.md") in err
    state_root = resolve.state_root(str(git_repo))
    assert (
        runstate.load(state_root, runkey.run_key("docs/specs/archive/alpha.md")) is None
    )


def test_new_decides_on_the_record_not_a_stale_active_status_mirror(git_repo, capsys):
    """project.json's per-generation status is a MIRROR and runstate.set_status does not touch
    it. With the record terminal and the mirror still naming the run current, `new` must agree
    with `resolve` that nothing is live — deciding on the mirror tells the operator to finish a
    run that is already finished, and --new-run then refuses identically."""
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key("docs/specs/alpha.md")
    runstate.set_status(state_root, key, "awaiting-team-merge")
    runstate.set_status(
        state_root, key, "terminal"
    )  # mirror deliberately left untouched
    project_doc = registry.load(state_root)
    assert project_doc is not None
    assert registry.current_run_key(project_doc, "docs/specs/alpha.md") == key

    assert _run(git_repo, "resolve") == 3  # the authority: nothing is live
    capsys.readouterr()
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    err = capsys.readouterr().err
    assert "unfinished" not in err
    assert "finish or fail" not in err
    assert "--new-run" in err


def test_new_run_refuses_while_the_record_is_live_behind_a_terminal_mirror(
    git_repo, capsys
):
    """The other skew. The mirror says terminal, so current_run_key is None and a mirror-only
    gate lets --new-run mint a second generation — leaving TWO authoritatively-active runs for
    one spec, which schema.validate_project cannot catch because it validates the mirror."""
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key("docs/specs/alpha.md")
    registry.update(state_root, lambda d: registry.mirror_status(d, key, "terminal"))
    project_doc = registry.load(state_root)
    assert project_doc is not None
    assert registry.current_run_key(project_doc, "docs/specs/alpha.md") is None
    record = runstate.load(state_root, key)
    assert record is not None and record["status"] == "active"

    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 1
    assert key in capsys.readouterr().err
    assert resolve.active_run_keys(state_root) == [key]


def test_new_refuses_a_registered_run_whose_record_is_gone_without_useless_advice(
    git_repo, capsys
):
    """The registry maps the spec to a run whose record has been deleted. Neither `finish`/`fail`
    nor --new-run can act on a record that does not exist, so the refusal must not offer them."""
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key("docs/specs/alpha.md")
    os.remove(runstate.run_path(state_root, key))

    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 1
    err = capsys.readouterr().err
    assert "missing or carry no status" in err
    assert f"rm -r {runstate.run_dir(state_root, key)}" in err
    assert "Start a new one: finish or fail" not in err
    assert runstate.load(state_root, f"{key}-g2") is None


def test_new_refuses_an_orphaned_run_record_with_a_remedy_that_works(git_repo, capsys):
    """A run.json the registry does not know about cannot be cleared by ``--new-run``: with no
    mapping, the next generation is 1 again and derives the same key. So the refusal has to name
    the removal, and this proves ``--new-run`` really is the wrong advice here."""
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key("docs/specs/alpha.md")
    derived = names.derived_names(key)
    atomic.write_json_atomic(
        runstate.run_path(state_root, key),
        schema.new_run_doc(
            run_key=key,
            generation=1,
            spec_path="docs/specs/alpha.md",
            workstation_id="ws-test",
            integration_branch=derived.integration_branch,
            gate_dir=derived.gate_dir,
            spec_digest=run_cmd.spec_digest(str(git_repo), "docs/specs/alpha.md"),
            now="2026-08-10T00:00:00+00:00",
        ),
    )
    assert _run(git_repo, "new", "docs/specs/alpha.md") == 1
    err = capsys.readouterr().err
    assert "not registered" in err
    assert f"rm -r {runstate.run_dir(state_root, key)}" in err
    assert _run(git_repo, "new", "docs/specs/alpha.md", "--new-run") == 1
    assert "not registered" in capsys.readouterr().err


def test_list_shows_active_runs_and_all_shows_history(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    _run(git_repo, "new", "docs/specs/beta.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    beta = runkey.run_key("docs/specs/beta.md")
    runstate.set_status(state_root, beta, "awaiting-team-merge")
    runstate.set_status(state_root, beta, "terminal")
    _run(git_repo, "list")
    active = capsys.readouterr().out
    assert runkey.run_key("docs/specs/alpha.md") in active and beta not in active
    _run(git_repo, "list", "--all")
    assert beta in capsys.readouterr().out


def test_list_json_is_machine_readable(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    _run(git_repo, "list", "--json")
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["run_key"] == runkey.run_key("docs/specs/alpha.md")
    assert payload[0]["status"] == "active"
    assert payload[0]["spec_path"] == "docs/specs/alpha.md"


def test_list_says_so_when_the_project_has_no_registry(git_repo, capsys):
    assert _run(git_repo, "list") == 0
    assert capsys.readouterr().out.strip() == "no active runs"
    assert _run(git_repo, "list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_show_prints_the_run_record(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "show", "--run", key) == 0
    assert json.loads(capsys.readouterr().out)["run_key"] == key


def test_show_of_an_unknown_run_exits_3(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    assert _run(git_repo, "show", "--run", "nope-00000000") == 3
    assert "conductor run list --all" in capsys.readouterr().err


def test_resolve_without_a_key_works_with_one_active_run(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "resolve") == 0
    assert capsys.readouterr().out.strip() == key


def test_resolve_with_two_active_runs_exits_2_and_lists_both_commands(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    _run(git_repo, "new", "docs/specs/beta.md")
    capsys.readouterr()
    assert _run(git_repo, "resolve") == 2
    err = capsys.readouterr().err
    for spec in ("docs/specs/alpha.md", "docs/specs/beta.md"):
        assert f"--run {runkey.run_key(spec)}" in err


def test_resolve_with_no_active_run_exits_3(git_repo, capsys):
    assert _run(git_repo, "resolve") == 3
    assert "conductor run new" in capsys.readouterr().err


def test_gate_dir_prints_the_run_scoped_directory(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "gate-dir", "--run", key) == 0
    assert capsys.readouterr().out.strip() == os.path.join(
        os.path.realpath(str(git_repo)), "assertions", key
    )


def test_gate_dir_fails_closed_when_run_json_disagrees_with_the_key(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    state_root = resolve.state_root(str(git_repo))
    doc = runstate.load(state_root, key)
    assert doc is not None
    # Written straight to disk on purpose: schema.validate_run refuses a path-hash-v2 record whose
    # gate_dir is not the run_key-derived one, so runstate.update cannot produce this document.
    # A hand edit can, and that is exactly what has to fail closed.
    atomic.write_json_atomic(
        runstate.run_path(state_root, key),
        {**doc, "gate_dir": "assertions/some-other-run"},
    )
    assert _run(git_repo, "gate-dir", "--run", key) == 1
    assert "gate_dir" in capsys.readouterr().err


def test_gate_dir_fails_closed_when_the_record_belongs_to_another_run(git_repo, capsys):
    """A mis-paired record must not hand this key another run's gate directory — the segment in
    the foreign record is perfectly safe, so only the identity check catches it."""
    _run(git_repo, "new", "docs/specs/alpha.md")
    alpha = capsys.readouterr().out.strip()
    _run(git_repo, "new", "docs/specs/beta.md")
    beta = capsys.readouterr().out.strip()
    state_root = resolve.state_root(str(git_repo))
    foreign = runstate.load(state_root, beta)
    assert foreign is not None
    atomic.write_json_atomic(runstate.run_path(state_root, alpha), foreign)
    assert _run(git_repo, "gate-dir", "--run", alpha) == 1
    err = capsys.readouterr().err
    assert alpha in err and beta in err


def test_repoint_spec_moves_the_mapping(git_repo, git, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    git(git_repo, "mv", "docs/specs/alpha.md", "docs/specs/archive/alpha.md")
    assert (
        _run(git_repo, "repoint-spec", "--run", key, "docs/specs/archive/alpha.md") == 0
    )
    state_root = resolve.state_root(str(git_repo))
    doc = runstate.load(state_root, key)
    assert doc is not None
    assert doc["spec_path"] == "docs/specs/archive/alpha.md"


def test_repoint_spec_refusal_exits_1(git_repo, capsys):
    _run(git_repo, "new", "docs/specs/alpha.md")
    key = capsys.readouterr().out.strip()
    assert _run(git_repo, "repoint-spec", "--run", key, "docs/specs/beta.md") == 1
    assert "not the same spec" in capsys.readouterr().err


def test_unknown_subcommand_is_a_usage_error(git_repo):
    assert _run(git_repo, "frobnicate") == 64


def test_help_exits_0(git_repo, capsys):
    assert run_cmd.main(["--help"]) == 0
    assert "repoint-spec" in capsys.readouterr().out


# --- The two behaviours main() owns ----------------------------------------------------------


def test_project_is_honoured_before_the_subcommand(git_repo, capsys):
    """``--project`` is accepted on both sides of the subcommand. Every subparser declares it
    with ``argparse.SUPPRESS`` so it sets the attribute only when actually present; with a plain
    ``default=None`` the subparser overwrites the value the top-level parser already stored, and
    ``conductor run --project /repo new spec.md`` silently falls back to $CONDUCTOR_HOME."""
    assert run_cmd.main(["--project", str(git_repo), "new", "docs/specs/alpha.md"]) == 0
    key = capsys.readouterr().out.strip()
    assert key == runkey.run_key("docs/specs/alpha.md")
    assert runstate.load(resolve.state_root(str(git_repo)), key) is not None


def _committed_but_unapplied_registration(git_repo, spec):
    """Prepare and commit — but never apply — a transaction that registers ``spec``.

    This is the on-disk shape a crash between ``transaction.commit`` and ``transaction.apply``
    leaves behind: the run is committed, but project.json and run.json still hold their BEFORE
    images, so an unrecovered read cannot see it."""
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key(spec)
    derived = names.derived_names(key)
    project_doc = registry.register(
        schema.new_project_doc(
            workstation_id="ws-test",
            repo_identity=resolve.repo_identity(str(git_repo)),
        ),
        spec=spec,
        run_key=key,
        generation=1,
    )
    run_doc = schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=spec,
        workstation_id="ws-test",
        integration_branch=derived.integration_branch,
        gate_dir=derived.gate_dir,
        spec_digest=run_cmd.spec_digest(str(git_repo), spec),
        now="2026-08-10T00:00:00+00:00",
    )
    transaction.prepare(
        state_root,
        "crashed-registration",
        [
            {
                "path": registry.registry_path(state_root),
                "before": None,
                "after": project_doc,
            },
            {
                "path": runstate.run_path(state_root, key),
                "before": None,
                "after": run_doc,
            },
        ],
    )
    transaction.commit(state_root, "crashed-registration")
    return key


def test_a_committed_transaction_is_recovered_before_any_verb_dispatches(
    git_repo, capsys
):
    """main() calls resolve.recover_pending before dispatching. Without it a committed-but-
    unapplied journal makes the run invisible: ``resolve`` would report no active run (exit 3)
    for a run that is already committed, and the journal would still be pending afterwards."""
    key = _committed_but_unapplied_registration(git_repo, "docs/specs/alpha.md")
    state_root = resolve.state_root(str(git_repo))
    assert registry.load(state_root) is None
    assert runstate.load(state_root, key) is None
    assert transaction.pending(state_root) == ["crashed-registration"]

    assert _run(git_repo, "resolve") == 0
    assert capsys.readouterr().out.strip() == key
    assert transaction.pending(state_root) == []


def test_a_crash_between_new_s_commit_and_apply_is_recovered_by_the_next_verb(
    git_repo, capsys, monkeypatch
):
    """The same guarantee end to end: kill ``run new`` after its journal commits and the run is
    still recovered — which is only true because ``new`` journals both writes together."""
    monkeypatch.setattr(
        transaction,
        "apply",
        lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash")),
    )
    with pytest.raises(OSError, match="simulated crash"):
        _run(git_repo, "new", "docs/specs/alpha.md")
    capsys.readouterr()
    state_root = resolve.state_root(str(git_repo))
    key = runkey.run_key("docs/specs/alpha.md")
    # Committed, but neither file carries it yet: the run is invisible until recovery.
    project_doc = registry.load(state_root)
    assert project_doc is not None and registry.run_keys(project_doc) == []
    assert runstate.load(state_root, key) is None
    assert transaction.pending(state_root) == [f"new-{key}"]

    monkeypatch.undo()
    assert _run(git_repo, "resolve") == 0
    assert capsys.readouterr().out.strip() == key
    assert transaction.pending(state_root) == []


def test_a_non_repository_project_fails_without_a_traceback(tmp_path, capsys):
    assert _run(tmp_path, "resolve") == 1
    assert "git failed" in capsys.readouterr().err


# --- bin/conductor ---------------------------------------------------------------------------


def test_the_bin_wrapper_dispatches_the_run_verb(git_repo):
    out = subprocess.run(
        [CONDUCTOR, "run", "new", "docs/specs/alpha.md"],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env={**os.environ, "CONDUCTOR_HOME": str(git_repo)},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == runkey.run_key("docs/specs/alpha.md")


def test_the_bin_wrapper_routes_gate_dir_run_to_the_resolver(git_repo):
    env = {**os.environ, "CONDUCTOR_HOME": str(git_repo)}
    key = subprocess.run(
        [CONDUCTOR, "run", "new", "docs/specs/alpha.md"],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env=env,
        timeout=60,
    ).stdout.strip()
    out = subprocess.run(
        [CONDUCTOR, "gate-dir", "--run", key],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith(f"assertions/{key}")


def test_the_bin_wrapper_keeps_the_legacy_gate_dir_form(git_repo):
    out = subprocess.run(
        [CONDUCTOR, "gate-dir", "docs/specs/alpha.md"],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env={**os.environ, "CONDUCTOR_HOME": str(git_repo)},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("/assertions/alpha")


def test_the_bin_wrapper_usage_lists_the_run_verb(git_repo):
    out = subprocess.run(
        [CONDUCTOR],
        capture_output=True,
        text=True,
        cwd=str(git_repo),
        env={**os.environ, "CONDUCTOR_HOME": str(git_repo)},
        timeout=60,
    )
    assert out.returncode == 64
    assert "conductor run new <spec.md>" in out.stderr
    assert "conductor gate-dir <spec.md> | --run <run-key>" in out.stderr
