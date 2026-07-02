from unittest.mock import MagicMock

from ledger import sync

PLAN_MD = """\
# Plan: URL Shortener

## Phase A [ready]
- [ ] Create domain model
- [ ] Add REST endpoint

## Phase B [draft]
- [ ] Write integration tests
"""


def test_parse_plan_md_title():
    result = sync.parse_plan_md(PLAN_MD)
    assert result["title"] == "Plan: URL Shortener"


def test_parse_plan_md_phases():
    result = sync.parse_plan_md(PLAN_MD)
    phases = result["phases"]
    assert len(phases) == 2
    assert phases[0]["title"] == "Phase A"
    assert phases[0]["status"] == "ready"
    assert phases[1]["title"] == "Phase B"
    assert phases[1]["status"] == "draft"


def test_parse_plan_md_tasks():
    result = sync.parse_plan_md(PLAN_MD)
    phases = result["phases"]
    assert phases[0]["tasks"] == ["Create domain model", "Add REST endpoint"]
    assert phases[1]["tasks"] == ["Write integration tests"]


def test_generate_happy_path():
    plan = {
        "title": "Plan: URL Shortener",
        "phases": [
            {"title": "Phase A", "status": "ready", "tasks": ["Task 1", "Task 2"]},
            {"title": "Phase B", "status": "draft", "tasks": ["Task 3"]},
        ],
    }

    gh = MagicMock()
    gh.find_milestone.return_value = None  # first run: nothing exists yet
    gh.find_issue.return_value = None
    gh.find_issues.return_value = []
    gh.create_milestone.return_value = 7

    # Each create_issue call returns distinct {number, id}
    gh.create_issue.side_effect = [
        {"number": 10, "id": 1001},  # Phase A issue
        {"number": 11, "id": 1002},  # Task 1 issue
        {"number": 12, "id": 1003},  # Task 2 issue
        {"number": 13, "id": 1004},  # Phase B issue
        {"number": 14, "id": 1005},  # Task 3 issue
    ]

    result = sync.generate("owner/repo", plan, gh)

    # Milestone created with plan title
    gh.create_milestone.assert_called_once_with("owner/repo", "Plan: URL Shortener")

    # Labels ensured for distinct statuses (ready, draft)
    gh.ensure_label.assert_any_call("owner/repo", "status:ready")
    gh.ensure_label.assert_any_call("owner/repo", "status:draft")

    # Phase A issue created with correct label and milestone
    gh.create_issue.assert_any_call(
        "owner/repo",
        "Phase A",
        body="",
        milestone=7,
        labels=["status:ready"],
    )
    # Phase B issue created with correct label and milestone
    gh.create_issue.assert_any_call(
        "owner/repo",
        "Phase B",
        body="",
        milestone=7,
        labels=["status:draft"],
    )
    # Task issues created without labels
    gh.create_issue.assert_any_call("owner/repo", "Task 1", body="", milestone=7)
    gh.create_issue.assert_any_call("owner/repo", "Task 2", body="", milestone=7)
    gh.create_issue.assert_any_call("owner/repo", "Task 3", body="", milestone=7)

    # add_sub_issue called with CHILD DB ID (id), not display number
    gh.add_sub_issue.assert_any_call(
        "owner/repo", 10, 1002
    )  # phase A (#10), Task 1 id=1002
    gh.add_sub_issue.assert_any_call(
        "owner/repo", 10, 1003
    )  # phase A (#10), Task 2 id=1003
    gh.add_sub_issue.assert_any_call(
        "owner/repo", 13, 1005
    )  # phase B (#13), Task 3 id=1005

    # Returned structure
    assert result["milestone"] == 7
    assert len(result["phases"]) == 2
    phase_a = result["phases"][0]
    assert phase_a["number"] == 10
    assert phase_a["sub_issues"] == [11, 12]
    assert phase_a["fallback"] is False
    phase_b = result["phases"][1]
    assert phase_b["number"] == 13
    assert phase_b["sub_issues"] == [14]
    assert phase_b["fallback"] is False


