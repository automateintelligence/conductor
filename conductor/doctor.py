"""``conductor doctor relocation`` — refuse to call a checkout safe to move while run artifacts
still live under it.

This is a READ-ONLY scan. It moves nothing, renames nothing, and writes nothing anywhere: every
git invocation carries ``--no-optional-locks`` so not even an index stat-cache refresh can touch
the tree being judged, and the crontab is only ever read. The command's whole product is an exit
code and a report.

WHAT IT ASKS, AND WHY IN TWO CLASSES
------------------------------------
``docs/superpowers/specs/2026-08-12-conductor-source-decommission-design.md`` splits the
predicates, and this command keeps the split because the two halves gate different events on
different schedules:

* **Quiesce conditions** — a live owner process, a held driver fire lock, an installed schedule
  (crontab OR harness scheduled task), a registered linked worktree. These ask *is anything using
  this path right now*. They gate the MOVE, they are checked inside a declared quiesce window,
  and a failure is rescheduled rather than remediated. They are what this command refuses on by
  default, and they are what A-DH-5 governs.

  EACH OF THE TWO "SOMETHING IS RUNNING" PREDICATES SEES ONLY HALF, WHICH IS WHY THERE ARE TWO.
  ``live-owner`` reads ``owner.json``, and only ``conductor heartbeat`` writes one — a driver
  fired straight from cron records no ownership at all and was invisible to it.
  ``driver-fire-lock`` reads the kernel's lock table for the ``flock`` that driver holds for the
  whole of its fire. Likewise ``installed-schedule`` used to read the crontab alone, so a run
  driven by a harness scheduled task had no installed schedule this scan could see. With a fire
  lock actively held, no owner record and no cron line, the scan printed CLEAR.
* **Loss-risk gates** — a commit carried by no remote, untracked or ignored content with no
  second copy. These ask *would anything be destroyed that exists in no second place*. They gate
  DELETION of the quarantined copy, which the design puts a week after the move. They are
  reported here always, and refused on only under ``--strict``.

The split is not cosmetic and the default is not laxity. A working checkout with untracked run
state and an unpushed branch is the normal condition of a live project — the design says so in as
many words, and re-tightening those into absolute invariants produces a gate that goes red
whenever anyone is working, which gates nothing. Meanwhile a checkout with a live owner or an
installed cron line is unsafe to move *this minute*, whatever its git state.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not scan ``/proc/*/cwd``. The design already reframed that predicate for the reason that
sinks it: 59 transient processes held a cwd under the checkout, *including the session taking the
reading*, so a cwd scan reports the observer as the blocker and can never distinguish an editor
from a driver. What makes a process durable here is that a run RECORDS it as the owner, and that
is what ``live-owner`` reads.

It does not sweep external configuration for the path (the design's P4) or smoke-test the
installed plugin (P6). Those are decommission-checklist work with no bearing on whether a run
artifact is live beneath the checkout.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import subprocess
import sys
from typing import NamedTuple

from conductor import resume_script
from conductor.core import ownership, resolve
from conductor.hosts import base as hostbase

_GIT_TIMEOUT = 120.0

QUIESCE = "quiesce"
LOSS_RISK = "loss-risk"

#: The lock the generated driver holds — `flock -n 9` on `$PROJECT/.conductor/resume.lock` — for
#: the WHOLE of one fire, so that a second cron tick exits 0 rather than double-driving. It is the
#: only durable "something is running here right now" signal a CRON-LAUNCHED driver leaves: that
#: path writes no ownership record at all, so `live-owner` cannot see it.
FIRE_LOCK_NAME = "resume.lock"

#: The kernel's own lock table. Read, never opened for locking: taking the lock to test it would
#: contend with the holder this scan exists to detect, and `flock` on a free file would briefly
#: exclude a driver that was about to start.
PROC_LOCKS = "/proc/locks"

#: Keys a harness scheduled-task entry uses for the directory it runs in. Same set
#: `conductor.driver` reads, for the same reason: an entry that names a path is an entry that
#: fires at that path.
_TASK_DIR_FIELDS = ("cwd", "project", "workingDirectory", "working_directory")

#: Shapes a scheduled-task file may take: a bare list, or a mapping under one of these keys.
_TASK_LIST_KEYS = ("tasks", "scheduled_tasks", "schedules")


class Finding(NamedTuple):
    """One reason to refuse, named by the artifact's own path and the command that clears it."""

    predicate: str
    artifact: str
    detail: str
    recovery: str


