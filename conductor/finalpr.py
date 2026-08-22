"""The run's final integration-to-default pull request: how it is identified, and the one thing
Conductor never does to it.

**Conductor never completes this pull request.** Design §"Heartbeat and autodev" and assertion
A-DH-7: no code path may merge, squash, rebase, force-push, close, retarget, auto-enroll or
merge-queue the pull request whose base is the repository default branch. A human on the
repository team merges it, and Conductor's job is to *observe* that they did. Every subprocess
this module runs is therefore a READ — ``git ls-remote``, ``gh pr view`` — and that is the whole
inventory. If a future edit adds a write here, it is the invariant breaking, not a feature.

The prohibition is not the same as inertness: ``conductor merge`` still merges PHASE pull
requests into the run integration branch (``conductor/merge_cmd.py`` refuses only ``base ==
default``). What stops at the default branch is the last hop.

IDENTIFICATION IS RECONCILIATION, NOT A LOOKUP. ``run.json`` is fourth in the design's evidence
precedence, behind GitHub pull-request state, so ``github.final_pr`` is a cache and not the
authority. When it is absent — the ordinary case for a run whose final pull request was opened
by hand, or by a generation of the tooling that did not record it — the pull request is
recovered from authoritative remote metadata:

1. Candidate numbers come from the remote's own ``refs/pull/<n>/{head,merge}`` refs. This is the
   only enumeration available through a plain fetch URL, and it is what makes the recovery work
   against a repository whose pull-request list the caller cannot page.
2. Each candidate is read with ``gh pr view`` and kept when its base is the repository default
   branch — the definition of "final" in the design's branch model.
3. When the run's integration branch resolves on the remote, candidates are further narrowed to
   the one whose head is that branch's tip. A repository with several open pull requests onto its
   default branch is normal; without this narrowing the recovery would be a coin flip. When the
   branch does NOT resolve (it was deleted after the pull request opened, or the record predates
   the current naming), the narrowing is skipped and uniqueness has to carry the identification
   on its own — which is why a non-unique result REFUSES rather than picking one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import NamedTuple

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))
_GIT_REMOTE_TIMEOUT = float(os.environ.get("CONDUCTOR_GIT_REMOTE_TIMEOUT", "60"))

#: Only fields every supported `gh` version returns for a pull request, and only ones this
#: module reads. Asking for more would couple the refusal path to fields it does not use.
_PR_FIELDS = ("baseRefName", "headRefOid", "state", "url")

_PULL_REF_RE = re.compile(r"^refs/pull/(\d+)/(?:head|merge)$")


class FinalPullRequestError(RuntimeError):
    """The final pull request could not be identified from authoritative remote metadata.

    Always a refusal, never a default. Guessing which pull request is "the final one" is how a
    cleanup path would delete the branch behind somebody else's review.
    """


class PullRequest(NamedTuple):
    number: int
    base: str
    head_sha: str
    state: str
    url: str

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED"


def _run(
    argv: list[str], *, cwd: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def view(repo: str, number: int, *, cwd: str | None = None) -> PullRequest:
    """Read one pull request. A read; it can complete nothing."""
    out = _run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            ",".join(_PR_FIELDS),
        ],
        cwd=cwd,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise FinalPullRequestError(
            f"could not read pull request #{number} in {repo}: "
            f"{(out.stderr or '').strip() or 'gh exited ' + str(out.returncode)}"
        )
    try:
        doc = json.loads(out.stdout or "{}")
        return PullRequest(
            number=number,
            base=str(doc["baseRefName"]),
            head_sha=str(doc["headRefOid"]),
            state=str(doc["state"]),
            url=str(doc["url"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise FinalPullRequestError(
            f"pull request #{number} in {repo} returned metadata this build cannot read "
            f"({exc}); refusing rather than assuming its state"
        ) from None


def candidate_numbers(repo_root: str, remote: str) -> list[int]:
    """Pull-request numbers the remote itself advertises, ascending.

    ``refs/pull/<n>/head`` and ``.../merge`` are the refs a fetch URL exposes. Both patterns are
    asked for because a repository may publish one and not the other."""
    out = _run(
        [
            "git",
            "-C",
            repo_root,
            "ls-remote",
            remote,
            "refs/pull/*/head",
            "refs/pull/*/merge",
        ],
        cwd=None,
        timeout=_GIT_REMOTE_TIMEOUT,
    )
    if out.returncode != 0:
        raise FinalPullRequestError(
            f"could not list pull-request refs on remote {remote!r}: "
            f"{(out.stderr or '').strip() or 'git ls-remote exited ' + str(out.returncode)}"
        )
    numbers = set()
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        match = _PULL_REF_RE.match(parts[1].strip())
        if match:
            numbers.add(int(match.group(1)))
    return sorted(numbers)


def remote_tip(repo_root: str, remote: str, branch: str) -> str | None:
    """The remote's tip for ``branch``, or ``None`` when the remote does not have it."""
    out = _run(
        ["git", "-C", repo_root, "ls-remote", remote, f"refs/heads/{branch}"],
        cwd=None,
        timeout=_GIT_REMOTE_TIMEOUT,
    )
    if out.returncode != 0:
        return None
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == f"refs/heads/{branch}":
            return parts[0].strip()
    return None


