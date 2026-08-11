"""``conductor run repoint-spec`` — move a spec inside the repository without minting a new run.

The run key hashes the repository-relative spec path, so renaming a spec would otherwise derive a
different key: a second run, a second gate, a second branch, and a silently abandoned first run.
Repointing keeps the key and rewrites the mapping instead (design §"Project and run identity").

Safety comes from five checks and one journal:

* no live owner — an executing worker holds ``owner.lock``, and rewriting its spec path underneath
  it would desynchronize the goal it already loaded;
* the old and new paths must describe the same content — a staged Git rename, or a digest equal to
  the ``spec_digest`` recorded when the run was created;
* the target must not already be mapped, not even by a fully terminal generation list: the mapping
  is replaced wholesale, so repointing onto one would drop that run's history from the registry;
* the run record must agree with the registry about where the run currently lives, or the path
  history this operation writes would be fiction;
* both ``project.json`` and ``run.json`` change through one journalled transaction, so a crash
  completes or reverses both and never leaves a split identity.

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


class RepointRefused(RuntimeError):
    """The repoint was refused before any write occurred."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _rename_detected(repo_root: str, old_rel: str, new_rel: str) -> bool:
    """Whether git sees the change as a rename from ``old_rel`` to ``new_rel``."""
    out = subprocess.run(
        [
            "git",
            "-C",
            repo_root,
            "diff",
            "-M",
            "--name-status",
            "HEAD",
            "--",
            old_rel,
            new_rel,
        ],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if (
            len(parts) == 3
            and parts[0].startswith("R")
            and parts[1] == old_rel
            and parts[2] == new_rel
        ):
            return True
    return False


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
    """Whether the old and new paths describe the same spec: a staged Git rename, or content
    equal to the digest approved when the run was created."""
    return _rename_detected(repo_root, old_rel, new_rel) or _digest_matches(
        repo_root, run, new_rel
    )


def repoint(
    state_root: str,
    *,
    repo_root: str,
    run_key: str,
    new_spec_path: str,
    owner_timeout: float = 5.0,
) -> dict:
    """Repoint ``run_key`` at ``new_spec_path``. Returns the updated run document."""
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
    with locks.hold(registry.lock_path(state_root), kind="project"):
        transaction.recover(state_root)
        project_doc = registry.load(state_root)
        if project_doc is None:
            raise registry.RegistryMissing(
                f"no project registry at {registry.registry_path(state_root)}; no write occurred. "
                "Create one with: conductor run new <spec.md>"
            )
        found = registry.find_run(project_doc, run_key)
        if found is None:
            raise RepointRefused(
                f"run {run_key!r} is not registered under {state_root}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        old_rel, _generation_entry = found
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(
                    locks.hold(
                        runstate.owner_lock_path(state_root, run_key),
                        kind="owner",
                        run_key=run_key,
                        timeout=owner_timeout,
                    )
                )
            except locks.LockTimeout as exc:
                # Plan 01 reads a busy owner.lock as a flat refusal; Plan 02 replaces this with
                # lease and liveness interpretation without changing the acquisition order.
                raise RepointRefused(
                    f"run {run_key!r} has a live owner holding "
                    f"{runstate.owner_lock_path(state_root, run_key)}; no write occurred. "
                    f"Stop the worker or wait for it to exit, then retry: "
                    f"conductor run repoint-spec --run {run_key} {new_rel}"
                ) from exc
            with locks.hold(
                runstate.state_lock_path(state_root, run_key),
                kind="state",
                run_key=run_key,
            ):
                run_doc = runstate.load(state_root, run_key)
                if run_doc is None:
                    raise RepointRefused(
                        f"run {run_key!r} is registered but has no record at "
                        f"{runstate.run_path(state_root, run_key)}; no write occurred. "
                        "List known runs with: conductor run list --all"
                    )
                if (
                    run_doc.get("run_key") != run_key
                    or run_doc.get("spec_path") != old_rel
                ):
                    raise RepointRefused(
                        f"run {run_key!r} disagrees with the registry: project.json maps it to "
                        f"{old_rel!r}, while {runstate.run_path(state_root, run_key)} records "
                        f"run_key {run_doc.get('run_key')!r} at {run_doc.get('spec_path')!r}; "
                        f"no write occurred. Reconcile them before repointing — inspect both "
                        f"with: conductor run show --run {run_key}"
                    )
                if old_rel == new_rel:
                    return run_doc
                existing = registry.mapping(project_doc, new_rel)
                if existing is not None:
                    mapped = [
                        str(entry.get("run_key"))
                        for entry in existing.get("generations", [])
                    ]
                    shown = registry.current_run_key(project_doc, new_rel) or (
                        mapped[0] if mapped else run_key
                    )
                    raise RepointRefused(
                        f"{new_rel} is already mapped to run(s) {', '.join(mapped)}; no write "
                        f"occurred — replacing that mapping would drop its generation history, "
                        f"terminal or not. Inspect it with: conductor run show --run {shown}"
                    )
                if not content_identity_matches(repo_root, run_doc, old_rel, new_rel):
                    raise RepointRefused(
                        f"{old_rel} and {new_rel} are not the same spec: git records no rename "
                        f"between them and {new_rel} does not match the digest approved for run "
                        f"{run_key!r}; no write occurred. Stage the rename "
                        f"(git mv {old_rel} {new_rel}) or start a new run for the new spec."
                    )
                new_project = schema.clone(project_doc)
                moved = new_project["specs"].pop(old_rel)
                moved["path_history"] = [*moved.get("path_history", []), old_rel]
                new_project["specs"][new_rel] = moved
                new_project["revision"] = project_doc["revision"] + 1
                schema.validate_project(new_project)

                new_run = schema.clone(run_doc)
                new_run["spec_path"] = new_rel
                new_run["path_history"] = [*new_run.get("path_history", []), old_rel]
                new_run["revision"] = run_doc["revision"] + 1
                new_run["updated_at"] = _now()
                schema.validate_run(new_run)

                txn_id = f"repoint-{run_key}"
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
                            "before": run_doc,
                            "after": new_run,
                        },
                    ],
                )
                transaction.commit(state_root, txn_id)
                transaction.apply(state_root, txn_id)
                return new_run
