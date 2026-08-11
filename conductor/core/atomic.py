"""Durable state writes.

Every Conductor state file (``project.json``, ``run.json``, transaction journals, ownership
records) is written the same way: sibling temporary file, write, flush, fsync, atomic replace,
then fsync the containing directory. A crash therefore leaves either the previous bytes or the
complete new ones — never a truncated file that reads as corrupt state on the next heartbeat.

Locking and revision checks live in ``locks``/``registry``/``runstate``; this module only
guarantees the bytes.
"""

from __future__ import annotations

import json
import os
import tempfile


def _fsync_dir(directory: str) -> None:
    """fsync the directory entry so the rename itself is durable, not just the file bytes."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(directory: str) -> None:
    """Public form of the directory fsync, for callers that publish a name themselves."""
    _fsync_dir(directory)


def makedirs_durably(directory: str) -> None:
    """Create ``directory`` and fsync the entry that links each newly created level to its parent.

    ``os.makedirs`` can create several levels at once. Fsyncing only the leaf — which is all the
    write path did — leaves those links unflushed, so a crash can lose the entire new subtree and
    with it a state file whose own bytes were faithfully fsynced. The first run of a project
    creates ``.conductor/runs/<run-key>/`` in one call, which is exactly this shape."""
    if os.path.isdir(directory):
        return
    created: list[str] = []
    current = directory
    while not os.path.isdir(current):
        created.append(current)
        parent = os.path.dirname(current)
        if parent == current:  # reached the filesystem root
            break
        current = parent
    os.makedirs(directory, exist_ok=True)
    # Deepest-existing ancestor outward: each level is durable only once the directory holding
    # its entry is fsynced, so walk back down from the ancestor that already existed.
    for path in reversed(created):
        _fsync_dir(os.path.dirname(path) or ".")


def write_atomic(path: str, data: str | bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` durably. Creates missing parents. On any failure the temp
    file is removed and ``path`` keeps its previous contents."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    directory = os.path.dirname(os.path.abspath(path)) or "."
    makedirs_durably(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".conductor-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            # Set the mode BEFORE the fsync, on the same descriptor. A chmod afterwards would
            # mutate the inode with nothing left to flush it — the directory fsync below covers
            # the rename's entry, not the mode — so a crash could publish the file with
            # mkstemp's private 0600 instead of the mode the caller asked for.
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def write_json_atomic(path: str, doc: dict, *, mode: int = 0o644) -> None:
    """Write ``doc`` as sorted, indented JSON. Sorted keys keep diffs and digests stable."""
    write_atomic(path, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode=mode)


def remove_durably(path: str) -> None:
    """Delete ``path`` and fsync its directory, so the removal survives an unclean shutdown.

    ``write_atomic`` fsyncs the directory after its rename; a bare ``os.unlink`` does not, and an
    unfsynced delete can be reordered after a later durable operation. Deleting a file the caller
    asked to remove therefore needs the same discipline as writing one. Absent is success — the
    postcondition is that the path does not exist."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_dir(os.path.dirname(os.path.abspath(path)) or ".")


def read_json(path: str) -> dict | None:
    """The document at ``path``, or ``None`` when the file does not exist.

    Malformed JSON raises rather than returning ``None``: an unreadable state file is a
    fail-closed condition, and silently treating it as "absent" would let a caller mint fresh
    state on top of a corrupted run. Well-formed JSON that is not an OBJECT raises for the same
    reason — the return type says ``dict``, and ``registry.load`` / ``runstate.load`` hand this
    value straight to callers that subscript it, so a top-level array would surface as an
    ``AttributeError`` traceback instead of a refusal naming the file."""
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(doc, dict):
        raise ValueError(
            f"{path} does not hold a JSON object (found {type(doc).__name__}); refusing to read "
            "it as state"
        )
    return doc
