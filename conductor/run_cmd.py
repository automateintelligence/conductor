"""``conductor run`` — create, list, inspect, resolve and repoint runs.

This is the operator-facing and skill-facing surface over ``conductor.core``. Everything a later
plan needs to name a run goes through here, so the disambiguation rule is stated once: every
scheduled or non-interactive invocation carries an explicit run key, and a bare command is allowed
only when exactly one active run exists.

SCOPE: ``run new`` creates registry state, the run directory and ``run.json``. It does not create
branches or worktrees, install a schedule, or record hosts — those belong to the branch/PR,
heartbeat and adapter plans respectively, and ``/conductor:start`` composes them.

Exit codes: 0 success, 1 refusal/failure, 2 ambiguous run, 3 no such run / no active run,
64 usage.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
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


def _unfinished_generations(state_root: str, mapped: dict) -> list[str]:
    """Run keys in one spec-path mapping whose AUTHORITATIVE record is not finished, sorted.

    ``project.json``'s per-generation status is a MIRROR (``conductor/core/registry.py:14-17``)
    and ``runstate.set_status`` does not touch it, so the two can skew either way and neither
    ``registry.current_run_key`` nor ``schema.validate_project`` can be trusted to decide whether
    a new run may start. ``run.json`` is the authority, so it decides here:

    * mirror says current, record says terminal/failed — NOT unfinished. Refusing on the mirror
      would tell the operator to finish a run that is already finished, and ``--new-run`` would
      refuse identically; ``conductor run resolve`` meanwhile reports no active run. Two verbs
      contradicting each other about one run is worse than a missed refusal.
    * mirror says terminal, record still active/checkpointed/blocked — unfinished. Minting a
      second generation there would put TWO authoritatively-live runs on one spec, breaking the
      design's central invariant somewhere ``validate_project`` cannot see, because it validates
      the mirror.

    ``awaiting-team-merge`` counts as unfinished even though ``schema.is_active`` excludes it: the
    run is not over until ``finish`` proves its final pull request merged, and its branch and gate
    are still live.

    A MISSING record also counts: the registry names a run whose state has been removed, and
    minting a second one over it is the opposite of fail-closed."""
    unfinished = []
    for entry in mapped.get("generations", []):
        key = str(entry.get("run_key"))
        run = runstate.load(state_root, key)
        if run is None:
            unfinished.append(key)
            continue
        status = str(run.get("status", ""))
        if schema.is_active(status) or status == "awaiting-team-merge":
            unfinished.append(key)
    return sorted(unfinished)


def cmd_new(args: argparse.Namespace) -> int:
    root = resolve.repo_root(args.project)
    hygiene.assert_state_paths_untracked(root)
    hygiene.ensure_local_exclude(root)
    state_root = os.path.join(root, ".conductor")
    relative = runkey.normalize_spec_path(root, args.spec)
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
        transaction.recover(state_root)
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)} immediately after "
                "creating one; no write occurred. Re-run the command."
            )
        mapped = registry.mapping(project_doc, relative)
        # Decided against run.json, never against project.json's status mirror — see
        # _unfinished_generations for both skew directions and what each one costs.
        unfinished = _unfinished_generations(state_root, mapped) if mapped else []
        if unfinished:
            listing = ", ".join(unfinished)
            print(
                f"{relative} already has the unfinished run(s) {listing}; no write occurred.\n"
                f"  Inspect:         conductor run show --run {unfinished[0]}\n"
                f"  Start a new one: finish or fail {listing}, then "
                f"conductor run new {relative} --new-run",
                file=sys.stderr,
            )
            return EXIT_FAIL
        if mapped is not None and not args.new_run:
            print(
                f"{relative} has {len(mapped['generations'])} completed generation(s); no write "
                f"occurred.\n  Start the next one with: conductor run new {relative} --new-run",
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
                    f"to a different path; no write occurred. This is a move, not a new run:\n"
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
        new_project = registry.register(
            schema.clone(project_doc), spec=relative, run_key=key, generation=generation
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
                print(
                    f"run {key!r} has a record at {runstate.run_path(state_root, key)} but is "
                    f"not registered in {registry.registry_path(state_root)}; no run state was "
                    f"written — only the repository's local git exclude and the project "
                    f"registry, both idempotent scaffolding. Remove the orphaned record and "
                    f"retry:\n"
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


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
