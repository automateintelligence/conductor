"""Atomic end-of-phase bookkeeping.

The dogfood run proved prompt-listed clerical steps decay to zero (0/27 plan checkboxes, labels
never maintained, handoff never written across three phases), so phase completion is ONE command
in the load-bearing path instead of four steps a model will drop. Fail-closed: nothing is touched
unless the phase's ``conductor-assertions`` marker resolves against ``results.json`` all-green
(or the caller explicitly passes ``no_gate_check``).
"""

from __future__ import annotations

import re
from typing import Any

from ledger import claim, gate_link, sync

_UNTICKED = re.compile(r"(?m)^(\s*)- \[ \]")
_CHECKLIST_REF = re.compile(r"- \[ \] #(\d+)")


def phase_done(
    repo: str,
    n: int,
    *,
    gh: Any,
    results: dict[str, Any] | None = None,
    plan_path: str | None = None,
    no_gate_check: bool = False,
) -> dict[str, Any]:
    body = gh.get_body(repo, n) or ""
    if not no_gate_check:
        tokens = gate_link.read_assertion_tokens(body)
        if not tokens:
            return {"ok": False, "error": "no-assertion-marker", "issue": n}
        if results is None:
            return {"ok": False, "error": "no-results", "issue": n}
        state = gate_link.tests_red_from_results(tokens, results)
        if state["unresolved"]:
            return {
                "ok": False,
                "error": "unresolved-assertions",
                "unresolved": state["unresolved"],
                "issue": n,
            }
        if state["ambiguous"]:
            return {
                "ok": False,
                "error": "ambiguous-assertions",
                "ambiguous": state["ambiguous"],
                "issue": n,
            }
        if state["red"]:
            return {
                "ok": False,
                "error": "assertions-red",
                "red_ids": state["red_ids"],
                "issue": n,
            }

    # Mutation order (codex #3): sub-issues, lease, assignees FIRST; the done label is the
    # LAST mutation before close, so a gh failure mid-way can never leave the ledger falsely
    # advertising done. Any gh error returns a failure dict — the command is re-runnable.
    st = gh.issue_state(repo, n)
    sub_issues_closed: list[int] = []
    checklist_ticked = 0
    try:
        try:
            children = gh.list_sub_issues(repo, n)
        except RuntimeError:  # ONLY the list call falls back (API unavailable);
            children = None  # a failing child close below is a real gh error
        if children is None:
            current = gh.get_body(repo, n) or ""
            new_body, checklist_ticked = _CHECKLIST_REF.subn(r"- [x] #\1", current)
            if checklist_ticked:
                gh.set_body(repo, n, new_body)
        else:
            for child in children:
                gh.close_issue(repo, child["number"])
                sub_issues_closed.append(child["number"])

        for worker in list(st["assignees"]):
            gh.unassign(repo, n, worker)
        current = gh.get_body(repo, n) or ""
        stripped = claim.strip_markers(current)
        if stripped != current:
            gh.set_body(repo, n, stripped)

        gh.set_labels(
            repo,
            n,
            add=["status:done"],
            remove=[lbl for lbl in st["labels"] if lbl.startswith("status:")],
        )
        gh.close_issue(repo, n)
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": f"gh-error: {exc}",
            "issue": n,
            "sub_issues_closed": sub_issues_closed,
        }

    result: dict[str, Any] = {
        "ok": True,
        "issue": n,
        "sub_issues_closed": sub_issues_closed,
        "checklist_ticked": checklist_ticked,
    }
    if plan_path:
        result["plan"] = _tick_plan_section(plan_path, gh.issue_title(repo, n) or "")
    return result


def _tick_plan_section(plan_path: str, issue_title: str) -> dict[str, Any]:
    """Tick every ``- [ ]`` in the plan section whose phase heading equals the issue title
    (titles match because ``convert``/``generate`` create issues from those headings).
    Best-effort by design: a missing section is reported, never fatal — the ledger, not the
    plan prose, is the durable state."""
    try:
        with open(plan_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return {"error": f"plan-unreadable: {exc}"}
    headings = list(sync._H2_ANY.finditer(text))
    for i, m in enumerate(headings):
        parsed = sync._phase_heading(m.group(1))
        if parsed is None or parsed[0] != issue_title:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section, count = _UNTICKED.subn(r"\1- [x]", text[m.end() : end])
        if count:
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(text[: m.end()] + section + text[end:])
        return {"ticked": count}
    return {"error": "section-not-found"}
