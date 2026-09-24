"""`conductor preflight --host <id>` — the running agent names itself before anything reads the host.

`/conductor:start` used to learn its host at step 6 (`driver install --host`), after preflight
(step 0) and plan-lint (step 4b) had already resolved it. On a fresh project neither found a
recording, so both answered the legacy `claude` default: a Codex start checked Claude's skill
roots and demanded the `codex` reviewer, and stopped on a Codex-only machine. `preflight` also
ignored a `--host` argument outright.

`--host` records the host (``runhost.declare``) and then checks THAT host. It is a no-op on
the recording when the same id is already recorded, and refuses — changing nothing, checking
nothing — when it would move a run that already has a host, or when an ambient
`$CONDUCTOR_HOST` would make every later check answer for a different host.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conductor import preflight, resume_script
from conductor.hosts import runhost


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    monkeypatch.delenv(runhost.HOST_ENV, raising=False)


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    return str(root)


# ---- runhost.declare: the recording policy -------------------------------------------


def test_a_fresh_project_records_the_declared_host(proj):
    path, wrote = runhost.declare(proj, "codex")
    assert wrote and path == runhost.host_file(proj)
    assert runhost.recorded(proj) == "codex"


def test_a_recorded_codex_host_makes_preflight_require_the_claude_reviewer(proj):
    """The downstream consequence the recording exists for: the opposite-host wrapper flips."""
    unrecorded = preflight.required_commands(runhost.resolve(proj))
    assert "codex" in unrecorded  # the legacy `claude` default wants the codex wrapper
    runhost.declare(proj, "codex")
    required = preflight.required_commands(runhost.resolve(proj))
    assert "claude" in required and "codex" not in required


def test_declaring_the_same_host_again_is_idempotent(proj):
    runhost.declare(proj, "codex")
    path = runhost.host_file(proj)
    before = os.stat(path).st_mtime_ns
    assert runhost.declare(proj, "codex") == (path, False)
    assert os.stat(path).st_mtime_ns == before  # no rewrite
    assert runhost.recorded(proj) == "codex"


def test_a_different_recorded_host_is_refused_and_left_alone(proj):
    runhost.record(proj, "claude")
    with pytest.raises(runhost.HostConflict) as exc:
        runhost.declare(proj, "codex")
    msg = str(exc.value)
    assert runhost.recorded(proj) == "claude"
    # Actionable: names both hosts, says nothing changed, and names the deliberate move.
    assert "`claude`" in msg and "`codex`" in msg
    assert "nothing was changed" in msg.lower()
    assert "conductor driver install" in msg and "--host codex" in msg
    assert "/conductor:start" in msg  # resume from the recorded host, in its own form


def test_the_refusal_names_the_start_skill_the_way_the_recorded_host_invokes_it(proj):
    runhost.record(proj, "codex")
    with pytest.raises(runhost.HostConflict) as exc:
        runhost.declare(proj, "claude")
    assert "$conductor:start" in str(exc.value)


def test_a_legacy_run_with_a_driver_and_no_host_file_is_a_claude_run(proj):
    """Every run installed before the host file existed has none, and fires claude. Recording
    `codex` there would repoint a live run; the absence means `claude` exactly as it does to
    `runhost.resolve`."""
    driver = resume_script.driver_script_path(proj)
    os.makedirs(os.path.dirname(driver), exist_ok=True)
    with open(driver, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n")
    with pytest.raises(runhost.HostConflict) as exc:
        runhost.declare(proj, "codex")
    assert runhost.recorded(proj) is None
    assert "`claude`" in str(exc.value)
    assert runhost.declare(proj, "claude")[1] is True
    assert runhost.recorded(proj) == "claude"


def test_an_ambient_override_for_another_host_is_refused(proj, monkeypatch):
    """`$CONDUCTOR_HOST` outranks the recording, so recording `codex` under
    `CONDUCTOR_HOST=claude` would leave every later setup check running as claude."""
    monkeypatch.setenv(runhost.HOST_ENV, "claude")
    with pytest.raises(runhost.HostConflict) as exc:
        runhost.declare(proj, "codex")
    assert runhost.recorded(proj) is None
    assert runhost.HOST_ENV in str(exc.value)


def test_an_ambient_override_that_agrees_is_fine(proj, monkeypatch):
    monkeypatch.setenv(runhost.HOST_ENV, "codex")
    runhost.declare(proj, "codex")
    assert runhost.recorded(proj) == "codex"


def test_an_unsupported_host_is_refused_before_anything_is_written(proj):
    with pytest.raises(runhost.UnknownHost):
        runhost.declare(proj, "gemini")
    assert not os.path.exists(runhost.host_file(proj))


def test_a_garbage_recording_is_reported_not_overwritten(proj):
    path = runhost.host_file(proj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("gemini\n")
    with pytest.raises(runhost.UnknownHost):
        runhost.declare(proj, "codex")
    with open(path, encoding="utf-8") as f:
        assert f.read() == "gemini\n"


# ---- preflight's argument handling ---------------------------------------------------


def test_preflight_refuses_a_conflicting_host_without_checking_anything(
    proj, monkeypatch, capsys
):
    runhost.record(proj, "claude")
    monkeypatch.chdir(proj)

    def _must_not_run(**_):
        raise AssertionError("preflight checked skills after refusing the host")

    monkeypatch.setattr(preflight, "check", _must_not_run)
    assert preflight.main(["--host", "codex"]) == preflight.HOST_REFUSED
    assert "Nothing was changed" in capsys.readouterr().err
    assert runhost.recorded(proj) == "claude"


def test_preflight_checks_the_host_it_was_given(proj, monkeypatch, capsys):
    monkeypatch.chdir(proj)
    seen = {}

    def _check(host_id=None, **_):
        seen["host"] = host_id
        return {"ok": True, "missing": [], "unverified": [], "advice": []}

    monkeypatch.setattr(preflight, "check", _check)
    assert preflight.main(["--host=codex"]) == 0
    assert seen["host"] == "codex"
    assert runhost.recorded(proj) == "codex"
    assert "on host codex" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["--host"], ["--host", "gemini"], ["--host", "codex", "extra"], ["-x"]]
)
def test_preflight_refuses_arguments_it_does_not_understand(proj, monkeypatch, argv):
    """`preflight --host codex` used to run the CLAUDE check and say nothing about it: an
    argument is either understood or refused, never ignored."""
    monkeypatch.chdir(proj)
    assert preflight.main(argv) == 64
    assert not os.path.exists(runhost.host_file(proj))


def test_preflight_help_does_not_print_a_usage_block(proj, monkeypatch, capsys):
    """Frozen A-DH-7 classifies `preflight` as hand-rolled (no `usage:` block on `--help`)."""
    monkeypatch.chdir(proj)
    assert preflight.main(["--help"]) == 64
    out = capsys.readouterr()
    assert not any(
        ln.lower().startswith("usage:") for ln in (out.out + out.err).splitlines()
    )


def test_the_cli_passes_host_through(proj, tmp_path):
    """`bin/conductor preflight --host <id>` reaches the recording, and a conflicting id comes
    back as exit 3 with nothing changed. Claude is the host exercised here because its
    discovery reads only files (pointed at an empty config dir); Codex's asks the `codex` CLI,
    which this suite never runs for real."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CONDUCTOR_HOST", "CONDUCTOR_HOME", "CONDUCTOR_PLUGIN_DIRS")
    }
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "empty-claude-home")

    def _run(host):
        return subprocess.run(
            [os.path.join(root, "bin", "conductor"), "preflight", "--host", host],
            cwd=proj,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    first = _run("claude")
    assert first.returncode in (0, 1), (
        first.stderr
    )  # the check ran; its verdict is not ours
    assert "preflight: host claude (recorded in" in first.stdout
    assert runhost.recorded(proj) == "claude"
    refused = _run("codex")
    assert refused.returncode == preflight.HOST_REFUSED, refused.stderr
    assert runhost.recorded(proj) == "claude"
