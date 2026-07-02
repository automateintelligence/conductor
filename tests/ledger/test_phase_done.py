from unittest.mock import MagicMock

from ledger import phase_done

MARKER = "<!-- conductor-assertions: A3 -->"
LEASE = "<!-- conductor-lease worker=w ts=100 -->"
ATTEMPTS = "<!-- conductor-attempts n=2 -->"


def _store_gh(
    body,
    labels=("status:in-progress",),
    assignees=("w",),
    sub_issues=(),
    title="Phase 1 — X (A3)",
):
    store = {
        "body": body,
        "labels": list(labels),
        "assignees": list(assignees),
        "closed": [],
    }
    g = MagicMock()
    g.issue_state.side_effect = lambda r, n: {
        "state": "open",
        "id": 1,
        "labels": list(store["labels"]),
        "assignees": list(store["assignees"]),
    }
    g.get_body.side_effect = lambda r, n: store["body"]
    g.set_body.side_effect = lambda r, n, b: store.__setitem__("body", b)
    g.set_labels.side_effect = lambda r, n, add=(), remove=(): store.__setitem__(
        "labels", sorted((set(store["labels"]) | set(add)) - set(remove))
    )
    g.close_issue.side_effect = lambda r, n: store["closed"].append(n)
    g.unassign.side_effect = lambda r, n, w: store["assignees"].remove(w)
    g.list_sub_issues.return_value = list(sub_issues)
    g.issue_title.return_value = title
    return g, store


GREEN = {"a03-x": {"pass": True}}
RED = {"a03-x": {"pass": False}}


def test_happy_path_full_bookkeeping():
    g, store = _store_gh(
        f"body\n{MARKER}\n{LEASE}\n{ATTEMPTS}",
        sub_issues=[{"number": 21, "id": 2001, "title": "t"}],
    )
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN)
    assert out["ok"] is True
    assert store["labels"] == ["status:done"]
    assert 21 in store["closed"] and 10 in store["closed"]  # tasks AND the phase closed
    assert out["sub_issues_closed"] == [21]
    assert store["assignees"] == []
    assert "conductor-lease" not in store["body"]
    assert "conductor-attempts" not in store["body"]
    assert "conductor-assertions" in store["body"]  # the gate mapping stays


def test_red_assertion_blocks_all_bookkeeping():
    g, store = _store_gh(f"body\n{MARKER}")
    out = phase_done.phase_done("o/r", 10, gh=g, results=RED)
    assert out["ok"] is False and out["error"] == "assertions-red"
    assert out["red_ids"] == ["a03-x"]
    assert store["labels"] == ["status:in-progress"]  # untouched
    assert store["closed"] == []
    g.set_labels.assert_not_called()


def test_no_marker_fails_closed():
    g, store = _store_gh("body without marker")
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN)
    assert out["ok"] is False and out["error"] == "no-assertion-marker"
    assert store["closed"] == []


def test_unresolved_token_fails_closed():
    g, store = _store_gh("<!-- conductor-assertions: A9 -->")
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN)
    assert out["ok"] is False and out["error"] == "unresolved-assertions"
    assert out["unresolved"] == ["A9"]


def test_missing_results_fails_closed():
    g, store = _store_gh(f"body\n{MARKER}")
    out = phase_done.phase_done("o/r", 10, gh=g, results=None)
    assert out["ok"] is False and out["error"] == "no-results"


def test_no_gate_check_bypasses_verification():
    g, store = _store_gh("body without marker")
    out = phase_done.phase_done("o/r", 10, gh=g, results=None, no_gate_check=True)
    assert out["ok"] is True
    assert store["labels"] == ["status:done"] and 10 in store["closed"]


def test_checklist_fallback_ticked_when_sub_issue_api_unavailable():
    g, store = _store_gh(f"{MARKER}\n- [ ] #21\n- [ ] #22\n- [x] #23")
    g.list_sub_issues.side_effect = RuntimeError("unsupported")
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN)
    assert out["ok"] is True and out["checklist_ticked"] == 2
    assert store["body"].count("- [x] #") == 3
    assert "- [ ] #" not in store["body"]


def test_plan_section_checkboxes_ticked(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# T\n\n## Phase 1 — X (A3)\n\n- [ ] one\n- [ ] two\n\n"
        "## Phase 2 — Y (A4)\n\n- [ ] other\n"
    )
    g, _ = _store_gh(f"body\n{MARKER}")
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN, plan_path=str(plan))
    assert out["plan"] == {"ticked": 2}
    text = plan.read_text()
    assert "- [x] one" in text and "- [x] two" in text
    assert "- [ ] other" in text  # other phases untouched


def test_plan_section_not_found_is_reported_not_fatal(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# T\n\n## Phase 2 — Y (A4)\n\n- [ ] other\n")
    g, store = _store_gh(f"body\n{MARKER}")
    out = phase_done.phase_done("o/r", 10, gh=g, results=GREEN, plan_path=str(plan))
    assert out["ok"] is True
    assert out["plan"] == {"error": "section-not-found"}
    assert 10 in store["closed"]  # ledger bookkeeping still completed
