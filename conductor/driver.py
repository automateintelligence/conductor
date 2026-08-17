"""`conductor driver install|status` — unconditional Tier-B install + the operator's
on-demand health signal (spec Phase 6, reviews B-3/B-6; frozen A13/A14).

Why a tested module, not skill prose: the 2026-07-05 live run stalled SILENTLY because
durability was a judgment call ("if the response does not confirm persistence…") and
health was a prose log-tail. `install` is the fail-closed default — always write the
resume script AND the crontab lines for an unattended run, no durability judgment.
`status` answers, honestly and mechanically: does a durable driver exist (crontab
marker or a matching harness scheduled task), and did recent fires fail?

Fail-closed grain throughout:
- durability evidence must MATCH this project (marker path / task cwd) — mere file
  existence or another project's marker never false-greens the signal;
- an unparseable scheduled_tasks.json is NOT durability evidence;
- a failing log line whose timestamp cannot be parsed counts as RECENT (toward
  reporting), never silently aged out.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import subprocess
import sys

from conductor import resume_script

_RECENT_HOURS_ENV = "CONDUCTOR_DRIVER_RECENT_HOURS"
_RECENT_HOURS_DEFAULT = 24.0
_FIRE_END_RE = re.compile(r"fire-end rc=(\d+)")
_FIRE_START = "fire-start"
# Every tag the generated driver prints when it REFUSES to fire — each one is a loud
# non-zero exit, none of them is a skip. One tuple, so a new fail-loud path in the template
# has exactly one place to be registered here; `driver-unresolved` alone left the others
# (locking broken or unavailable, world-writable env file, missing worktree) reading as
# "recent fires clean".
_FAIL_TAGS = (
    "driver-unresolved",
    "lock-unavailable",
    "env-unsafe",
    "worktree-missing",
)
_SKIP_RE = re.compile(r"skip reason=(\S+)")
# How long an unmatched `fire-start` may stay in flight before status calls it stalled. A
# hung fire (a headless `-p` blocked on a permission prompt it cannot answer) and a long
# phase produce the SAME log shape, so elapsed time is the only honest discriminator.
_STALL_HOURS_ENV = "CONDUCTOR_DRIVER_STALL_HOURS"
_STALL_HOURS_DEFAULT = 6.0
# How many CONSECUTIVE lock-held skips with no fire in flight count as a stranded lock.
# Two, not one: between winning the lock and printing `fire-start` a fire runs the whole
# spec done-gate, so ONE heartbeat can legitimately land in that window and see lock-held
# with nothing in the log yet. Two heartbeats (20 min apart) landing in the same few
# seconds is not a thing; a stranded lock skips every fire from then on.
_STRANDED_LOCK_RUN = 2
# Only this many trailing log lines are considered "the recent tail" — the recency
# window does the real filtering; this just bounds work on a long-lived log.
_TAIL_LINES = 500


def _crontab_lines() -> list[str]:
    """The current user crontab for READING durability; rc≠0 or empty = no crontab. A
    machine with no `crontab` binary at all simply has no crontab leg — status must
    still consult the scheduled-task leg, not traceback."""
    try:
        proc = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=30
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _scheduled_tasks_file() -> str:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(cfg, "scheduled_tasks.json")


def _scheduled_task_matches(root: str) -> bool:
    """Does a harness scheduled task durably drive THIS project? An entry counts only
    when its prompt is /conductor:autodev AND its cwd/project field points at `root`.
    The file merely EXISTING is not durability evidence — a stale or unrelated task
    would false-green the health signal. Unparseable/unmatchable → False, fail-closed."""
    path = _scheduled_tasks_file()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = None
        for key in ("tasks", "scheduled_tasks", "schedules"):
            value = data.get(key)
            if isinstance(value, list):
                entries = value
                break
        if entries is None:
            return False
    else:
        return False
    # HERMETICITY INVARIANT (frozen A13): the frozen no-durable fixture does NOT
    # isolate this file, so only the exact-project match below keeps the frozen gate
    # independent of real machine state. NEVER loosen this to a prompt-only or
    # basename match — a real scheduled task on the dev machine would then
    # false-green the frozen "no durable driver" test.
    want = os.path.normpath(root)
    want_real = os.path.realpath(root)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("prompt", "")).strip() != "/conductor:autodev":
            continue
        for field in ("cwd", "project", "workingDirectory", "working_directory"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                continue
            cand = os.path.expanduser(value)
            if os.path.normpath(cand) == want or os.path.realpath(cand) == want_real:
                return True
    return False


def _hours(env: str, default: float) -> float:
    """A positive-hours window from the environment; any malformed or non-finite override
    (unparseable, nan, inf, non-positive) degrades to the default rather than crashing
    timedelta."""
    raw = os.environ.get(env, "")
    try:
        hours = float(raw) if raw else default
    except ValueError:
        return default
    if not math.isfinite(hours) or hours <= 0:
        return default
    return hours


def _recent_hours() -> float:
    return _hours(_RECENT_HOURS_ENV, _RECENT_HOURS_DEFAULT)


def _timestamp(line: str) -> datetime.datetime | None:
    """The line's LEADING ISO timestamp, tz-aware; None when it cannot be parsed."""
    token = line.split(maxsplit=1)[0] if line.split() else ""
    try:
        ts = datetime.datetime.fromisoformat(token)
    except ValueError:
        return None
    return ts.astimezone() if ts.tzinfo is None else ts


