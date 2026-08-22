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
from conductor.core import locks
from conductor.hosts import base, runhost

_RECENT_HOURS_ENV = "CONDUCTOR_DRIVER_RECENT_HOURS"
_RECENT_HOURS_DEFAULT = 24.0
_FIRE_END_RE = re.compile(r"fire-end rc=(\d+)")
#: Log markers the generated driver writes when a fire could not do its job. DECLARED, because
#: the set is what `status` can see and anything outside it is a stall reported as clean:
#: `plugin-list-timeout` is written BEFORE `fire-start` and before the flock, so when the Codex
#: plugin lookup is cut off it is the only line that fire ever writes — and status matching just
#: two shapes greened exactly that driver. A marker the driver emits and this list omits is a
#: silent stall by construction, so they are added together.
_FAILURE_MARKERS = (
    "driver-unresolved",
    "plugin-list-timeout",
    "plugin-root-unverified",
    # The fire's own silence bound expiring. It is written BETWEEN `fire-start` and
    # `fire-end rc=`, so `fire-end rc=124|137` would also be seen — but only the marker says
    # WHICH failure it was, and only the marker survives a driver killed before it could write
    # its `fire-end` line at all.
    "fire-timeout",
    # The fire ran with no supervisor because `ps` did not resolve, so nothing bounded it. That
    # is the pre-fix behaviour, deliberately degraded to rather than a false kill — and it is a
    # stall waiting to happen, so status has to see it.
    "fire-unsupervised",
)
# Only this many trailing log lines are considered "the recent tail" — the recency
# window does the real filtering; this just bounds work on a long-lived log.
_TAIL_LINES = 500

#: The skill a durable driver fires, as a host-NEUTRAL plugin-qualified name. It is never
#: written as an invocation here: each host spells one differently, and the adapter's
#: `native_invocation` is the single place that knows how. Comparing against one host's
#: spelling is a comparison that cannot match on the other, and the leg it guards then
#: reports "not durable" for a driver that is durable — or, in the message below, tells an
#: operator to look for a prompt their host would never write.
_AUTODEV_SKILL = "conductor:autodev"

#: How long a second writer waits for the first to finish before refusing. ONE value shared with
#: `resume_script`, which is the other documented writer of the same file: two timeouts on one
#: lock is two answers to "how long is a stuck holder".
INSTALL_LOCK_TIMEOUT_S = resume_script.INSTALL_LOCK_TIMEOUT_S

#: The advisory lock serializing every writer of this project's driver script. Defined in
#: `resume_script` because the lock is keyed on the FILE, and that module owns the file — see
#: `resume_script.install_lock_for`.
install_lock_path = resume_script.install_lock_path


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


