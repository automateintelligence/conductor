from ledger import gate_link


def test_assertions_marker_roundtrip():
    marker = gate_link.assertions_marker(["A3", "A4"])
    assert marker == "<!-- conductor-assertions: A3,A4 -->"
    assert gate_link.read_assertion_tokens(f"text\n{marker}\nmore") == ["A3", "A4"]


def test_read_tokens_splits_comma_slash_whitespace():
    body = "<!-- conductor-assertions: A3, A4/A5 -->"
    assert gate_link.read_assertion_tokens(body) == ["A3", "A4", "A5"]


def test_read_tokens_absent_or_none_body():
    assert gate_link.read_assertion_tokens("no marker here") == []
    assert gate_link.read_assertion_tokens(None) == []


def test_match_exact_wins():
    ids = ["a03-foo", "A3"]
    assert gate_link.match_ids("A3", ids) == {"A3"}


def test_match_case_insensitive_exact():
    assert gate_link.match_ids("b7", ["B7", "other"]) == {"B7"}


def test_match_zero_padded_prefix():
    ids = ["a03-rich-predicate-not-label", "a30-other", "a3x-nope"]
    assert gate_link.match_ids("A3", ids) == {"a03-rich-predicate-not-label"}


def test_match_multi_digit_token():
    ids = ["a12-blind-judge", "a01-no-secrets"]
    assert gate_link.match_ids("A12", ids) == {"a12-blind-judge"}
    assert gate_link.match_ids("A1", ids) == {"a01-no-secrets"}


def test_match_unresolved_returns_empty():
    assert gate_link.match_ids("A9", ["a03-x"]) == set()


def test_tests_red_when_any_matched_id_red():
    results = {"a03-x": {"pass": True}, "a04-y": {"pass": False}}
    out = gate_link.tests_red_from_results(["A3", "A4"], results)
    assert out["red"] is True
    assert out["red_ids"] == ["a04-y"]
    assert out["unresolved"] == []


def test_tests_green_when_all_matched_pass():
    out = gate_link.tests_red_from_results(["A3"], {"a03-x": {"pass": True}})
    assert out["red"] is False and out["red_ids"] == []


def test_missing_pass_key_counts_red():
    out = gate_link.tests_red_from_results(["A3"], {"a03-x": {"rc": 0}})
    assert out["red"] is True


def test_unresolved_token_reported_not_silently_green():
    out = gate_link.tests_red_from_results(["A9"], {"a03-x": {"pass": True}})
    assert out["unresolved"] == ["A9"]


def test_upsert_marker_appends_replaces_and_noops():
    assert gate_link.upsert_marker("", ["A1"]) == "<!-- conductor-assertions: A1 -->"
    appended = gate_link.upsert_marker("body", ["A1"])
    assert appended == "body\n\n<!-- conductor-assertions: A1 -->"
    replaced = gate_link.upsert_marker(appended, ["A1", "A2"])
    assert replaced.count("conductor-assertions") == 1
    assert "A1,A2" in replaced
    assert (
        gate_link.upsert_marker(replaced, ["A1", "A2"]) is None
    )  # unchanged -> no rewrite
