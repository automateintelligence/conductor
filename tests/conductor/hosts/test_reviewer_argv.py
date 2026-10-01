"""Reviewer launch vectors: read-only, machine-readable, each built in its own adapter.

Postures verified against real hosts on 2026-09-30 (docs/reviews/2026-09-30-host-usage-ground-truth.md
section 5).
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

from conductor.hosts import base
from tests.conductor import codex_stub


def stub_on_path(monkeypatch, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    bindir = tmp_path / f"{name}-bin"
    bindir.mkdir()
    exe = bindir / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return exe


def _stub(host_id, monkeypatch, tmp_path):
    if host_id == "codex":
        codex_stub.put_on_path(monkeypatch, tmp_path / "stub-bin")
    else:
        stub_on_path(monkeypatch, tmp_path, host_id)


def test_claude_reviewer_is_read_only_and_json(monkeypatch, tmp_path):
    exe = stub_on_path(monkeypatch, tmp_path, "claude")
    argv = base.load("claude").reviewer_argv("review PR 5", project_root=str(tmp_path))
    assert argv[0] == str(exe)
    assert argv[1:3] == ["-p", "review PR 5"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    allowed = [a for a in argv if a.startswith("--allowedTools=")]
    assert len(allowed) == 1 and "Write" not in allowed[0] and "Edit" not in allowed[0]
    assert allowed[0] == (
        "--allowedTools=Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
    )
    assert "--dangerously-skip-permissions" not in argv


def test_codex_reviewer_is_read_only_sandboxed_json(monkeypatch, tmp_path):
    codex_stub.put_on_path(monkeypatch, tmp_path / "stub-bin")
    argv = base.load("codex").reviewer_argv("review PR 5", project_root=str(tmp_path))
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[-1] == "review PR 5" and "-p" not in argv


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_reviewer_argv_refuses_a_flaglike_prompt(host_id, monkeypatch, tmp_path):
    _stub(host_id, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="option"):
        base.load(host_id).reviewer_argv("--help", project_root=str(tmp_path))


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_executable_resolves_from_path(host_id, monkeypatch, tmp_path):
    _stub(host_id, monkeypatch, tmp_path)
    exe = base.load(host_id).executable()
    assert os.path.basename(exe) == host_id and os.access(exe, os.X_OK)


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_executable_off_path_is_host_unavailable(host_id, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(base.HostUnavailable, match="resume-env.sh") as exc:
        base.load(host_id).executable()
    assert f"`{host_id}` is not on PATH" in str(exc.value)
