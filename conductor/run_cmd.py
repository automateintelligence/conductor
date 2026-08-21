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
import shlex
import subprocess
import sys

from conductor.core import (
    hygiene,
    locks,
    names,
    registry,
    repoint,
    resolve,
    runkey,
    runstate,
    schema,
    transaction,
    workstation,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 2
EXIT_NO_RUN = 3
EXIT_USAGE = 64


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
        resolve.recover_pending(
            os.path.join(
                resolve.repo_root(getattr(args, "project", None)), ".conductor"
            )
        )
        return _HANDLERS[args.cmd](args)
    except resolve.RunAmbiguous as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AMBIGUOUS
    except resolve.RunNotFound as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_RUN
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