def test_generate_fallback_on_add_sub_issue_error():
    plan = {
        "title": "Plan: Fallback Test",
        "phases": [
            {"title": "Phase X", "status": "ready", "tasks": ["Task Alpha"]},
        ],
    }

    gh = MagicMock()
    gh.find_milestone.return_value = None  # first run: nothing exists yet
    gh.find_issue.return_value = None
    gh.find_issues.return_value = []
    gh.create_milestone.return_value = 3
    gh.create_issue.side_effect = [
        {"number": 20, "id": 2001},  # Phase X issue
        {"number": 21, "id": 2002},  # Task Alpha issue
    ]
    gh.add_sub_issue.side_effect = RuntimeError("sub_issues not supported")
    gh.get_body.return_value = ""

    result = sync.generate("owner/repo", plan, gh)

    # set_body called with checklist line using task DISPLAY number
    gh.set_body.assert_called_once_with("owner/repo", 20, "- [ ] #21")

    # Phase reports fallback=True and task is still recorded
    phase = result["phases"][0]
    assert phase["fallback"] is True
    assert phase["number"] == 20
    assert phase["sub_issues"] == [21]


def test_generate_fallback_appends_to_existing_body():
    plan = {
        "title": "Plan: Append Test",
        "phases": [
            {
                "title": "Phase Y",
                "status": "ready",
                "tasks": ["Task One", "Task Two"],
            },
        ],
    }

    gh = MagicMock()
    gh.find_milestone.return_value = None  # first run: nothing exists yet
    gh.find_issue.return_value = None
    gh.find_issues.return_value = []
    gh.create_milestone.return_value = 5
    gh.create_issue.side_effect = [
        {"number": 30, "id": 3001},  # Phase Y
        {"number": 31, "id": 3002},  # Task One
        {"number": 32, "id": 3003},  # Task Two
    ]
    gh.add_sub_issue.side_effect = RuntimeError("not supported")

    # Simulate body accumulating across set_body calls
    body_store = {"v": ""}
    gh.get_body.side_effect = lambda r, n: body_store["v"]
    gh.set_body.side_effect = lambda r, n, b: body_store.__setitem__("v", b)

    result = sync.generate("owner/repo", plan, gh)

    final_body = body_store["v"]
    assert "- [ ] #31" in final_body
    assert "- [ ] #32" in final_body
    phase = result["phases"][0]
    assert phase["fallback"] is True
    assert phase["sub_issues"] == [31, 32]


