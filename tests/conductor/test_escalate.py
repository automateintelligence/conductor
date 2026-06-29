from unittest.mock import MagicMock

from conductor import escalate


def test_file_followup_labels_and_links():
    gh = MagicMock()
    gh.create_issue.return_value = {"number": 42, "id": 1}
    assert (
        escalate.file_followup(
            "o/r", "debt", "hard bit", "what's hard", link_issue=7, gh=gh
        )
        == 42
    )
    gh.ensure_label.assert_called_with("o/r", "debt")
    gh._gh_api.assert_called_once_with(
        "POST", "repos/o/r/issues/7/comments", body={"body": "Excavated debt: #42"}
    )


def test_block_on_subplan_sets_labels():
    gh = MagicMock()
    escalate.block_on_subplan("o/r", 7, gh=gh)
    _, kwargs = gh.set_labels.call_args
    assert "status:blocked" in kwargs["add"] and "blocked-on-subplan" in kwargs["add"]
    assert "status:in-progress" in kwargs["remove"]


def test_write_adr(tmp_path):
    p = escalate.write_adr(str(tmp_path), "deepen-phase-2", "## Decision\nDid X.")
    assert p.endswith("deepen-phase-2.md")
    content = open(p).read()
    assert "deepen-phase-2" in content and "Did X." in content
