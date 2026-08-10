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


def write_atomic(path: str, data: str | bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` durably. Creates missing parents. On any failure the temp
    file is removed and ``path`` keeps its previous contents."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".conductor-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
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
    state on top of a corrupted run."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
