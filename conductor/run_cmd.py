"""``conductor run`` — create, list, inspect, resolve and repoint runs.

This is the operator-facing and skill-facing surface over ``conductor.core``. Everything a later
plan needs to name a run goes through here, so the disambiguation rule is stated once: every
scheduled or non-interactive invocation carries an explicit run key, and a bare command is allowed
only when exactly one active run exists.

SCOPE: ``run new`` creates registry state, the run directory and ``run.json``. It does not create
branches or worktrees, install a schedule, or record hosts — those belong to the branch/PR,
heartbeat and adapter plans respectively, and the ``conductor:start`` skill composes them.

Exit codes: 0 success, 1 refusal/failure, 2 ambiguous run, 3 no such run / no active run,
64 usage.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

from conductor.core import (
    hygiene,
    locks,
    names,
    ownership,
    registry,
    repoint,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
    workstation,
)
from conductor.hosts import base as hostbase
from conductor.hosts import runhost

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 2
EXIT_NO_RUN = 3
EXIT_USAGE = 64

#: `run owner-busy` answers one question with exactly two codes, and the split is deliberately
#: NOT "0 success / non-zero failure". The caller is a cron driver deciding whether to launch a
#: worker into a checkout, so the only safe shape is one where EVERY unplanned outcome means
#: "do not launch".
#:
#: * `EXIT_OK` (0) — the condition the verb is named for HOLDS: a live, or unverifiable, owner
#:   is on this run. Do not proceed. This is the `grep`/`test` convention: 0 means the predicate
#:   matched.
#: * `EXIT_OWNER_FREE` (11) — proven that nothing owns this run. Proceed.
#:
#: 11 rather than 1 because 1, 2, 3, 64 and 0 are all reachable from this module's generic
#: failure paths — an argparse error, a lock timeout, an unrecoverable journal, an uncaught
#: exception. If "proceed" were spelled with any of those, a crash in this verb would become a
#: licence to fire into an occupied worktree. Only the one code that no generic path can produce
#: means proceed, so every way this check can break lands on "do not launch".
EXIT_OWNER_FREE = 11


def spec_digest(repo_root: str, relative: str) -> str:
    """The sha256 of a spec's bytes — the identity a later repoint checks against."""
    with open(os.path.join(repo_root, relative), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _matching_run(state_root: str, project_doc: dict, digest: str) -> str | None:
    """An existing run whose recorded spec digest equals ``digest``."""
    for key in registry.run_keys(project_doc):
        run = runstate.load(state_root, key)
        if run is not None and run.get("spec_digest") == digest:
            return key
    return None


def _record_statuses(state_root: str, mapped: dict) -> dict[str, str]:
    """Each generation's AUTHORITATIVE status in one spec-path mapping, keyed by run key.

    ``""`` means the record is missing or carries no status — the fail-closed value, since it is
    never in ``schema.TERMINAL_STATUSES``.

    ``project.json``'s per-generation status is a MIRROR (``conductor/core/registry.py:14-17``)
    and NO product code updates it: ``runstate.set_status`` deliberately holds only ``state.lock``
    (taking ``project.lock`` from there would invert the global lock order) and
    ``registry.mirror_status`` has no caller outside this module and the tests. So the mirror
    skews in both directions and neither ``registry.current_run_key`` nor
    ``schema.validate_project`` can decide whether a new run may start:

    * mirror says current, record says terminal/failed — the run IS finished. Refusing on the
      mirror would tell the operator to finish a run that is already finished, while
      ``conductor run resolve`` reports no active run. This is the DEFAULT state of a finished
      run, not an edge case, because nothing writes the mirror when a run ends.
    * mirror says terminal, record still active — the run is NOT finished. Minting a second
      generation there would put two authoritatively-live runs on one spec, breaking the design's
      central invariant somewhere ``validate_project`` cannot see, because it validates the
      mirror.

    Read once and reused for both the refusal gate and the reconciliation ``cmd_new`` folds into
    its after-image, so the mapping is walked a single time under ``project.lock``."""
    statuses = {}
    for entry in mapped.get("generations", []):
        key = str(entry.get("run_key"))
        run = runstate.load(state_root, key)
        statuses[key] = "" if run is None else str(run.get("status", ""))
    return statuses


def _reconcile_mirror(project_doc: dict, statuses: dict[str, str]) -> dict:
    """Fold every generation's authoritative status into ``project_doc``'s mirror and recompute
    ``current``. Mutates and returns ``project_doc``; pure, takes no lock.

    ``registry.mirror_status`` is the single definition of the recompute rule (the one nonterminal
    key, else ``None``), and it is pure, so it is reused rather than restated. Only ever called
    once every generation is terminal or failed — ``cmd_new`` refuses otherwise — so it cannot be
    handed a status ``mirror_status`` rejects, and the recomputed ``current`` is always ``None``.

    This is a WRITE path: ``cmd_new`` holds ``project.lock``, is already constructing a
    ``project.json`` after-image, and writes it journalled alongside the new ``run.json``. A read
    path must never do this."""
    for run_key, status in statuses.items():
        registry.mirror_status(project_doc, run_key, status)
    return project_doc


def cmd_new(args: argparse.Namespace) -> int:
    root = resolve.repo_root(args.project)
    # Normalizing the spec path is a PURE computation and deliberately runs before the two
    # hygiene calls, which write: `ensure_local_exclude` may append to the repository's local git
    # exclude. Refusing a spec outside the repository afterwards would print "no write occurred"
    # over a write that had already happened, and the raw ValueError carried neither the write
    # status nor a retry command — unlike every sibling refusal here.
    try:
        relative = runkey.normalize_spec_path(root, args.spec)
    except ValueError as exc:
        print(
            f"{exc}; no write occurred. Move the spec inside the repository first, then re-run: "
            f"conductor run new <path-under-{root}>",
            file=sys.stderr,
        )
        return EXIT_FAIL
    hygiene.assert_state_paths_untracked(root)
    hygiene.ensure_local_exclude(root)
    state_root = os.path.join(root, ".conductor")
    if not os.path.isfile(os.path.join(root, relative)):
        # Not "no write occurred": ensure_local_exclude ran above and may have written the
        # repository's local git exclude. That is idempotent scaffolding, not run state, but the
        # failure-report contract is about writes, so say which happened.
        print(
            f"{relative} does not exist in {root}; no run state was written (only the "
            f"repository's local git exclude was ensured).",
            file=sys.stderr,
        )
        return EXIT_FAIL
    registry.init(
        state_root,
        workstation_id=workstation.workstation_id(),
        repo_identity=resolve.repo_identity(root),
    )
    digest = spec_digest(root, relative)
    # project.lock is held across the read, every refusal check, and the write, so the document
    # the checks ran against is the document the transaction replaces. `registry.init` above
    # takes and releases the same lock, hence the re-read here rather than reusing its return.
    with locks.hold(registry.lock_path(state_root), kind="project"):
        # `recover` WRITES — committed after-images land and the journal is removed — so the
        # refusals below cannot claim "no write occurred" when it handled anything.
        # `transaction.write_status` is the one place that decides which phrase is true.
        handled = transaction.recover(state_root)
        write_status = transaction.write_status(handled)
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)} immediately after "
                f"creating one; {write_status}. Re-run the command."
            )
        mapped = registry.mapping(project_doc, relative)
        # Decided against run.json, never against project.json's status mirror — see
        # _record_statuses for both skew directions and what each one costs.
        statuses = _record_statuses(state_root, mapped) if mapped else {}
        unknown = sorted(key for key, status in statuses.items() if not status)
        if unknown:
            print(
                f"{relative} is mapped to run(s) {', '.join(unknown)} whose record(s) are "
                f"missing or carry no status; {write_status}. Refusing to mint a second run "
                f"over registered state that was removed — the mapping still owns those branch "
                f"and gate names, and neither finishing them nor --new-run can clear it. Recover "
                f"by removing the run directory and then the mapping:\n"
                f"  rm -r {runstate.run_dir(state_root, unknown[0])}\n"
                f"  remove the {relative!r} entry from "
                f"{registry.registry_path(state_root)}",
                file=sys.stderr,
            )
            return EXIT_FAIL
        unfinished = sorted(
            key
            for key, status in statuses.items()
            if status not in schema.TERMINAL_STATUSES
        )
        if unfinished:
            listing = ", ".join(unfinished)
            print(
                f"{relative} already has the unfinished run(s) {listing}; {write_status}.\n"
                f"  Inspect:         conductor run show --run {unfinished[0]}\n"
                f"  Start a new one: finish or fail {listing}, then "
                f"conductor run new {relative} --new-run",
                file=sys.stderr,
            )
            return EXIT_FAIL
        if mapped is not None and not args.new_run:
            print(
                f"{relative} has {len(mapped['generations'])} completed generation(s); "
                f"{write_status}.\n  Start the next one with: conductor run new {relative} --new-run",
                file=sys.stderr,
            )
            return EXIT_FAIL
        if mapped is None:
            # A spec whose bytes already belong to a run, at a path this registry does not map,
            # is a MOVE — minting a second run for it would abandon the first one's branch and
            # gate. Only checked when the path is unmapped: for a mapped path the twin found
            # would be its own earlier generation.
            twin = _matching_run(state_root, project_doc, digest)
            if twin is not None:
                print(
                    f"{relative} is byte-identical to the spec of run {twin!r}, which is mapped "
                    f"to a different path; {write_status}. This is a move, not a new run:\n"
                    f"  conductor run repoint-spec --run {twin} {relative}",
                    file=sys.stderr,
                )
                return EXIT_FAIL
        generation = registry.next_generation(project_doc, relative)
        key = runkey.run_key(relative, generation)
        derived = names.derived_names(
            key
        )  # THE definition of both formats; never inline them
        run_doc = schema.validate_run(
            schema.new_run_doc(
                run_key=key,
                generation=generation,
                spec_path=relative,
                workstation_id=project_doc["workstation_id"],
                integration_branch=derived.integration_branch,
                gate_dir=derived.gate_dir,
                spec_digest=digest,
                now=_now(),
            )
        )
        # Reconcile BEFORE appending: every existing generation's mirror takes its record's
        # status, so `register`'s new generation is the only nonterminal one and
        # `validate_project` accepts the result. Without this, --new-run after a genuinely
        # finished run is refused for "2 nonterminal generations" — the mirror still calls
        # generation 1 active because nothing in the product ever wrote it.
        new_project = registry.register(
            _reconcile_mirror(schema.clone(project_doc), statuses),
            spec=relative,
            run_key=key,
            generation=generation,
        )
        new_project["revision"] = project_doc["revision"] + 1
        schema.validate_project(new_project)
        with locks.hold(
            runstate.state_lock_path(state_root, key), kind="state", run_key=key
        ):
            if runstate.load(state_root, key) is not None:
                # Unreachable through the registry: a mapped path is refused above, and a new
                # generation gets a new key. So a record here that project.json does not know
                # about is an orphan, and --new-run cannot clear it — say what will.
                # Same write-status rule, different plain phrase: this refusal follows
                # ensure_local_exclude and registry.init, so even with nothing recovered it
                # cannot claim the bare "no write occurred".
                scaffolding = transaction.write_status(
                    handled,
                    phrase=(
                        "no run state was written — only the repository's local git exclude "
                        "and the project registry, both idempotent scaffolding"
                    ),
                )
                print(
                    f"run {key!r} has a record at {runstate.run_path(state_root, key)} but is "
                    f"not registered in {registry.registry_path(state_root)}; {scaffolding}. "
                    f"Remove the orphaned record and retry:\n"
                    f"  rm -r {runstate.run_dir(state_root, key)}\n"
                    f"  conductor run new {relative}",
                    file=sys.stderr,
                )
                return EXIT_FAIL
            # project.json and run.json are one write. `transaction` exists because a crash
            # between them leaves a registry mapping a spec to a run key no record backs (or the
            # reverse), and the next entry point's `recover_pending` rolls this forward.
            txn_id = f"new-{key}"
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
                        "path": runstate.run_path(state_root, key),
                        "before": None,
                        "after": run_doc,
                        # Recovery holds only project.lock; the journal names the lock that
                        # guards this file so replay serializes against the run's own writers.
                        "lock": {
                            "path": runstate.state_lock_path(state_root, key),
                            "run_key": key,
                        },
                    },
                ],
            )
            transaction.commit(state_root, txn_id)
            transaction.apply(state_root, txn_id)
    print(key)
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    state_root = resolve.state_root(args.project)
    project_doc = registry.load(state_root)
    empty = "no runs" if args.all else "no active runs"
    if project_doc is None:
        print("[]" if args.json else empty)
        return EXIT_OK
    active = set(resolve.active_run_keys(state_root))
    rows = []
    for key in registry.run_keys(project_doc):
        run = runstate.load(state_root, key)
        if run is None:
            continue
        if not args.all and key not in active:
            continue
        rows.append(
            {
                "run_key": key,
                "generation": run["generation"],
                "status": run["status"],
                "spec_path": run["spec_path"],
                "integration_branch": run["integration_branch"],
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return EXIT_OK
    if not rows:
        print(empty)
        return EXIT_OK
    width = max(len(r["run_key"]) for r in rows)
    for row in rows:
        print(f"{row['run_key']:<{width}}  {row['status']:<19}  {row['spec_path']}")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    print(json.dumps(resolution.run, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_resolve(args: argparse.Namespace) -> int:
    print(resolve.resolve(run_key=args.run, start=args.project).run_key)
    return EXIT_OK


def cmd_gate_dir(args: argparse.Namespace) -> int:
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    gate = resolve.gate_for_run(resolution)
    if gate.fail_closed:
        print(
            f"run {resolution.run_key}: {gate.fail_closed}; no write occurred. "
            f"Inspect it with: conductor run show --run {resolution.run_key}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(gate.directory)
    return EXIT_OK


# --- ownership: register, consult, recover ---------------------------------------------------
#
# ONE RECORD, THREE VERBS, NO PROCESS-NAME MATCHING ANYWHERE. `own` is what an interactive
# worker calls before it touches product code; `owner-busy` is what the cron driver and the
# heartbeat consult; `disown` is the supported recovery. All three read and write the same
# `owner.json` through `conductor.core.ownership`, and the liveness answer under all of them
# comes from the kernel — `/proc/<pid>/stat` field 22 plus `boot_id` on Claude, an `flock` in
# `/proc/locks` on Codex — never from a process name. The `pgrep -f 'claude'` guard this
# replaces matched Conductor's own shells and missed every real session; it does not come back
# in a narrower form either.


def _session_identity(repo_root: str, host_override: str | None) -> tuple[str, str]:
    """``(host_id, identity)`` for the SESSION running this command.

    The host comes from what Conductor recorded for this project, or from an explicit
    ``--host``. It is never inferred from which environment variables are present, and that is
    a measured hazard rather than a principle: a Codex session started from inside a Claude
    session inherits ``CLAUDECODE``, ``CLAUDE_PID`` and ``CLAUDE_CODE_SESSION_ID`` wholesale and
    exports ``CODEX_THREAD_ID`` on top of them. A worker that sniffed the environment would
    register the outer Claude session's pid and block every fire for as long as that session
    lives.

    Raises ``OwnerUnidentified`` naming the variable when the host offers no identity. Refusing
    to register is the fail-SAFE direction: the alternative is a record whose liveness nothing
    can check, which does not degrade to "no protection" but to a permanent block.
    """
    host = host_override or runhost.resolve(repo_root)
    adapter = hostbase.load(host)
    identity = adapter.session_identity(os.environ)
    if identity is None:
        raise ownership.OwnerUnidentified(
            f"host {host!r} exposes no session identity in this environment, so ownership "
            "cannot be registered and no write occurred.\n"
            "  On claude this needs $CLAUDE_PID (exported into every tool shell) plus a "
            "readable /proc/<pid>/stat and /proc/sys/kernel/random/boot_id.\n"
            "  On codex it needs $CODEX_THREAD_ID (exported into every tool shell, TUI "
            "included) or an ancestor holding exactly one lock in "
            "$CODEX_HOME/thread-writer-locks/.\n"
            "Both variables are UNDOCUMENTED and can be withdrawn by a host release. If this "
            "started after a host upgrade, that is the first thing to check."
        )
    return host, identity


def cmd_own(args: argparse.Namespace) -> int:
    """Register this session as the owner of the run, before it does any product work."""
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    state_root, key = resolution.state_root, resolution.run_key
    host, identity = _session_identity(resolution.repo_root, args.host)
    existing = ownership.read(state_root, key)
    if existing is not None and ownership.is_inherited(existing, os.environ):
        # Our own wrapper already holds it. Overwriting would be worse than a no-op: the
        # wrapper releases only a record still naming ITS identity, so a rewrite here would
        # orphan the record and leave the run owned by a session that has since exited.
        print(
            f"run {key} is already owned by the wrapper that launched this session "
            f"({existing.host} identity {existing.wrapper_identity}, tier {existing.tier}); "
            "no write occurred."
        )
        return EXIT_OK
    record = ownership.claim(
        state_root, key, host=host, wrapper_identity=identity, tier=args.tier
    )
    print(
        f"run {key} owned by {record.host} identity {record.wrapper_identity} "
        f"(tier {record.tier}). Release it with: conductor run disown --run {key}"
    )
    return EXIT_OK


def _git_detail(exc: subprocess.SubprocessError) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        return " ".join(detail.split()) or f"exit {exc.returncode}"
    return " ".join(str(exc).split())


#: git's answer when discovery walked the whole tree and found NO repository: the parenthesised
#: "(or any of the parent directories)" form, or the "(or any parent up to mount point …)" form
#: that a filesystem boundary produces. Anchored on the first line of stderr.
_NO_REPOSITORY = re.compile(
    r"\Afatal: not a git repository \(or any (?:of the parent directories|parent up to "
    r"mount point [^)]*)\)"
)


def _definitely_no_repository(exc: subprocess.SubprocessError) -> bool:
    """Did git ANSWER that there is no repository here? Exit 128 with git's discovery-failed
    message, and nothing else.

    NOT the bare substring "not a git repository": git prints ``fatal: not a git repository:
    /path/.git/worktrees/x`` for a linked worktree whose gitdir pointer is broken, and that
    names a repository that existed here, with run state that may still be live. That form, a
    localized message and anything unfamiliar all stay on the refusing side."""
    return (
        isinstance(exc, subprocess.CalledProcessError)
        and exc.returncode == 128
        and isinstance(exc.stderr, str)
        and _NO_REPOSITORY.match(exc.stderr.lstrip()) is not None
    )


def _owner_busy_unanswerable(
    project: str | None, exc: subprocess.SubprocessError
) -> str:
    """``owner-busy``'s fail-closed line when git could not say where the run state lives."""
    base = (
        project
        if project is not None
        else (os.environ.get("CONDUCTOR_HOME") or os.getcwd())
    )
    what = (
        "timed out"
        if isinstance(exc, subprocess.TimeoutExpired)
        else f"failed ({_git_detail(exc)})"
    )
    return (
        f"owner-busy state=unreadable reason=repository-unresolved project={base} "
        f"detail=git rev-parse --git-common-dir {what}, so whether a run here is owned cannot "
        f"be read; no write occurred. Check it with: git -C {shlex.quote(base)} rev-parse "
        "--git-common-dir"
    )


def cmd_owner_busy(args: argparse.Namespace) -> int:
    """Is a live owner on this run? See ``EXIT_OWNER_FREE`` for the two-code contract.

    Prints ONE line to stdout carrying a ``state=`` token, so a shell caller can put the reason
    in its log without re-deriving it. ``state=live`` is evidence that the contract worked;
    ``state=unreadable`` is a fault an operator has to clear.
    """
    try:
        resolution = resolve.resolve(run_key=args.run, start=args.project)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(_owner_busy_unanswerable(args.project, exc))
        return EXIT_OK
    except resolve.RunNotFound:
        # No run here means no ownership record, which means nothing is claiming this checkout.
        # This is the condition every project was in before ownership existed, and reporting it
        # as "cannot tell" would turn a driver that has always fired into one that never does.
        print("owner-busy state=free reason=no-run")
        return EXIT_OWNER_FREE
    state_root, key = resolution.state_root, resolution.run_key
    try:
        record = ownership.read(state_root, key)
    except ownership.OwnerAmbiguous as exc:
        print(f"owner-busy state=unreadable run={key} detail={exc}")
        return EXIT_OK
    if record is not None and ownership.is_inherited(record, os.environ):
        print(
            f"owner-busy state=free run={key} reason=inherited "
            f"identity={record.wrapper_identity}"
        )
        return EXIT_OWNER_FREE
    live = None if record is None else ownership.identity_is_live(record)
    if record is None or live is False:
        # No live RECORDED owner. The run is free only if no fire outlives it either.
        fire = ownership.running_fire(state_root)
        if fire:
            print(f"owner-busy state=live run={key} reason=fire-running detail={fire}")
            return EXIT_OK
    if record is None:
        print(f"owner-busy state=free run={key} reason=no-record")
        return EXIT_OWNER_FREE
    if live is False:
        print(
            f"owner-busy state=free run={key} reason=owner-exited "
            f"identity={record.wrapper_identity}"
        )
        return EXIT_OWNER_FREE
    state = "live" if live else "unreadable"
    recovery = (
        ""
        if live
        else " recover=confirm no process is working on this run, then: conductor run "
        f"disown --run {key} --force"
    )
    print(
        f"owner-busy state={state} run={key} host={record.host} tier={record.tier} "
        f"identity={record.wrapper_identity} since={record.acquired_at}{recovery}"
    )
    return EXIT_OK


def cmd_disown(args: argparse.Namespace) -> int:
    """Release this session's own ownership, or clear a record whose owner is provably gone.

    RELEASING YOUR OWN IS THE COMMON CASE AND IT IS NOT THE SAME OPERATION. A worker that
    finishes its phase is still running when it lets go, so it can never satisfy the exit proof
    ``disown`` demands of a stranger — and an early version of this verb refused every worker
    that tried to release itself, which would have left an ownership record behind after every
    single fire and blocked the run until a human forced it. Ownership one holds is one's own to
    drop; ``ownership.release`` is the same "only if it is still mine" write the wrapper tier
    makes when its ``with`` block ends.

    A DESCENDANT DOES NOT RELEASE ITS ANCESTOR. A worker launched by a wrapper that holds the
    record must leave it alone: the wrapper is still supervising the fire and will release it
    itself. Dropping it here would hand the run to the next cron tick while the fire is running.
    """
    resolution = resolve.resolve(run_key=args.run, start=args.project)
    state_root, key = resolution.state_root, resolution.run_key
    try:
        current = ownership.read(state_root, key)
    except ownership.OwnerAmbiguous:
        current = None  # only --force can clear it; `disown` below says so.
    if current is not None:
        if ownership.is_inherited(current, os.environ):
            print(
                f"run {key} is owned by the wrapper that launched this session "
                f"({current.wrapper_identity}); it releases its own ownership. Nothing was "
                "removed."
            )
            return EXIT_OK
        try:
            _host, identity = _session_identity(resolution.repo_root, args.host)
        except ownership.OwnerUnidentified:
            identity = None
        if identity is not None and identity == current.wrapper_identity:
            refusal = ownership.release(state_root, key, wrapper_identity=identity)
            if refusal:
                print(refusal, file=sys.stderr)
                return EXIT_FAIL
            print(f"run {key}: released this session's own ownership ({identity}).")
            return EXIT_OK
    outcome, detail = ownership.disown(state_root, key, force=args.force)
    if outcome == "refused":
        print(detail, file=sys.stderr)
        return EXIT_FAIL
    print(detail)
    return EXIT_OK


def cmd_repoint_spec(args: argparse.Namespace) -> int:
    root = resolve.repo_root(args.project)
    doc = repoint.repoint(
        os.path.join(root, ".conductor"),
        repo_root=root,
        run_key=args.run,
        new_spec_path=args.new_path,
    )
    print(f"{doc['run_key']} -> {doc['spec_path']}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor run", description="Create, inspect and repoint conductor runs."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="any path inside the repo (default: $CONDUCTOR_HOME, else the current directory)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="create a run for a spec")
    new.add_argument("spec", help="path to the spec, absolute or repository-relative")
    new.add_argument(
        "--new-run",
        action="store_true",
        help="start the next generation for a spec whose generations have all ended",
    )
    new.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    listing = sub.add_parser("list", help="list runs")
    listing.add_argument("--all", action="store_true", help="include inactive runs")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    show = sub.add_parser("show", help="print a run record")
    show.add_argument("--run", required=True, help="run key")
    show.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    resolving = sub.add_parser(
        "resolve", help="print the run key this invocation means"
    )
    resolving.add_argument("--run", default=None, help="run key (optional)")
    resolving.add_argument(
        "--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    gate = sub.add_parser("gate-dir", help="print a run's done-gate directory")
    gate.add_argument("--run", default=None, help="run key (optional)")
    gate.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    own = sub.add_parser(
        "own", help="register this session as the run's owner, before any product work"
    )
    own.add_argument("--run", default=None, help="run key (optional)")
    own.add_argument(
        "--host",
        default=None,
        choices=hostbase.HOST_IDS,
        help="the invoking skill's own host id (default: the project's recorded host). "
        "NEVER inferred from the environment: a Codex session nested in a Claude one sees "
        "both hosts' variables.",
    )
    own.add_argument(
        "--tier",
        default="in-session",
        choices=ownership.TIERS,
        help="in-session (a fire inside a live host REPL, the default) or wrapper (a process "
        "that outlives the fire it supervises)",
    )
    own.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    busy = sub.add_parser(
        "owner-busy",
        help="exit 0 if a live or unverifiable owner holds the run, 11 if it is provably free",
    )
    busy.add_argument("--run", default=None, help="run key (optional)")
    busy.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    gone = sub.add_parser(
        "disown", help="clear an ownership record whose owner is provably gone"
    )
    gone.add_argument("--run", default=None, help="run key (optional)")
    gone.add_argument(
        "--host",
        default=None,
        choices=hostbase.HOST_IDS,
        help="the invoking skill's own host id, for releasing ownership this session holds "
        "(default: the project's recorded host)",
    )
    gone.add_argument(
        "--force",
        action="store_true",
        help="clear a record that CANNOT be proven exited (a foreign host, an unloadable "
        "adapter, a refused schema, hidepid). Confirm nothing is working on the run first.",
    )
    gone.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    move = sub.add_parser("repoint-spec", help="point a run at a moved spec")
    move.add_argument("--run", required=True, help="run key")
    move.add_argument("new_path", help="the spec's new repository-relative path")
    move.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


# `--project` is accepted both before and after the subcommand, and every subparser declares it
# with `argparse.SUPPRESS` rather than `default=None`. That is load-bearing: a subparser default
# OVERWRITES a value the top-level parser already stored, so `conductor run --project /repo new
# spec.md` would silently fall back to $CONDUCTOR_HOME / the current directory. Verified on
# CPython 3.12.8:
#
#     before-subcmd, sub default=None : None      <-- the bug
#     before-subcmd, sub SUPPRESS     : /repo
#     after-subcmd,  sub SUPPRESS     : /repo
#     neither,       sub SUPPRESS     : None

_HANDLERS = {
    "new": cmd_new,
    "list": cmd_list,
    "show": cmd_show,
    "resolve": cmd_resolve,
    "gate-dir": cmd_gate_dir,
    "repoint-spec": cmd_repoint_spec,
    "own": cmd_own,
    "owner-busy": cmd_owner_busy,
    "disown": cmd_disown,
}


def _write_failure(
    args: argparse.Namespace, invocation: list[str], exc: OSError
) -> str:
    """The refusal for a filesystem error that killed an operation mid-write — ENOSPC, EIO, a
    revoked mount, a state directory removed underneath the process.

    Every other refusal in this module names the run, whether a write occurred, and the exact
    command that recovers; an unhandled ``OSError`` escaped as a traceback carrying none of the
    three. The journal is what makes it recoverable, so the report reads it: a COMMITTED journal
    means the intended change survives and the next verb completes it, a prepared one means it is
    reversed, and neither leaves a half-applied write. The transaction ids embed the run key
    (``new-<key>``, ``repoint-<key>``), which is how an operation that never got as far as
    resolving a key still names its run."""
    try:
        states = transaction.pending_states(
            os.path.join(
                resolve.repo_root(getattr(args, "project", None)), ".conductor"
            )
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # The reporter must never replace the operator's real error with one of its own.
        states = {}
    committed = sorted(t for t, state in states.items() if state == "committed")
    prepared = sorted(t for t, state in states.items() if state != "committed")
    if committed:
        journal = (
            f"transaction(s) {', '.join(committed)} are journalled and COMMITTED — the next "
            "conductor run verb completes them, so the intended state is not lost."
        )
    elif prepared:
        journal = (
            f"transaction(s) {', '.join(prepared)} are journalled but NOT committed — the next "
            "conductor run verb reverses them, so no partial write survives."
        )
    else:
        journal = "no transaction is pending, so no state write was left half-applied."
    run_key = getattr(args, "run", None)
    subject = f"run {run_key}" if run_key else f"conductor run {args.cmd}"
    return (
        f"{subject} failed while writing state: {exc}\n"
        f"  {journal}\n"
        f"  Fix the underlying error, then retry: conductor run {shlex.join(invocation)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    invocation = sys.argv[1:] if argv is None else list(argv)
    try:
        args = parser.parse_args(invocation)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        # EVERY verb is an entry point, so recovery happens here — once, before dispatch and
        # before any handler takes a lock — rather than in each cmd_*, so a verb added later
        # cannot forget it. `resolve.resolve` and `resolve.active_run_keys` are deliberately pure
        # lock-free reads (Task 9) so a takeover or a repoint can call them under `owner.lock`;
        # the cost of that purity is that nothing recovers an unfinished transaction unless an
        # entry point does, and a committed-but-unapplied journal makes a run invisible — a bare
        # command then resolves to a different run and lands work on the wrong branch.
        # `recover_pending` returns [] cheaply when no journal is pending and creates nothing
        # when the project has no state root, so this is safe on a first-ever `run new`.
        try:
            state_root = os.path.join(
                resolve.repo_root(getattr(args, "project", None)), ".conductor"
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # NO REPOSITORY RESOLVES. For every other verb this is a failure and falls through
            # to the handler below. For `owner-busy` a DEFINITIVE "not a git repository" is an
            # ANSWER: run state is anchored at the git common dir, so where there is no
            # repository there is no `.conductor/runs/<key>/owner.json` and nothing can be
            # claiming this checkout — the same fact `reason=no-run` reports one level in.
            # Answering "cannot tell" there would be a REGRESSION dressed as caution: a driver
            # installed against a directory that is not a git checkout fired before this
            # contract existed and would now skip every tick forever.
            #
            # ONLY that answer. A timeout, or git failing for any other reason (a dubious-
            # ownership refusal, a corrupt repository, a missing binary), is git NOT answering,
            # and reading it as free fires a driver past a record nobody could consult.
            if args.cmd != "owner-busy":
                raise
            if _definitely_no_repository(exc):
                print(
                    f"owner-busy state=free reason=no-repository detail={_git_detail(exc)}"
                )
                return EXIT_OWNER_FREE
            print(_owner_busy_unanswerable(getattr(args, "project", None), exc))
            return EXIT_OK
        resolve.recover_pending(state_root)
        return _HANDLERS[args.cmd](args)
    except resolve.RunAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AMBIGUOUS
    except resolve.RunNotFound as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_RUN
    except ownership.OwnerBusy as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except ownership.OwnerAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except (
        hygiene.TrackedStateError,
        locks.LockTimeout,
        registry.RegistryMissing,
        registry.RevisionConflict,
        repoint.RepointRefused,
        runstate.RunExists,
        runstate.RunMissing,
        runstate.RevisionConflict,
        schema.SchemaError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except subprocess.CalledProcessError as exc:
        print(
            f"git failed while resolving the project for {args.cmd}: "
            f"{(exc.stderr or '').strip() or exc}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    except OSError as exc:
        # LAST, so nothing above it is shadowed. A write that dies mid-transaction is the one
        # failure this module cannot prevent; it can still refuse the way every other path does
        # instead of printing a traceback that names no run, no write status and no recovery.
        print(_write_failure(args, invocation, exc), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