def recorded_number(run: dict) -> int | None:
    """``github.final_pr`` as an int, or ``None``. A malformed record is refused, not ignored."""
    github = run.get("github")
    value = github.get("final_pr") if isinstance(github, dict) else None
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise FinalPullRequestError(
            f"run {run.get('run_key')!r} records github.final_pr as {value!r}, which is not a "
            "pull-request number. Inspect it with: conductor run show --run "
            f"{run.get('run_key')}"
        ) from None
    if number <= 0:
        raise FinalPullRequestError(
            f"run {run.get('run_key')!r} records github.final_pr as {value!r}; pull-request "
            "numbers are positive"
        )
    return number


def reconcile(
    *,
    repo_root: str,
    repo: str,
    remote: str,
    default_branch: str,
    run: dict,
) -> tuple[PullRequest, bool]:
    """The run's final pull request, and whether it had to be recovered from the remote.

    ``(pull_request, recovered)``. ``recovered`` is ``True`` when ``github.final_pr`` was absent
    and the identification came from remote metadata, which is the caller's cue to record it.
    """
    recorded = recorded_number(run)
    if recorded is not None:
        return view(repo, recorded, cwd=repo_root), False

    integration = run.get("integration_branch")
    tip = (
        remote_tip(repo_root, remote, str(integration))
        if isinstance(integration, str) and integration
        else None
    )
    numbers = candidate_numbers(repo_root, remote)
    if not numbers:
        raise FinalPullRequestError(
            f"run {run.get('run_key')!r} records no final pull request and remote {remote!r} "
            f"advertises no refs/pull/* refs, so none can be identified; no write occurred. "
            f"Open the final pull request from {integration!r} onto {default_branch!r}, then "
            f"retry."
        )
    onto_default = [
        pr
        for pr in (view(repo, n, cwd=repo_root) for n in numbers)
        if pr.base == default_branch
    ]
    narrowed = (
        [pr for pr in onto_default if pr.head_sha == tip]
        if tip is not None
        else onto_default
    )
    if len(narrowed) == 1:
        return narrowed[0], True
    if not narrowed:
        raise FinalPullRequestError(
            f"run {run.get('run_key')!r} records no final pull request and none of the "
            f"{len(numbers)} pull request(s) {remote!r} advertises has base {default_branch!r}"
            + (f" at {integration!r}'s tip {tip}" if tip is not None else "")
            + "; no write occurred. Open it, or record the existing one, then retry."
        )
    listing = ", ".join(f"#{pr.number} ({pr.url})" for pr in narrowed)
    raise FinalPullRequestError(
        f"run {run.get('run_key')!r} records no final pull request and {len(narrowed)} pull "
        f"requests target {default_branch!r}: {listing}. Refusing to guess which one is this "
        f"run's; no write occurred. Re-run naming it: --pr <number>"
    )
