"""Two specs conducted in one repository (design §"Unit and contract tests").

Two simultaneous run keys must resolve distinct goals, gates, manifests, baselines and results —
and must keep doing so while conflicting legacy files and ambient gate environment variables are
present, because that is exactly the state a half-migrated repository is in.

Every distinctness assertion here is paired with a POSITIVE one. "the two gates differ" alone
would still hold if the resolver failed closed on both and collapsed them onto two different
wrong paths, so each run is also asserted to land on the gate its own key names.
"""

from __future__ import annotations

import json
import os

import pytest

from conductor import paths, run_cmd
from conductor.core import registry, resolve, runkey, runstate, transaction

# The git_env / git / git_repo fixtures come from tests/conductor/conftest.py (Task 9), which
# covers this directory. Do not add `pytest_plugins` — pytest only honours it in the rootdir
# conftest, and a nested reference is deprecated.

ALPHA = "docs/specs/alpha.md"
BETA = "docs/specs/beta.md"
GAMMA = "docs/specs/gamma.md"

GATE_ENV = (
    "CONDUCTOR_GATE_DIR",
    "CONDUCTOR_GATE_SLUG",
    "CONDUCTOR_MANIFEST",
    "CONDUCTOR_FREEZE_BASELINE",
)


@pytest.fixture(autouse=True)
def isolated_conductor_env(tmp_path, monkeypatch):
    """Keep the installation ID out of the developer's real config directory, clear the ambient
    gate overrides so each test starts from a neutral environment, and point the ambient project
    at a directory that is NOT a git repository — so a bug that drops ``--project`` fails loudly
    instead of writing run state into whatever repository the suite runs from."""
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    ambient = tmp_path / "not-a-repo"
    ambient.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(ambient))
    for name in GATE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def two_runs(git_repo, capsys):
    """Two runs for two specs in one repository, created through the CLI."""
    assert run_cmd.main(["new", ALPHA, "--project", str(git_repo)]) == 0
    alpha = capsys.readouterr().out.strip()
    assert run_cmd.main(["new", BETA, "--project", str(git_repo)]) == 0
    beta = capsys.readouterr().out.strip()
    assert alpha and beta
    return str(git_repo), resolve.state_root(str(git_repo)), alpha, beta


def _own_gate_dir(root: str, run_key: str) -> str:
    """The gate directory ``run_key`` names. Realpath because ``resolve.repo_root`` reads git's
    ``--path-format=absolute --git-common-dir``, which git reports resolved."""
    return os.path.join(os.path.realpath(root), "assertions", run_key)


def test_the_two_runs_have_distinct_keys_state_dirs_and_locks(two_runs):
    """Catches per-run state collapsing onto one slot: a runstate that ignored the run key would
    give both runs the same run.json and the same locks, so a write by one would clobber the
    other and the two would serialize against each other's state lock."""
    _root, state_root, alpha, beta = two_runs
    assert alpha != beta
    assert alpha == runkey.run_key(ALPHA) and beta == runkey.run_key(BETA)
    for key in (alpha, beta):
        own = os.path.join(state_root, "runs", key)
        assert runstate.run_dir(state_root, key) == own
        assert runstate.run_path(state_root, key) == os.path.join(own, "run.json")
        assert runstate.state_lock_path(state_root, key) == os.path.join(
            own, "state.lock"
        )
        assert runstate.owner_lock_path(state_root, key) == os.path.join(
            own, "owner.lock"
        )
    assert runstate.run_dir(state_root, alpha) != runstate.run_dir(state_root, beta)
    assert runstate.state_lock_path(state_root, alpha) != runstate.state_lock_path(
        state_root, beta
    )
    assert runstate.owner_lock_path(state_root, alpha) != runstate.owner_lock_path(
        state_root, beta
    )


