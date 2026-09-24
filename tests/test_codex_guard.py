"""The suite-wide guard in `tests/conftest.py` fails a test that runs `codex` unstubbed, and
stands aside for a stub and for the opt-in live marker."""

from __future__ import annotations

import pathlib

import pytest

pytest_plugins = ["pytester"]

CONFTEST = pathlib.Path(__file__).resolve().parent / "conftest.py"


def test_the_guard_fails_a_test_that_runs_codex_unstubbed(pytester):
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import os, pathlib, subprocess

        def test_forgot_to_stub():
            subprocess.run(["codex", "plugin", "list", "--json"], capture_output=True)

        def test_stubbed(tmp_path, monkeypatch):
            stub = tmp_path / "codex"
            stub.write_text("#!/bin/sh\\nexit 0\\n")
            stub.chmod(0o755)
            monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
            assert subprocess.run(["codex"]).returncode == 0
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        ["*ran `codex` without stubbing it*plugin list --json*"]
    )


# Marked itself so the OUTER guard's directory is not on the PATH the inner run inherits;
# otherwise the check below would find the outer one. It runs no `codex`.
@pytest.mark.live_codex
def test_the_guard_stands_aside_for_the_live_marker(pytester):
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import os, pytest

        @pytest.mark.live_codex
        def test_live():
            guard = [d for d in os.environ["PATH"].split(os.pathsep) if "codex-guard" in d]
            assert guard == []
        """
    )
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)
