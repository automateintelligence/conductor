import json

import pytest
from unittest.mock import MagicMock

from ledger import __main__ as cli

MARKER = "<!-- conductor-assertions: A3 -->"


def _gh_with_body(body):
    g = MagicMock()
    g.get_body.return_value = body
    return g


def _results_file(tmp_path, data):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_from_gate_red_when_matched_id_red(tmp_path):
    path = _results_file(tmp_path, {"a03-x": {"pass": False}})
    assert cli._tests_red_from_gate("o/r", 1, path, gh=_gh_with_body(MARKER)) is True


def test_from_gate_green_when_all_pass(tmp_path):
    path = _results_file(tmp_path, {"a03-x": {"pass": True}})
    assert cli._tests_red_from_gate("o/r", 1, path, gh=_gh_with_body(MARKER)) is False


def test_from_gate_no_marker_exits(tmp_path):
    path = _results_file(tmp_path, {"a03-x": {"pass": True}})
    with pytest.raises(SystemExit):
        cli._tests_red_from_gate("o/r", 1, path, gh=_gh_with_body("no marker"))


def test_from_gate_unresolved_token_exits(tmp_path):
    path = _results_file(tmp_path, {"a03-x": {"pass": True}})
    with pytest.raises(SystemExit):
        cli._tests_red_from_gate(
            "o/r", 1, path, gh=_gh_with_body("<!-- conductor-assertions: A9 -->")
        )


def test_from_gate_missing_results_exits(tmp_path):
    with pytest.raises(SystemExit):
        cli._tests_red_from_gate(
            "o/r", 1, str(tmp_path / "absent.json"), gh=_gh_with_body(MARKER)
        )


def test_reconcile_parser_rejects_tests_red_with_from_gate():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "1", "--tests-red", "--from-gate"])


def test_phase_done_parser_wires_defaults():
    parser = cli._build_parser()
    args = parser.parse_args(["phase-done", "7", "--plan", "p.md"])
    assert args.issue == 7 and args.plan == "p.md"
    assert args.no_gate_check is False and args.results is None
