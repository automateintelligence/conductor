#!/usr/bin/env python3
"""E3 reconcile -- enforce the design's §7 ground-truth precedence on a phase issue.

§7 precedence (highest source wins; the lower source is repaired to match):

    git commits + assertion/test results   >   PR state   >   issue status-label

E3 scope: the decisive ground truth is the assertion/test result, produced by
running ``assert_phaseA.sh`` (exit 0 = green, non-zero = red). The simulated
"merged PR" is encoded as the issue being closed + carrying ``status:done``
(the two lowest sources). When the issue label/state disagrees with the test
result, the TEST wins and the issue is repaired.

Handled per §7 "Invalid combinations -> repair":

    status:done / closed  BUT  tests red   ->  reopen  ->  status:in-progress
    status:done / closed  AND  tests green ->  permitted (precedence satisfied)

Exit codes (reconcile contract):
    0   state already consistent with ground truth (incl. done+green permitted)
    10  invalid combo detected and REPAIRED
    1   error / could not reconcile
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = "automateintelligence/conductor"
DONE = "status:done"
IN_PROGRESS = "status:in-progress"


def sh(cmd: list[str]) -> str:
    """Run a command, raise on failure, return stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(cmd)}")
    return r.stdout


def read_issue(num: int) -> tuple[str, list[str]]:
    """Read the issue's ground state via gh api: (state, [label names])."""
    data = json.loads(sh(["gh", "api", f"repos/{REPO}/issues/{num}"]))
    return data["state"], [lbl["name"] for lbl in data.get("labels", [])]


def run_assertion(path: str) -> int:
    """Run the assertion gate; return its exit code (0 green / non-zero red)."""
    return subprocess.run(["bash", path]).returncode


def main() -> int:
    num = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ["E3_PHASE_A_ISSUE"])
    assert_path = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assert_phaseA.sh"
        )
    )

    state, labels = read_issue(num)
    rc = run_assertion(assert_path)
    tests_green = rc == 0

    print(f"[reconcile] issue #{num}: state={state!r} labels={labels}")
    print(
        f"[reconcile] assertion {os.path.basename(assert_path)} exit={rc} "
        f"-> tests {'GREEN' if tests_green else 'RED'}"
    )
    print("[reconcile] §7 precedence: git/tests > PR > issue-label")

    done_or_closed = (DONE in labels) or (state == "closed")

    # --- Invalid combo: done/closed but tests red. Tests outrank label -> repair.
    if done_or_closed and not tests_green:
        print(
            "[reconcile] CONFLICT: issue=done/closed but tests=RED "
            "-> tests outrank issue-label (§7); the label/state is wrong."
        )
        print(f"[reconcile] REPAIR (§7): reopen -> {IN_PROGRESS} (remove {DONE}).")
        if state == "closed":
            sh(["gh", "issue", "reopen", str(num), "-R", REPO])
        sh(
            [
                "gh",
                "issue",
                "edit",
                str(num),
                "-R",
                REPO,
                "--add-label",
                IN_PROGRESS,
                "--remove-label",
                DONE,
            ]
        )
        nstate, nlabels = read_issue(num)
        print(f"[reconcile] AFTER repair: state={nstate!r} labels={nlabels}")
        print(
            "[reconcile] RESULT: REPAIRED invalid combo (done+red -> in-progress/open)."
        )
        return 10

    # --- Consistent: done/closed and tests green. Precedence satisfied -> permit.
    if done_or_closed and tests_green:
        print(
            "[reconcile] CONSISTENT: issue=done/closed AND tests=GREEN "
            "-> §7 precedence satisfied; status:done + closed PERMITTED."
        )
        print("[reconcile] RESULT: no repair needed (done+green allowed).")
        return 0

    # --- Not in a terminal (done/closed) state; nothing in scope to reconcile.
    print("[reconcile] issue not in done/closed terminal state; nothing to reconcile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
