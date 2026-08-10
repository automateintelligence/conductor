"""The workstation identity shared by both host adapters.

Design §"Project and run identity": ``project.json`` records the workstation that owns a
project's local state, and a checkout whose registry names a *different* workstation refuses
automatic takeover. That identity is a random Conductor installation ID in host-neutral user
configuration — deliberately not a hostname, username, MAC address, or machine-id, so the file
carries nothing personal and copying a home directory does not silently authorize takeover.

Created exactly once. Creation uses ``O_EXCL`` so two adapters racing on first use converge on
one value instead of overwriting each other.
"""

from __future__ import annotations

import json
import os
import secrets

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    candidate = secrets.token_hex(16)
    payload = (
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "workstation_id": candidate},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another adapter created it between our read and our create: theirs wins.
        winner = atomic.read_json(path)
        if isinstance(winner, dict) and isinstance(winner.get("workstation_id"), str):
            return winner["workstation_id"]
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return candidate