class Predicate(NamedTuple):
    """One question, its class, and either its findings or the evidence that it is clear."""

    name: str
    kind: str
    findings: tuple[Finding, ...]
    clear_note: str

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


def _git(checkout: str, *args: str) -> subprocess.CompletedProcess[str]:
    """A read-only git call against ``checkout``.

    ``--no-optional-locks`` is load-bearing, not hygiene: ``git status`` otherwise refreshes the
    index stat cache and rewrites ``.git/index``, so a scan whose entire contract is "mutates
    nothing" would mutate the very tree it just refused to touch."""
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", checkout, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )


def _under(candidate: str, roots: tuple[str, ...]) -> bool:
    """Is ``candidate`` the checkout itself or something beneath it? Compared against every
    spelling of the checkout — as given and as resolved — because a crontab line and a worktree
    registration record whichever one was current when they were written."""
    for root in roots:
        if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _spellings(checkout: str) -> tuple[str, ...]:
    absolute = os.path.abspath(checkout)
    real = os.path.realpath(checkout)
    return (absolute,) if absolute == real else (absolute, real)


def _recheck(checkout: str) -> str:
    return f"conductor doctor relocation --checkout {shlex.quote(checkout)}"


# --- quiesce conditions ---------------------------------------------------------------------


def linked_worktrees(checkout: str) -> Predicate:
    """Every linked worktree registered against this checkout's repository.

    A linked worktree is two administrative files pointing at each other by ABSOLUTE path — the
    worktree's ``.git`` file names the main checkout, and ``.git/worktrees/<id>/gitdir`` names the
    worktree. Moving the main checkout dangles both directions at once, which is why the design
    retires worktrees explicitly instead of moving a tree that has any.
    """
    listing = _git(checkout, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return Predicate(
            "linked-worktree",
            QUIESCE,
            (
                Finding(
                    "linked-worktree",
                    os.path.abspath(checkout),
                    "git could not list this repository's worktrees, so whether one is "
                    f"registered under the checkout is unknown: {listing.stderr.strip()}",
                    f"git -C {shlex.quote(checkout)} worktree list --porcelain",
                ),
            ),
            "",
        )
    try:
        main = os.path.realpath(resolve.repo_root(checkout))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return Predicate(
            "linked-worktree",
            QUIESCE,
            (
                Finding(
                    "linked-worktree",
                    os.path.abspath(checkout),
                    f"the main checkout of this repository could not be resolved: {exc}",
                    f"git -C {shlex.quote(checkout)} rev-parse --git-common-dir",
                ),
            ),
            "",
        )
    roots = _spellings(checkout)
    findings: list[Finding] = []
    registered = 0
    for line in listing.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered += 1
        path = line[len("worktree ") :].strip()
        if os.path.realpath(path) == main:
            continue
        nested = " nested under the checkout" if _under(path, roots) else ""
        findings.append(
            Finding(
                "linked-worktree",
                path,
                f"a linked worktree{nested} is registered against this repository. Its "
                "administrative files record absolute paths on both sides of the link, so "
                "relocating the checkout leaves both ends dangling.",
                f"git -C {shlex.quote(checkout)} worktree remove {shlex.quote(path)}   "
                "(only once its own untracked and ignored state is preserved)",
            )
        )
    return Predicate(
        "linked-worktree",
        QUIESCE,
        tuple(findings),
        f"{registered} registered worktree(s), none of them linked",
    )


def scheduled_task_files() -> list[str]:
    """Every host's harness scheduled-task file, deduplicated, in host order.

    ALL hosts, not the project's recorded one. ``conductor driver status`` asks a narrower
    question — "is THIS project's run durably driven" — and rightly consults only the run's own
    host. This asks "would anything still fire out of this path", and a Claude scheduled task
    left over from before a project was repointed at Codex fires just the same.
    """
    files: list[str] = []
    for host_id in hostbase.HOST_IDS:
        try:
            path = hostbase.load(host_id).scheduled_tasks_file()
        except Exception:  # a host this build cannot construct contributes no leg
            continue
        if path and path not in files:
            files.append(path)
    return files


def _task_entries(doc: object) -> list[dict] | None:
    """The entry list inside a scheduled-task document, or ``None`` when its shape is unknown.

    ``None`` is not "empty": an unrecognised shape means the file may well carry a task naming
    this checkout and this build cannot see it, which is a refusal rather than a clearance."""
    if isinstance(doc, list):
        return [entry for entry in doc if isinstance(entry, dict)]
    if isinstance(doc, dict):
        for key in _TASK_LIST_KEYS:
            value = doc.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return None


def _task_names_checkout(entry: dict, roots: tuple[str, ...]) -> str | None:
    """The path in this entry that sits under the checkout, or ``None``.

    Two ways an entry names a path, and both count. Its directory field IS a path, so it is
    resolved (``~`` expanded, made absolute) before comparison. Its prompt or command is free
    text that may merely CONTAIN one, so the checkout's own spellings are looked for inside it —
    a task whose prompt is the host's own rendering of the autodev skill, aimed at the old path,
    is as live as one whose cwd field says so. (How each host spells that prompt is the adapter
    layer's business and is deliberately not repeated here: this predicate matches on the PATH,
    which is the same in either spelling.)"""
    for field in _TASK_DIR_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value:
            resolved = os.path.abspath(os.path.expanduser(value))
            if _under(resolved, roots):
                return resolved
    for value in entry.values():
        if not isinstance(value, str):
            continue
        for root in roots:
            if root in value:
                return root
    return None


def _scheduled_task_findings(checkout: str, roots: tuple[str, ...]) -> list[Finding]:
    """Harness scheduled tasks that fire at or beneath the checkout, plus the files that could
    not be read — a file this build cannot parse is not evidence that it holds no task."""
    findings: list[Finding] = []
    for path in scheduled_task_files():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError) as exc:
            findings.append(
                Finding(
                    "installed-schedule",
                    path,
                    "a harness scheduled-task file could not be read, so whether a task still "
                    f"fires out of this checkout is unknown: {exc}",
                    f"read {shlex.quote(path)} by hand, then re-run {_recheck(checkout)}",
                )
            )
            continue
        entries = _task_entries(doc)
        if entries is None:
            findings.append(
                Finding(
                    "installed-schedule",
                    path,
                    "a harness scheduled-task file is in a shape this build does not "
                    f"recognise ({type(doc).__name__}), so whether a task still fires out of "
                    "this checkout is unknown",
                    f"read {shlex.quote(path)} by hand, then re-run {_recheck(checkout)}",
                )
            )
            continue
        for entry in entries:
            named = _task_names_checkout(entry, roots)
            if named is None:
                continue
            findings.append(
                Finding(
                    "installed-schedule",
                    named,
                    f"a scheduled task registered in {path} names this checkout and would keep "
                    "firing at the old path after a move:\n      "
                    + json.dumps(entry, sort_keys=True),
                    f"remove that entry from {shlex.quote(path)} through the harness that owns "
                    f"it — conductor never writes it — then re-run {_recheck(checkout)}",
                )
            )
    return findings


