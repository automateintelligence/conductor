"""``conductor usage ingest`` — records one worker fire's token usage from the driver log slice.

Real git repository and a real run record (via ``run_cmd``), never mocks: the verb resolves its
run from git plumbing and writes through ``runstate``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conductor import run_cmd, usage_cmd
from conductor.core import (
    names,
    registry,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLAUDE_FIXTURE_TEXT = (
    (FIXTURES / "claude-print-json-2.1.286.json").read_text(encoding="utf-8").strip()
)
CLAUDE_SUBAGENT_FIXTURE_TEXT = (
    (FIXTURES / "claude-print-json-subagent-2.1.286.json")
    .read_text(encoding="utf-8")
    .strip()
)
SUBAGENT_CACHE_READ_SUM = sum(
    m["cacheReadInputTokens"]
    for m in json.loads(CLAUDE_SUBAGENT_FIXTURE_TEXT)["modelUsage"].values()
)

DEFAULT_BRANCH = "trunk"


class Proj:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_key = ""

    @property
    def state_root(self) -> str:
        return os.path.join(str(self.root), ".conductor")

    @property
    def run(self) -> dict:
        doc = runstate.load(self.state_root, self.run_key)
        assert doc is not None
        return doc


def _init_repo(root: Path, git_env) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", DEFAULT_BRANCH, str(root)],
        check=True,
        capture_output=True,
        env=git_env,
        timeout=30,
    )


@pytest.fixture
def proj(tmp_path, git_env, git, monkeypatch, capsys):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "alpha.md").write_text("# alpha\n")
    _init_repo(root, git_env)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    monkeypatch.setenv("CONDUCTOR_HOME", str(root))
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    for name in ("CONDUCTOR_GATE_DIR", "CONDUCTOR_GATE_SLUG", "CONDUCTOR_HOST"):
        monkeypatch.delenv(name, raising=False)
    p = Proj(root)
    assert run_cmd.main(["new", "docs/alpha.md", "--project", str(root)]) == 0
    p.run_key = capsys.readouterr().out.strip()
    (root / ".conductor").mkdir(exist_ok=True)
    return p


def _ingest(
    proj: Proj,
    log: Path,
    *,
    host: str = "claude",
    offset: int = 0,
    wall_s: str = "40",
    rc: str = "0",
) -> int:
    return usage_cmd.main(
        [
            "ingest",
            "--project",
            str(proj.root),
            "--host",
            host,
            "--log",
            str(log),
            "--offset",
            str(offset),
            "--wall-s",
            wall_s,
            "--rc",
            rc,
        ]
    )


def test_ingest_reads_only_bytes_after_the_offset(proj, capsys):
    old = CLAUDE_FIXTURE_TEXT.replace('"alpha"', '"previous fire"')
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text("2026 fire-start posture=supervised\n" + old + "\n")
    offset = log.stat().st_size
    with log.open("a") as f:
        f.write("SessionEnd hook failed\n" + CLAUDE_SUBAGENT_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log, offset=offset) == 0
    d = proj.run["dispatches"][-1]
    assert d["role"] == "worker" and d["outcome"] == "ok"
    assert d["host"] == "claude"
    assert d["cached_input_tokens"] == SUBAGENT_CACHE_READ_SUM
    out = capsys.readouterr().out
    assert "usage-recorded role=worker phase=- " in out
    assert f"input={d['input_tokens']} output={d['output_tokens']}" in out


def test_ingest_records_nulls_when_the_log_slice_has_no_usage(proj):
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text("plain text worker output\n")
    assert _ingest(proj, log, wall_s="5", rc="137") == 0
    d = proj.run["dispatches"][-1]
    assert d["input_tokens"] is None and d["outcome"] == "timeout"
    assert d["note"] == "no-usage-in-output"


@pytest.mark.parametrize(
    ("rc", "outcome"),
    [("0", "ok"), ("1", "error"), ("124", "timeout"), ("137", "timeout")],
)
def test_ingest_maps_exit_status_to_outcome(proj, rc, outcome):
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log, rc=rc) == 0
    d = proj.run["dispatches"][-1]
    assert d["outcome"] == outcome
    assert d["note"] is None


def test_ingest_offset_past_eof_reads_nothing(proj):
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log, offset=log.stat().st_size + 1000) == 0
    d = proj.run["dispatches"][-1]
    assert d["input_tokens"] is None and d["note"] == "no-usage-in-output"


def test_ingest_decodes_invalid_utf8_with_replacement(proj):
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_bytes(b"\xff\xfe garbage\n" + CLAUDE_FIXTURE_TEXT.encode() + b"\n")
    assert _ingest(proj, log) == 0
    assert proj.run["dispatches"][-1]["input_tokens"] is not None


def test_ingest_takes_phase_and_head_from_a_fresh_handoff_only(proj):
    handoff = proj.root / ".conductor" / "handoff.md"
    handoff.write_text(
        "**Active:** plan=p; milestone=#1; phase issue #42 (in-progress)\n"
        "**Last unit:** aaa111..bbb222 — did things\n"
    )
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log, wall_s="60") == 0
    d = proj.run["dispatches"][-1]
    assert d["phase_id"] == "42" and d["head_sha"] == "bbb222"

    os.utime(handoff, (0, 0))
    assert _ingest(proj, log, wall_s="60") == 0
    d = proj.run["dispatches"][-1]
    assert d["phase_id"] is None and d["head_sha"] is None


def test_ingest_from_a_linked_worktree_reads_its_handoff_and_finds_the_run(proj, git):
    """The driver passes the run WORKTREE: the worker's handoff lives there, and the run is
    still found through the git common dir the worktree shares with the main checkout."""
    wt = proj.root.parent / "wt"
    git(proj.root, "worktree", "add", "-q", "-b", "run", str(wt))
    (wt / ".conductor").mkdir()
    (wt / ".conductor" / "handoff.md").write_text(
        "**Active:** plan=p; milestone=#1; phase issue #42 (in-progress)\n"
        "**Last unit:** aaa111..bbb222 — did things\n"
    )
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    rc = usage_cmd.main(
        ["ingest", "--project", str(wt), "--host", "claude", "--log", str(log)]
        + ["--offset", "0", "--wall-s", "60", "--rc", "0"]
    )
    assert rc == 0
    d = proj.run["dispatches"][-1]
    assert d["phase_id"] == "42" and d["head_sha"] == "bbb222"
    assert not (
        wt / ".conductor" / "runs"
    ).exists()  # recorded on the main checkout's run


def test_ingest_without_a_run_reports_unrecorded_and_exits_1(
    tmp_path, git_env, monkeypatch, capsys
):
    root = tmp_path / "bare"
    root.mkdir()
    _init_repo(root, git_env)
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    log = tmp_path / "x.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    rc = usage_cmd.main(
        ["ingest", "--project", str(root), "--host", "claude", "--log", str(log),
         "--offset", "0", "--wall-s", "5", "--rc", "0"]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage-unrecorded reason=" in out
    assert "fire-end" not in out and "driver-unresolved" not in out


def test_ingest_with_an_unreadable_log_reports_unrecorded_and_exits_1(proj, capsys):
    assert _ingest(proj, proj.root / ".conductor" / "missing.log") == 1
    out = capsys.readouterr().out
    assert "usage-unrecorded reason=" in out
    assert proj.run["dispatches"] == []


def test_ingest_with_two_active_runs_reports_unrecorded_and_exits_1(proj, git, capsys):
    (proj.root / "docs" / "beta.md").write_text("# beta\n")
    git(proj.root, "add", "-A")
    git(proj.root, "commit", "-qm", "beta")
    assert run_cmd.main(["new", "docs/beta.md", "--project", str(proj.root)]) == 0
    capsys.readouterr()
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log) == 1
    out = capsys.readouterr().out
    assert "usage-unrecorded reason=" in out
    assert "fire-end" not in out and "driver-unresolved" not in out


def _run_worktree(proj: Proj, git) -> Path:
    """A linked worktree with the run's integration branch checked out, as the driver fires in."""
    wt = proj.root.parent / "wt-run"
    branch = proj.run["integration_branch"]
    git(proj.root, "worktree", "add", "-q", "-b", branch, str(wt))
    return wt


