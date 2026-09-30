"""Each host's machine-readable output -> one `Usage`, replayed from recorded 2026-09-30 probes."""

from __future__ import annotations

import json
import pathlib

from conductor.hosts import base

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_claude_usage_sums_model_usage_not_the_last_call():
    raw = _text("claude-print-json-subagent-2.1.286.json")
    doc = json.loads(raw)
    mu = doc["modelUsage"].values()
    u = base.load("claude").usage_from_output(raw)
    assert u.cached_input_tokens == sum(m["cacheReadInputTokens"] for m in mu)
    assert u.cached_input_tokens != doc["usage"]["cache_read_input_tokens"]
    assert u.input_tokens == sum(
        m["inputTokens"] + m["cacheReadInputTokens"] + m["cacheCreationInputTokens"]
        for m in mu
    )
    assert u.cache_write_tokens == sum(m["cacheCreationInputTokens"] for m in mu)
    assert u.output_tokens == sum(m["outputTokens"] for m in mu)
    assert u.session_id == doc["session_id"] and u.result_text == doc["result"]
    assert u.is_error is False


def test_claude_usage_skips_non_json_lines():
    raw = (
        "SessionEnd hook [node x] failed\n"
        + _text("claude-print-json-2.1.286.json")
        + "\nnoise\n"
    )
    u = base.load("claude").usage_from_output(raw)
    assert u.result_text == "alpha" and u.output_tokens is not None


def test_claude_usage_without_a_result_is_unknown_not_zero():
    u = base.load("claude").usage_from_output("plain text answer\n")
    assert u == base.Usage.unknown()
    assert u.input_tokens is None and u.output_tokens is None


def test_claude_usage_with_a_result_but_no_model_usage_records_null_tokens():
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s",
        }
    )
    u = base.load("claude").usage_from_output(raw)
    assert u.result_text == "ok" and u.input_tokens is None and u.is_error is False


def test_claude_error_result_is_an_error():
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "session_id": "s",
            "modelUsage": {},
        }
    )
    assert base.load("claude").usage_from_output(raw).is_error is True


def test_codex_usage_reads_turn_completed_and_ignores_warning_items():
    raw = _text("codex-exec-json-0.156.1.jsonl")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    turn = [e for e in events if e["type"] == "turn.completed"][-1]["usage"]
    u = base.load("codex").usage_from_output(raw)
    assert u.input_tokens == turn["input_tokens"]
    assert u.cached_input_tokens == turn["cached_input_tokens"]
    assert u.cache_write_tokens == turn["cache_write_input_tokens"]
    assert u.output_tokens == turn["output_tokens"]
    assert u.session_id == events[0]["thread_id"]
    assert u.result_text == "alpha" and u.is_error is False


def test_codex_usage_without_turn_completed_is_an_error_with_null_tokens():
    raw = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    u = base.load("codex").usage_from_output(raw)
    assert u.is_error is True and u.input_tokens is None and u.session_id == "t"


def test_codex_turn_failed_is_an_error():
    raw = (
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"turn.failed","error":{"message":"usage limit"}}\n'
    )
    assert base.load("codex").usage_from_output(raw).is_error is True


def test_a_missing_usage_field_is_null_not_zero():
    raw = (
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}\n'
    )
    u = base.load("codex").usage_from_output(raw)
    assert (
        u.input_tokens == 5
        and u.cached_input_tokens is None
        and u.cache_write_tokens is None
    )
