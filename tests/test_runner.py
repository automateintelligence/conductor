import os
import subprocess
import sys
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py")]


def _manifest(tmp_path, body):
    (tmp_path / "manifest.yaml").write_text(body)


def _env(tmp_path, **extra):
    # Hermetic isolation from the ambient repo: CONDUCTOR_HOME puts run state
    # (results.json etc.) in tmp, and a NONEXISTENT freeze baseline turns the guard off
    # (run.py's documented opt-in semantics) so the repo's live assertions/.frozen never
    # fail-closes a fabricated tmp manifest as tampered. Both stay overridable via
    # **extra — the freeze-guard tests below pass a real baseline to exercise tamper.
    return {
        **os.environ,
        "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"),
        "CONDUCTOR_HOME": str(tmp_path),
        "CONDUCTOR_FREEZE_BASELINE": str(tmp_path / "no-baseline"),
        **extra,
    }


def test_teardown_runs_after_command(tmp_path):
    marker = tmp_path / "torn_down"
    _manifest(
        tmp_path,
        textwrap.dedent(
            f"""\
        assertions:
          - id: t1
            command: "true"
            teardown: "touch {marker}"
            timeout: 10
            level: spec
    """
        ),
    )
    subprocess.run(RUN, env=_env(tmp_path), cwd=ROOT)
    assert marker.exists()


def test_isolation_clean_cwd_per_assertion(tmp_path):
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a
            command: "touch leaked"
            level: spec
          - id: b
            command: "test ! -e leaked"
            level: spec
    """
        ),
    )
    assert (
        subprocess.run(
            RUN, env=_env(tmp_path, CONDUCTOR_ISOLATE="1"), cwd=ROOT
        ).returncode
        == 0
    )


def test_level_filter_runs_only_spec(tmp_path):
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: spec1
            command: "true"
            level: spec
          - id: phase1
            command: "false"
            level: phase
    """
        ),
    )
    assert (
        subprocess.run(
            RUN + ["--level", "spec"], env=_env(tmp_path), cwd=ROOT
        ).returncode
        == 0
    )


def test_empty_level_filter_is_fail_closed(tmp_path):  # Codex #1
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: only-phase
            command: "true"
            level: phase
    """
        ),
    )
    # spec-done gate with ZERO spec-level assertions must NOT be green-by-default
    assert (
        subprocess.run(
            RUN + ["--level", "spec"], env=_env(tmp_path), cwd=ROOT
        ).returncode
        == 5
    )


def test_overall_timeout_enforced_exit_4(tmp_path):
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: slow
            command: "sleep 5"
            timeout: 10
            level: spec
    """
        ),
    )
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    assert p.returncode == 4 and (time.monotonic() - start) < 3


def test_overall_budget_not_rounded_up(tmp_path):  # Codex #2 (sub-second)
    # a1 eats most of the 1s budget; a2 must be cut by the <1s remainder, not a false pass
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a1
            command: "sleep 0.7"
            timeout: 30
            level: spec
          - id: a2
            command: "sleep 0.5"
            timeout: 30
            level: spec
    """
        ),
    )
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    assert (
        p.returncode == 4 and (time.monotonic() - start) < 2.0
    )  # not a 1.2s false pass


def test_invalid_level_is_fail_closed(tmp_path):
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a
            command: "true"
            level: spec
    """
        ),
    )
    p = subprocess.run(RUN + ["--level", "bogus"], env=_env(tmp_path), cwd=ROOT)
    assert p.returncode == 5  # invalid args -> fail-closed, NOT exit 2


def test_bin_conductor_wrapper_passes_through(tmp_path):
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: ok
            command: "true"
            level: spec
    """
        ),
    )
    conductor = os.path.join(ROOT, "bin", "conductor")
    p = subprocess.run(
        [conductor, "assert", "run", "--level", "spec"], env=_env(tmp_path), cwd=ROOT
    )
    assert p.returncode == 0


def test_unknown_flag_is_fail_closed(tmp_path):  # Codex (fail-open args)
    # A typoed/unsupported flag must NOT be silently dropped and pass the gate.
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a
            command: "true"
            level: spec
    """
        ),
    )
    p = subprocess.run(RUN + ["--levl", "phase"], env=_env(tmp_path), cwd=ROOT)
    assert p.returncode == 5  # unknown flag -> fail-closed, not green-by-default


