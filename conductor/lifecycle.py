"""``conductor status | resume | heartbeat | finish`` — the run lifecycle verbs.

One module, four verbs, because all four answer the same question from different distances:
*what is this run's durable state, and what is allowed to happen to it next?* They share the
resolver, the ownership reader, and one write helper, and splitting them would give that helper
four callers in four files with nothing else in common.

WHAT EACH VERB IS

``status``   Read-only. Prints the run record, the owner record and its liveness, and any
             journalled transaction that is still pending. It never recovers a transaction and
             never mutates anything (design §"Migration": *status is read-only… but never
             migrates as a side effect*), which is why it is the one verb here that does not
             call ``resolve.recover_pending``.

``resume``   Returns an eligible run to ``active`` after reconciling. ``checkpointed`` and
             ``blocked`` resume on a bare invocation. ``awaiting-team-merge`` requires explicit
             ``--reactivate``: the design permits reactivating a run whose final pull request is
             still open ("to address team feedback or create a synchronization phase"), but
             doing it on a bare invocation would re-admit unattended phases underneath a human
             who is mid-review. A run the team CLOSED without merging reconciles to ``blocked``
             and resumes bare, which is the case the design wants cheap.

``heartbeat``One fire. Reconciles, then either launches the run's durable driver under exclusive
             ownership or explains why it did not. A run at ``awaiting-team-merge``, ``terminal``
             or ``failed`` owns no schedule, so a heartbeat reaching one is an orphaned schedule
             entry and is reported as such. A ``blocked`` run reconciles and reports only;
             advancing it requires ``resume`` (design §"Project and run identity").

``finish``   Completes the run AFTER the repository team merged the final pull request. It
             verifies from authoritative remote metadata that the pull request is merged, that
             its base is the repository default branch, that its head matches the audited run
             head, and that no review debt remains — and otherwise refuses, printing the pull
             request's URL and current state.

WHAT NO VERB HERE DOES

None of them completes the final default-branch pull request, and none of them can: the only
module that touches it is ``conductor.finalpr``, whose entire subprocess inventory is
``git ls-remote`` and ``gh pr view``. See that module's header for the invariant (A-DH-7).

WRITES ARE JOURNALLED, AND CARRY THEIR LOCK

``_commit`` writes ``run.json`` and ``project.json``'s status mirror as one transaction, under
``project.lock`` then ``state.lock`` — the global order. The ``run.json`` entry carries a
``lock`` hint because ``transaction.recover`` cannot derive a lock path from an opaque target
and would otherwise replay that write with no serialization against the run's own writers
(``docs/reviews/2026-08-10-plan-01-residuals.md``). Writing the mirror in the same transaction
as the record is what that review asked these two verbs for: ``registry.mirror_status`` had one
production caller, so a project whose runs all ended kept a permanently wrong ``project.json``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys

from conductor import branches, finalpr, remote as remote_mod, resume_script
from conductor.core import (
    locks,
    ownership,
    registry,
    resolve,
    runstate,
    schema,
    transaction,
)
from conductor.hosts import base as hostbase
from conductor.hosts import runhost
from conductor.merge_gate import _resolve_repo

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 2
EXIT_NO_RUN = 3
EXIT_USAGE = 64

#: Statuses a heartbeat may fire on. `blocked` is deliberately absent from the LAUNCH set below
#: while counting as active: it reconciles and reports.
_WORK_CAPABLE = ("active", "checkpointed")

_GIT_TIMEOUT = float(os.environ.get("CONDUCTOR_GIT_TIMEOUT", "30"))


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )


# --- shared state helpers -----------------------------------------------------------------


def _commit(
    state_root: str,
    run_key: str,
    *,
    status: str,
    expect_revision: int,
    mutate=lambda doc: doc,
) -> dict:
    """Write ``run.json`` and ``project.json``'s status mirror as ONE journalled transaction.

    ``status`` may equal the current status — ``schema.assert_transition`` allows same-to-same —
    which is how a reconciliation that only records evidence gets the same durability guarantee
    as a status change without pretending to be one.

    COMPARE-AND-SET ON ``expect_revision``, the revision the caller DECIDED on. Every verb here
    reads the run, decides, and writes later; a transition check alone does not protect that
    window, because ``awaiting-team-merge -> active`` is legal — a heartbeat that read ``active``
    would otherwise silently undo a completion that landed after its read. The revision is
    checked again under ``state.lock``, because ``runstate.commit`` writers take only that lock
    and can advance the record after the ``project.lock`` read."""
    with locks.hold(registry.lock_path(state_root), kind="project"):
        transaction.recover(state_root)
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)}; no write "
                "occurred. Create a run first with: conductor run new <spec.md>"
            )
        current = runstate.load(state_root, run_key)
        if current is None:
            raise runstate.RunMissing(
                f"no run record at {runstate.run_path(state_root, run_key)}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        _expect(state_root, run_key, current, expect_revision)
        schema.assert_transition(current["status"], status)
        after = mutate(schema.clone(current))
        after["status"] = status
        after["revision"] = current["revision"] + 1
        after["updated_at"] = _now()
        schema.validate_run(after)
        try:
            new_project = registry.mirror_status(
                schema.clone(project_doc), run_key, status
            )
        except KeyError:
            raise registry.RegistryMissing(
                f"run {run_key!r} has a record at {runstate.run_path(state_root, run_key)} but "
                f"is not registered in {registry.registry_path(state_root)}; no write occurred. "
                f"Inspect both with: conductor run show --run {run_key}"
            ) from None
        new_project["revision"] = project_doc["revision"] + 1
        schema.validate_project(new_project)
        with locks.hold(
            runstate.state_lock_path(state_root, run_key), kind="state", run_key=run_key
        ):
            _expect(
                state_root, run_key, runstate.load(state_root, run_key), expect_revision
            )
            txn_id = f"lifecycle-{run_key}"
            transaction.prepare(
                state_root,
                txn_id,
                [
                    {
                        "path": registry.registry_path(state_root),
                        "before": project_doc,
                        "after": new_project,
                    },
                    {
                        "path": runstate.run_path(state_root, run_key),
                        "before": current,
                        "after": after,
                        # Recovery holds only project.lock; without this hint the replay of a
                        # run.json write serializes against nothing (Plan 01 residuals).
                        "lock": {
                            "path": runstate.state_lock_path(state_root, run_key),
                            "run_key": run_key,
                        },
                    },
                ],
            )
            transaction.commit(state_root, txn_id)
            transaction.apply(state_root, txn_id)
    return after


def _expect(
    state_root: str, run_key: str, current: dict | None, expect_revision: int
) -> None:
    if current is None or current["revision"] != expect_revision:
        found = "no record" if current is None else f"revision {current['revision']}"
        status = "" if current is None else f" ({current['status']})"
        raise runstate.RevisionConflict(
            f"run {run_key!r} was read at revision {expect_revision} but "
            f"{runstate.run_path(state_root, run_key)} now holds {found}{status}; no write "
            f"occurred. Something else changed the run since this verb decided. Inspect it "
            f"with: conductor status --run {run_key}"
        )


def _stamp_reconciled(doc: dict) -> dict:
    doc["last_reconciled_at"] = _now()
    return doc


def _live_owner(state_root: str, run_key: str) -> str | None:
    """A refusal sentence when a live owner holds this run, else ``None``.

    Reads the record rather than acquiring ``owner.lock``: every mutating verb here takes
    ``project.lock`` afterwards, and ``project`` ranks BEFORE ``owner`` in the global order, so
    holding ownership across the write would be a lock-order violation. ``ownership.read`` is
    lock-free by construction and answers the only question a refusal needs."""
    record = ownership.read(state_root, run_key)
    live = None if record is None else ownership.identity_is_live(record)
    if record is None or live is False:
        fire = ownership.running_fire(state_root)
        return (
            f"run {run_key!r} is not free: {fire}; no write occurred." if fire else None
        )
    return (
        f"run {run_key!r} is owned by {record.host} identity {record.wrapper_identity} "
        + ("(live)" if live else "(liveness unknown)")
        + f", recorded at {ownership.record_path(state_root, run_key)}; no write occurred. "
        "Wait for that fire to finish, or prove it exited before disturbing the run."
    )


def _repo_context(repo_root: str) -> tuple[str, str, str]:
    """``(repo, remote, default_branch)`` for THE PROJECT AT ``repo_root`` — all three fail closed.

    ``branches.default_branch`` never substitutes a literal (A-DH-6), so an unresolvable default
    branch propagates as a refusal here rather than becoming a guessed pull-request base.

    EVERY REPOSITORY FACT COMES FROM ``repo_root``, NEVER FROM THE AMBIENT PROJECT. This function
    used to ignore its argument: ``gh repo view`` resolved the repository from the process cwd,
    and ``default_branch``/``remote`` resolved theirs from ``$CONDUCTOR_HOME`` — which
    ``bin/conductor`` exports from the CALLER'S cwd, before ``--project`` has been parsed. So
    ``conductor finish --project /repo/B`` run from inside repo A read A's repository name, asked
    ``gh pr view -R <A>`` whether A's pull request was merged, and then removed B's worktrees,
    deleted B's branches and marked B terminal on the strength of that answer. Forks sharing an
    audited head SHA make that a realistic false positive rather than a theoretical one."""
    repo = _resolve_repo(root=repo_root)
    default = branches.default_branch(repo_root)
    try:
        remote = remote_mod.resolve(repo_root)
    except (
        Exception
    ):  # discovery failure degrades to the historical default, never to empty
        remote = "origin"
    return repo, remote, default


def _project_env(
    repo_root: str, *, owner_identity: str | None = None
) -> dict[str, str]:
    """This process's environment with ``CONDUCTOR_HOME`` re-anchored onto ``repo_root``.

    ``owner_identity`` is the ownership this process has ALREADY taken, handed to the child so
    the child does not block on it. Without it the heartbeat deadlocks against itself: it
    acquires ownership, launches the driver, and the driver's whole job is to refuse to fire
    while this run has a live owner — which is now the heartbeat that launched it. Every fire
    would be skipped, forever, logging the most correct-looking reason available.

    An explicit token, never an inference: the child either carries the exact string its parent
    recorded or it does not. Nothing compares process names or guesses at ancestry.

    ``bin/conductor`` exports ``CONDUCTOR_HOME`` from the caller's cwd before any verb has parsed
    ``--project``, so a child launched from a ``--project``-scoped verb would otherwise inherit
    the WRONG project as its ambient one — a driver fired for run B resolving B's state root from
    the environment of repo A. The child's cwd is already ``repo_root``; this makes the variable
    agree with it."""
    env = {**os.environ, "CONDUCTOR_HOME": repo_root}
    if owner_identity:
        env[ownership.INHERITED_IDENTITY_ENV] = owner_identity
    else:
        env.pop(ownership.INHERITED_IDENTITY_ENV, None)
    return env


# --- status -------------------------------------------------------------------------------


def _owner_report(state_root: str, run_key: str) -> dict:
    try:
        record = ownership.read(state_root, run_key)
    except ownership.OwnerAmbiguous as exc:
        return {"state": "ambiguous", "detail": str(exc)}
    if record is None:
        return {"state": "none"}
    live = ownership.identity_is_live(record)
    return {
        "state": {True: "live", False: "exited", None: "unknown"}[live],
        "host": record.host,
        "tier": record.tier,
        "identity": record.wrapper_identity,
        "acquired_at": record.acquired_at,
    }


def cmd_status(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    run = resolution.run
    raw_github, raw_heartbeat = run.get("github"), run.get("heartbeat")
    github = raw_github if isinstance(raw_github, dict) else {}
    heartbeat = raw_heartbeat if isinstance(raw_heartbeat, dict) else {}
    report = {
        "run_key": resolution.run_key,
        "status": run["status"],
        "generation": run["generation"],
        "spec_path": run["spec_path"],
        "integration_branch": run["integration_branch"],
        "gate_dir": run["gate_dir"],
        "current_phase": run.get("current_phase"),
        "phase_branch": run.get("phase_branch"),
        "worker_host": run.get("worker_host"),
        "reviewer_host": run.get("reviewer_host"),
        "review_policy": run.get("review_policy"),
        "final_pr": github.get("final_pr"),
        "schedule_id": heartbeat.get("schedule_id"),
        "revision": run["revision"],
        "updated_at": run["updated_at"],
        "last_reconciled_at": run.get("last_reconciled_at"),
        "last_checkpoint_at": run.get("last_checkpoint_at"),
        "owner": _owner_report(resolution.state_root, resolution.run_key),
        # READ, never recovered: an unfinished journal is a fact about the run, and completing
        # it here would make the read-only verb the one that mutates state.
        "pending_transactions": transaction.pending_states(resolution.state_root),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_OK
    order = [
        ("run", "run_key"),
        ("status", "status"),
        ("generation", "generation"),
        ("spec", "spec_path"),
        ("integration", "integration_branch"),
        ("gate dir", "gate_dir"),
        ("phase", "current_phase"),
        ("phase branch", "phase_branch"),
        ("worker host", "worker_host"),
        ("reviewer host", "reviewer_host"),
        ("review policy", "review_policy"),
        ("final PR", "final_pr"),
        ("schedule", "schedule_id"),
        ("revision", "revision"),
        ("updated", "updated_at"),
        ("reconciled", "last_reconciled_at"),
        ("checkpoint", "last_checkpoint_at"),
    ]
    width = max(len(label) for label, _ in order)
    for label, key in order:
        value = report[key]
        print(f"{label:<{width}}  {'(none)' if value is None else value}")
    owner = report["owner"]
    detail = (
        "(none)"
        if owner["state"] == "none"
        else (
            owner["detail"]
            if owner["state"] == "ambiguous"
            else f"{owner['host']} identity {owner['identity']} ({owner['state']})"
        )
    )
    print(f"{'owner':<{width}}  {detail}")
    pending = report["pending_transactions"]
    if pending:
        listing = ", ".join(f"{t}={state}" for t, state in sorted(pending.items()))
        print(f"{'pending txn':<{width}}  {listing}")
        print(
            "A journalled transaction is unfinished. Any mutating conductor verb completes or "
            "reverses it; status deliberately does not.",
            file=sys.stderr,
        )
    return EXIT_OK


# --- resume -------------------------------------------------------------------------------


def _final_pr_or_none(
    repo_root: str, run: dict
) -> tuple[finalpr.PullRequest | None, str | None]:
    """The run's final pull request, or ``(None, why-not)``. Never raises: ``resume``'s report
    is better with a reason than absent because the remote was unreachable."""
    try:
        repo, remote, default = _repo_context(repo_root)
        pull, _ = finalpr.reconcile(
            repo_root=repo_root,
            repo=repo,
            remote=remote,
            default_branch=default,
            run=run,
        )
        return pull, None
    except (
        finalpr.FinalPullRequestError,
        branches.DefaultBranchUnresolvable,
        subprocess.SubprocessError,
        RuntimeError,
        OSError,
    ) as exc:
        return None, str(exc)


def cmd_resume(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    run, state_root, key = resolution.run, resolution.state_root, resolution.run_key
    status = run["status"]
    if status in schema.TERMINAL_STATUSES:
        print(
            f"run {key!r} is {status}; no write occurred. A finished run is not resumable — "
            f"start the next generation with: conductor run new {run['spec_path']} --new-run",
            file=sys.stderr,
        )
        return EXIT_FAIL
    if status == "active":
        print(f"run {key} is already active; nothing to resume")
        return EXIT_OK
    busy = _live_owner(state_root, key)
    if busy:
        print(busy, file=sys.stderr)
        return EXIT_FAIL
    if status == "awaiting-team-merge":
        pull, why = _final_pr_or_none(resolution.repo_root, run)
        if pull is not None and pull.merged:
            print(
                f"run {key!r} is awaiting-team-merge and the repository team already merged its "
                f"final pull request {pull.url} ({pull.state}); no write occurred. Complete the "
                f"run instead of resuming it:\n  conductor finish --run {key}",
                file=sys.stderr,
            )
            return EXIT_FAIL
        if not args.reactivate:
            where = (
                f"{pull.url} ({pull.state})"
                if pull is not None
                else f"could not be read ({why})"
            )
            print(
                f"run {key!r} is awaiting-team-merge; its final pull request {where}. No write "
                "occurred. Reactivating admits further unattended phases underneath a review "
                "that is still open, so it takes explicit consent:\n"
                f"  conductor resume --run {key} --reactivate   (team feedback, or a "
                "synchronization phase)\n"
                f"  conductor finish --run {key}                (once the team has merged it)",
                file=sys.stderr,
            )
            return EXIT_FAIL
    after = _commit(
        state_root,
        key,
        status="active",
        expect_revision=run["revision"],
        mutate=_stamp_reconciled,
    )
    print(f"run {key} resumed: {status} -> active (revision {after['revision']})")
    # The per-run schedule is NOT reinstalled here. Design §"Heartbeat and autodev" wants
    # resume to restore it, but per-run heartbeat artifacts (`.conductor/runs/<key>/heartbeat.sh`
    # and its scheduler entry) are not built yet, and silently editing the operator's crontab
    # from a reconciliation verb is the wrong direction to guess in. Name the command instead.
    print(
        "Reinstall this project's durable driver if it is not scheduled:\n"
        f"  conductor driver status\n"
        f"  {_driver_install_hint(run)}",
        file=sys.stderr,
    )
    return EXIT_OK


# --- heartbeat ----------------------------------------------------------------------------


def _driver_install_hint(run: dict) -> str:
    """The install command for THIS run's driver. The driver fires in a run worktree, never the
    owner checkout, so the hint names the worktree run.json records, or says which one to name."""
    recorded = run.get("integration_worktree")
    if isinstance(recorded, str) and recorded:
        return f"conductor driver install --worktree {shlex.quote(recorded)}"
    return (
        "conductor driver install --worktree <the worktree with "
        f"{run.get('integration_branch')} checked out>"
    )


def _driver_unbound(repo_root: str, script: str, run: dict) -> str | None:
    """A refusal sentence unless the installed driver fires in THIS run's worktree, else ``None``.

    The project has one driver script (``resume_script.driver_script_path``) and it is rendered
    for one run worktree. ``--run`` selects the run whose ownership this verb takes, but the
    script it launches drives whichever worktree it was installed for — so a heartbeat fired for
    run A would launch run B's worker under A's ownership. Launching is allowed only when the
    script's binding is provably this run's: the worktree ``run.json`` records for it, or a
    checkout of this run's integration or phase branch. Anything else, including a binding this
    build cannot read, launches nothing."""
    key = run["run_key"]
    reinstall = f"  {_driver_install_hint(run)}"
    worktree = resume_script.installed_worktree(script)
    if worktree is None:
        return (
            f"run {key!r}: the durable driver {script} names no run worktree this build can "
            "read, so which run it would drive is unknown; no fire was launched and no write "
            f"occurred. Reinstall it for this run:\n{reinstall}"
        )
    real = os.path.realpath(worktree)
    recorded = {
        os.path.realpath(path)
        for path in (run.get("integration_worktree"), run.get("phase_worktree"))
        if isinstance(path, str) and path
    }
    if real in recorded:
        return None
    branches_of_run = [
        name
        for name in (run.get("integration_branch"), run.get("phase_branch"))
        if isinstance(name, str) and name
    ]
    # A branch NAME is evidence only inside THIS repository: a clone elsewhere with the same
    # branch checked out is another repository's work tree. The worktree must share this
    # project's git common dir before its branch counts.
    common = _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_dir = (common.stdout or "").strip() if common.returncode == 0 else ""
    if not common_dir or os.path.realpath(
        os.path.dirname(common_dir)
    ) != os.path.realpath(repo_root):
        where = (
            f"belongs to the repository at {os.path.dirname(common_dir)}"
            if common_dir
            else f"is not a git work tree git could resolve (exit {common.returncode}: "
            f"{(common.stderr or '').strip() or 'no output'})"
        )
        return (
            f"run {key!r}: the durable driver {script} fires in {worktree}, which {where}, "
            f"not this project ({repo_root}); no fire was launched and no write occurred. "
            f"Reinstall the driver for this run:\n{reinstall}"
        )
    head = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    checked_out = (head.stdout or "").strip() if head.returncode == 0 else None
    if checked_out and checked_out in branches_of_run:
        return None
    found = (
        f"branch {checked_out!r}"
        if checked_out
        else f"no branch git could name (exit {head.returncode}: "
        f"{(head.stderr or '').strip() or 'no output'})"
    )
    return (
        f"run {key!r}: the durable driver {script} fires in {worktree}, which has {found} "
        f"checked out — not this run's {' or '.join(map(repr, branches_of_run))} — and is not "
        "a worktree run.json records for it. Launching it would drive another run under this "
        "run's ownership; no fire was launched and no write occurred. Reinstall the driver for "
        f"this run:\n{reinstall}"
    )


def cmd_heartbeat(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    run, state_root, key = resolution.run, resolution.state_root, resolution.run_key
    status = run["status"]
    if status not in schema.ACTIVE_STATUSES:
        print(
            f"run {key!r} is {status} and owns no heartbeat schedule, so this fire is an "
            "orphaned schedule entry; no write occurred. "
            + (
                f"Complete it with: conductor finish --run {key}"
                if status == "awaiting-team-merge"
                else "Remove the schedule entry that fired it: conductor driver status"
            ),
            file=sys.stderr,
        )
        return EXIT_FAIL
    if status == "blocked":
        _commit(
            state_root,
            key,
            status="blocked",
            expect_revision=run["revision"],
            mutate=_stamp_reconciled,
        )
        print(
            f"run {key} is blocked: reconciled and reported only, no phase advanced. "
            f"Advancing it requires: conductor resume --run {key}"
        )
        return EXIT_OK
    assert status in _WORK_CAPABLE
    script = resume_script.driver_script_path(resolution.repo_root)
    if not os.access(script, os.X_OK):
        print(
            f"run {key!r} is {status} but its durable driver {script} is missing or not "
            "executable, so this fire has nothing to launch; no write occurred. Install it "
            f"with:\n  {_driver_install_hint(run)}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    unbound = _driver_unbound(resolution.repo_root, script, run)
    if unbound:
        print(unbound, file=sys.stderr)
        return EXIT_FAIL
    # Stamped BEFORE ownership is taken: `_commit` acquires project.lock, which ranks ahead of
    # owner.lock, so doing it inside `ownership.acquire` would be a lock-order violation.
    reconciled = _commit(
        state_root,
        key,
        status=status,
        expect_revision=run["revision"],
        mutate=_stamp_reconciled,
    )
    host = runhost.resolve(resolution.repo_root)
    try:
        with ownership.acquire(state_root, key, host=host) as record:
            # Judged again UNDER ownership: the reconciliation write above had to happen before
            # it (lock order), so the run may have left the work-capable set in between — and
            # a fire is launched for the status ownership was taken over, not a remembered one.
            now = runstate.load(state_root, key)
            if now is None or now["status"] not in _WORK_CAPABLE:
                print(
                    f"run {key!r} moved to "
                    f"{'(no record)' if now is None else now['status']} after this heartbeat "
                    f"reconciled it at revision {reconciled['revision']}; no fire was launched "
                    f"and no write occurred. Inspect it with: conductor status --run {key}",
                    file=sys.stderr,
                )
                return EXIT_FAIL
            print(
                f"run {key} fire: {host} owner {record.wrapper_identity}, launching {script}",
                file=sys.stderr,
            )
            # Deliberately NOT wrapped in a wall-clock timeout. The generated driver supervises
            # its own fire on SILENCE, not elapsed time (`conductor.hosts.base`
            # FIRE_STARTUP_TIMEOUT_S / FIRE_IDLE_TIMEOUT_S) because a legitimate phase runs for
            # hours; a ceiling here would kill working phases or bound nothing.
            fire = subprocess.run(
                [script],
                cwd=resolution.repo_root,
                env=_project_env(
                    resolution.repo_root, owner_identity=record.wrapper_identity
                ),
                check=False,
                timeout=None,
            )
    except ownership.OwnerBusy as exc:
        # A skipped fire caused by a live owner is a SUCCESSFUL fire and must not create a
        # second process (design §"Heartbeat and autodev").
        print(f"run {key} fire skipped: {exc}", file=sys.stderr)
        return EXIT_OK
    except ownership.OwnerAmbiguous as exc:
        print(
            f"run {key!r} has an ownership record this build cannot interpret: {exc}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    if fire.returncode != 0:
        print(
            f"run {key} fire ended rc={fire.returncode}; the driver's own log at "
            f"{os.path.join(resolution.repo_root, '.conductor')} records what it did. "
            f"Inspect with: conductor driver status",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(
        f"run {key} fire ended rc=0 (bound: {hostbase.FIRE_IDLE_TIMEOUT_S}s of silence)"
    )
    return EXIT_OK


# --- finish -------------------------------------------------------------------------------


def _outstanding_debt(run: dict) -> list[str]:
    """Review debt that blocks completion. An UNREADABLE record counts as debt: the design makes
    outstanding debt block the final pull request, and a record this build cannot parse is not
    evidence that none is owed."""
    findings = []
    reviews = run.get("phase_reviews")
    if not isinstance(reviews, list):
        return [f"phase_reviews is {type(reviews).__name__}, not a list"]
    for entry in reviews:
        if not isinstance(entry, dict):
            findings.append(f"phase review record {entry!r} is not a mapping")
            continue
        debt = entry.get("review_debt")
        if debt in (None, False):
            continue
        if not isinstance(debt, dict):
            findings.append(
                f"phase {entry.get('phase_id')!r} records review_debt {debt!r}, which is "
                "neither absent nor a mapping"
            )
            continue
        if debt.get("outstanding"):
            findings.append(
                f"phase {entry.get('phase_id')!r} owes a review from "
                f"{debt.get('required_host') or 'an unrecorded host'}; discharge it with: "
                f"conductor review --run {run['run_key']} --phase {entry.get('phase_id')} "
                "--discharge-debt"
            )
    return findings


def _audited_head(repo_root: str, remote: str, run: dict) -> tuple[str | None, str]:
    """The run head the final pull request must match, and where it came from."""
    recorded = run.get("last_review_head_sha")
    if isinstance(recorded, str) and recorded:
        return recorded, "run.json last_review_head_sha"
    branch = run.get("integration_branch")
    if isinstance(branch, str) and branch:
        tip = finalpr.remote_tip(repo_root, remote, branch)
        if tip:
            return tip, f"{remote}/{branch}"
    return None, "nothing"


class WorktreeListUnavailable(RuntimeError):
    """git could not say which worktrees this run has, so cleanup cannot be called complete."""


def _run_worktrees(repo_root: str, run: dict) -> list[str]:
    """Registered linked worktrees belonging to this run, from git's own registration list.

    FAILS CLOSED. Returning ``[]`` on a nonzero ``git worktree list`` read "this run has no
    worktrees", and the caller went on to delete branches and mark the run TERMINAL — so one
    transient git failure produced a successful ``finish`` that removed nothing, after which
    every later call short-circuits on ``already terminal`` and the worktrees are stranded with
    no verb left that would clean them up. An empty list must mean git ANSWERED and said none."""
    out = _git(repo_root, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        raise WorktreeListUnavailable(
            f"git could not list the worktrees of {repo_root} (exit {out.returncode}): "
            f"{(out.stderr or '').strip() or 'no output'}"
        )
    registered = [
        line.split(" ", 1)[1].strip()
        for line in (out.stdout or "").splitlines()
        if line.startswith("worktree ")
    ]
    owned = {
        path
        for path in (run.get("integration_worktree"), run.get("phase_worktree"))
        if isinstance(path, str) and path
    }
    prefix = os.path.join(repo_root, ".worktrees", "conductor", run["run_key"]) + os.sep
    return [
        path
        for path in registered
        if os.path.realpath(path) != os.path.realpath(repo_root)
        and (
            path in owned
            or os.path.realpath(path) in {os.path.realpath(p) for p in owned}
            or os.path.realpath(path).startswith(os.path.realpath(prefix[:-1]) + os.sep)
        )
    ]


def _remove_worktrees(repo_root: str, paths: list[str]) -> tuple[list[str], list[str]]:
    removed, refused = [], []
    for path in paths:
        out = _git(repo_root, "worktree", "remove", path)
        if out.returncode == 0:
            removed.append(path)
        else:
            # No `--force`. A dirty worktree holds work nobody has read; reporting it is the
            # only safe outcome for a verb whose other half is "retain audit evidence".
            refused.append(f"{path}: {(out.stderr or '').strip()}")
    if removed:
        _git(repo_root, "worktree", "prune")
    return removed, refused


def _delete_local_branches(
    repo_root: str, run: dict, remote: str, default_branch: str
) -> tuple[list[str], list[str]]:
    """Delete this run's local branches, and only the ones git agrees are already merged.

    ``git branch -d`` (never ``-D``) does the deciding: it refuses a branch whose commits are
    not reachable from its upstream or from HEAD. Nothing here touches a remote ref."""
    candidates = [
        name
        for name in [run.get("integration_branch"), run.get("phase_branch")]
        if isinstance(name, str) and name
    ]
    deleted, kept = [], []
    for name in candidates:
        if (
            _git(repo_root, "rev-parse", "--verify", f"refs/heads/{name}").returncode
            != 0
        ):
            continue
        out = _git(repo_root, "branch", "-d", name)
        if out.returncode == 0:
            deleted.append(name)
        else:
            kept.append(f"{name}: {(out.stderr or '').strip()}")
    return deleted, kept


def cmd_finish(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    run, state_root, key = resolution.run, resolution.state_root, resolution.run_key
    status = run["status"]
    if status == "terminal":
        print(f"run {key} is already terminal; nothing to finish")
        return EXIT_OK
    if status != "awaiting-team-merge":
        print(
            f"run {key!r} is {status}, not awaiting-team-merge; no write occurred. finish "
            "completes a run whose final pull request the repository team has merged, and this "
            "run has not reached that point.\n"
            f"  Inspect it with: conductor status --run {key}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    busy = _live_owner(state_root, key)
    if busy:
        print(busy, file=sys.stderr)
        return EXIT_FAIL
    # RESERVE, DO NOT SAMPLE. The check above is a courtesy: it produces the better sentence,
    # naming the record and its path, for the ordinary case where someone is already working.
    # It cannot be the exclusion, because between it and the cleanup below sit `gh pr view`,
    # `git ls-remote` and two journalled writes — seconds of wall clock in which a heartbeat can
    # legitimately acquire ownership and launch a fire. Reproduced: finish returned success and
    # removed the worktree while `identity_is_live()` was true for a heartbeat that had taken
    # ownership inside that window. Holding the RECORD for the rest of the verb is what makes a
    # concurrent `ownership.acquire` refuse, and it is the same mechanism a heartbeat uses, so
    # whichever of the two arrives second is the one that backs off.
    #
    # Ownership is a record, not a held lock: `acquire` takes `owner.lock` only around the two
    # record mutations and releases it before yielding, so `_commit`'s `project.lock` inside this
    # block does not invert the global order (migration -> project -> owner -> state).
    try:
        with ownership.acquire(
            state_root, key, host=runhost.resolve(resolution.repo_root)
        ):
            return _finish_reserved(args, resolution, run, status)
    except ownership.OwnerBusy as exc:
        print(
            f"finish refused for run {key}: {exc} Nothing was removed and the run stays "
            f"{status}. Wait for that fire to finish, then re-run finish.",
            file=sys.stderr,
        )
        return EXIT_FAIL


def _finish_reserved(
    args: argparse.Namespace,
    resolution: resolve.RunResolution,
    run: dict,
    status: str,
) -> int:
    """``finish``'s body, under this run's ownership record. Every write and every removal
    below happens while a concurrent acquirer would be refused."""
    state_root, key = resolution.state_root, resolution.run_key
    repo, remote, default = _repo_context(resolution.repo_root)
    if args.pr is not None:
        pull, recovered = (
            finalpr.view(repo, args.pr, cwd=resolution.repo_root),
            finalpr.recorded_number(run) != args.pr,
        )
    else:
        pull, recovered = finalpr.reconcile(
            repo_root=resolution.repo_root,
            repo=repo,
            remote=remote,
            default_branch=default,
            run=run,
        )
    if recovered:
        # Cache the reconciled reference so the next verb does not have to re-derive it. A
        # failure here must not swallow the ANSWER, so it degrades to a warning: the checks
        # below, and the pull request's URL and state, are printed either way.
        try:
            run = _commit(
                state_root,
                key,
                status=status,
                expect_revision=run["revision"],
                mutate=_record_final_pr(pull.number),
            )
        except (
            locks.LockTimeout,
            registry.RegistryMissing,
            runstate.RunMissing,
            schema.SchemaError,
            ValueError,
            OSError,
        ) as exc:
            print(
                f"could not record final pull request #{pull.number} on run {key}: {exc}",
                file=sys.stderr,
            )

    blockers = []
    if not pull.merged:
        blockers.append(
            f"the final pull request is {pull.state}, not MERGED. Conductor never merges it — "
            "the repository team does, on the default branch."
        )
    if pull.base != default:
        blockers.append(
            f"its base is {pull.base!r}, not the repository default branch {default!r}"
        )
    audited, source = _audited_head(resolution.repo_root, remote, run)
    if audited is None:
        blockers.append(
            "this run records no audited head and its integration branch "
            f"{run.get('integration_branch')!r} does not resolve on {remote!r}, so the pull "
            "request's head cannot be matched against anything"
        )
    elif pull.head_sha != audited:
        blockers.append(
            f"its head {pull.head_sha} is not the audited run head {audited} (from {source})"
        )
    blockers.extend(_outstanding_debt(run))

    if blockers:
        print(
            f"finish refused for run {key}: {pull.url} is {pull.state}\n"
            + "\n".join(f"  - {blocker}" for blocker in blockers)
            + f"\n  Nothing was removed and the run stays {status}. Re-run finish once the "
            "repository team has merged it.",
            file=sys.stderr,
        )
        return EXIT_FAIL

    try:
        owned = _run_worktrees(resolution.repo_root, run)
    except WorktreeListUnavailable as exc:
        print(
            f"finish refused for run {key}: {pull.url} is {pull.state} but {exc}\n"
            f"  Nothing was removed and the run stays {status} — marking it terminal on an "
            "unanswered question would strand any worktree this run still has, since finish "
            "then short-circuits on 'already terminal'. Retry once git answers.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    removed, refused = _remove_worktrees(resolution.repo_root, owned)
    if refused:
        print(
            f"finish refused for run {key}: {pull.url} is {pull.state} but these worktrees "
            "could not be removed cleanly, so the run stays "
            f"{status}:\n" + "\n".join(f"  - {item}" for item in refused),
            file=sys.stderr,
        )
        return EXIT_FAIL
    deleted, kept = _delete_local_branches(resolution.repo_root, run, remote, default)
    after = _commit(
        state_root,
        key,
        status="terminal",
        expect_revision=run["revision"],
        mutate=_stamp_completed,
    )
    print(f"run {key} finished: {pull.url} merged into {default}")
    for path in removed:
        print(f"  removed worktree {path}")
    for name in deleted:
        print(f"  deleted local branch {name}")
    for item in kept:
        print(f"  kept local branch {item}", file=sys.stderr)
    print(
        f"  run state and {run['gate_dir']} retained as audit evidence "
        f"(revision {after['revision']})"
    )
    return EXIT_OK


def _record_final_pr(number: int):
    def mutate(doc: dict) -> dict:
        github = doc.get("github")
        if not isinstance(github, dict):
            github = {}
        github["final_pr"] = number
        doc["github"] = github
        doc["last_reconciled_at"] = _now()
        return doc

    return mutate


def _stamp_completed(doc: dict) -> dict:
    doc["completed_at"] = _now()
    doc["last_reconciled_at"] = _now()
    return doc


# --- CLI ----------------------------------------------------------------------------------


_HANDLERS = {
    "status": cmd_status,
    "resume": cmd_resume,
    "heartbeat": cmd_heartbeat,
    "finish": cmd_finish,
}


def _parser(verb: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"conductor {verb}")
    parser.add_argument(
        "--run",
        default=None,
        help="run key (required when more than one run is active)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="any path inside the repo (default: $CONDUCTOR_HOME, else the current directory)",
    )
    if verb == "status":
        parser.add_argument(
            "--json", action="store_true", help="machine-readable output"
        )
    if verb == "resume":
        parser.add_argument(
            "--reactivate",
            action="store_true",
            help="reactivate a run awaiting the team's merge of its final pull request",
        )
    if verb == "finish":
        parser.add_argument(
            "--pr",
            type=int,
            default=None,
            help="the final pull request's number, when it cannot be identified automatically",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else list(argv)
    if not args_in or args_in[0] not in _HANDLERS:
        print(
            "usage:\n"
            "  conductor status    [--run <run-key>] [--json]\n"
            "  conductor resume     --run <run-key> [--reactivate]\n"
            "  conductor heartbeat  --run <run-key>\n"
            "  conductor finish     --run <run-key> [--pr <number>]\n",
            file=sys.stderr,
        )
        return EXIT_USAGE
    verb, rest = args_in[0], args_in[1:]
    try:
        args = _parser(verb).parse_args(rest)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if verb != "status":
            # Every mutating entry point recovers before it takes a lock. `status` deliberately
            # does not: it is read-only and reports the pending journal instead.
            resolve.recover_pending(resolve.state_root(args.project))
        return _HANDLERS[verb](args)
    except resolve.RunAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AMBIGUOUS
    except resolve.RunNotFound as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_RUN
    except (
        branches.DefaultBranchUnresolvable,
        finalpr.FinalPullRequestError,
        locks.LockOrderError,
        locks.LockTimeout,
        ownership.OwnerAmbiguous,
        ownership.OwnerBusy,
        registry.RegistryMissing,
        registry.RevisionConflict,
        runstate.RunMissing,
        runstate.RevisionConflict,
        schema.SchemaError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except subprocess.TimeoutExpired as exc:
        print(
            f"conductor {verb} timed out asking git or gh: {exc}; no write occurred. Retry once "
            "the remote answers.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    except subprocess.CalledProcessError as exc:
        print(
            f"git failed while resolving the project for {verb}: "
            f"{(exc.stderr or '').strip() or exc}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    except RuntimeError as exc:
        print(f"conductor {verb}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"conductor {verb} failed on the filesystem: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
