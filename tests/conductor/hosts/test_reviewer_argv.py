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
    ctx = str(tmp_path / "ctx")
    argv = base.load("claude").reviewer_argv(
        "review PR 5", project_root=str(tmp_path), context_dir=ctx
    )
    assert argv == [
        str(exe),
        "-p",
        "review PR 5",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--restricted",
        "--strict-mcp-config",
        "--tools=Read,Grep,Glob",
        "--add-dir",
        ctx,
    ]


def test_claude_reviewer_grants_no_shell_or_write_tool(monkeypatch, tmp_path):
    """``--allowedTools`` only ADDS to the allow rules a checkout's own settings grant, and
    ``git diff --output=<file>`` writes; the posture names the whole tool set instead."""
    stub_on_path(monkeypatch, tmp_path, "claude")
    ctx = str(tmp_path / "ctx")
    argv = base.load("claude").reviewer_argv(
        "review PR 5", project_root=str(tmp_path), context_dir=ctx
    )
    assert not any(a.startswith("--allowedTools") for a in argv)
    assert "--dangerously-skip-permissions" not in argv
    assert "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--add-dir") + 1] == ctx
    (tools,) = [a for a in argv if a.startswith("--tools")]
    granted = set(tools.split("=", 1)[1].split(","))
    assert granted == {"Read", "Grep", "Glob"}
    for word in ("Bash", "Write", "Edit", "NotebookEdit"):
        assert not any(word in a for a in argv if a != "review PR 5")


def test_codex_reviewer_is_read_only_sandboxed_json(monkeypatch, tmp_path):
    codex_stub.put_on_path(monkeypatch, tmp_path / "stub-bin")
    argv = base.load("codex").reviewer_argv(
        "review PR 5", project_root=str(tmp_path), context_dir=str(tmp_path / "ctx")
    )
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[-1] == "review PR 5" and "-p" not in argv
    # A read-only sandbox reads outside --cd (ground truth section 5): no extra flag needed.
    assert str(tmp_path / "ctx") not in argv


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_reviewer_argv_refuses_a_flaglike_prompt(host_id, monkeypatch, tmp_path):
    _stub(host_id, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="option"):
        base.load(host_id).reviewer_argv(
            "--help", project_root=str(tmp_path), context_dir=str(tmp_path)
        )


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