def _ingest_from(worktree: Path, log: Path) -> int:
    return usage_cmd.main(
        ["ingest", "--project", str(worktree), "--host", "claude", "--log", str(log)]
        + ["--offset", "0", "--wall-s", "60", "--rc", "0"]
    )


def test_ingest_with_two_active_runs_records_on_the_run_its_worktree_belongs_to(
    proj, git, capsys
):
    """Two active runs in one repository (per-spec gate namespacing): a bare ``resolve`` is
    ambiguous, so the fire is attributed through the worktree the driver fired in."""
    (proj.root / "docs" / "beta.md").write_text("# beta\n")
    git(proj.root, "add", "-A")
    git(proj.root, "commit", "-qm", "beta")
    assert run_cmd.main(["new", "docs/beta.md", "--project", str(proj.root)]) == 0
    beta_key = capsys.readouterr().out.strip()
    wt = _run_worktree(proj, git)
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest_from(wt, log) == 0
    assert "usage-recorded" in capsys.readouterr().out
    assert len(proj.run["dispatches"]) == 1
    beta = runstate.load(proj.state_root, beta_key)
    assert beta is not None and beta["dispatches"] == []


def test_ingest_records_a_run_its_own_fire_moved_to_awaiting_team_merge(
    proj, git, capsys
):
    """The fire that opened the final PR moved its run out of the active set before the driver
    ingested it; the worktree still binds the fire to that run."""
    wt = _run_worktree(proj, git)
    runstate.set_status(proj.state_root, proj.run_key, "awaiting-team-merge")
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest_from(wt, log) == 0
    assert "usage-recorded" in capsys.readouterr().out
    assert len(proj.run["dispatches"]) == 1


