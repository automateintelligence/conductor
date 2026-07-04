"""Brownfield ledger alignment — the core of ``/conductor:prepare``.

An existing repo's phase issues usually carry PARAPHRASED titles (the first dogfood's did),
which breaks ``generate``'s exact-title idempotency and every downstream title match. Titles
lie; assertion-id SETS don't — each phase's set is unique — so align matches existing issues
to plan phases by token set (the ``conductor-assertions`` body marker first, the heading
tokens as fallback, case-insensitive) and renames issues + the milestone to the canonical
plan headings. After an applied align, ``convert`` reuses everything and fills in what's
missing. Fail-closed: an ambiguous match (two issues, one phase) renames NOTHING for that
phase; a milestone spanning ambiguity skips the milestone rename. Dry-run by default —
``apply=True`` is the only mutating path.
"""

from __future__ import annotations

from typing import Any

from ledger import gate_link, sync


def _tokens(issue: dict[str, Any]) -> frozenset[str]:
    """Case-normalized assertion-token set for an existing issue: body marker wins
    (machine-written), heading-style title tokens as fallback."""
    tokens = gate_link.read_assertion_tokens(issue.get("body"))
    if not tokens:
        tokens = sync._assertion_tokens(issue.get("title") or "")
    return frozenset(t.upper() for t in tokens)


def align(
    repo: str, plan: dict[str, Any], gh: Any, apply: bool = False
) -> dict[str, Any]:
    phase_sets = {
        p["title"]: frozenset(t.upper() for t in p["assertions"])
        for p in plan["phases"]
    }
    # Two plan phases with the SAME token set would both match one issue and silently
    # double-rename it (codex PR-31 #1) — pre-filter them into ambiguity instead.
    set_counts: dict[frozenset[str], int] = {}
    for token_set in phase_sets.values():
        if token_set:
            set_counts[token_set] = set_counts.get(token_set, 0) + 1
    duplicated_sets = {s for s, n in set_counts.items() if n > 1}

    issues: list[dict[str, Any]] = []
    for milestone in gh.list_milestones(repo):
        for issue in gh.list_milestone_issues(repo, milestone["number"]):
            issue["milestone"] = milestone
            issues.append(issue)

    matches: list[dict[str, Any]] = []
    ambiguous_phases: dict[str, list[int]] = {}
    matched_issue_numbers: set[int] = set()
    ambiguous_issue_numbers: set[int] = set()
    matched_milestones: dict[int, str] = {}
    unmatched_phases: list[str] = []

    for phase_title, wanted in phase_sets.items():
        if not wanted:  # gateless phase (gate: none) — nothing to match on
            unmatched_phases.append(phase_title)
            continue
        found = [i for i in issues if _tokens(i) == wanted]
        if wanted in duplicated_sets:  # plan-side ambiguity: never guess an assignment
            ambiguous_phases[phase_title] = sorted(i["number"] for i in found)
            ambiguous_issue_numbers.update(i["number"] for i in found)
            continue
        if not found:
            unmatched_phases.append(phase_title)
            continue
        if len(found) > 1:  # broken mapping must surface, never guess (fail-closed)
            ambiguous_phases[phase_title] = sorted(i["number"] for i in found)
            ambiguous_issue_numbers.update(i["number"] for i in found)
            continue
        issue = found[0]
        matched_issue_numbers.add(issue["number"])
        matched_milestones[issue["milestone"]["number"]] = issue["milestone"]["title"]
        matches.append(
            {
                "issue": issue["number"],
                "from": issue["title"],
                "to": phase_title,
                "rename": issue["title"] != phase_title,
            }
        )

    unmatched_issues = sorted(
        i["number"]
        for i in issues
        # ambiguity participants are neither matched nor stray (codex r2): listing them
        # as unmatched would mislead follow-on automation
        if i["number"] not in matched_issue_numbers
        and i["number"] not in ambiguous_issue_numbers
        and _tokens(i)
    )

    milestone_report: Any = None
    if len(matched_milestones) == 1:
        number, title = next(iter(matched_milestones.items()))
        milestone_report = {
            "number": number,
            "from": title,
            "to": plan["title"],
            "rename": title != plan["title"],
        }
    elif len(matched_milestones) > 1:
        milestone_report = "ambiguous"

    if apply:
        for match in matches:
            if match["rename"]:
                gh.update_issue_title(repo, match["issue"], match["to"])
        if isinstance(milestone_report, dict) and milestone_report["rename"]:
            gh.update_milestone_title(
                repo, milestone_report["number"], milestone_report["to"]
            )

    return {
        "applied": apply,
        "matches": matches,
        "milestone": milestone_report,
        "unmatched_phases": unmatched_phases,
        "unmatched_issues": unmatched_issues,
        "ambiguous_phases": ambiguous_phases,
    }
