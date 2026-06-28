"""Isolated deterministic E2E tests — Codex #2.

Three tests, fully offline (no Stage-0 state, no repo mutation):
  1. Gate-driven RED→GREEN convergence (tmp_path + CONDUCTOR_MANIFEST + PYTHONPATH).
  2. Reconcile-first step-skip idempotency (pure temp probe, no scripts).
  3. §10 stale-lease reclaim (mocked gh, no network).
"""

import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

from ledger import reconcile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = [sys.executable, os.path.join(ROOT, "assertions", "run.py"), "--level", "spec"]


def test_gate_driven_convergence_red_to_green(tmp_path):
    """autodev's core loop: gate RED -> implement one unit -> gate GREEN. Fully isolated."""
    (tmp_path / "test_feat.py").write_text(
        textwrap.dedent("""\
            from feat import ship
            def test_ship(): assert ship() == "SHIPPED"
        """)
    )
    (tmp_path / "manifest.yaml").write_text(
        textwrap.dedent(
            f"""\
            assertions:
              - id: ship
                claim: "ship() returns SHIPPED"
                command: "python3 -m pytest -q {tmp_path / "test_feat.py"}"
                level: spec
                kind: example
            """
        )
    )
    env = {
        **os.environ,
        "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"),
        "PYTHONPATH": str(tmp_path),
    }
    assert subprocess.run(RUN, env=env, cwd=ROOT).returncode == 1  # RED
    (tmp_path / "feat.py").write_text('def ship():\n    return "SHIPPED"\n')
    assert (
        subprocess.run(RUN, env=env, cwd=ROOT).returncode == 0
    )  # GREEN -> would self-stop


def test_setup_step_is_idempotent(tmp_path):
    """The reconcile-first step-skip pattern: a step does its work once, then probes-and-skips.

    Pure + isolated (temp probe; no Stage-0 script, no repo mutation) — Codex #2.
    """
    probe = tmp_path / "goal.md"
    runs: list[int] = []

    def setup_step() -> str:
        if probe.exists():
            return "already done"
        probe.write_text("goal")
        runs.append(1)
        return "doing"

    assert setup_step() == "doing"
    assert setup_step() == "already done"
    assert len(runs) == 1


def test_refire_reclaims_stale_phase():
    """§10: an in-progress phase with a stale lease (dead worker) is reclaimed, not double-worked."""
    g = MagicMock()
    g.issue_state.return_value = {
        "state": "open",
        "labels": ["status:in-progress"],
        "assignees": ["dead"],
        "id": 1,
    }
    g.get_body.return_value = "<!-- conductor-lease worker=dead ts=100 -->"
    out = reconcile.reconcile(
        "o/r",
        1,
        tests_red=True,
        pr_merged=False,
        commits_since_baseline=0,
        retries=0,
        R=3,
        gh=g,
        now_ts=100 + 5000,
        L=900,
    )
    assert out["action"] == "stale-lease-reclaim"
    assert out["new_status"] == "status:ready"
    g.unassign.assert_called_once_with("o/r", 1, "dead")