def installed_schedules(checkout: str) -> Predicate:
    """Every installed schedule naming a path at or beneath the checkout.

    TWO LEGS, because there are two schedulers. The crontab is read through ``resume_script`` so
    the "no crontab for this user" absence rule is the same one install and uninstall use. The
    harness scheduled-task files are the second leg, and omitting them was a false CLEAR: a run
    driven by a Claude scheduled task rather than a cron line has no crontab entry at all, and
    ``conductor driver status`` has always treated the two as interchangeable evidence of a
    durable driver. Nothing here can write either one.
    """
    roots = _spellings(checkout)
    findings: list[Finding] = []
    table = ""
    try:
        table = resume_script.read_crontab()
    except resume_script.CrontabReadError as exc:
        # Not an early return. An unreadable crontab already blocks, but the OTHER leg still has
        # to be reported: an operator told only "crontab unreadable" would fix that and re-run,
        # and a scheduled task naming this checkout would surface only on the second pass.
        findings.append(
            Finding(
                "installed-schedule",
                os.path.abspath(checkout),
                f"the installed crontab could not be read, so whether a schedule still "
                f"fires out of this checkout is unknown: {exc}",
                "crontab -l",
            )
        )
    for raw in table.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        naming = [token for token in tokens if _under(token, roots)]
        if not naming:
            continue
        # The artifact is the deepest path the line names — the launcher it actually runs, not
        # the bare checkout a marker comment repeats.
        artifact = max(naming, key=len)
        findings.append(
            Finding(
                "installed-schedule",
                artifact,
                "an installed crontab line fires out of this checkout and would keep firing at "
                f"the old path after a move:\n      {line}",
                f"conductor resume-script uninstall-cron --project {shlex.quote(checkout)}",
            )
        )
    findings.extend(_scheduled_task_findings(checkout, roots))
    return Predicate(
        "installed-schedule",
        QUIESCE,
        tuple(findings),
        "no crontab line and no harness scheduled task names this checkout ("
        + ", ".join(scheduled_task_files() or ["no scheduled-task file on this host"])
        + ")",
    )


