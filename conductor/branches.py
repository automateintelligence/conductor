"""Single-sourced branch identifiers: `run-branch name <spec>` and `default-branch`.

Review B-5: start and autodev each derived `conductor/run-<spec-slug>` and "the default
branch" in prose, so the two skills could diverge (and did drift in review). Mirrors the
`conductor remote` precedent — ONE implementation per cross-skill string contract, exposed
as a CLI verb the prose calls instead of re-deriving.

`run_branch_name` is a pure deterministic function of the spec path: same spec → the
byte-identical `conductor/run-<slug>`, different specs → different names. `default_branch`
resolves the repo's real default (gh repo view, then the origin/HEAD symbolic ref) and
fails OPEN to `main` — exit 0, NEVER an empty string, which would make a downstream
`git fetch "$R" ""` operate on the wrong ref.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import sys

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))


def run_branch_name(spec_path: str) -> str:
    """The canonical run branch for a spec: `conductor/run-<slug>`, deterministic.

    Slug = the spec filename's stem, lowercased, non-`[a-z0-9._-]` runs collapsed to one
    hyphen, then stripped of leading/trailing `-`/`.` so the result is a valid ref segment
    matching `[a-z0-9][a-z0-9._-]*`. A stem that strips to nothing (or can't start with an
    alphanumeric) falls back to a deterministic `spec-<sha256[:8]>` of the full path —
    still unique per spec, never an invalid ref."""
    stem = pathlib.PurePath(spec_path).stem.lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-.")
    if not slug or not re.match(r"[a-z0-9]", slug):
        slug = "spec-" + hashlib.sha256(spec_path.encode()).hexdigest()[:8]
    return f"conductor/run-{slug}"


def _gh_default() -> str | None:
    """gh knows the server truth; time-bounded, any failure → None (next probe)."""
    out = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _git_default() -> str | None:
    """The `refs/remotes/<remote>/HEAD` symbolic ref — local, no network. The remote comes
    from `conductor.remote`'s resolver (the same one the merge gate uses), falling back to
    `origin` when discovery fails."""
    try:
        from conductor.remote import resolve

        remote = resolve() or "origin"
    except Exception:
        remote = "origin"
    from conductor.paths import project_root

    out = subprocess.run(
        ["git", "-C", project_root(), "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        return None
    target = (out.stdout or "").strip()
    prefix = f"refs/remotes/{remote}/"
    if not target.startswith(prefix):
        return None
    return target[len(prefix) :] or None


def default_branch() -> str:
    """The repo's default branch; ANY failure or empty result fails open to `main`."""
    for probe in (_gh_default, _git_default):
        try:
            name = probe()
        except Exception:  # timeout/missing binary/bad repo → try the next probe
            name = None
        if name:
            return name
    return "main"


_USAGE = (
    "usage:\n"
    "  conductor run-branch name <spec.md>   emit the canonical conductor/run-<slug>\n"
    "  conductor default-branch              emit the repo default (fail-open: main)\n"
)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "name":
        if len(argv) != 2:
            print("usage: conductor run-branch name <spec.md>", file=sys.stderr)
            return 64
        print(run_branch_name(argv[1]))
        return 0
    if argv and argv[0] == "default":
        print(default_branch())
        return 0
    print(_USAGE, file=sys.stderr, end="")
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