def test_bin_conductor_requires_run_subcommand(tmp_path):  # Codex (fail-open args)
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a
            command: "true"
            level: spec
    """
        ),
    )
    conductor = os.path.join(ROOT, "bin", "conductor")
    # bare `assert` and `assert <junk>` must be usage errors, not a silent green gate
    assert (
        subprocess.run([conductor, "assert"], env=_env(tmp_path), cwd=ROOT).returncode
        == 64
    )
    assert (
        subprocess.run(
            [conductor, "assert", "bogus"], env=_env(tmp_path), cwd=ROOT
        ).returncode
        == 64
    )


def test_teardown_counts_toward_overall_budget(tmp_path):  # Codex (teardown wall-clock)
    # teardown is wall-clock too: a teardown that runs past the overall budget
    # must (a) be capped, and (b) make the gate report exit 4, not a false DONE.
    _manifest(
        tmp_path,
        textwrap.dedent(
            """\
        assertions:
          - id: a
            command: "true"
            teardown: "sleep 3"
            timeout: 30
            level: spec
    """
        ),
    )
    start = time.monotonic()
    p = subprocess.run(RUN, env=_env(tmp_path, CONDUCTOR_OVERALL_TIMEOUT="1"), cwd=ROOT)
    elapsed = time.monotonic() - start
    assert p.returncode == 4  # wall-clock overrun (incl. teardown) -> NOT done
    assert elapsed < 2.0  # teardown capped by remaining budget, not the full 3s


def _frozen_manifest(tmp_path):
    """A gate whose command references an ABSOLUTE test path (resolves regardless of the
    runner's REPO_ROOT), frozen with conductor.freeze. Returns (body, baseline_path)."""
    from conductor import freeze

    gate_test = tmp_path / "gate_test.py"
    gate_test.write_text("def test_gate():\n    assert True\n")
    body = textwrap.dedent(
        f"""\
        assertions:
          - id: g
            command: "python3 -m pytest -q {gate_test}"
            level: spec
    """
    )
    _manifest(tmp_path, body)
    baseline = tmp_path / ".frozen"
    freeze.record(str(tmp_path / "manifest.yaml"), str(baseline), str(tmp_path))
    return body, str(baseline)


def test_freeze_guard_blocks_tampered_done_gate(tmp_path):  # gate-integrity (§5)
    _, baseline = _frozen_manifest(tmp_path)
    # worker weakens the gate: same id, real check swapped for `true`
    _manifest(
        tmp_path, 'assertions:\n  - id: g\n    command: "true"\n    level: spec\n'
    )
    p = subprocess.run(
        RUN, env=_env(tmp_path, CONDUCTOR_FREEZE_BASELINE=baseline), cwd=ROOT
    )
    assert p.returncode == 6  # tampered done-gate -> fail-closed, NOT done


def test_freeze_guard_allows_intact_done_gate(tmp_path):  # gate-integrity (§5)
    _, baseline = _frozen_manifest(tmp_path)
    p = subprocess.run(
        RUN, env=_env(tmp_path, CONDUCTOR_FREEZE_BASELINE=baseline), cwd=ROOT
    )
    assert p.returncode == 0  # intact baseline -> runs normally, gate green


def test_piped_failure_is_red(tmp_path):  # review: shell pipefail
    # a failing command in a pipe must make the gate RED, not be masked by the last stage.
    _manifest(
        tmp_path,
        'assertions:\n  - id: p\n    command: "false | cat"\n    level: spec\n',
    )
    p = subprocess.run(RUN, env=_env(tmp_path), cwd=ROOT)
    assert p.returncode == 1  # piped failure -> red, not a false green


def test_non_numeric_timeout_is_exit_3(tmp_path):  # review: manifest hygiene
    _manifest(
        tmp_path,
        'assertions:\n  - id: a\n    command: "true"\n    timeout: "soon"\n    level: spec\n',
    )
    p = subprocess.run(RUN, env=_env(tmp_path), cwd=ROOT)
    assert p.returncode == 3  # unparseable timeout -> manifest invalid, not a crash
