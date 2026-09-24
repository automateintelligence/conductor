"""Suite-wide guard: no test runs this machine's real `codex`.

A real `codex` makes a test's result depend on what is installed here: its version, its
`$CODEX_HOME`, its plugins, and whether its startup plugin sync reaches the network. Every test
that needs Codex stubs it on `PATH` (see `tests/conductor/codex_stub.py`). This fixture puts a
`codex` that refuses ahead of everything else on the inherited `PATH`, so a test that forgot to
stub reaches the refusal instead of the real binary, and the test FAILS naming what it ran.

Real-Codex coverage lives in `tests/conductor/hosts/test_codex_live.py`, opted into with
`CONDUCTOR_LIVE_CODEX=1`; this guard stands aside only there, via the `live_codex` marker.
"""

from __future__ import annotations

import os
import shlex

import pytest

_REFUSING_CODEX = """#!/bin/sh
printf '%s\\n' "$*" >> {marker}
echo "refused: this test ran \\`codex $*\\` without stubbing it (tests/conftest.py)" >&2
exit 97
"""


@pytest.fixture(autouse=True)
def _no_real_codex(request, tmp_path_factory, monkeypatch):
    if request.node.get_closest_marker("live_codex"):
        yield
        return
    guard = tmp_path_factory.mktemp("codex-guard")
    marker = guard / "reached"
    exe = guard / "codex"
    exe.write_text(_REFUSING_CODEX.format(marker=shlex.quote(str(marker))))
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ.get('PATH', '')}")
    yield
    if marker.exists():
        pytest.fail(
            "this test ran `codex` without stubbing it; a real Codex would have answered. "
            f"Invocations: {marker.read_text().strip().splitlines()}",
            pytrace=False,
        )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_codex: runs the real `codex`; opt-in via CONDUCTOR_LIVE_CODEX=1",
    )
