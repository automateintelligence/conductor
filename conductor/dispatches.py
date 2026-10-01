"""Per-dispatch token accounting in run.json `dispatches` (sustained-context spec §3).

Derived data for the owner's go/no-go on §4/§5. No gate path reads it."""

from __future__ import annotations

import datetime

from conductor.core import runstate, schema
from conductor.hosts import base

UNATTRIBUTED = "unattributed"


def make(
    *,
    host: str,
    role: str,
    phase_id: str | None,
    head_sha: str | None,
    usage: base.Usage,
    wall_s: float,
    outcome: str,
    note: str | None = None,
    now: str | None = None,
) -> dict:
    entry = {
        "host": host,
        "role": role,
        "phase_id": phase_id,
        "head_sha": head_sha,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "wall_s": round(float(wall_s), 3),
        "outcome": outcome,
        "note": note,
        "recorded_at": now or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return schema.validate_dispatch(entry)


def append(state_root: str, run_key: str, entry: dict) -> dict:
    """Append ``entry`` to the run's dispatches under the state lock; returns the committed run."""
    schema.validate_dispatch(entry)

    def mutate(doc: dict) -> dict:
        doc["dispatches"] = [*doc["dispatches"], entry]
        return doc

    return runstate.update(state_root, run_key, mutate)


def phase_totals(dispatches: list[dict]) -> dict[str, dict[str, dict]]:
    """Sum dispatches per phase and role. ``None`` token values are skipped in the sums and flip
    ``complete`` to False, so unknown usage is never silently counted as zero."""
    out: dict[str, dict[str, dict]] = {}
    for d in dispatches:
        key = d.get("phase_id") or UNATTRIBUTED
        row = out.setdefault(key, {}).setdefault(
            d["role"],
            {
                "dispatches": 0,
                **dict.fromkeys(schema.DISPATCH_TOKEN_FIELDS, 0),
                "wall_s": 0.0,
                "complete": True,
            },
        )
        row["dispatches"] += 1
        row["wall_s"] = round(row["wall_s"] + float(d["wall_s"]), 3)
        for f in schema.DISPATCH_TOKEN_FIELDS:
            if d[f] is None:
                row["complete"] = False
            else:
                row[f] += d[f]
    return out