def _is_recent(line: str, now: datetime.datetime, hours: float) -> bool:
    """A line is recent when its LEADING ISO timestamp is within the window. A
    timestamp that cannot be parsed counts as recent — fail-closed toward reporting."""
    ts = _timestamp(line)
    if ts is None:
        return True
    return (now - ts) <= datetime.timedelta(hours=hours)


def _recent_failures(lines: list[str]) -> list[str]:
    """The recent fail-loud tag lines and `fire-end rc=<non-zero>` lines, verbatim."""
    now = datetime.datetime.now().astimezone()
    hours = _recent_hours()
    failures = []
    for line in lines:
        if not line.strip():
            continue
        m = _FIRE_END_RE.search(line)
        failing = any(tag in line for tag in _FAIL_TAGS) or (
            m is not None and int(m.group(1)) != 0
        )
        if failing and _is_recent(line, now, hours):
            failures.append(line)
    return failures


def _blockages(lines: list[str]) -> list[str]:
    """Stalls that NO single log line announces — read off the SEQUENCE of fire brackets
    and `skip reason=` lines instead.

    `_recent_failures` only recognises lines that declare their own failure, so a run
    skipping EVERY fire reported "recent fires clean". Two shapes have to surface:

    - STRANDED LOCK: `_STRANDED_LOCK_RUN`+ consecutive `skip reason=lock-held` with no fire
      in flight to hold it. That is what a leaked lock descriptor looks like — a descendant
      of a finished fire keeping the open-file-description (and its flock) alive — and what
      a stale holder from a SIGKILLed fire's descendant looks like. Zero headless progress.
    - A FIRE THAT NEVER ENDED: a `fire-start` with no `fire-end`, either stepped over by a
      later lock acquisition (another `fire-start`, or a `gate-green` skip — both come
      AFTER flock succeeds), meaning that fire died without logging its rc; or still in
      flight past the stall window, meaning it is hung rather than merely slow.

    A `fire-start` followed only by lock-held skips inside the stall window is NOT reported:
    that is a long phase and later fires deferring to it correctly. Crying wolf there would
    make the one signal an operator reads worthless."""
    now = datetime.datetime.now().astimezone()
    recent = _recent_hours()
    stall = _hours(_STALL_HOURS_ENV, _STALL_HOURS_DEFAULT)
    findings: list[str] = []
    in_flight: str | None = None
    stranded: list[str] = []

    def never_ended(line: str, why: str) -> str:
        return f"driver: a fire never logged a `fire-end` ({why}):\n{line}"

    def flush_stranded() -> None:
        # Recency is judged on the LAST skip in the run: an unbroken run that a later
        # `fire-end` resolved, or one that stopped days ago, is history, not the signal.
        if len(stranded) >= _STRANDED_LOCK_RUN and _is_recent(
            stranded[-1], now, recent
        ):
            findings.append(
                f"driver: {len(stranded)} consecutive fires skipped `lock-held` with no "
                f"fire in flight to hold the lock — the run is making ZERO headless "
                f"progress (a leaked lock descriptor or a stale holder):\n"
                + "\n".join(stranded)
            )
        stranded.clear()

    for line in lines:
        if not line.strip():
            continue
        if _FIRE_START in line:
            flush_stranded()
            if in_flight is not None:
                findings.append(never_ended(in_flight, "a later fire took the lock"))
            in_flight = line
        elif _FIRE_END_RE.search(line):
            flush_stranded()
            in_flight = None
        elif (m := _SKIP_RE.search(line)) is not None:
            if m.group(1) == "lock-held":
                if in_flight is None:
                    stranded.append(line)
                # else: a fire holds the lock and later fires defer — healthy. Whether that
                # fire has held it too long is the stall check below.
            else:
                # gate-green and every other skip happen only AFTER flock succeeds, so any
                # fire still "in flight" per the log is gone.
                flush_stranded()
                if in_flight is not None:
                    findings.append(
                        never_ended(in_flight, "the lock was re-acquired after it")
                    )
                    in_flight = None
    flush_stranded()
    if in_flight is not None:
        # No recency filter here, deliberately: nothing in the log RESOLVED this fire, so it
        # is not aged-out history. The stall window is the filter, and an unparseable
        # timestamp reports — fail-closed, same as _is_recent.
        started = _timestamp(in_flight)
        if started is None or (now - started) > datetime.timedelta(hours=stall):
            findings.append(
                never_ended(in_flight, f"still in flight after more than {stall:g}h")
            )
    return findings


