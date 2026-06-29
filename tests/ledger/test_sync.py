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
