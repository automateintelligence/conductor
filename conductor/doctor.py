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

* **Quiesce conditions** — a live owner process, an installed schedule, a registered linked
  worktree. These ask *is anything using this path right now*. They gate the MOVE, they are
  checked inside a declared quiesce window, and a failure is rescheduled rather than remediated.
  They are what this command refuses on by default, and they are what A-DH-5 governs.
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
import os
import shlex
import subprocess
import sys
from typing import NamedTuple

from conductor import resume_script
from conductor.core import ownership, resolve

_GIT_TIMEOUT = 120.0

QUIESCE = "quiesce"
LOSS_RISK = "loss-risk"


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


def installed_schedules(checkout: str) -> Predicate:
    """Crontab lines naming a path at or beneath the checkout.

    Read through ``resume_script`` so the "no crontab for this user" absence rule is the same one
    install and uninstall use. Nothing here can write a crontab.
    """
    try:
        table = resume_script.read_crontab()
    except resume_script.CrontabReadError as exc:
        return Predicate(
            "installed-schedule",
            QUIESCE,
            (
                Finding(
                    "installed-schedule",
                    os.path.abspath(checkout),
                    f"the installed crontab could not be read, so whether a schedule still "
                    f"fires out of this checkout is unknown: {exc}",
                    "crontab -l",
                ),
            ),
            "",
        )
    roots = _spellings(checkout)
    findings: list[Finding] = []
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
    return Predicate(
        "installed-schedule",
        QUIESCE,
        tuple(findings),
        "no crontab line names this checkout",
    )


def live_owners(checkout: str) -> Predicate:
    """Runs whose ownership record names a process identity that is still alive.

    The record is the durable statement that something is executing this run; a process with a
    cwd here is not. An uninterpretable record blocks as well — "I cannot tell whether anyone is
    working on this run" is not clearance.
    """
    try:
        state_root = resolve.state_root(checkout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        state_root = os.path.join(os.path.abspath(checkout), ".conductor")
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
        live = ownership.identity_is_live(owner.wrapper_identity)
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
