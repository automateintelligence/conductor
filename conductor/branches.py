"""Single-sourced branch identifiers: `run-branch name <spec>` and `default-branch`.

Review B-5: start and autodev each derived `conductor/run-<spec-slug>` and "the default
branch" in prose, so the two skills could diverge (and did drift in review). Mirrors the
`conductor remote` precedent — ONE implementation per cross-skill string contract, exposed
as a CLI verb the prose calls instead of re-deriving.

`run_branch_name` is a pure deterministic function of the spec path: same spec → the
byte-identical `conductor/run-<slug>`, different spec STEMS → different names (the slug
carries the filename's stem — the a11-pinned granularity — so two specs sharing a stem in
different directories map to the same run branch; one run per spec stem). `default_branch`
resolves the repo's real default from AUTHORITATIVE remote metadata (gh repo view, then the
origin/HEAD symbolic ref) and fails CLOSED: when neither probe can name the branch it raises
`DefaultBranchUnresolvable` and the CLI verb exits non-zero having printed NOTHING on stdout.

It used to fail OPEN to the literal `main` (design §"Branch, worktree, and pull-request
model", assertion A-DH-6: "If the default branch cannot be resolved, every automated merge is
refused; there is no fallback default"). A guessed default is worse than no default: on a repo
whose default is `trunk` the guess silently names a branch that either does not exist or is
somebody else's, and the fetch/merge/PR-base built from it is wrong while looking healthy. The
empty-string hazard the fail-open was guarding — `git fetch "$R" ""` operating on the wrong ref
— is closed the other way: the verb never emits an empty line, because on failure it emits no
line at all and a non-zero status the shell caller must check (`D="$(conductor default-branch)"
|| exit 1`).
"""

from __future__ import annotations

import os
import subprocess
import sys

from conductor.paths import project_root, spec_slug

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))
_GIT_TIMEOUT = (
    10.0  # local symbolic-ref lookup, no network — decoupled from _GH_TIMEOUT
)


def run_branch_name(spec_path: str) -> str:
    """The canonical run branch for a spec: `conductor/run-<slug>`, deterministic.

    The slug is `conductor.paths.spec_slug` — the SINGLE source shared with the per-spec
    done-gate dir (`assertions/<slug>/`), so the run branch and the gate dir never diverge.
    Same spec -> byte-identical name; different spec stems -> different names."""
    return f"conductor/run-{spec_slug(spec_path)}"


def _gh_default() -> str | None:
    """gh knows the server truth; time-bounded, any failure → None (next probe).

    Bound to `project_root()` (like `_git_default`) — gh resolves the repo from its cwd,
    and the process cwd may be a DIFFERENT repo than `$CONDUCTOR_HOME`; both probes must
    answer for the same project."""
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
        cwd=project_root(),
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
    out = subprocess.run(
        ["git", "-C", project_root(), "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if out.returncode != 0:
        return None
    target = (out.stdout or "").strip()
    prefix = f"refs/remotes/{remote}/"
    if not target.startswith(prefix):
        return None
    return target[len(prefix) :] or None


class DefaultBranchUnresolvable(RuntimeError):
    """No authoritative probe could name the repository's default branch.

    Raised instead of returning a guess. Callers must propagate the refusal: nothing that
    consumes a default branch (fetch base, merge base, final-PR base, protection probe) has a
    safe thing to do without one."""


def default_branch() -> str:
    """The repo's default branch, from authoritative remote metadata only — fail CLOSED.

    Tries `gh repo view` (server truth), then the `refs/remotes/<remote>/HEAD` symbolic ref.
    If neither answers, raises `DefaultBranchUnresolvable` naming both probes. NEVER
    substitutes a literal (`main`/`master`/anything): A-DH-6 forbids a fallback default, and a
    wrong-but-plausible branch name is undetectable downstream."""
    for probe in (_gh_default, _git_default):
        try:
            name = probe()
        except Exception:  # timeout/missing binary/bad repo → try the next probe
            name = None
        if name:
            return name
    raise DefaultBranchUnresolvable(
        "unresolvable-default-branch: neither `gh repo view --json defaultBranchRef` nor the "
        "refs/remotes/<remote>/HEAD symbolic ref could name this repository's default branch. "
        "Refusing rather than substituting a fallback name. Fix the repository's remote "
        "metadata (authenticate `gh`, or run `git remote set-head <remote> -a`) and retry."
    )


_USAGE = (
    "usage:\n"
    "  conductor run-branch name <spec.md>   emit the canonical conductor/run-<slug>\n"
    "  conductor default-branch              emit the repo default (fail-closed: refuses,\n"
    "                                        printing nothing, when it cannot be resolved)\n"
)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "name":
        if len(argv) != 2:
            print("usage: conductor run-branch name <spec.md>", file=sys.stderr)
            return 64
        print(run_branch_name(argv[1]))
        return 0
    if argv and argv[0] == "default":
        try:
            name = default_branch()
        except DefaultBranchUnresolvable as exc:
            # stdout stays EMPTY — not an empty line. A shell caller doing
            # D="$(conductor default-branch)" gets "" plus a non-zero status to check;
            # printing a blank line here would look like a resolved value to `read`.
            print(str(exc), file=sys.stderr)
            return 1
        print(name)
        return 0
    print(_USAGE, file=sys.stderr, end="")
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