def _scheduled_task_matches(
    root: str, path: str | None, adapter: base.HostAdapter
) -> bool:
    """Does a harness scheduled task durably drive THIS project? An entry counts only
    when its prompt is `adapter`'s own rendering of `_AUTODEV_SKILL` AND its cwd/project
    field points at `root`. The file merely EXISTING is not durability evidence — a stale
    or unrelated task would false-green the health signal. Unparseable/unmatchable → False,
    fail-closed.

    `path` is the RUN'S host's scheduled-task file, or None for a host that has no such
    mechanism. None means the leg does not exist for this run — not that it is empty. A Codex
    run sitting on a machine where Claude has a task registered is not driven by that task:
    nothing fires its prompt into codex, so counting it would false-green the only signal an
    operator has that an unattended run will actually resume.

    `adapter` is the RUN'S host's adapter, and it is what renders the prompt to match. A
    literal here is a Claude spelling: it can only ever match a Claude-hosted task, so any
    other host's durable task reads as absent no matter what it says."""
    if not path or not os.path.isfile(path):
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
    # EXACT-PROJECT INVARIANT (frozen A13): NEVER loosen this to a prompt-only or
    # basename match. A13 pins BOTH directions off one seeded harness file — the
    # entry naming the claude project must green it, the entry naming the codex
    # project must not green that one — so a looser match would false-green a run
    # whose host cannot be driven by that task at all, and any unrelated task left
    # on an operator's machine would report a stalled run as healthy.
    want = os.path.normpath(root)
    want_real = os.path.realpath(root)
    want_prompt = adapter.native_invocation(_AUTODEV_SKILL)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("prompt", "")).strip() != want_prompt:
            continue
        for field in ("cwd", "project", "workingDirectory", "working_directory"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                continue
            cand = os.path.expanduser(value)
            if os.path.normpath(cand) == want or os.path.realpath(cand) == want_real:
                return True
    return False


def _not_durable_reason(
    root: str, marker: str, tasks_file: str | None, adapter: base.HostAdapter
) -> str:
    """Why no durable driver was found, naming ONLY the legs this host actually has.

    Every host-specific noun is the adapter's: the harness file this host reads, and the
    prompt as this host writes it. Reporting a missing scheduled-task entry to a Codex
    operator sends them looking for a file their host never reads, and reporting a Claude
    slash command as the prompt to look for names a line their harness would never have
    written — in both cases the one actionable sentence the failure carries is wrong.

    Separate from `status` so the wording can be checked for EVERY host: which hosts have a
    scheduled-task file at all is a different fact, wired in `status` and covered there.
    """
    missing = f"no crontab line carrying '{marker}'"
    if tasks_file:
        missing += (
            f" and no {os.path.basename(tasks_file)} entry driving {root} "
            f"with {adapter.native_invocation(_AUTODEV_SKILL)}"
        )
    return (
        f"driver: NOT durable — {missing}.\n"
        f"Install one: conductor driver install --worktree <run-worktree>"
    )


def _recent_hours() -> float:
    """The recency window; any malformed or non-finite override (unparseable, nan, inf,
    non-positive) degrades to the default rather than crashing timedelta."""
    raw = os.environ.get(_RECENT_HOURS_ENV, "")
    try:
        hours = float(raw) if raw else _RECENT_HOURS_DEFAULT
    except ValueError:
        return _RECENT_HOURS_DEFAULT
    if not math.isfinite(hours) or hours <= 0:
        return _RECENT_HOURS_DEFAULT
    return hours


def _is_recent(line: str, now: datetime.datetime, hours: float) -> bool:
    """A line is recent when its LEADING ISO timestamp is within the window. A
    timestamp that cannot be parsed counts as recent — fail-closed toward reporting."""
    token = line.split(maxsplit=1)[0] if line.split() else ""
    try:
        ts = datetime.datetime.fromisoformat(token)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return (now - ts) <= datetime.timedelta(hours=hours)


def _recent_failures(lines: list[str]) -> list[str]:
    """The recent `_FAILURE_MARKERS` / `fire-end rc=<non-zero>` lines, verbatim."""
    now = datetime.datetime.now().astimezone()
    hours = _recent_hours()
    failures = []
    for line in lines:
        if not line.strip():
            continue
        m = _FIRE_END_RE.search(line)
        failing = any(marker in line for marker in _FAILURE_MARKERS) or (
            m is not None and int(m.group(1)) != 0
        )
        if failing and _is_recent(line, now, hours):
            failures.append(line)
    return failures


def status(project: str) -> int:
    """Exit 0 iff a durable driver exists for `project` AND its recent fires are clean.
    Not durable → print why, exit 1. Durable but recent failures → print each offending
    line VERBATIM (named, not just counted), exit 1. Durable with no log at all is a
    driver with no fires yet — healthy."""
    root = resume_script.main_root(project)
    marker = resume_script.cron_marker(root)
    adapter = runhost.adapter(root)
    tasks_file = adapter.scheduled_tasks_file()
    # An ACTIVE crontab entry only: a commented-out/disabled line that still carries
    # the marker is not a durable driver and must not false-green the signal.
    if any(marker in ln and not ln.lstrip().startswith("#") for ln in _crontab_lines()):
        leg = "crontab marker"
    elif _scheduled_task_matches(root, tasks_file, adapter):
        leg = "scheduled task"
    else:
        print(_not_durable_reason(root, marker, tasks_file, adapter))
        return 1
    log_path = os.path.join(root, ".conductor", "resume-autodev.log")
    if not os.path.isfile(log_path):
        print(f"driver: durable ({leg}), no fires logged yet")
        return 0
    with open(log_path, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-_TAIL_LINES:]
    failures = _recent_failures(tail)
    if failures:
        print(
            f"driver: durable ({leg}) but the recent log tail shows "
            f"{len(failures)} failed fire(s):"
        )
        for line in failures:
            print(line)
        return 1
    print(f"driver: durable ({leg}), recent fires clean")
    return 0


def install(project: str, worktree: str, host: str | None = None) -> int:
    """The fail-closed default for an unattended run — no durability judgment call:
    write the resume script (through `resume-script write`, so its inline-owner-env
    no-clobber guard is respected) and then the marker-tagged crontab lines.

    `host` names which host this run's fires spawn. It is the caller's to state because it is
    only knowable one level up: the
    `conductor:start` skill runs ON the host, while every layer below it is a subprocess with
    no marker it can trust (Claude exports `CLAUDECODE` and the plugin-root variable named by
    the Claude adapter's `PLUGIN_ROOT_ENV`, the Codex ground truth records no exported
    analogue, and "neither present" is indistinguishable from a plain shell — so a probe here
    could only ever positively identify claude).

    Omitting it leaves any EXISTING recording alone — a re-install must never move a live run
    onto another host as a side effect, not even via a stray `$CONDUCTOR_HOST` in the operator's
    shell. A run with NO recording gets one anyway, naming the host this install actually
    rendered. Leaving it unrecorded is what let the two disagree: the render honours
    `$CONDUCTOR_HOST`, the next reconcile runs from cron without it, and the run silently
    regenerates back to claude.

    ORDER: the script is rendered and written FIRST, and only a written script is recorded. The
    two are one durable fact and the recording is the half that reroutes everything else — cron
    fires whatever the script says, while `status`, preflight, plan-lint and the merge gate all
    believe the recording. Recording first made a failure split them: the inline-owner-env
    no-clobber guard refused the old driver (rc 2), the recording said codex, and the surviving
    script still fired claude — permanently, because the guard refuses every retry. Rolling the
    record back on failure would fix that case but not a crash between the steps. This order
    fails the other way instead: a crash after the write leaves a new script and an older
    recording, which `resume-script verify` already reports as stale and reconcile regenerates.
    Inconsistent-and-self-healing beats inconsistent-and-stuck.

    SERIALIZED: that ordering argument only holds for ONE install. Two of them naming different
    hosts interleave straight through it — codex writes, claude writes and records, codex
    records — and both return 0 while the script fires claude and `.conductor/host` says codex.
    That state is not the self-healing kind: `resume-script verify` reports it, but `status`
    does not, and nothing reconciles before the next cron tick fires the wrong host. The lock
    covers the decision as well as the writes, because `chosen` reads the recording a competitor
    is about to change.

    The lock is keyed on the driver SCRIPT, not on this entry point, because `install` is not
    the only documented writer of it: the `conductor:start` skill's reconcile regenerates a
    stale driver
    with `conductor resume-script write` (skills/start/SKILL.md), which recreates this exact
    split state through a public path — that write renders the RECORDED host, which is still the
    old one until the line below runs. `resume_script._write` takes the same lock."""
    root = resume_script.main_root(project)
    lock = install_lock_path(root)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:
        with locks.hold(lock, kind="project", timeout=INSTALL_LOCK_TIMEOUT_S):
            return _install_locked(root, worktree, host)
    except locks.LockTimeout as e:
        # Loud, and having changed nothing: the lock is taken before the first write, so a
        # refusal here cannot have replaced a live driver on its way out.
        print(f"driver install: {e}", file=sys.stderr)
        return 1


def _install_locked(root: str, worktree: str, host: str | None) -> int:
    """`install`'s body, under the install lock. Split out so the critical section is exactly
    the durable fact and nothing returns early past the lock's release."""
    # Validate BEFORE anything is written: a typo'd host must not leave a driver behind.
    chosen = (
        base.load(host).id
        if host
        else (runhost.recorded(root) or runhost.resolve(root))
    )
    out = resume_script.driver_script_path(root)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # `--host` explicitly, never via the recording: it is not written yet, and the render must
    # be the host this install decided on rather than the one the project is leaving behind.
    rc = resume_script.main(
        [
            "write",
            "--project",
            root,
            "--worktree",
            worktree,
            "--out",
            out,
            "--host",
            chosen,
        ]
    )
    if rc != 0:
        return rc
    runhost.record(root, chosen)
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
        "--host",
        default=None,
        choices=base.HOST_IDS,
        help="record which host the fires spawn (default: leave the run's recording alone; "
        "an unrecorded run is claude)",
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
            return install(project, args.worktree, args.host)
        return status(project)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        print(f"cannot resolve main root for {project}: {detail or e}", file=sys.stderr)
        return 1
    except resume_script.CrontabReadError as e:
        print(str(e), file=sys.stderr)
        return 1
    except base.UnknownHost as e:
        # A typo'd or unsupported host: name it, never traceback, and never fall back to a
        # host the operator did not ask for.
        print(f"driver {args.cmd} failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        # e.g. `crontab` binary missing on the install path — name it, never traceback.
        print(f"driver {args.cmd} failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
