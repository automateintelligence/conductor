"""Link the ledger to the done-gate.

A phase issue carries a ``<!-- conductor-assertions: A3,A4 -->`` body marker naming the manifest
assertion ids the phase owns, so ``reconcile --from-gate`` and ``phase-done`` can DERIVE test
state from the runner's ``results.json`` instead of trusting caller-supplied flags — the dogfood
run showed model-reported bookkeeping decays to zero. Plan headings cite spec-numbered tokens
(``A3``) while manifest ids are slugs (``a03-rich-predicate-not-label``); ``match_ids`` bridges
the two, and anything unresolvable is surfaced, never silently green.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MARKER = re.compile(r"<!--\s*conductor-assertions:\s*([^>]*?)\s*-->")
_NUMBERED_TOKEN = re.compile(r"([A-Za-z]+)0*(\d+)")


def assertions_marker(tokens: list[str]) -> str:
    return f"<!-- conductor-assertions: {','.join(tokens)} -->"


def read_assertion_tokens(body: str | None) -> list[str]:
    m = _MARKER.search(body or "")
    if not m:
        return []
    return [t for t in re.split(r"[,/\s]+", m.group(1)) if t]


def upsert_marker(body: str | None, tokens: list[str]) -> str | None:
    """Body with the marker for ``tokens`` appended or replaced-in-place; None if the body
    already carries exactly this marker (idempotent re-runs never rewrite the issue)."""
    body = body or ""
    marker = assertions_marker(tokens)
    if _MARKER.search(body):
        new = _MARKER.sub(marker, body, count=1)
    elif body.strip():
        new = f"{body.rstrip()}\n\n{marker}"
    else:
        new = marker
    return None if new == body else new


def match_ids(token: str, ids: list[str]) -> set[str]:
    """Result ids a marker token names: exact, else case-insensitive exact, else the
    letters+number prefix rule — ``A3`` matches ``a3``/``a03-…``/``a3-…`` but never
    ``a30-…`` (the number must end at a ``-``/``_`` or the end of the id)."""
    exact = {i for i in ids if i == token}
    if exact:
        return exact
    ci = {i for i in ids if i.lower() == token.lower()}
    if ci:
        return ci
    m = _NUMBERED_TOKEN.fullmatch(token)
    if not m:
        return set()
    pat = re.compile(rf"^{re.escape(m.group(1).lower())}0*{int(m.group(2))}(?:[-_]|$)")
    return {i for i in ids if pat.match(i.lower())}


def tests_red_from_results(
    tokens: list[str], results: dict[str, Any]
) -> dict[str, Any]:
    """Per-phase test state derived from a runner ``results.json`` dict (id -> {pass, ...}).
    A matched id whose ``pass`` is not True counts red. Fail-closed reporting: an
    unresolvable token lands in ``unresolved`` and a token matching MORE than one id lands
    in ``ambiguous`` (codex #1: a broken mapping must never silently read as green) —
    callers must reject on either."""
    ids = list(results.keys())
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    red_ids: set[str] = set()
    matched: dict[str, list[str]] = {}
    for token in tokens:
        found = sorted(match_ids(token, ids))
        if not found:
            unresolved.append(token)
            continue
        if len(found) > 1:  # exact match returns exactly one by construction
            ambiguous[token] = found
            continue
        matched[token] = found
        red_ids.update(i for i in found if results[i].get("pass") is not True)
    return {
        "red": bool(red_ids),
        "red_ids": sorted(red_ids),
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "matched": matched,
    }


def remove_marker(body: str | None) -> str | None:
    """Body with the marker removed, or None if there was no marker (nothing to write).
    Used when a plan explicitly declares a phase's assertions empty — a stale marker must
    not keep gating the phase against ids the plan no longer owns (codex #2)."""
    body = body or ""
    if not _MARKER.search(body):
        return None
    return _MARKER.sub("", body).rstrip()


def load_results(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)
