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
    mutate=lambda doc: doc,
) -> dict:
    """Write ``run.json`` and ``project.json``'s status mirror as ONE journalled transaction.

    ``status`` may equal the current status — ``schema.assert_transition`` allows same-to-same —
    which is how a reconciliation that only records evidence gets the same durability guarantee
    as a status change without pretending to be one."""
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
    if record is None:
        return None
    live = ownership.identity_is_live(record.wrapper_identity)
    if live is False:
        return None
    return (
        f"run {run_key!r} is owned by {record.host} identity {record.wrapper_identity} "
        + ("(live)" if live else "(liveness unknown)")
        + f", recorded at {ownership.record_path(state_root, run_key)}; no write occurred. "
        "Wait for that fire to finish, or prove it exited before disturbing the run."
    )


def _repo_context(repo_root: str) -> tuple[str, str, str]:
    """``(repo, remote, default_branch)`` — all three fail closed.

    ``branches.default_branch`` never substitutes a literal (A-DH-6), so an unresolvable default
    branch propagates as a refusal here rather than becoming a guessed pull-request base."""
    repo = _resolve_repo()
    default = branches.default_branch()
    try:
        remote = remote_mod.resolve()
    except (
        Exception
    ):  # discovery failure degrades to the historical default, never to empty
        remote = "origin"
    return repo, remote, default


# --- status -------------------------------------------------------------------------------


def _owner_report(state_root: str, run_key: str) -> dict:
    try:
        record = ownership.read(state_root, run_key)
    except ownership.OwnerAmbiguous as exc:
        return {"state": "ambiguous", "detail": str(exc)}
    if record is None:
        return {"state": "none"}
    live = ownership.identity_is_live(record.wrapper_identity)
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
    after = _commit(state_root, key, status="active", mutate=_stamp_reconciled)
    print(f"run {key} resumed: {status} -> active (revision {after['revision']})")
    # The per-run schedule is NOT reinstalled here. Design §"Heartbeat and autodev" wants
    # resume to restore it, but per-run heartbeat artifacts (`.conductor/runs/<key>/heartbeat.sh`
    # and its scheduler entry) are not built yet, and silently editing the operator's crontab
    # from a reconciliation verb is the wrong direction to guess in. Name the command instead.
    print(
        "Reinstall this project's durable driver if it is not scheduled:\n"
        f"  conductor driver status\n"
        f"  conductor driver install --worktree {resolution.repo_root}",
        file=sys.stderr,
    )
    return EXIT_OK


# --- heartbeat ----------------------------------------------------------------------------


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
        _commit(state_root, key, status="blocked", mutate=_stamp_reconciled)
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
            f"with:\n  conductor driver install --worktree {resolution.repo_root}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    # Stamped BEFORE ownership is taken: `_commit` acquires project.lock, which ranks ahead of
    # owner.lock, so doing it inside `ownership.acquire` would be a lock-order violation.
    _commit(state_root, key, status=status, mutate=_stamp_reconciled)
    host = runhost.resolve(resolution.repo_root)
    try:
        with ownership.acquire(state_root, key, host=host) as record:
            print(
                f"run {key} fire: {host} owner {record.wrapper_identity}, launching {script}",
                file=sys.stderr,
            )
            # Deliberately NOT wrapped in a wall-clock timeout. The generated driver supervises
            # its own fire on SILENCE, not elapsed time (`conductor.hosts.base`
            # FIRE_STARTUP_TIMEOUT_S / FIRE_IDLE_TIMEOUT_S) because a legitimate phase runs for
            # hours; a ceiling here would kill working phases or bound nothing.
            fire = subprocess.run(
                [script], cwd=resolution.repo_root, check=False, timeout=None
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


def _run_worktrees(repo_root: str, run: dict) -> list[str]:
    """Registered linked worktrees belonging to this run, from git's own registration list."""
    out = _git(repo_root, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        return []
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
                state_root, key, status=status, mutate=_record_final_pr(pull.number)
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

    removed, refused = _remove_worktrees(
        resolution.repo_root, _run_worktrees(resolution.repo_root, run)
    )
    if refused:
        print(
            f"finish refused for run {key}: {pull.url} is {pull.state} but these worktrees "
            "could not be removed cleanly, so the run stays "
            f"{status}:\n" + "\n".join(f"  - {item}" for item in refused),
            file=sys.stderr,
        )
        return EXIT_FAIL
    deleted, kept = _delete_local_branches(resolution.repo_root, run, remote, default)
    after = _commit(state_root, key, status="terminal", mutate=_stamp_completed)
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