def test_convert_reads_file_and_delegates(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(PLAN_MD)

    gh = MagicMock()
    gh.find_milestone.return_value = None  # first run: nothing exists yet
    gh.find_issue.return_value = None
    gh.find_issues.return_value = []
    gh.create_milestone.return_value = 99
    gh.create_issue.side_effect = [
        {"number": 100, "id": 10001},
        {"number": 101, "id": 10002},
        {"number": 102, "id": 10003},
        {"number": 103, "id": 10004},
        {"number": 104, "id": 10005},
    ]

    result = sync.convert("owner/repo", str(plan_file), gh)

    assert result["milestone"] == 99
    assert len(result["phases"]) == 2
    gh.create_milestone.assert_called_once_with("owner/repo", "Plan: URL Shortener")


def test_generate_is_idempotent_on_rerun():  # idempotency (review)
    # second run: milestone + phase + its linked tasks already exist -> reuse, never duplicate.
    plan = {
        "title": "Plan: URL Shortener",
        "phases": [
            {"title": "Phase A", "status": "ready", "tasks": ["Task 1", "Task 2"]},
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 7
    gh.find_issue.return_value = {"number": 10, "id": 1001}  # Phase A exists
    gh.list_sub_issues.return_value = [  # its tasks are already linked under it
        {"number": 11, "id": 1002, "title": "Task 1"},
        {"number": 12, "id": 1003, "title": "Task 2"},
    ]
    gh.get_body.return_value = ""

    result = sync.generate("owner/repo", plan, gh)

    gh.create_milestone.assert_not_called()  # reused
    gh.create_issue.assert_not_called()  # reused
    gh.add_sub_issue.assert_not_called()  # already linked on the prior run
    assert result["milestone"] == 7
    assert result["phases"][0]["number"] == 10
    assert result["phases"][0]["sub_issues"] == [11, 12]
    assert result["phases"][0]["fallback"] is False


def test_repeated_task_title_across_phases_are_distinct():  # review Finding 1
    # two phases that share a task title ("Write tests") must get TWO separate task issues,
    # each linked to its own phase — not one issue collapsed across both.
    plan = {
        "title": "P",
        "phases": [
            {"title": "Phase A", "status": "ready", "tasks": ["Write tests"]},
            {"title": "Phase B", "status": "ready", "tasks": ["Write tests"]},
        ],
    }
    issues: dict[int, dict] = {}  # stateful fake: find returns the FIRST title match
    gh = MagicMock()
    gh.find_milestone.return_value = None
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = ""

    def _find_all(repo, title, milestone=None):
        return [
            {"number": num, "id": issues[num]["id"]}
            for num in sorted(issues)
            if issues[num]["title"] == title
        ]

    def _create(repo, title, body="", milestone=None, labels=()):
        num = 11 + len(issues)
        issues[num] = {"title": title, "id": 1000 + num}
        return {"number": num, "id": 1000 + num}

    gh.find_issues.side_effect = _find_all
    gh.find_issue.side_effect = lambda r, t, m=None: next(
        iter(_find_all(r, t, m)), None
    )
    gh.create_issue.side_effect = _create

    result = sync.generate("o/r", plan, gh)
    a = result["phases"][0]["sub_issues"]
    b = result["phases"][1]["sub_issues"]
    assert len(a) == 1 and len(b) == 1
    assert a != b, f"phases collapsed onto the same task issue: {a} == {b}"


def test_existing_phase_relinks_unlinked_task_on_retry():  # review Finding 2
    # prior run created the task issue but crashed before linking it -> the re-run must LINK
    # the existing issue (not leave the phase taskless, and not create a duplicate).
    plan = {
        "title": "P",
        "phases": [{"title": "Phase A", "status": "ready", "tasks": ["Build"]}],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 7
    gh.find_issue.side_effect = lambda repo, title, milestone=None: (
        {"number": 10, "id": 1001} if title == "Phase A" else None
    )
    gh.find_issues.side_effect = lambda repo, title, milestone=None: (
        [{"number": 11, "id": 1002}] if title == "Build" else []  # orphan, never linked
    )
    gh.list_sub_issues.return_value = []  # phase has NO linked children yet
    gh.get_body.return_value = ""

    result = sync.generate("o/r", plan, gh)

    gh.create_issue.assert_not_called()  # the orphan is reused, not duplicated
    gh.add_sub_issue.assert_called_once_with("o/r", 10, 1002)  # now linked to its phase
    assert result["phases"][0]["sub_issues"] == [11]


def test_unlinked_orphan_reused_when_linked_sibling_exists():  # review (PR#20 round 4)
    # Phase A's "Build" is already LINKED (#10); Phase B has an UNLINKED orphan "Build" (#13)
    # from a crashed run. Phase B must reuse #13 (not create a duplicate, not steal A's #10).
    plan = {
        "title": "P",
        "phases": [
            {"title": "Phase A", "status": "ready", "tasks": ["Build"]},
            {"title": "Phase B", "status": "ready", "tasks": ["Build"]},
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 7
    gh.find_issue.side_effect = lambda repo, title, milestone=None: {
        "Phase A": {"number": 1, "id": 101},
        "Phase B": {"number": 2, "id": 102},
    }.get(title)
    # both Builds exist in the milestone; #10 is linked to A, #13 is the unlinked orphan.
    gh.find_issues.side_effect = lambda repo, title, milestone=None: (
        [{"number": 10, "id": 110}, {"number": 13, "id": 113}]
        if title == "Build"
        else []
    )
    gh.list_sub_issues.side_effect = lambda repo, parent: (
        [{"number": 10, "id": 110, "title": "Build"}] if parent == 1 else []
    )
    gh.get_body.return_value = ""

    result = sync.generate("o/r", plan, gh)

    gh.create_issue.assert_not_called()  # orphan #13 reused, no duplicate
    assert result["phases"][0]["sub_issues"] == [10]  # A keeps its linked task
    assert result["phases"][1]["sub_issues"] == [
        13
    ]  # B reuses the orphan, not a new #14
    gh.add_sub_issue.assert_any_call("o/r", 2, 113)  # B linked to the reused orphan


# --- Real writing-plans dialect (fix-pass-1): `## Phase N — Title (ids)`, no [status] ---

REAL_PLAN_MD = """\
# Model-Extraction Eval — Implementation Plan

**Normative spec:** docs/specs/spec.md

## Global Constraints

- [ ] not a phase task; must be ignored

## Phase 1 — Relationship-quality scoring (A3, A4, A5)

Some prose.

- [ ] Confirm RED
- [ ] Implement scoring

## Phase 2 — Analysis contract (A8/A16/A19)

- [ ] Implement axis_verdict

## CI / gate coexistence — Phase-6 re-include (DO NOT FORGET)

- [ ] CI task that must NOT leak into Phase 2

## Phase 7 — Harness entrypoint (build-only, NOT executed) — OPTIONAL

- [ ] build cli
"""


def test_parse_real_dialect_phase_headings_default_ready():
    phases = sync.parse_plan_md(REAL_PLAN_MD)["phases"]
    assert [p["title"] for p in phases] == [
        "Phase 1 — Relationship-quality scoring (A3, A4, A5)",
        "Phase 2 — Analysis contract (A8/A16/A19)",
        "Phase 7 — Harness entrypoint (build-only, NOT executed) — OPTIONAL",
    ]
    assert all(p["status"] == "ready" for p in phases)


def test_parse_real_dialect_non_phase_h2_tasks_do_not_leak():
    phases = sync.parse_plan_md(REAL_PLAN_MD)["phases"]
    assert phases[0]["tasks"] == ["Confirm RED", "Implement scoring"]
    # Phase 2's section ends at the NEXT H2 of ANY kind — the CI section's task must not leak in.
    assert phases[1]["tasks"] == ["Implement axis_verdict"]
    all_tasks = [t for p in phases for t in p["tasks"]]
    assert "not a phase task; must be ignored" not in all_tasks
    assert "CI task that must NOT leak into Phase 2" not in all_tasks


def test_parse_real_dialect_assertion_tokens():
    phases = sync.parse_plan_md(REAL_PLAN_MD)["phases"]
    assert phases[0]["assertions"] == ["A3", "A4", "A5"]  # comma-separated
    assert phases[1]["assertions"] == ["A8", "A16", "A19"]  # slash-separated
    assert phases[2]["assertions"] == []  # parens not trailing -> not an id list


def test_parse_old_dialect_gets_empty_assertions():
    phases = sync.parse_plan_md(PLAN_MD)["phases"]
    assert all(p["assertions"] == [] for p in phases)


def test_parse_status_bracket_combined_with_assertions():
    md = "# T\n\n## Phase 9 — Foo (A1) [draft]\n\n- [ ] task\n"
    (phase,) = sync.parse_plan_md(md)["phases"]
    assert phase["title"] == "Phase 9 — Foo (A1)"
    assert phase["status"] == "draft"
    assert phase["assertions"] == ["A1"]


def test_parse_prose_parens_rejected_as_assertions():
    md = "# T\n\n## Phase 3 — Cleanup (best effort, optional)\n\n- [ ] task\n"
    (phase,) = sync.parse_plan_md(md)["phases"]
    assert phase["assertions"] == []  # tokens contain whitespace -> prose, not ids


def test_generate_writes_assertion_marker_on_create():
    plan = {
        "title": "P",
        "phases": [
            {
                "title": "Phase 1 — X (A3, A4)",
                "status": "ready",
                "tasks": [],
                "assertions": ["A3", "A4"],
            },
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = None
    gh.find_issue.return_value = None
    gh.find_issues.return_value = []
    gh.create_milestone.return_value = 1
    gh.create_issue.return_value = {"number": 10, "id": 1001}
    sync.generate("o/r", plan, gh)
    _, kwargs = gh.create_issue.call_args
    assert "<!-- conductor-assertions: A3,A4 -->" in kwargs["body"]


def test_generate_backfills_marker_on_reused_phase():
    plan = {
        "title": "P",
        "phases": [
            {
                "title": "Phase 1 — X (A3)",
                "status": "ready",
                "tasks": [],
                "assertions": ["A3"],
            },
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 1
    gh.find_issue.return_value = {"number": 10, "id": 1001}
    gh.find_issues.return_value = []
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = "existing body"
    sync.generate("o/r", plan, gh)
    gh.set_body.assert_called_once()
    new_body = gh.set_body.call_args[0][2]
    assert "existing body" in new_body
    assert "<!-- conductor-assertions: A3 -->" in new_body


def test_generate_replaces_stale_marker_not_duplicates():
    plan = {
        "title": "P",
        "phases": [
            {
                "title": "Phase 1 — X (A3, A4)",
                "status": "ready",
                "tasks": [],
                "assertions": ["A3", "A4"],
            },
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 1
    gh.find_issue.return_value = {"number": 10, "id": 1001}
    gh.find_issues.return_value = []
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = "body\n\n<!-- conductor-assertions: A3 -->"
    sync.generate("o/r", plan, gh)
    new_body = gh.set_body.call_args[0][2]
    assert new_body.count("conductor-assertions") == 1
    assert "<!-- conductor-assertions: A3,A4 -->" in new_body


def test_generate_marker_unchanged_is_not_rewritten():
    plan = {
        "title": "P",
        "phases": [
            {
                "title": "Phase 1 — X (A3)",
                "status": "ready",
                "tasks": [],
                "assertions": ["A3"],
            },
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 1
    gh.find_issue.return_value = {"number": 10, "id": 1001}
    gh.find_issues.return_value = []
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = "body\n\n<!-- conductor-assertions: A3 -->"
    sync.generate("o/r", plan, gh)
    gh.set_body.assert_not_called()


# --- codex round-1: token shape + stale-marker lifecycle ---


def test_prose_single_word_parens_rejected_digit_required():
    md = "# T\n\n## Phase 1 — Cleanup (optional)\n\n- [ ] task\n"
    (phase,) = sync.parse_plan_md(md)["phases"]
    assert phase["assertions"] == []  # no digit -> prose, not an assertion id


def test_mixed_valid_invalid_tokens_reject_whole_group():
    md = "# T\n\n## Phase 1 — Cleanup (A3, optional)\n\n- [ ] task\n"
    (phase,) = sync.parse_plan_md(md)["phases"]
    assert phase["assertions"] == []  # half-parsed bindings are worse than none


def test_generate_removes_marker_when_assertions_explicitly_empty():
    plan = {
        "title": "P",
        "phases": [
            {
                "title": "Phase 1 — Cleanup",
                "status": "ready",
                "tasks": [],
                "assertions": [],
            },
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 1
    gh.find_issue.return_value = {"number": 10, "id": 1001}
    gh.find_issues.return_value = []
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = "body\n\n<!-- conductor-assertions: A3 -->"
    sync.generate("o/r", plan, gh)
    new_body = gh.set_body.call_args[0][2]
    assert "conductor-assertions" not in new_body
    assert "body" in new_body


def test_generate_preserves_marker_when_assertions_key_absent():
    # A hand-built JSON dict that carries no assertion info must not destroy an
    # existing (e.g. manually backfilled) marker.
    plan = {
        "title": "P",
        "phases": [
            {"title": "Phase 1 — Cleanup", "status": "ready", "tasks": []},
        ],
    }
    gh = MagicMock()
    gh.find_milestone.return_value = 1
    gh.find_issue.return_value = {"number": 10, "id": 1001}
    gh.find_issues.return_value = []
    gh.list_sub_issues.return_value = []
    gh.get_body.return_value = "body\n\n<!-- conductor-assertions: A3 -->"
    sync.generate("o/r", plan, gh)
    gh.set_body.assert_not_called()