def test_ingest_collapses_a_three_dot_range_to_its_head(proj):
    (proj.root / ".conductor" / "handoff.md").write_text(
        "**Active:** phase issue #7 (in-progress)\n"
        "**Last unit:** aaa111...ccc333 — did things\n"
    )
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log, wall_s="60") == 0
    d = proj.run["dispatches"][-1]
    assert d["phase_id"] == "7" and d["head_sha"] == "ccc333"


def test_unrecorded_reason_names_the_error_on_one_line_without_driver_markers(
    proj, monkeypatch, capsys
):
    def boom(*_a, **_k):
        raise RuntimeError("bad\nfire-end and driver-unresolved " + "x" * 500)

    monkeypatch.setattr(usage_cmd.dispatches, "append", boom)
    log = proj.root / ".conductor" / "resume-autodev.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    assert _ingest(proj, log) == 1
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert (
        "usage-unrecorded reason=RuntimeError: bad fire_end and driver_unresolved "
        in out
    )
    assert "fire-end" not in out and "driver-unresolved" not in out
    reason = out.split("reason=", 1)[1].strip()
    assert len(reason) <= usage_cmd._REASON_MAX


def test_ingest_recovers_a_committed_journal_before_resolving_the_run(
    tmp_path, git_env, git, monkeypatch, capsys
):
    """A crash between ``transaction.commit`` and ``transaction.apply`` leaves the run invisible
    to ``resolve``. Ingest is a mutating entry point, so it must recover first and then record
    against the run that journal registered."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "alpha.md").write_text("# alpha\n")
    _init_repo(root, git_env)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "conductor-config"))
    for name in ("CONDUCTOR_HOME", "CONDUCTOR_GATE_DIR", "CONDUCTOR_GATE_SLUG"):
        monkeypatch.delenv(name, raising=False)

    state_root = resolve.state_root(str(root))
    spec = "docs/alpha.md"
    key = runkey.run_key(spec)
    derived = names.derived_names(key)
    project_doc = registry.register(
        schema.new_project_doc(
            workstation_id="ws-test",
            repo_identity=resolve.repo_identity(str(root)),
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
        spec_digest=run_cmd.spec_digest(str(root), spec),
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
    assert runstate.load(state_root, key) is None
    assert transaction.pending(state_root) == ["crashed-registration"]

    log = root / "worker.log"
    log.write_text(CLAUDE_FIXTURE_TEXT + "\n")
    p = Proj(root)
    p.run_key = key
    assert _ingest(p, log) == 0
    assert "usage-recorded" in capsys.readouterr().out
    assert transaction.pending(state_root) == []
    assert len(p.run["dispatches"]) == 1
