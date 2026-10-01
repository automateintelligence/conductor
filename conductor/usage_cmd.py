"""``conductor usage ingest`` — record one worker fire's token usage from the driver log.

The cron driver calls this once after each worker fire, passing the byte offset the log had
before the fire started, so only that fire's slice is parsed. The result is one ``worker``
dispatch record appended to the run (sustained-context spec §3). Derived data only: nothing
that gates a merge reads it, so any failure here is reported and exits 1 for the driver to log
and ignore.

The output lines never contain the driver's own markers (``fire-end``, ``driver-unresolved``):
``conductor driver status`` scans the same log for them, and echoing one would corrupt its read.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time

from conductor import dispatches
from conductor.core import resolve
from conductor.hosts import base as hostbase

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64

_TIMEOUT_RCS = (124, 137)
_PHASE_RE = re.compile(r"phase issue #(\d+)")
_HEAD_RE = re.compile(r"\*\*Last unit:\*\*\s*\S+?\.\.(\S+)")


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_slice(log: str, offset: int) -> str:
    with open(log, "rb") as handle:
        handle.seek(max(0, offset))
        return handle.read().decode("utf-8", errors="replace")


def _outcome(rc: int) -> str:
    if rc in _TIMEOUT_RCS:
        return "timeout"
    return "error" if rc != 0 else "ok"


def _phase_and_head(project: str, wall_s: float) -> tuple[str | None, str | None]:
    """Phase id and head sha from ``handoff.md``, only if this fire (or a later write) wrote it."""
    path = os.path.join(project, ".conductor", "handoff.md")
    try:
        if os.stat(path).st_mtime < time.time() - wall_s - 1:
            return None, None
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, None
    phase = _PHASE_RE.search(text)
    head = _HEAD_RE.search(text)
    return (phase.group(1) if phase else None, head.group(1) if head else None)


def _reason(exc: BaseException) -> str:
    """One short token: the exception class, never free text from the log or the repo."""
    return type(exc).__name__


def _ingest(args: argparse.Namespace) -> int:
    try:
        adapter = hostbase.load(args.host)
        usage = adapter.usage_from_output(_read_slice(args.log, args.offset))
        phase_id, head_sha = _phase_and_head(args.project, args.wall_s)
        entry = dispatches.make(
            host=args.host,
            role="worker",
            phase_id=phase_id,
            head_sha=head_sha,
            usage=usage,
            wall_s=args.wall_s,
            outcome=_outcome(args.rc),
            note="no-usage-in-output" if usage.input_tokens is None else None,
        )
        found = resolve.resolve(start=args.project)
        dispatches.append(found.state_root, found.run_key, entry)
    except Exception as exc:  # noqa: BLE001 — derived data must never fail the fire
        print(f"{_ts()} usage-unrecorded reason={_reason(exc)}")
        return EXIT_FAIL
    inp = "null" if usage.input_tokens is None else usage.input_tokens
    out = "null" if usage.output_tokens is None else usage.output_tokens
    print(
        f"{_ts()} usage-recorded role=worker phase={phase_id or '-'} "
        f"input={inp} output={out}"
    )
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conductor usage")
    sub = parser.add_subparsers(dest="verb", required=True)
    ing = sub.add_parser("ingest", help="record one worker fire from the driver log")
    ing.add_argument("--project", required=True)
    ing.add_argument("--host", required=True, choices=hostbase.HOST_IDS)
    ing.add_argument("--log", required=True)
    ing.add_argument("--offset", required=True, type=int)
    ing.add_argument("--wall-s", required=True, type=float, dest="wall_s")
    ing.add_argument("--rc", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    return _ingest(args)


if __name__ == "__main__":
    sys.exit(main())
