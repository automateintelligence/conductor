"""Journalled cross-file state writes.

``atomic.write_json_atomic`` makes each file durable on its own, but an operation that touches
``project.json`` and one or more ``run.json`` files can still crash *between* them and leave a
registry mapping a spec to a run key the run record disagrees with. Design §"Failure handling"
therefore requires the journal be written and fsynced first, and every project entry point
complete or reverse an unfinished transaction before reading mappings.

Lifecycle:

    prepare(...)   journal written with state="prepared" and every before/after image
    commit(...)    journal flipped to state="committed" — the point of no return
    apply(...)     after images written, journal removed

``recover`` reverses a ``prepared`` journal (restore the before images) and rolls a ``committed``
one forward (write the after images). Both directions are idempotent, so recovering twice, or
recovering a transaction that had already half-applied, converges on the same state.

This module takes no locks: it is used from inside operations that already hold ``project.lock``
and the relevant ``state.lock``, and taking them again would be a re-entrant acquisition.
"""

from __future__ import annotations

import os
import re

from conductor.core import atomic

SCHEMA_VERSION = 1

_TXN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def txn_dir(state_root: str) -> str:
    """Where journals live: ``<state_root>/transactions``."""
    return os.path.join(state_root, "transactions")


def journal_path(state_root: str, txn_id: str) -> str:
    """The journal file for one transaction."""
    return os.path.join(txn_dir(state_root), f"{txn_id}.json")


def _check_id(txn_id: str) -> None:
    if not isinstance(txn_id, str) or not _TXN_ID_RE.match(txn_id) or ".." in txn_id:
        raise ValueError(f"unsafe transaction id {txn_id!r}")


def _check_entries(entries: list[dict]) -> None:
    if not isinstance(entries, list) or not entries:
        raise ValueError("a transaction needs at least one entry")
    for entry in entries:
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(
                f"transaction entry must be a mapping with 'path': {entry!r}"
            )
        path = entry["path"]
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError(f"transaction entry path must be absolute, got {path!r}")
        before, after = entry.get("before"), entry.get("after")
        for label, value in (("before", before), ("after", after)):
            if value is not None and not isinstance(value, dict):
                raise ValueError(
                    f"transaction entry {label!r} must be a mapping or None"
                )
        if before is None and after is None:
            raise ValueError(
                f"transaction entry for {path!r} is a no-op (before and after None)"
            )


def prepare(state_root: str, txn_id: str, entries: list[dict]) -> str:
    """Record the intended write and fsync it. Targets are untouched. Returns the journal path."""
    _check_id(txn_id)
    _check_entries(entries)
    path = journal_path(state_root, txn_id)
    atomic.write_json_atomic(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "txn_id": txn_id,
            "state": "prepared",
            "entries": [
                {"path": e["path"], "before": e.get("before"), "after": e.get("after")}
                for e in entries
            ],
        },
    )
    return path


def _load_journal(state_root: str, txn_id: str) -> dict:
    doc = atomic.read_json(journal_path(state_root, txn_id))
    if doc is None:
        raise ValueError(
            f"no transaction journal for {txn_id!r} under {txn_dir(state_root)}"
        )
    if doc.get("state") not in ("prepared", "committed"):
        raise ValueError(
            f"transaction {txn_id!r} has unknown state {doc.get('state')!r}"
        )
    return doc


def commit(state_root: str, txn_id: str) -> None:
    """Flip the journal to ``committed``: from here recovery rolls forward, never back."""
    doc = _load_journal(state_root, txn_id)
    doc["state"] = "committed"
    atomic.write_json_atomic(journal_path(state_root, txn_id), doc)


def _write_image(path: str, image: dict | None) -> None:
    if image is None:
        atomic.remove_durably(path)
        return
    atomic.write_json_atomic(path, image)


def apply(state_root: str, txn_id: str) -> None:
    """Write every after image and remove the journal. Only valid once committed."""
    doc = _load_journal(state_root, txn_id)
    if doc["state"] != "committed":
        raise ValueError(
            f"transaction {txn_id!r} is {doc['state']!r}; commit before apply"
        )
    for entry in doc["entries"]:
        _write_image(entry["path"], entry.get("after"))
    atomic.remove_durably(journal_path(state_root, txn_id))


def pending(state_root: str) -> list[str]:
    """Transaction ids with an unfinished journal, sorted."""
    directory = txn_dir(state_root)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(n[: -len(".json")] for n in names if n.endswith(".json"))


def recover(state_root: str) -> list[str]:
    """Complete or reverse every unfinished transaction; return the ids handled, sorted.

    Called by every project entry point *before* reading mappings. A journal that cannot be
    parsed raises rather than being skipped — an unreadable journal means the split-identity
    question is unanswerable, which is a fail-closed condition, not a clean state."""
    handled: list[str] = []
    for txn_id in pending(state_root):
        doc = _load_journal(state_root, txn_id)
        forward = doc["state"] == "committed"
        for entry in doc["entries"]:
            _write_image(
                entry["path"], entry.get("after") if forward else entry.get("before")
            )
        atomic.remove_durably(journal_path(state_root, txn_id))
        handled.append(txn_id)
    return handled
