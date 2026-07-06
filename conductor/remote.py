"""Print the git remote that points at THIS repo — a worker must not assume `origin`.

Live-run finding (2026-07-05): the autodev prose hardcoded `git fetch origin` / `git ls-remote
origin`, but `merge_gate._remote_for` already derives the remote by matching the repo URL and
falls back to `origin`. On a repo whose remote is named `github` (common), a worker following the
literal prose failed its run-branch-currency merge. This exposes the SAME resolver the merge gate
uses, so prose and gate agree instead of drifting. Fail-open to `origin` so a resolution error
degrades to the historical default rather than an empty remote.
"""

from __future__ import annotations

import sys

from conductor.merge_gate import _remote_for, _resolve_repo


def resolve() -> str:
    return _remote_for(_resolve_repo())


def main() -> int:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print("usage: conductor remote — print the git remote pointing at this repo (workers use\nthis instead of assuming 'origin'; many repos are 'github'). Falls back to 'origin'.")
        return 0
    try:
        print(resolve())
    except (
        Exception
    ) as exc:  # any discovery/subprocess failure -> historical default, never empty
        print("origin")
        print(f"remote-resolve fell back to origin: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
