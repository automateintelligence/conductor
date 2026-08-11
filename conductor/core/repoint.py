"""``conductor run repoint-spec`` — move a spec inside the repository without minting a new run.

The run key hashes the repository-relative spec path, so renaming a spec would otherwise derive a
different key: a second run, a second gate, a second branch, and a silently abandoned first run.
Repointing keeps the key and rewrites the mapping instead (design §"Project and run identity").

Safety comes from five checks and one journal:

* no live owner — an executing worker holds ``owner.lock``, and rewriting its spec path underneath
  it would desynchronize the goal it already loaded;
* the old and new paths must describe the same content — a Git rename (staged or already
  committed), or a digest equal to the ``spec_digest`` recorded when the run was created;
* the target must not already be mapped, not even by a fully terminal generation list: the mapping
  is replaced wholesale, so repointing onto one would drop that run's history from the registry;
* every run record in the mapping must agree with the registry about where it currently lives, or
  the path history this operation writes would be fiction;
* ``project.json`` and EVERY generation's ``run.json`` change through one journalled transaction,
  so a crash completes or reverses all of them and never leaves a split identity.

A spec path's mapping holds every generation ever started for it, and the registry moves the whole
entry, so every generation's record moves with it — rewriting only the named run would leave its
siblings claiming the old path while the registry reports the new one. That divergence is exactly
what the agreement check above refuses to operate on, so it would strand those generations.
Multi-run locking follows the design's rule: ``owner.lock`` for every generation in sorted run-key
order, then ``state.lock`` for every generation in the same order, which is the only order
``locks._check_order`` permits.

Locks are taken here in the global order (project, owner, state) and held across the whole
sequence, so this module writes through ``transaction`` rather than ``registry.commit`` /
``runstate.commit`` — those take the same locks and would be re-entrant acquisitions.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import subprocess

from conductor.core import locks, registry, runkey, runstate, schema, transaction

_GIT_TIMEOUT = 30.0
# How far back a committed rename is looked for. `git log --follow` already prunes to commits that
# touch the spec, so a rename is normally the first or second entry; the bound keeps a pathological
# history from turning a refusal check into a long walk.
_RENAME_HISTORY_LIMIT = 50


class RepointRefused(RuntimeError):
    """The repoint was refused before any write occurred."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def _names_the_rename(stdout: str, old_rel: str, new_rel: str) -> bool:
    """Whether ``--name-status`` output contains ``R<score>\told_rel\tnew_rel``."""
    for line in stdout.splitlines():
        parts = line.split("\t")
        if (
            len(parts) == 3
            and parts[0].startswith("R")
            and parts[1] == old_rel
            and parts[2] == new_rel
        ):
            return True
    return False


def _staged_rename(repo_root: str, old_rel: str, new_rel: str) -> bool:
    """Whether the rename is visible in the index or working tree but not yet committed."""
    out = _git(
        repo_root,
        "diff",
        "-M",
        "--name-status",
        "HEAD",
        "--",
        old_rel,
        new_rel,
    )
    return out.returncode == 0 and _names_the_rename(out.stdout, old_rel, new_rel)


def _committed_rename(repo_root: str, old_rel: str, new_rel: str) -> bool:
    """Whether the rename is already recorded in history.

    Design line 178 says "the same Git rename", not "the same uncommitted Git rename": an operator
    who ran ``git mv`` and committed before repointing has done nothing wrong, and once the commit
    lands the working-tree diff above sees nothing at all."""
    out = _git(
        repo_root,
        "log",
        "--follow",
        "--name-status",
        "--format=",
        "-M",
        f"-n{_RENAME_HISTORY_LIMIT}",
        "--",
        new_rel,
    )
    return out.returncode == 0 and _names_the_rename(out.stdout, old_rel, new_rel)


