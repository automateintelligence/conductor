"""The workstation identity shared by both host adapters.

Design §"Project and run identity": ``project.json`` records the workstation that owns a
project's local state, and a checkout whose registry names a *different* workstation refuses
automatic takeover. That identity is a random Conductor installation ID in host-neutral user
configuration — deliberately not a hostname, username, MAC address, or machine-id, so the file
carries nothing personal and copying a home directory does not silently authorize takeover.

Created exactly once. Creation writes the document under a private temporary name and publishes
it with ``os.link``, so two adapters racing on first use converge on one value and the final name
never exists in a partial state.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile

from conductor.core import atomic

SCHEMA_VERSION = 1


def config_home() -> str:
    """The host-neutral Conductor config directory: ``$CONDUCTOR_CONFIG_HOME``, else
    ``$XDG_CONFIG_HOME/conductor``, else ``~/.config/conductor``."""
    override = os.environ.get("CONDUCTOR_CONFIG_HOME")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "conductor")


def installation_file() -> str:
    """Where the installation ID lives."""
    return os.path.join(config_home(), "installation.json")


def workstation_id() -> str:
    """Read the installation ID, creating it on first use. Mode 0600, no personal data."""
    path = installation_file()
    existing = atomic.read_json(path)
    if isinstance(existing, dict):
        value = existing.get("workstation_id")
        if isinstance(value, str) and value:
            return value
    directory = os.path.dirname(path)
    atomic.makedirs_durably(directory)
    candidate = secrets.token_hex(16)
    payload = (
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "workstation_id": candidate},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    # Build the file under a private name and publish it with a link. `O_EXCL` on the final name
    # would create `installation.json` EMPTY and only then fill it, so a racing adapter reading
    # between those two steps sees `{}` — no `workstation_id` — and goes on to mint a second ID,
    # which is the one thing this file exists to prevent. `os.link` is equally exclusive (it
    # raises FileExistsError) but the name never exists in a partial state.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".installation-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            # Another adapter published between our read and our link: theirs wins. It is a
            # complete document by construction, so a missing id here means the file was
            # corrupted from outside, which stays fail-closed.
            winner = atomic.read_json(path)
            if isinstance(winner, dict) and isinstance(
                winner.get("workstation_id"), str
            ):
                return winner["workstation_id"]
            raise
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    atomic.fsync_dir(directory)
    return candidate