def _state_root(checkout: str) -> str:
    """This checkout's canonical state root, falling back to the literal path when git cannot
    answer — the same fallback both state-reading predicates need."""
    try:
        return resolve.state_root(checkout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return os.path.join(os.path.abspath(checkout), ".conductor")


def flock_holders(path: str) -> list[tuple[str, int]] | None:
    """``(lock-type, pid)`` for every kernel lock held on ``path``, or ``None`` when this
    platform cannot answer.

    READ-ONLY, AND IT NEVER TAKES THE LOCK. The obvious test — ``flock(fd, LOCK_EX | LOCK_NB)``
    and see whether it fails — acquires the lock whenever it is free, which briefly excludes a
    driver that was about to start, and is a write to shared kernel state from a scan whose whole
    contract is that it mutates nothing. ``/proc/locks`` is the kernel's own table: it names the
    inode and the holding pid, and reading it disturbs nobody.

    A line is ``<n>: [-> ]<TYPE> <ADVISORY|MANDATORY> <READ|WRITE> <pid> <maj:min:inode> …``,
    with ``maj``/``min`` in hex and the inode in decimal. A ``->`` marks a process BLOCKED on the
    lock rather than holding it — it is counted too, because something waiting to drive out of
    this checkout is as much a reason not to move it as something already driving.

    ``None`` (rather than ``[]``) on a platform with no ``/proc/locks``: "I cannot tell" must not
    read as "nobody is there".
    """
    try:
        info = os.stat(path)
        with open(PROC_LOCKS, encoding="utf-8") as handle:
            table = handle.read()
    except OSError:
        return None
    want = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    holders: list[tuple[str, int]] = []
    for line in table.splitlines():
        tokens = line.split()
        if len(tokens) < 6:
            continue
        tokens = tokens[1:]  # the leading "<n>:" index
        if tokens and tokens[0] == "->":
            tokens = tokens[1:]
        if len(tokens) < 5:
            continue
        parts = tokens[4].split(":")
        if len(parts) != 3:
            continue
        try:
            found = (int(parts[0], 16), int(parts[1], 16), int(parts[2]))
            pid = int(tokens[3])
        except ValueError:
            continue
        if found == want and (tokens[0], pid) not in holders:
            holders.append((tokens[0], pid))
    return sorted(holders)


def held_fire_locks(checkout: str) -> Predicate:
    """Contention on the driver's fire lock — the signal a CRON-LAUNCHED driver is the only one
    to leave.

    ``live-owner`` reads ``owner.json``, which the heartbeat verb writes. The generated driver
    fired straight from cron writes no such record: its exclusion is ``flock -n 9`` on
    ``.conductor/resume.lock``, held for the whole fire. So a checkout with a driver mid-fire, no
    owner record and no cron line visible to this user reported CLEAR — the exact false negative
    this scan exists to prevent, since the operator then moves the tree out from under a running
    process.

    A lock file that does not exist blocks nothing: it is created by the first fire, and its mere
    presence afterwards means only that a fire once ran.
    """
    lock = os.path.join(_state_root(checkout), FIRE_LOCK_NAME)
    if not os.path.exists(lock):
        return Predicate(
            "driver-fire-lock",
            QUIESCE,
            (),
            f"no driver fire lock exists at {lock}",
        )
    holders = flock_holders(lock)
    if holders is None:
        return Predicate(
            "driver-fire-lock",
            QUIESCE,
            (
                Finding(
                    "driver-fire-lock",
                    lock,
                    "this driver fire lock exists but whether a process holds it cannot be "
                    f"determined here ({PROC_LOCKS} is unreadable, and testing the lock by "
                    "taking it would disturb the holder), so whether a fire is running under "
                    "this checkout is unknown",
                    f"confirm no driver is running, then re-run {_recheck(checkout)}",
                ),
            ),
            "",
        )
    if not holders:
        return Predicate(
            "driver-fire-lock",
            QUIESCE,
            (),
            f"{lock} exists and no process holds it",
        )
    return Predicate(
        "driver-fire-lock",
        QUIESCE,
        tuple(
            Finding(
                "driver-fire-lock",
                lock,
                f"process {pid} holds a {kind} lock on this checkout's driver fire lock, so a "
                "driver fire is running out of it right now. A cron-launched driver records no "
                "ownership, so this is the only trace it leaves.",
                (
                    f"let the fire finish, or stop process {pid}, then re-run "
                    f"{_recheck(checkout)}"
                    if pid > 0
                    else "let the fire finish — the kernel names no pid for this lock — then "
                    f"re-run {_recheck(checkout)}"
                ),
            )
            for kind, pid in holders
        ),
        "",
    )


def live_owners(checkout: str) -> Predicate:
    """Runs whose ownership record names a process identity that is still alive.

    The record is the durable statement that something is executing this run; a process with a
    cwd here is not. An uninterpretable record blocks as well — "I cannot tell whether anyone is
    working on this run" is not clearance.

    This is HALF the "something is executing here" question and the narrower half: only the
    ``conductor heartbeat`` path writes an ownership record. ``held_fire_locks`` above covers the
    cron-launched driver, which writes none.
    """
    state_root = _state_root(checkout)
    findings: list[Finding] = []
    inspected = 0
    for record in sorted(
        glob.glob(os.path.join(state_root, "runs", "*", "owner.json"))
    ):
        run_key = os.path.basename(os.path.dirname(record))
        inspected += 1
        try:
            owner = ownership.read(state_root, run_key)
        except (ownership.OwnerAmbiguous, ValueError) as exc:
            findings.append(
                Finding(
                    "live-owner",
                    record,
                    f"this run's ownership record cannot be interpreted, so whether a process "
                    f"is still executing it is unknown: {exc}",
                    f"conductor run show --run {run_key}",
                )
            )
            continue
        if owner is None:
            continue
        live = ownership.identity_is_live(owner)
        if live is False:
            continue
        state = "is alive" if live else "cannot be checked"
        findings.append(
            Finding(
                "live-owner",
                owner.wrapper_identity,
                f"run {run_key} records {owner.host} owner identity "
                f"{owner.wrapper_identity}, which {state}. Relocating the checkout under a "
                f"running owner moves the state it is writing to.\n"
                f"      record: {record}",
                f"let the run finish, or stop process {owner.wrapper_identity} and re-run "
                f"{_recheck(checkout)}",
            )
        )
    return Predicate(
        "live-owner",
        QUIESCE,
        tuple(findings),
        f"{inspected} ownership record(s), none naming a live process",
    )


# --- loss-risk gates ------------------------------------------------------------------------


def unpushed_commits(checkout: str) -> Predicate:
    """Commits reachable from a local branch and from no remote-tracking ref.

    ``git log --branches --not --remotes`` is the subject exactly. Never ``@{u}``: a branch with
    no upstream has nothing to be ahead of, so an upstream-based check reports it clean and
    cannot observe the only case that has ever failed here.
    """
    out = _git(checkout, "log", "--branches", "--not", "--remotes", "--oneline")
    if out.returncode != 0:
        return Predicate(
            "unpushed-commits",
            LOSS_RISK,
            (
                Finding(
                    "unpushed-commits",
                    os.path.abspath(checkout),
                    f"git could not answer which commits reach no remote: {out.stderr.strip()}",
                    f"git -C {shlex.quote(checkout)} log --branches --not --remotes --oneline",
                ),
            ),
            "",
        )
    commits = [line for line in out.stdout.splitlines() if line.strip()]
    if not commits:
        return Predicate(
            "unpushed-commits",
            LOSS_RISK,
            (),
            "every local commit is carried by a remote ref",
        )
    shown = "\n".join(f"      {line}" for line in commits[:20])
    if len(commits) > 20:
        shown += f"\n      … and {len(commits) - 20} more"
    return Predicate(
        "unpushed-commits",
        LOSS_RISK,
        (
            Finding(
                "unpushed-commits",
                os.path.abspath(checkout),
                f"{len(commits)} commit(s) are carried by no remote ref and exist only "
                f"here:\n{shown}",
                f"git -C {shlex.quote(checkout)} push <remote> <branch>",
            ),
        ),
        "",
    )


def unpreserved_state(checkout: str) -> Predicate:
    """Untracked and IGNORED content — the paths a remote has never seen.

    ``--ignored`` is the half that matters: ``.conductor/`` is ignored, so the plain ``-uall``
    form cannot see the run state that is the only copy of anything. Its entries are enumerated
    rather than counted, because a count is not a decision.
    """
    out = _git(checkout, "status", "--porcelain", "-uall", "--ignored")
    if out.returncode != 0:
        return Predicate(
            "unpreserved-state",
            LOSS_RISK,
            (
                Finding(
                    "unpreserved-state",
                    os.path.abspath(checkout),
                    f"git could not report untracked and ignored state: {out.stderr.strip()}",
                    f"git -C {shlex.quote(checkout)} status --porcelain -uall --ignored",
                ),
            ),
            "",
        )
    entries = [line[3:] for line in out.stdout.splitlines() if line.strip()]
    if not entries:
        return Predicate(
            "unpreserved-state", LOSS_RISK, (), "no untracked or ignored content"
        )
    run_state = [entry for entry in entries if entry.startswith(".conductor/")]
    listed = "\n".join(f"      {entry}" for entry in run_state[:40])
    if len(run_state) > 40:
        listed += f"\n      … and {len(run_state) - 40} more under .conductor/"
    body = (
        f"{len(entries)} untracked or ignored path(s), of which {len(run_state)} are run state "
        "under .conductor/ that no remote has ever seen"
    )
    if run_state:
        body += ":\n" + listed
    return Predicate(
        "unpreserved-state",
        LOSS_RISK,
        (
            Finding(
                "unpreserved-state",
                os.path.abspath(checkout),
                body,
                f"archive or explicitly discard each path, driven from: git -C "
                f"{shlex.quote(checkout)} status --porcelain -uall --ignored",
            ),
        ),
        "",
    )


# --- the scan ---------------------------------------------------------------------------------


def scan(checkout: str) -> list[Predicate]:
    """Every predicate, evaluated independently so each refusal has its own named reason."""
    return [
        linked_worktrees(checkout),
        installed_schedules(checkout),
        live_owners(checkout),
        held_fire_locks(checkout),
        unpushed_commits(checkout),
        unpreserved_state(checkout),
    ]


def render(checkout: str, predicates: list[Predicate], *, strict: bool) -> str:
    lines = [f"[relocation] checkout: {os.path.abspath(checkout)}"]
    headings = (
        (
            QUIESCE,
            "quiesce conditions — must be clear at the moment of the move",
        ),
        (
            LOSS_RISK,
            "loss-risk gates — must pass before the quarantined copy is deleted"
            + ("" if strict else " (reported, not refused; --strict refuses on them)"),
        ),
    )
    for kind, heading in headings:
        lines.append("")
        lines.append(heading)
        for predicate in predicates:
            if predicate.kind != kind:
                continue
            if not predicate.blocked:
                lines.append(f"  ok      {predicate.name}: {predicate.clear_note}")
                continue
            for finding in predicate.findings:
                lines.append(f"  BLOCKED {finding.predicate}")
                lines.append(f"      artifact: {finding.artifact}")
                lines.append(f"      {finding.detail}")
                lines.append(f"      recovery: {finding.recovery}")
    return "\n".join(lines)


USAGE = "usage: conductor doctor relocation [--checkout <path>] [--strict]"


def relocation(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="conductor doctor relocation",
        description=(
            "Read-only scan: refuse to call a checkout safe to relocate while a run artifact "
            "lives under it. Moves nothing."
        ),
    )
    parser.add_argument(
        "--checkout",
        default=None,
        help="the checkout to judge (default: $CONDUCTOR_HOME, else the current directory)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also refuse on the loss-risk gates, which gate deletion rather than the move",
    )
    args = parser.parse_args(argv)

    checkout = args.checkout or os.environ.get("CONDUCTOR_HOME") or os.getcwd()
    if not os.path.isdir(checkout):
        print(
            f"[relocation] REFUSED: {checkout} is not a directory; nothing was inspected",
            file=sys.stderr,
        )
        return 64

    predicates = scan(checkout)
    print(render(checkout, predicates, strict=args.strict))
    print("")
    # The verdict goes to stderr while the findings go to stdout. Redirected to one file, stdout
    # is block-buffered and stderr is not, so without this the verdict lands mid-report.
    sys.stdout.flush()

    blocking = [
        predicate
        for predicate in predicates
        if predicate.blocked and (args.strict or predicate.kind == QUIESCE)
    ]
    if blocking:
        names = ", ".join(sorted({predicate.name for predicate in blocking}))
        print(
            f"[relocation] REFUSED: {len(blocking)} predicate(s) block relocating "
            f"{os.path.abspath(checkout)} ({names}). Nothing was moved, renamed or written.",
            file=sys.stderr,
        )
        print(f"[relocation] re-check with: {_recheck(checkout)}", file=sys.stderr)
        return 1
    print(
        f"[relocation] CLEAR: no run artifact is live under {os.path.abspath(checkout)}. "
        f"Re-run inside the move window with: {_recheck(checkout)}"
    )
    outstanding = [
        predicate.name
        for predicate in predicates
        if predicate.blocked and predicate.kind == LOSS_RISK
    ]
    if outstanding:
        # Clear to MOVE is not clear to DELETE, and saying only the first would be read as both.
        print(
            f"[relocation] note: {', '.join(outstanding)} still holds content with no second "
            "copy. That gates deleting the quarantined copy, not the move; "
            f"{_recheck(checkout)} --strict refuses on it."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "relocation":
        print(USAGE, file=sys.stderr)
        return 64
    return relocation(arguments[1:])


if __name__ == "__main__":  # pragma: no cover — exercised through bin/conductor
    raise SystemExit(main())
