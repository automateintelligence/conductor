"""Where a conductor run's state and done-gate live.

The plugin dir holds read-only TOOL CODE; a run's state + gate belong to the PROJECT — the git
repo you invoke conductor from. ``bin/conductor`` resolves the project once and exports
``CONDUCTOR_HOME`` so the runner, the freeze guard, and the handoff writer all agree on the same
location. Kept in one module so those callers cannot diverge.
"""

from __future__ import annotations

import os
import subprocess


def project_root() -> str:
    """The PROJECT that owns run state + the done-gate: ``$CONDUCTOR_HOME``, else the git repo
    of the current directory, else the current directory. Distinct from the plugin dir (tool
    code), which must never hold a project's gate/state."""
    home = os.environ.get("CONDUCTOR_HOME")
    if home:
        return home
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return os.getcwd()