def status(project: str) -> int:
    """Exit 0 iff a durable driver exists for `project` AND its recent fires are clean.
    Not durable → print why, exit 1. Durable but recent failures → print each offending
    line VERBATIM (named, not just counted), exit 1. Durable with no log at all is a
    driver with no fires yet — healthy.

    "Clean" means BOTH per-line failures (`_recent_failures`) and sequence-level blockage
    (`_blockages`): a run that is skipping every fire, or holding a fire that never ended,
    is not clean no matter how successful each individual line looks."""
    root = resume_script.main_root(project)
    marker = resume_script.cron_marker(root)
    # An ACTIVE crontab entry only: a commented-out/disabled line that still carries
    # the marker is not a durable driver and must not false-green the signal.
    if any(marker in ln and not ln.lstrip().startswith("#") for ln in _crontab_lines()):
        leg = "crontab marker"
    elif _scheduled_task_matches(root):
        leg = "scheduled task"
    else:
        print(
            f"driver: NOT durable — no crontab line carrying '{marker}' and no "
            f"scheduled_tasks.json entry driving {root} with /conductor:autodev.\n"
            f"Install one: conductor driver install --worktree <run-worktree>"
        )
        return 1
    log_path = os.path.join(root, ".conductor", "resume-autodev.log")
    if not os.path.isfile(log_path):
        print(f"driver: durable ({leg}), no fires logged yet")
        return 0
    with open(log_path, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-_TAIL_LINES:]
    failures = _recent_failures(tail)
    blockages = _blockages(tail)
    if failures:
        print(
            f"driver: durable ({leg}) but the recent log tail shows "
            f"{len(failures)} failed fire(s):"
        )
        for line in failures:
            print(line)
    for finding in blockages:
        print(finding)
    if failures or blockages:
        return 1
    print(f"driver: durable ({leg}), recent fires clean")
    return 0


def install(project: str, worktree: str) -> int:
    """The fail-closed default for an unattended run — no durability judgment call:
    write the resume script (through `resume-script write`, so its inline-owner-env
    no-clobber guard is respected) and then the marker-tagged crontab lines."""
    root = resume_script.main_root(project)
    out = os.path.join(root, ".conductor", "resume-autodev.sh")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    rc = resume_script.main(
        ["write", "--project", root, "--worktree", worktree, "--out", out]
    )
    if rc != 0:
        return rc
    return resume_script.install_cron(root)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="conductor driver",
        description="Install / health-check the Tier-B unattended resume driver.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("install", help="write the resume script + crontab lines")
    sp.add_argument(
        "--worktree", required=True, help="run worktree the fires resume in"
    )
    sp.add_argument(
        "--project",
        default=None,
        help="any path inside the repo (default: CONDUCTOR_HOME, else cwd)",
    )
    ss = sub.add_parser("status", help="durability + recent-fire health; exit 0 iff ok")
    ss.add_argument("--project", default=None, help="same default as install")
    args = p.parse_args(argv)
    project = args.project or os.environ.get("CONDUCTOR_HOME") or os.getcwd()
    try:
        if args.cmd == "install":
            return install(project, args.worktree)
        return status(project)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        print(f"cannot resolve main root for {project}: {detail or e}", file=sys.stderr)
        return 1
    except resume_script.CrontabReadError as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:
        # e.g. `crontab` binary missing on the install path — name it, never traceback.
        print(f"driver {args.cmd} failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
