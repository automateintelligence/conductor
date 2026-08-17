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
    # A gateless phase is unmatchABLE, not unmatched: it gets its own bucket so the
    # owner's "these need attention" list stays the phases that really are missing.
    assert "Phase 3 — Glue — OPTIONAL" not in report["unmatched_phases"]
    assert report["gateless_phases"] == ["Phase 3 — Glue — OPTIONAL"]
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


def test_gateless_phase_does_not_dilute_unmatched_phases():
    # Every GATED phase matches, so the owner's unmatched list must be empty — the
    # `gate: none` phase belongs in gateless_phases, not in it.
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "x (A3, A4)", "body": ""},
                {"number": 11, "title": "y (A8)", "body": ""},
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert report["unmatched_phases"] == []
    assert report["gateless_phases"] == ["Phase 3 — Glue — OPTIONAL"]
    assert sorted(m["issue"] for m in report["matches"]) == [10, 11]


def test_markerless_issue_is_reported_for_hand_pairing():
    # The other half of the gateless pair: an issue with neither a conductor-assertions
    # marker nor heading tokens was dropped from every bucket, so the owner could not see
    # the issue that the gateless phase needs pairing with.
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "x (A3, A4)", "body": ""},
                {"number": 42, "title": "Phase 3 - glue (optional)", "body": ""},
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert report["markerless_issues"] == [
        {"number": 42, "title": "Phase 3 - glue (optional)"}
    ]
    # ...and it stays OUT of unmatched_issues, which means "carries a token set that no
    # plan phase claims" — task sub-issues live in the same milestone and are markerless.
    assert report["unmatched_issues"] == []
    assert [m["issue"] for m in report["matches"]] == [10]


def test_matched_issue_never_counted_markerless():
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
    report = align.align("o/r", _plan(), g)
    assert report["markerless_issues"] == []
    assert [m["issue"] for m in report["matches"]] == [10]


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
    # codex r2 LOW: an ambiguity-participating issue is neither matched NOR unmatched —
    # listing it in unmatched_issues would mislead follow-on automation.
    assert report["unmatched_issues"] == []
    g.update_issue_title.assert_not_called()


# --- codex production review round 2, finding 1 -----------------------------------------
#
# The prepare PAIRING GATE demanded `gateless_phases` AND `markerless_issues` be empty before
# `convert`. Neither can be: `gateless_phases` lists every `gate: none` phase by design, and
# `markerless_issues` lists every issue without assertion tokens — which is every task
# sub-issue, since `sync.generate` creates them with `body=""`. Renaming an issue to the phase
# heading exactly, the resolution the skill asks for, left both buckets unchanged, so the
# precondition could not be reached by doing what it said. A worker either stops forever or
# violates the gate.
#
# What the gate actually needs to assert is the thing that produces the harm: whether
# `sync.generate`'s EXACT-title lookup will find an existing issue for this phase, or create a
# duplicate. align now answers that question per gateless phase — the same way convert asks it
# — and the gate reads the answer.


def test_a_gateless_phase_paired_by_exact_title_is_resolved():
    # THE achievability test. Do what the skill says (rename the issue to the heading, character
    # for character) and the gate's bucket empties, WITHOUT markerless_issues emptying — which
    # it never can.
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "x (A3, A4)", "body": ""},
                {"number": 11, "title": "y (A8)", "body": ""},
                {"number": 42, "title": "Phase 3 — Glue — OPTIONAL", "body": ""},
                {"number": 43, "title": "three", "body": ""},  # a task sub-issue
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert report["gateless_unpaired"] == []
    assert report["gateless_pairs"] == [
        {"title": "Phase 3 — Glue — OPTIONAL", "issue": 42}
    ]
    assert report["ambiguous_phases"] == {}
    # the gate is satisfiable precisely because it no longer waits on this bucket
    assert [i["number"] for i in report["markerless_issues"]] == [42, 43]
    # the informational bucket still lists every gateless phase
    assert report["gateless_phases"] == ["Phase 3 — Glue — OPTIONAL"]


def test_a_gateless_phase_with_no_exact_title_issue_is_unpaired():
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 10, "title": "x (A3, A4)", "body": ""},
                {"number": 42, "title": "Phase 3 - glue (optional)", "body": ""},
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    # a paraphrase is not a pairing — convert would create a second phase issue
    assert report["gateless_unpaired"] == ["Phase 3 — Glue — OPTIONAL"]
    assert report["gateless_pairs"] == []


def test_two_issues_carrying_the_phase_heading_are_ambiguous_not_paired():
    # convert would reuse one of them and leave the other; which one is not align's to guess,
    # and `ledger align` exits nonzero on ambiguity, so the owner sees it.
    g = _gh(
        [{"number": 1, "title": "W"}],
        {
            1: [
                {"number": 42, "title": "Phase 3 — Glue — OPTIONAL", "body": ""},
                {"number": 44, "title": "Phase 3 — Glue — OPTIONAL", "body": ""},
            ]
        },
    )
    report = align.align("o/r", _plan(), g)
    assert report["ambiguous_phases"]["Phase 3 — Glue — OPTIONAL"] == [42, 44]
    assert report["gateless_pairs"] == []
    assert report["gateless_unpaired"] == []  # not a decision the owner can take yet


def test_pairing_a_gateless_phase_renames_nothing():
    # align's rename leg is for token-set matches. A gateless pair is recognised BY the title
    # already being exact, so there is nothing to rename — and align must not invent one.
    g = _gh(
        [{"number": 1, "title": "W"}],
        {1: [{"number": 42, "title": "Phase 3 — Glue — OPTIONAL", "body": ""}]},
    )
    report = align.align("o/r", _plan(), g, apply=True)
    assert [m["issue"] for m in report["matches"]] == []
    g.update_issue_title.assert_not_called()
