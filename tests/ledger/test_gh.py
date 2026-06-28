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
