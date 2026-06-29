from unittest.mock import MagicMock, patch

from ledger import gh


def test_set_labels_sends_real_json_array(monkeypatch):
    captured = {}

    def fake(method, path, body=None, jq=None):
        if method == "GET":
            return {
                "state": "open",
                "id": 1,
                "labels": [{"name": "status:ready"}],
                "assignees": [],
            }
        captured["body"] = body
        return None

    monkeypatch.setattr(gh, "_gh_api", fake)
    gh.set_labels("o/r", 1, add=["status:in-progress"], remove=["status:ready"])
    assert isinstance(captured["body"]["labels"], list)  # ARRAY, not a JSON string
    assert captured["body"]["labels"] == ["status:in-progress"]


def test_assign_unassign_send_list_bodies(monkeypatch):
    seen = []
    monkeypatch.setattr(
        gh, "_gh_api", lambda m, p, body=None, jq=None: seen.append((m, body)) or {}
    )
    gh.assign("o/r", 5, "alice")
    gh.unassign("o/r", 5, "alice")
    assert seen[0] == ("POST", {"assignees": ["alice"]})
    assert seen[1] == ("DELETE", {"assignees": ["alice"]})


def test_add_sub_issue_typed_int_db_id(monkeypatch):
    seen = []
    monkeypatch.setattr(
        gh, "_gh_api", lambda m, p, body=None, jq=None: seen.append((m, p, body)) or {}
    )
    gh.add_sub_issue("o/r", 1, 4761391764)
    assert seen[-1] == (
        "POST",
        "repos/o/r/issues/1/sub_issues",
        {"sub_issue_id": 4761391764},
    )


def test_gh_api_error_includes_response_body():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = "gh: Validation Failed (HTTP 422)"
    fake_result.stdout = '{"errors":[{"code":"already_exists"}]}'

    with patch("ledger.gh.subprocess.run", return_value=fake_result):
        try:
            gh._gh_api("POST", "repos/o/r/labels", body={"name": "x"})
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "already_exists" in str(e)


def test_find_milestone_matches_title(monkeypatch):  # idempotency (review)
    monkeypatch.setattr(
        gh,
        "_gh_api",
        lambda m, p, body=None, jq=None: [
            {"number": 4, "title": "Other"},
            {"number": 7, "title": "Plan: X"},
        ],
    )
    assert gh.find_milestone("o/r", "Plan: X") == 7
    monkeypatch.setattr(gh, "_gh_api", lambda m, p, body=None, jq=None: [])
    assert gh.find_milestone("o/r", "Plan: X") is None


def test_find_issue_matches_title_and_excludes_prs(monkeypatch):  # idempotency (review)
    rows = [
        {
            "number": 1,
            "id": 100,
            "title": "Phase A",
            "pull_request": {"url": "x"},
        },  # PR
        {"number": 2, "id": 200, "title": "Phase A"},  # the real issue
    ]
    monkeypatch.setattr(gh, "_gh_api", lambda m, p, body=None, jq=None: rows)
    assert gh.find_issue("o/r", "Phase A", milestone=7) == {"number": 2, "id": 200}
    assert gh.find_issue("o/r", "Nope", milestone=7) is None


def test_ensure_label_idempotent_on_already_exists(monkeypatch):
    def raise_already_exists(method, path, body=None, jq=None):
        raise RuntimeError("gh api POST repos/o/r/labels failed: already_exists")

    monkeypatch.setattr(gh, "_gh_api", raise_already_exists)
    gh.ensure_label("o/r", "status:ready")  # must not raise


def test_ensure_label_reraises_other_errors(monkeypatch):
    def raise_server_error(method, path, body=None, jq=None):
        raise RuntimeError(
            "gh api POST repos/o/r/labels failed: some other failure (HTTP 500)"
        )

    monkeypatch.setattr(gh, "_gh_api", raise_server_error)
    try:
        gh.ensure_label("o/r", "x")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
