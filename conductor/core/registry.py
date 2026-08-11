"""``project.json`` — one registry per project.

It records the state schema, its own monotonic revision, stable repository identity, the
normalized-spec-path-to-run-key mappings, and the workstation identity that owns this
project-local state. Each spec-path mapping is an ordered generation list with at most one
nonterminal run designated ``current``; starting a path whose latest generation is terminal
requires an explicit new generation.

Mutations are read-modify-write under ``project.lock`` with a revision compare-and-swap. The lock
serializes writers on one machine; the revision check catches a writer that read the document
*before* taking the lock — which ``update`` does on purpose, so the retry path is the normal path
rather than an untested branch.

AUTHORITY: the per-generation ``status`` recorded here is a MIRROR, kept for the new-generation
policy and cheap listing. ``run.json`` is the authority for run status, and
``resolve.active_run_keys`` reads run records rather than this mirror. Never let a decision that
matters hang off the mirror alone.
"""

from __future__ import annotations

from collections.abc import Callable

from conductor.core import atomic, locks, schema, transaction


class RegistryMissing(RuntimeError):
    """No ``project.json`` under this state root."""


class RevisionConflict(RuntimeError):
    """``project.json`` advanced underneath a writer that read an older revision."""


def registry_path(state_root: str) -> str:
    return f"{state_root}/project.json"


def lock_path(state_root: str) -> str:
    return f"{state_root}/project.lock"


def load(state_root: str) -> dict | None:
    """The registry, or ``None`` when this project has none yet."""
    return atomic.read_json(registry_path(state_root))


def init(state_root: str, *, workstation_id: str, repo_identity: dict) -> dict:
    """Create the registry if absent; return the existing one otherwise. Never overwrites, so a
    second caller cannot reset the workstation identity or drop mappings."""
    with locks.hold(lock_path(state_root), kind="project"):
        transaction.recover(state_root)
        existing = load(state_root)
        if existing is not None:
            return schema.validate_project(existing)
        doc = schema.new_project_doc(
            workstation_id=workstation_id, repo_identity=repo_identity
        )
        atomic.write_json_atomic(registry_path(state_root), doc)
        return doc


def commit(state_root: str, doc: dict, *, expect_revision: int) -> dict:
    """Write ``doc`` if the on-disk revision still equals ``expect_revision``.

    Completes or reverses any unfinished transaction first, then validates, then bumps the
    revision. Raises ``RevisionConflict`` without writing when the document moved."""
    with locks.hold(lock_path(state_root), kind="project"):
        # `recover` WRITES — after-images landed, journal removed — so the refusals below cannot
        # claim "no write occurred" when it handled anything. `write_status` is the one place
        # that decides which phrase is true.
        status = transaction.write_status(transaction.recover(state_root))
        current = load(state_root)
        if current is None:
            raise RegistryMissing(
                f"no project registry at {registry_path(state_root)}; {status}. "
                "Create one with: conductor run new <spec.md>"
            )
        if current["revision"] != expect_revision:
            raise RevisionConflict(
                f"project.json moved from revision {expect_revision} to {current['revision']} "
                f"at {registry_path(state_root)}; {status}. Re-read and retry."
            )
        proposed = dict(doc)
        proposed["revision"] = expect_revision + 1
        schema.validate_project(proposed)
        atomic.write_json_atomic(registry_path(state_root), proposed)
        return proposed


def update(
    state_root: str, mutate: Callable[[dict], dict], *, attempts: int = 5
) -> dict:
    """Apply ``mutate`` to a private copy of the registry and commit it, retrying on conflict.

    ``mutate`` receives a deep copy, must return the new document, and must not change
    ``revision`` — ``commit`` owns that."""
    last: RevisionConflict | None = None
    for _ in range(max(1, attempts)):
        current = load(state_root)
        if current is None:
            raise RegistryMissing(
                f"no project registry at {registry_path(state_root)}; no write occurred. "
                "Create one with: conductor run new <spec.md>"
            )
        expect = current["revision"]
        try:
            return commit(
                state_root, mutate(schema.clone(current)), expect_revision=expect
            )
        except RevisionConflict as exc:
            last = exc
    raise RevisionConflict(
        f"project.json at {registry_path(state_root)} changed under {attempts} attempts; "
        f"no write occurred. Last conflict: {last}"
    )


def mapping(doc: dict, normalized_spec_path: str) -> dict | None:
    """This spec path's mapping, or ``None``."""
    value = doc.get("specs", {}).get(normalized_spec_path)
    return value if isinstance(value, dict) else None


def current_run_key(doc: dict, normalized_spec_path: str) -> str | None:
    """The nonterminal run key for this spec path, or ``None`` when every generation ended."""
    entry = mapping(doc, normalized_spec_path)
    return entry.get("current") if entry else None


def next_generation(doc: dict, normalized_spec_path: str) -> int:
    """The generation a new run for this spec path would take."""
    entry = mapping(doc, normalized_spec_path)
    if not entry or not entry.get("generations"):
        return 1
    return max(int(g["generation"]) for g in entry["generations"]) + 1


def run_keys(doc: dict) -> list[str]:
    """Every run key in the registry, sorted."""
    return sorted(
        g["run_key"]
        for entry in doc.get("specs", {}).values()
        for g in entry.get("generations", [])
    )


def find_run(doc: dict, run_key: str) -> tuple[str, dict] | None:
    """``(spec_path, generation_entry)`` for ``run_key``, or ``None``."""
    for spec_path, entry in doc.get("specs", {}).items():
        for generation in entry.get("generations", []):
            if generation.get("run_key") == run_key:
                return spec_path, generation
    return None


def register(doc: dict, *, spec: str, run_key: str, generation: int) -> dict:
    """Append a generation for ``spec`` and make it current. Pure: mutates and returns ``doc``.

    Validation of the "at most one nonterminal generation" rule happens in
    ``schema.validate_project`` during ``commit``, so an attempt to register a second live
    generation is refused there rather than silently accepted here."""
    entry = doc.setdefault("specs", {}).setdefault(
        spec, {"generations": [], "current": None, "path_history": []}
    )
    entry.setdefault("path_history", [])
    entry["generations"].append(
        {"run_key": run_key, "generation": generation, "status": "active"}
    )
    entry["generations"].sort(key=lambda g: g["generation"])
    entry["current"] = run_key
    return doc


def mirror_status(doc: dict, run_key: str, status: str) -> dict:
    """Refresh the status mirror for ``run_key`` and recompute ``current``. Pure."""
    if status not in schema.RUN_STATUSES:
        raise schema.SchemaError(
            f"status {status!r}; expected one of {schema.RUN_STATUSES}"
        )
    found = find_run(doc, run_key)
    if found is None:
        raise KeyError(f"run {run_key!r} is not registered")
    spec_path, generation = found
    generation["status"] = status
    entry = doc["specs"][spec_path]
    live = [
        g["run_key"]
        for g in entry["generations"]
        if g["status"] not in schema.TERMINAL_STATUSES
    ]
    entry["current"] = live[0] if live else None
    return doc