def test_each_run_resolves_its_own_spec_branch_and_gate(two_runs):
    """Catches a resolver that hands a key another run's record, and a gate resolution not keyed
    on the run key. Each of goal (spec_path), branch, gate directory, manifest, baseline and
    results dir is asserted to be the one THIS key names, then asserted to differ from the
    other's — distinctness alone would pass if both fell closed onto different wrong paths."""
    root, _state_root, alpha, beta = two_runs
    first = resolve.resolve(run_key=alpha, start=root)
    second = resolve.resolve(run_key=beta, start=root)
    assert first.run["spec_path"] == ALPHA and second.run["spec_path"] == BETA
    assert first.run["integration_branch"] == f"conductor/run-{alpha}"
    assert second.run["integration_branch"] == f"conductor/run-{beta}"
    gate_a, gate_b = resolve.gate_for_run(first), resolve.gate_for_run(second)
    for gate, key in ((gate_a, alpha), (gate_b, beta)):
        own = _own_gate_dir(root, key)
        assert gate.source == "run_key"
        assert gate.slug == key
        assert gate.fail_closed is None
        assert gate.directory == own
        assert gate.manifest == os.path.join(own, "manifest.yaml")
        assert gate.baseline == os.path.join(own, ".frozen")
        assert gate.run_dir == os.path.join(own, "run")
    assert gate_a.directory != gate_b.directory
    assert gate_a.manifest != gate_b.manifest
    assert gate_a.baseline != gate_b.baseline
    assert gate_a.run_dir != gate_b.run_dir


