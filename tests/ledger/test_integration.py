"""
Full-stack integration test for the conductor ledger against the real GitHub repo.

Gate: set RUN_GH_INTEGRATION=1 to run. When unset the test is SKIPPED — the normal
quality gate (ruff/pyright/pytest) stays green without any GitHub access.

What this exercises (lifecycle order):
  1. sync.generate  → milestone + 2 phases + 3 sub-issues (Phase A)
  2. Assert hierarchy (sub-issue count or checklist fallback)
  3. claim.claim    → sole-owner confirm + lease body marker + labels
  4. Simulate done+merged (label + close)
  5. reconcile      → done+tests-red reopen repair
  6. reconcile      → done+tests-green permitted (no repair)
  7. Stale-lease:   renew_lease with old ts → reconcile detects stale → reclaim+ready+unassign
  8. Teardown (finally): close all created issues + DELETE milestone
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest

from ledger import claim as _claim
from ledger import gh
from ledger import reconcile as _rec
from ledger import sync as _sync


@pytest.mark.skipif(
    not os.environ.get("RUN_GH_INTEGRATION"),
    reason="set RUN_GH_INTEGRATION=1 for live gh test",
)
def test_full_ledger_lifecycle() -> None:
    repo = subprocess.check_output(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        text=True,
    ).strip()
    worker = subprocess.check_output(
        ["gh", "api", "user", "-q", ".login"],
        text=True,
    ).strip()
    runid = uuid.uuid4().hex[:8]
    now_ts = int(time.time())

    plan = {
        "title": f"IT-{runid}",
        "phases": [
            {
                "title": f"IT-{runid} Phase A",
                "status": "ready",
                "tasks": ["t1", "t2", "t3"],
            },
            {
                "title": f"IT-{runid} Phase B",
                "status": "draft",
                "tasks": [],
            },
        ],
    }

    try:
        # --- Step 2: generate hierarchy ---
        result = _sync.generate(repo, plan, gh)
        phase_a_info = result["phases"][0]
        phase_b_info = result["phases"][1]
        phase_a: int = phase_a_info["number"]
        phase_b: int = phase_b_info["number"]
        sub_issues_a: list[int] = phase_a_info["sub_issues"]

        # --- Step 3: Assert hierarchy ---
        assert len(sub_issues_a) == 3, f"Expected 3 task issues, got {sub_issues_a}"

        if not phase_a_info["fallback"]:
            subs = gh._gh_api("GET", f"repos/{repo}/issues/{phase_a}/sub_issues")
            assert isinstance(subs, list) and len(subs) == 3, (
                f"Expected 3 sub-issues via API, got: {subs}"
            )
        else:
            body = gh.get_body(repo, phase_a)
            count = body.count("- [ ] #")
            assert count == 3, f"Fallback checklist: expected 3 items, got {count}"

        state_a = gh.issue_state(repo, phase_a)
        state_b = gh.issue_state(repo, phase_b)
        assert "status:ready" in state_a["labels"], (
            f"Phase A missing status:ready — labels: {state_a['labels']}"
        )
        assert "status:draft" in state_b["labels"], (
            f"Phase B missing status:draft — labels: {state_b['labels']}"
        )

        # --- Step 4: Claim Phase A ---
        claimed = _claim.claim(repo, phase_a, worker, now_ts=now_ts, ttl=900, gh=gh)
        assert claimed, (
            "claim() returned False — issue not eligible or another worker won the race"
        )

        state_a = gh.issue_state(repo, phase_a)
        assert worker in state_a["assignees"], (
            f"Worker '{worker}' not in assignees after claim: {state_a['assignees']}"
        )
        assert "status:in-progress" in state_a["labels"], (
            f"Missing status:in-progress after claim: {state_a['labels']}"
        )

        lease = _claim.read_lease(repo, phase_a, gh)
        assert lease is not None, "conductor-lease marker not found in issue body"
        assert lease["worker"] == worker, (
            f"Lease worker mismatch: expected {worker!r}, got {lease['worker']!r}"
        )
        assert lease["ts"] == now_ts, (
            f"Lease ts mismatch: expected {now_ts}, got {lease['ts']}"
        )

        # --- Step 5: Simulate done + merged ---
        gh.set_labels(repo, phase_a, add=["status:done"], remove=["status:in-progress"])
        gh.close_issue(repo, phase_a)

        # --- Step 6: Reconcile — done + tests red → reopen-tests-red ---
        r6 = _rec.reconcile(
            repo,
            phase_a,
            tests_red=True,
            pr_merged=True,
            commits_since_baseline=1,
            retries=0,
            R=3,
            gh=gh,
            now_ts=now_ts + 1,
            L=900,
        )
        assert r6["action"] == "reopen-tests-red", (
            f"Step 6: expected reopen-tests-red, got {r6}"
        )
        assert r6["new_status"] == "status:in-progress", (
            f"Step 6: expected status:in-progress, got {r6['new_status']}"
        )
        reopened = gh.issue_state(repo, phase_a)
        assert reopened["state"] == "open", (
            f"Step 6: issue should be open after reopen-tests-red, got {reopened['state']}"
        )

        # --- Step 7: Reconcile — done + tests green → permitted (no repair) ---
        # Reset to done/closed so we exercise the permitted path
        gh.set_labels(repo, phase_a, add=["status:done"], remove=["status:in-progress"])
        gh.close_issue(repo, phase_a)

        r7 = _rec.reconcile(
            repo,
            phase_a,
            tests_red=False,
            pr_merged=True,
            commits_since_baseline=1,
            retries=0,
            R=3,
            gh=gh,
            now_ts=now_ts + 2,
            L=900,
        )
        assert r7["action"] == "none", (
            f"Step 7: done+tests-green+pr-merged should be permitted (none), got {r7}"
        )

        # --- Step 8: Stale-lease reclaim ---
        # Put the issue back to in-progress with an artificially old lease
        gh.reopen_issue(repo, phase_a)
        gh.set_labels(repo, phase_a, add=["status:in-progress"], remove=["status:done"])
        gh.assign(repo, phase_a, worker)

        stale_ts = now_ts - 100000  # well beyond any L
        _claim.renew_lease(repo, phase_a, worker, now_ts=stale_ts, gh=gh)

        r8 = _rec.reconcile(
            repo,
            phase_a,
            tests_red=True,
            pr_merged=False,
            commits_since_baseline=1,
            retries=0,
            R=3,
            gh=gh,
            now_ts=now_ts + 3,
            L=900,
        )
        assert r8["action"] == "stale-lease-reclaim", (
            f"Step 8: expected stale-lease-reclaim, got {r8}"
        )
        assert r8["new_status"] == "status:ready", (
            f"Step 8: expected status:ready, got {r8['new_status']}"
        )

        state_final = gh.issue_state(repo, phase_a)
        # NOTE: unassign sends DELETE with JSON body via --input -.
        # If gh does NOT remove the assignee (gh may drop the body on DELETE),
        # this assertion will fail — stop and report it; do NOT weaken the check.
        assert state_final["assignees"] == [], (
            f"KNOWN RISK: unassign did not clear assignees after stale-lease-reclaim. "
            f"Remaining assignees: {state_final['assignees']}. "
            "This may indicate that gh cli drops the JSON body on DELETE /assignees. "
            "Report to controller for fallback decision — do not weaken this assertion."
        )

    finally:
        # Authoritative teardown: search GitHub for any milestone/issues tagged
        # with this run's unique runid — robust even if generate() raised mid-way
        # (orphan-safe).  Does NOT rely on the return value of generate().
        try:
            milestones = gh._gh_api(
                "GET", f"repos/{repo}/milestones?state=all&per_page=100"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[teardown] WARNING: could not list milestones: {exc}")
            milestones = []
        for ms in milestones or []:
            if runid not in ms.get("title", ""):
                continue
            ms_number = ms["number"]
            ms_title = ms["title"]
            try:
                issues = gh._gh_api(
                    "GET",
                    f"repos/{repo}/issues?milestone={ms_number}&state=all&per_page=100",
                )
                for issue in issues or []:
                    try:
                        gh.close_issue(repo, issue["number"])
                        print(f"[teardown] closed issue #{issue['number']}")
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[teardown] WARNING: could not close issue"
                            f" #{issue['number']}: {exc}"
                        )
                gh._gh_api("DELETE", f"repos/{repo}/milestones/{ms_number}")
                print(f"[teardown] deleted milestone #{ms_number} ({ms_title!r})")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[teardown] WARNING: failed to clean milestone"
                    f" #{ms_number} ({ms_title!r}): {exc}"
                )
