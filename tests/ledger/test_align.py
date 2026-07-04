from unittest.mock import MagicMock

from ledger import align, sync

PLAN_MD = """\
# Widget Harness — Implementation Plan

## Phase 1 — Scoring (A3, A4)

- [ ] one

## Phase 2 — Reporting (A8)

- [ ] two

## Phase 3 — Glue — OPTIONAL

gate: none

- [ ] three
"""


def _plan():
    return sync.parse_plan_md(PLAN_MD)


def _gh(milestones, issues_by_milestone):
    g = MagicMock()
    g.list_milestones.return_value = milestones
    g.list_milestone_issues.side_effect = lambda r, m: issues_by_milestone.get(m, [])
    return g


def test_match_by_marker_and_plan_rename(monkeypatch):
    g = _gh(
        [{"number": 1, "title": "Widget Harness"}],
        {
            1: [
                {
                    "number": 10,
                    "title": "Phase 1 - scoring (a3/a4)",  # paraphrased
                    "body": "<!-- conductor-assertions: A3,A4 -->",
                },
                {
                    "number": 11,
                    "title": "Phase 2 - reporting (A8)",
                    "body": "<!-- conductor-assertions: A8 -->",
                },
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert report["applied"] is False
    matches = {m["issue"]: m for m in report["matches"]}
    assert matches[10]["to"] == "Phase 1 — Scoring (A3, A4)" and matches[10]["rename"]
    assert matches[11]["to"] == "Phase 2 — Reporting (A8)" and matches[11]["rename"]
    assert report["milestone"] == {
        "number": 1,
        "from": "Widget Harness",
        "to": "Widget Harness — Implementation Plan",
        "rename": True,
    }
    # dry run mutates NOTHING
    g.update_issue_title.assert_not_called()
    g.update_milestone_title.assert_not_called()


def test_title_token_fallback_when_no_marker():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {1: [{"number": 10, "title": "scoring work (A3/A4)", "body": ""}]},
    )
    report = align.align("o/r", _plan(), g)
    assert [m["issue"] for m in report["matches"]] == [10]


def test_token_match_is_case_insensitive():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {1: [{"number": 10, "title": "x (a3, a4)", "body": ""}]},
    )
    report = align.align("o/r", _plan(), g)
    assert [m["issue"] for m in report["matches"]] == [10]


def test_apply_executes_renames():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {
                    "number": 10,
                    "title": "old title",
                    "body": "<!-- conductor-assertions: A3,A4 -->",
                }
            ]
        },
    )
    report = align.align("o/r", _plan(), g, apply=True)
    assert report["applied"] is True
    g.update_issue_title.assert_called_once_with(
        "o/r", 10, "Phase 1 — Scoring (A3, A4)"
    )
    g.update_milestone_title.assert_called_once_with(
        "o/r", 1, "Widget Harness — Implementation Plan"
    )


def test_already_canonical_needs_no_renames():
    g = _gh(
        [{"number": 1, "title": "Widget Harness — Implementation Plan"}],
        {
            1: [
                {
                    "number": 10,
                    "title": "Phase 1 — Scoring (A3, A4)",
                    "body": "<!-- conductor-assertions: A3,A4 -->",
                }
            ]
        },
    )
    report = align.align("o/r", _plan(), g, apply=True)
    assert all(not m["rename"] for m in report["matches"])
    assert report["milestone"]["rename"] is False
    g.update_issue_title.assert_not_called()
    g.update_milestone_title.assert_not_called()


def test_ambiguous_duplicate_match_fails_closed():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "a (A3/A4)", "body": ""},
                {"number": 12, "title": "b (A4, A3)", "body": ""},  # same token SET
            ]
        },
    )
    report = align.align("o/r", _plan(), g, apply=True)
    assert report["ambiguous_phases"] == {"Phase 1 — Scoring (A3, A4)": [10, 12]}
    g.update_issue_title.assert_not_called()  # nothing renamed for ambiguous phases


def test_unmatched_phase_and_issue_reported():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "x (A3, A4)", "body": ""},
                {"number": 99, "title": "y (A19)", "body": ""},  # no A19 phase in plan
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert "Phase 2 — Reporting (A8)" in report["unmatched_phases"]
    assert "Phase 3 — Glue — OPTIONAL" in report["unmatched_phases"]  # gateless
    assert report["unmatched_issues"] == [99]


def test_milestone_ambiguous_when_matches_span_two():
    g = _gh(
        [{"number": 1, "title": "W1"}, {"number": 2, "title": "W2"}],
        {
            1: [{"number": 10, "title": "x (A3, A4)", "body": ""}],
            2: [{"number": 11, "title": "y (A8)", "body": ""}],
        },
    )
    report = align.align("o/r", _plan(), g, apply=True)
    assert report["milestone"] == "ambiguous"
    g.update_milestone_title.assert_not_called()


# --- codex PR-31 round 1 ---


def test_duplicate_plan_assertion_sets_fail_closed():
    # Two plan phases with the SAME token set: one issue would match both and get
    # double-renamed (last writer wins, silently). Both phases must land in
    # ambiguous_phases and nothing renames.
    dup_plan = sync.parse_plan_md(
        "# P\n\n## Phase 1 — A (A3, A4)\n\n- [ ] t\n\n## Phase 2 — B (A4, A3)\n\n- [ ] t\n"
    )
    g = _gh(
        [{"number": 1, "title": "W"}],
        {1: [{"number": 10, "title": "x (A3/A4)", "body": ""}]},
    )
    report = align.align("o/r", dup_plan, g, apply=True)
    assert set(report["ambiguous_phases"]) == {
        "Phase 1 — A (A3, A4)",
        "Phase 2 — B (A4, A3)",
    }
    assert report["matches"] == []
    g.update_issue_title.assert_not_called()