def test_conflicting_legacy_files_and_environment_do_not_leak_into_either_run(
    two_runs, monkeypatch, capsys
):
    """The design's central claim: a run key carrying invocation ignores legacy
    ``.conductor/run_branch``, legacy ``.conductor/goal.md`` and the four ``CONDUCTOR_GATE_*``
    variables rather than consulting them as fallback.

    THREE CONTROLS make the hostility real, one per group of hostile input, and between them they
    cover all six. Without them this test would still pass if a legacy file were never written or
    an environment variable name were typo'd — the failure mode that makes an isolation test
    unfalsifiable. Only once each input is shown to steer the LEGACY resolver is "both runs still
    land on their own gate" evidence of anything.

    Control A covers ``.conductor/run_branch`` AND ``.conductor/goal.md``: the two disagree, which
    is the §5 signature the legacy resolver refuses on, and the refusal names both slugs — so a
    missing or unparsed file changes the verdict. Control B covers ``CONDUCTOR_GATE_DIR``,
    ``CONDUCTOR_MANIFEST`` and ``CONDUCTOR_FREEZE_BASELINE``, each planted at a value the default
    for that directory would never produce. Control C covers ``CONDUCTOR_GATE_SLUG``, which
    ``CONDUCTOR_GATE_DIR`` shadows by precedence (``conductor/paths.py:307-314``), so the control
    lifts the shadowing variable for the length of one call rather than pretending it is
    observable underneath it."""
    root, _state_root, alpha, beta = two_runs
    legacy = os.path.join(root, ".conductor")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "run_branch"), "w", encoding="utf-8") as fh:
        fh.write("conductor/run-hijacked\n")
    with open(os.path.join(legacy, "goal.md"), "w", encoding="utf-8") as fh:
        fh.write("docs/specs/gamma.md\n")
    hijacked = os.path.join(root, "assertions", "hijacked")
    os.makedirs(hijacked, exist_ok=True)
    # Built and frozen, so the legacy resolver selects this gate rather than falling back to flat,
    # and so it reaches the run_branch-vs-goal.md comparison instead of stopping at "unfrozen".
    with open(os.path.join(hijacked, "manifest.yaml"), "w", encoding="utf-8") as fh:
        fh.write("assertions: []\n")
    with open(os.path.join(hijacked, ".frozen"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")

    control_a = paths.resolve_gate(root)  # CONTROL A: no run key, no environment
    assert control_a.source == "run_branch"
    assert control_a.directory == hijacked
    # Both legacy files are live: run_branch chose the directory, and goal.md disagreeing with it
    # is what makes the legacy resolver refuse. The message names each file's slug.
    assert control_a.fail_closed is not None
    assert "hijacked" in control_a.fail_closed
    assert paths.spec_slug(GAMMA) in control_a.fail_closed

    hijacked_manifest = os.path.join(hijacked, "hijacked-manifest.yaml")
    hijacked_baseline = os.path.join(hijacked, "hijacked.frozen")
    monkeypatch.setenv("CONDUCTOR_GATE_SLUG", "hijacked-slug")
    monkeypatch.setenv("CONDUCTOR_GATE_DIR", hijacked)
    monkeypatch.setenv("CONDUCTOR_MANIFEST", hijacked_manifest)
    monkeypatch.setenv("CONDUCTOR_FREEZE_BASELINE", hijacked_baseline)

    control_b = paths.resolve_gate(root)  # CONTROL B: no run key, hostile environment
    assert control_b.source == "gate_dir_env"
    assert control_b.directory == hijacked
    # Both planted at values the directory's own defaults would never produce, so equality here
    # cannot be satisfied by the variable being unset.
    assert (
        control_b.manifest
        == hijacked_manifest
        != os.path.join(hijacked, "manifest.yaml")
    )
    assert control_b.baseline == hijacked_baseline != os.path.join(hijacked, ".frozen")

    with (
        pytest.MonkeyPatch.context() as unshadow
    ):  # CONTROL C: the slug, minus its shadow
        unshadow.delenv("CONDUCTOR_GATE_DIR")
        control_c = paths.resolve_gate(root)
    assert control_c.source == "explicit_slug"
    assert control_c.directory == os.path.join(root, "assertions", "hijacked-slug")

    for key, spec in ((alpha, ALPHA), (beta, BETA)):
        resolution = resolve.resolve(run_key=key, start=root)
        # The goal comes from the run record, not from the legacy goal.md naming gamma.
        assert resolution.run["spec_path"] == spec
        assert resolution.run["integration_branch"] == f"conductor/run-{key}"
        gate = resolve.gate_for_run(resolution)
        own = _own_gate_dir(root, key)
        assert gate.source == "run_key"
        assert gate.fail_closed is None
        assert gate.directory == own
        assert gate.manifest == os.path.join(own, "manifest.yaml")
        assert gate.baseline == os.path.join(own, ".frozen")
        assert gate.run_dir == os.path.join(own, "run")
        assert "hijacked" not in gate.directory
        assert "hijacked" not in gate.manifest
        assert "hijacked" not in gate.baseline
        assert "hijacked" not in gate.run_dir
        # The same claim through the operator-facing verb, not only the library.
        assert run_cmd.main(["gate-dir", "--run", key, "--project", root]) == 0
        assert capsys.readouterr().out.strip() == own


def test_a_bare_command_refuses_while_both_runs_are_active(two_runs, capsys):
    """Catches guessing. Exit 2 (ambiguous) and exit 3 (no such run / none active) are different
    operator situations with different remedies, so the code is asserted exactly rather than as
    "nonzero", and the message must name every active key in a command that can be pasted."""
    root, _state_root, alpha, beta = two_runs
    code = run_cmd.main(["resolve", "--project", root])
    assert code == run_cmd.EXIT_AMBIGUOUS == 2
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # it must not pick one and print it
    err = captured.err
    for key in (alpha, beta):
        assert f"conductor run show --run {key}" in err
    # The exit-3 advice ("start one") would be wrong here: two runs already exist.
    assert "conductor run new" not in err


def test_finishing_one_run_lets_the_other_resolve_bare(two_runs, capsys):
    """Also proves the disambiguation reads run.json, not project.json's status MIRROR: the
    mirror is deliberately left saying both runs are current (nothing in the product writes it
    when a run ends), so a resolver consulting it would still see two active runs and exit 2."""
    root, state_root, alpha, beta = two_runs
    runstate.set_status(state_root, beta, "awaiting-team-merge")
    assert resolve.active_run_keys(state_root) == [alpha]
    assert run_cmd.main(["resolve", "--project", root]) == 0
    assert capsys.readouterr().out.strip() == alpha

    runstate.set_status(state_root, beta, "terminal")
    project_doc = registry.load(state_root)
    assert project_doc is not None
    assert registry.current_run_key(project_doc, BETA) == beta  # the stale mirror
    assert run_cmd.main(["resolve", "--project", root]) == 0
    assert capsys.readouterr().out.strip() == alpha


def test_a_state_write_to_one_run_does_not_touch_the_other(two_runs):
    """Catches a shared run record or a shared revision counter: writing alpha's phase must leave
    beta's document byte-identical, revision included."""
    _root, state_root, alpha, beta = two_runs
    before = runstate.load(state_root, beta)
    assert before is not None
    runstate.update(state_root, alpha, lambda d: {**d, "current_phase": "phase-1"})
    assert runstate.load(state_root, beta) == before
    written = runstate.load(state_root, alpha)
    assert written is not None
    assert written["current_phase"] == "phase-1"
    assert written["revision"] == before["revision"] + 1


def test_repointing_one_run_leaves_the_other_mapping_and_record_intact(
    two_runs, git, git_repo
):
    """``repoint`` moves EVERY generation sharing the repointed spec path, and takes their locks
    in sorted run-key order. A version that walked every run in the registry instead of the one
    mapping would rewrite beta's spec_path, bump its revision and append to its path_history —
    so beta's record is compared whole, not just its spec_path."""
    root, state_root, alpha, beta = two_runs
    before = runstate.load(state_root, beta)
    assert before is not None
    project_before = registry.load(state_root)
    assert project_before is not None
    beta_mapping_before = registry.mapping(project_before, BETA)
    # Not None, or the comparison below would pass vacuously on a dropped mapping.
    assert beta_mapping_before is not None

    os.makedirs(str(git_repo / "docs" / "specs" / "archive"), exist_ok=True)
    git(git_repo, "mv", ALPHA, "docs/specs/archive/alpha.md")
    assert (
        run_cmd.main(
            [
                "repoint-spec",
                "--run",
                alpha,
                "docs/specs/archive/alpha.md",
                "--project",
                root,
            ]
        )
        == 0
    )

    doc = registry.load(state_root)
    assert doc is not None
    assert registry.current_run_key(doc, "docs/specs/archive/alpha.md") == alpha
    assert registry.mapping(doc, ALPHA) is None
    assert registry.current_run_key(doc, BETA) == beta
    assert registry.mapping(doc, BETA) == beta_mapping_before
    assert runstate.load(state_root, beta) == before
    moved = runstate.load(state_root, alpha)
    assert moved is not None
    assert moved["spec_path"] == "docs/specs/archive/alpha.md"
    assert moved["path_history"] == [ALPHA]


def test_the_registry_lists_both_runs_with_their_own_generations(two_runs, capsys):
    """Catches a listing that reads one run's record for every key, or drops a row."""
    root, _state_root, alpha, beta = two_runs
    assert run_cmd.main(["list", "--json", "--project", root]) == 0
    rows = {row["run_key"]: row for row in json.loads(capsys.readouterr().out)}
    assert set(rows) == {alpha, beta}
    assert rows[alpha]["spec_path"] == ALPHA and rows[beta]["spec_path"] == BETA
    assert rows[alpha]["generation"] == 1 and rows[beta]["generation"] == 1
    assert rows[alpha]["integration_branch"] == f"conductor/run-{alpha}"
    assert rows[beta]["integration_branch"] == f"conductor/run-{beta}"


def test_generation_two_of_one_spec_coexists_with_the_other_run(two_runs, capsys):
    """``cmd_new`` reconciles the status mirror inside its own transaction, so no test-side
    ``registry.mirror_status`` is needed — and leaving it out is what makes this discriminating:
    the reconcile must fold ONLY the repointed spec's own generations. Folding across mappings
    would rewrite beta's entry while minting alpha's generation 2."""
    root, state_root, alpha, beta = two_runs
    project_before = registry.load(state_root)
    assert project_before is not None
    beta_mapping_before = registry.mapping(project_before, BETA)
    assert beta_mapping_before is not None  # else the comparison below is vacuous
    beta_record_before = runstate.load(state_root, beta)
    assert beta_record_before is not None

    runstate.set_status(state_root, alpha, "awaiting-team-merge")
    runstate.set_status(state_root, alpha, "terminal")
    assert run_cmd.main(["new", ALPHA, "--new-run", "--project", root]) == 0
    second = capsys.readouterr().out.strip()
    assert second == f"{runkey.run_key(ALPHA)}-g2" == f"{alpha}-g2"

    assert sorted(resolve.active_run_keys(state_root)) == sorted([beta, second])
    doc = registry.load(state_root)
    assert doc is not None
    assert registry.mapping(doc, BETA) == beta_mapping_before
    assert runstate.load(state_root, beta) == beta_record_before

    gate_first = resolve.gate_for_run(resolve.resolve(run_key=alpha, start=root))
    gate_second = resolve.gate_for_run(resolve.resolve(run_key=second, start=root))
    gate_beta = resolve.gate_for_run(resolve.resolve(run_key=beta, start=root))
    assert gate_first.directory == _own_gate_dir(root, alpha)
    assert gate_second.directory == _own_gate_dir(root, second)
    assert gate_beta.directory == _own_gate_dir(root, beta)
    assert len({gate_first.directory, gate_second.directory, gate_beta.directory}) == 3


def test_a_crash_mid_registration_is_recovered_without_disturbing_the_other_runs(
    two_runs, git_repo, capsys
):
    """A third run crashes between ``transaction.commit`` and ``transaction.apply``: project.json
    and run.json still hold their BEFORE images, so the committed run is invisible. The next verb
    must roll it forward — and roll ONLY it forward. An after-image built from a document read
    before the lock, or a recovery that rewrote the registry from the new run alone, would drop
    alpha and beta on the way through."""
    root, state_root, alpha, beta = two_runs
    (git_repo / "docs" / "specs" / "gamma.md").write_text("# gamma\n")
    alpha_before = runstate.load(state_root, alpha)
    beta_before = runstate.load(state_root, beta)
    assert alpha_before is not None and beta_before is not None
    gamma = runkey.run_key(GAMMA)

    # A SCOPED patch, never `monkeypatch.undo()`: the autouse isolated_conductor_env fixture and
    # this test share one MonkeyPatch instance, so undo() would also restore CONDUCTOR_CONFIG_HOME,
    # CONDUCTOR_HOME and the four CONDUCTOR_GATE_* variables — and the next `run new` would write
    # the installation ID into the developer's real ~/.config.
    with pytest.MonkeyPatch.context() as crashed:
        crashed.setattr(
            transaction,
            "apply",
            lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash")),
        )
        # The failed write REFUSES rather than escaping: exit 1 plus the transaction id (which
        # embeds gamma's run key), the write status and the retry. A traceback here would name
        # none of the three, and this assertion used to be `pytest.raises(OSError)`.
        assert run_cmd.main(["new", GAMMA, "--project", root]) == 1
        err = capsys.readouterr().err
        assert "simulated crash" in err
        assert f"new-{gamma}" in err
        assert "COMMITTED" in err
        assert f"conductor run new {GAMMA}" in err
        assert "Traceback" not in err

        project_doc = registry.load(state_root)
        assert project_doc is not None
        assert registry.run_keys(project_doc) == sorted(
            [alpha, beta]
        )  # gamma invisible
        assert runstate.load(state_root, gamma) is None
        assert transaction.pending(state_root) == [f"new-{gamma}"]

    assert os.environ[
        "CONDUCTOR_CONFIG_HOME"
    ]  # the fixture's isolation is still in force
    assert run_cmd.main(["list", "--all", "--json", "--project", root]) == 0
    rows = {row["run_key"]: row for row in json.loads(capsys.readouterr().out)}
    assert set(rows) == {alpha, beta, gamma}
    assert rows[gamma]["spec_path"] == GAMMA
    assert transaction.pending(state_root) == []
    assert runstate.load(state_root, alpha) == alpha_before
    assert runstate.load(state_root, beta) == beta_before
    recovered = registry.load(state_root)
    assert recovered is not None
    assert registry.current_run_key(recovered, ALPHA) == alpha
    assert registry.current_run_key(recovered, BETA) == beta
    assert registry.current_run_key(recovered, GAMMA) == gamma