def _rename_detected(repo_root: str, old_rel: str, new_rel: str) -> bool:
    """Whether git sees the change as a rename from ``old_rel`` to ``new_rel``, staged or
    committed. This is the only authorizer for a rename that also edited the spec, which no
    digest can cover."""
    return _staged_rename(repo_root, old_rel, new_rel) or _committed_rename(
        repo_root, old_rel, new_rel
    )


def _digest_matches(repo_root: str, run: dict, new_rel: str) -> bool:
    """Whether the file at ``new_rel`` still hashes to the run's recorded ``spec_digest``."""
    recorded = run.get("spec_digest")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        with open(os.path.join(repo_root, new_rel), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest() == recorded
    except OSError:
        return False


def content_identity_matches(
    repo_root: str, run: dict, old_rel: str, new_rel: str
) -> bool:
    """Whether the old and new paths describe the same spec: a Git rename, or content equal to the
    digest approved when the run was created."""
    return _rename_detected(repo_root, old_rel, new_rel) or _digest_matches(
        repo_root, run, new_rel
    )


def _generation_keys(mapping: dict) -> list[str]:
    """Every run key in one spec-path mapping, sorted — the order multi-run operations must take
    per-run locks in (design §"Failure handling", enforced by ``locks._check_order``)."""
    return sorted(
        {str(entry.get("run_key")) for entry in mapping.get("generations", [])}
    )


def repoint(
    state_root: str,
    *,
    repo_root: str,
    run_key: str,
    new_spec_path: str,
    owner_timeout: float = 5.0,
) -> dict:
    """Repoint ``run_key`` — and every other generation sharing its spec path — at
    ``new_spec_path``. Returns the updated run document for ``run_key``."""
    try:
        new_rel = runkey.normalize_spec_path(repo_root, new_spec_path)
    except ValueError as exc:
        raise RepointRefused(
            f"{exc}; no write occurred. Move the spec inside the repository first, then re-run: "
            f"conductor run repoint-spec --run {run_key} <path-under-{repo_root}>"
        ) from exc
    if not os.path.isfile(os.path.join(repo_root, new_rel)):
        raise RepointRefused(
            f"{new_rel} does not exist in {repo_root}; no write occurred. Move the spec first, "
            f"then re-run: conductor run repoint-spec --run {run_key} {new_rel}"
        )
    retry = f"conductor run repoint-spec --run {run_key} {new_rel}"
    with locks.hold(registry.lock_path(state_root), kind="project"):
        # `recover` WRITES (after-images landed, journal removed), so every refusal from here on
        # reports `status` rather than the bare phrase — the two refusals above run before it and
        # keep theirs.
        status = transaction.write_status(transaction.recover(state_root))
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)}; {status}. "
                "Create one with: conductor run new <spec.md>"
            )
        found = registry.find_run(project_doc, run_key)
        if found is None:
            raise RepointRefused(
                f"run {run_key!r} is not registered under {state_root}; {status}. "
                "List known runs with: conductor run list --all"
            )
        old_rel, _generation_entry = found
        keys = _generation_keys(project_doc["specs"][old_rel])
        with contextlib.ExitStack() as stack:
            for key in keys:
                try:
                    stack.enter_context(
                        locks.hold(
                            runstate.owner_lock_path(state_root, key),
                            kind="owner",
                            run_key=key,
                            timeout=owner_timeout,
                        )
                    )
                except locks.LockTimeout as exc:
                    # Plan 01 reads a busy owner.lock as a flat refusal; Plan 02 replaces this with
                    # lease and liveness interpretation without changing the acquisition order.
                    shares = (
                        ""
                        if key == run_key
                        else f" — it shares {old_rel} with run {run_key!r} and moves with it"
                    )
                    raise RepointRefused(
                        f"run {key!r} has a live owner holding "
                        f"{runstate.owner_lock_path(state_root, key)}{shares}; {status}. "
                        f"Stop the worker or wait for it to exit, then retry: {retry}"
                    ) from exc
            for key in keys:
                stack.enter_context(
                    locks.hold(
                        runstate.state_lock_path(state_root, key),
                        kind="state",
                        run_key=key,
                    )
                )
            runs: dict[str, dict] = {}
            for key in keys:
                doc = runstate.load(state_root, key)
                if doc is None:
                    raise RepointRefused(
                        f"run {key!r} is registered at {old_rel} but has no record at "
                        f"{runstate.run_path(state_root, key)}; {status}. It moves with "
                        f"run {run_key!r}, so the whole repoint is refused. List known runs "
                        "with: conductor run list --all"
                    )
                if doc.get("run_key") != key or doc.get("spec_path") != old_rel:
                    raise RepointRefused(
                        f"run {key!r} disagrees with the registry: project.json maps it to "
                        f"{old_rel!r}, while {runstate.run_path(state_root, key)} records "
                        f"run_key {doc.get('run_key')!r} at {doc.get('spec_path')!r}; {status}, "
                        f"including for run {run_key!r}. Reconcile them before "
                        f"repointing — inspect both with: conductor run show --run {key}"
                    )
                runs[key] = doc
            if old_rel == new_rel:
                return runs[run_key]
            existing = registry.mapping(project_doc, new_rel)
            if existing is not None:
                mapped = _generation_keys(existing)
                shown = registry.current_run_key(project_doc, new_rel) or (
                    mapped[0] if mapped else run_key
                )
                raise RepointRefused(
                    f"refusing to repoint run {run_key!r} onto {new_rel}: it is already mapped "
                    f"to run(s) {', '.join(mapped)}; {status} — replacing that mapping "
                    f"would drop its generation history, terminal or not. Inspect it with: "
                    f"conductor run show --run {shown}"
                )
            if not content_identity_matches(repo_root, runs[run_key], old_rel, new_rel):
                if os.path.exists(os.path.join(repo_root, old_rel)):
                    remedy = (
                        f"Stage the rename (git mv {old_rel} {new_rel}), or start a new run for "
                        f"the new spec: conductor run new {new_rel}"
                    )
                else:
                    # `git mv` is not a command the operator can still run: the old path is gone.
                    remedy = (
                        f"{old_rel} no longer exists, so the rename can no longer be staged — "
                        f"restore {new_rel} to the content recorded for run {run_key!r}, or start "
                        f"a new run for the new spec: conductor run new {new_rel}"
                    )
                raise RepointRefused(
                    f"{old_rel} and {new_rel} are not the same spec: git records no rename "
                    f"between them, staged or committed, and {new_rel} does not match the digest "
                    f"approved for run {run_key!r}; {status}. {remedy}"
                )
            new_project = schema.clone(project_doc)
            moved = new_project["specs"].pop(old_rel)
            moved["path_history"] = [*moved.get("path_history", []), old_rel]
            new_project["specs"][new_rel] = moved
            new_project["revision"] = project_doc["revision"] + 1
            schema.validate_project(new_project)

            stamp = _now()
            after: dict[str, dict] = {}
            entries = [
                {
                    "path": registry.registry_path(state_root),
                    "before": project_doc,
                    "after": new_project,
                }
            ]
            for key in keys:
                new_run = schema.clone(runs[key])
                new_run["spec_path"] = new_rel
                new_run["path_history"] = [*new_run.get("path_history", []), old_rel]
                new_run["revision"] = runs[key]["revision"] + 1
                new_run["updated_at"] = stamp
                schema.validate_run(new_run)
                after[key] = new_run
                entries.append(
                    {
                        "path": runstate.run_path(state_root, key),
                        "before": runs[key],
                        "after": new_run,
                    }
                )

            txn_id = f"repoint-{run_key}"
            transaction.prepare(state_root, txn_id, entries)
            transaction.commit(state_root, txn_id)
            transaction.apply(state_root, txn_id)
            return after[run_key]
