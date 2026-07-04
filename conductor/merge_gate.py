"""Mechanical merge gate: the one command a worker never skips.

Trust note: the process legs (Closes#, review count/provenance, review-stale)
defend against a NEGLIGENT worker dropping clerical steps, not an adversarial
one — the same worker already runs arbitrary CONDUCTOR_MERGE_VERIFY commands.
CONDUCTOR_REVIEW_AUTHOR narrows marker comments to a known reviewer account
(unset = any author; local-posting flows can't be provenance-checked), and
review-stale uses the newer of committedDate/pushedDate so a backdated or
amended committedDate can't slip under an old review where pushedDate exists
(GitHub deprecated pushedDate — observed null on current github.com pushes —
so committedDate is the floor)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))
_VERIFY_TIMEOUT = float(os.environ.get("CONDUCTOR_MERGE_VERIFY_TIMEOUT", "900"))
_CLOSES_RE = re.compile(r"(?i)\b(close[sd]?|fix(es|ed)?|resolve[sd]?)\s+#\d+")


def _expected_base() -> str | None:
    """The run's integration branch, when one is configured: env CONDUCTOR_RUN_BRANCH,
    else the `<project>/.conductor/run_branch` file `/conductor:start` writes (re-derived
    from `git ls-remote 'conductor/run-*'` on a fresh clone). None = no run topology
    configured → the base leg is disabled (0.4.x direct-merge runs keep working)."""
    env = (os.environ.get("CONDUCTOR_RUN_BRANCH") or "").strip()
    if env:
        return env
    from conductor.paths import project_root  # deferred: not needed on the env path

    path = os.path.join(project_root(), ".conductor", "run_branch")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if not value:  # present-but-empty = corrupt topology, not "no topology"
            raise ValueError(f"run-branch-empty: {path}")  # codex PR-31 #3
        return value
    return None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))  # gh emits trailing Z


def _gh_json(repo: str, pr: int, fields: str) -> Any:
    out = subprocess.run(
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", fields],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def _unresolved_threads(repo: str, pr: int) -> list[str]:
    """gh 2.4.0 has no reviewThreads json field -> GraphQL. MVP scans the first 100 threads; if
    there are MORE (hasNextPage) it fails closed ('threads-unpaginated') rather than risk missing
    an unresolved thread past page 1 (Codex minor)."""
    owner, name = repo.split("/")
    q = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
        "pullRequest(number:$n){reviewThreads(first:100){"
        "pageInfo{hasNextPage} nodes{isResolved}}}}}"
    )
    out = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={q}",
            "-F",
            f"o={owner}",
            "-F",
            f"r={name}",
            "-F",
            f"n={pr}",
            "--jq",
            "(.data.repository.pullRequest.reviewThreads|"
            '[(.nodes[]|select(.isResolved==false)|"unresolved"),'
            '(if .pageInfo.hasNextPage then "threads-unpaginated" else empty end)])[]',
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _newest_commit_dates(repo: str, pr: int) -> Any:
    """`gh pr view --json commits` returns only the FIRST 100 commits (unpaginated), so on a
    big PR max(committedDate) can miss the newest commit and review-stale would fail open.
    GraphQL commits(last:1) fetches the true newest commit directly, with both committedDate
    (author-controlled, backdatable) and pushedDate (server-set; null where deprecated)."""
    owner, name = repo.split("/")
    q = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
        "pullRequest(number:$n){commits(last:1){nodes{commit{committedDate pushedDate}}}}}}"
    )
    out = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={q}",
            "-F",
            f"o={owner}",
            "-F",
            f"r={name}",
            "-F",
            f"n={pr}",
            "--jq",
            ".data.repository.pullRequest.commits.nodes[0].commit",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    node = out.stdout.strip()
    if not node or node == "null":
        raise RuntimeError("newest-commit-empty")
    return json.loads(node)


def _remote_for(repo: str, run: Any = subprocess.run) -> str:
    """Pick the git remote whose URL points at <owner/repo>; fall back to 'origin'."""
    out = run(
        ["git", "remote", "-v"], capture_output=True, text=True, timeout=_GH_TIMEOUT
    )
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and repo in parts[1]:
            return parts[0]
    return "origin"


def _merge_ref_verify(
    repo: str, pr: int, local_verify: str, run: Any = subprocess.run
) -> bool:
    """§6.2: re-verify on the ACTUAL merge ref (base+PR merged), not the current workspace.
    Every subprocess (remote lookup, fetch, verify) is time-bounded; any hang fails closed."""
    wt = tempfile.mkdtemp(prefix=f"mergeref-{pr}-")
    try:
        remote = _remote_for(repo, run)  # inside the try: its timeout fails closed too
        fetched = run(
            f"git fetch {remote} refs/pull/{pr}/merge && git worktree add --detach {wt} FETCH_HEAD",
            shell=True,
            timeout=_GH_TIMEOUT,
        )
        if fetched.returncode != 0:
            return False
        return (
            run(local_verify, shell=True, cwd=wt, timeout=_VERIFY_TIMEOUT).returncode
            == 0
        )
    except subprocess.TimeoutExpired:  # a hung remote/fetch/verify fails closed
        return False
    finally:
        try:
            run(f"git worktree remove --force {wt}", shell=True, timeout=_GH_TIMEOUT)
        except Exception:  # cleanup is best-effort; never let it escape the gate
            pass
        shutil.rmtree(wt, ignore_errors=True)


def check(
    repo: str,
    pr: int,
    *,
    local_verify: str,
    gh_json: Any = _gh_json,
    threads: Any = _unresolved_threads,
    newest_commit: Any = _newest_commit_dates,
    merge_ref_verify: Any = _merge_ref_verify,
) -> dict[str, Any]:
    try:
        d: Any = gh_json(
            repo,
            pr,
            "mergeStateStatus,mergeable,reviewDecision,isDraft,body,comments,baseRefName",
        )
    except Exception as exc:  # gh failure/timeout -> fail closed, never crash the fire
        return {"ok": False, "blockers": [f"gh-error: {exc}"]}
    blockers: list[str] = []
    if d.get("isDraft"):
        blockers.append("draft")
    if (
        d.get("mergeStateStatus") != "CLEAN"
    ):  # CLEAN = checks green + reviews ok + current + queue ok
        blockers.append(f"merge-state:{d.get('mergeStateStatus')}")
    if d.get("mergeable") != "MERGEABLE":
        blockers.append(f"mergeable:{d.get('mergeable')}")
    if d.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("changes-requested")
    try:  # process legs: models drop clerical steps unless the gate enforces them
        # 0.5.0 run topology: phase PRs must target the run branch, never main directly.
        expected_base = _expected_base()
        if expected_base and (d.get("baseRefName") or "") != expected_base:
            blockers.append(f"base-mismatch:{d.get('baseRefName')}")
        if not _CLOSES_RE.search(d.get("body") or ""):
            blockers.append("closes-missing")  # recipe: one PR per phase, Closes #issue
        # env read per call (tests monkeypatch); empty marker would match everything
        marker = (os.environ.get("CONDUCTOR_REVIEW_MARKER") or "Codex review").lower()
        min_reviews = int(os.environ.get("CONDUCTOR_MIN_REVIEWS", "2"))
        if min_reviews < 0:  # 0 disables the review legs; negative is invalid
            raise ValueError(f"CONDUCTOR_MIN_REVIEWS must be >= 0, got {min_reviews}")
        # provenance (see module trust note); GH logins compare case-insensitively
        author = (os.environ.get("CONDUCTOR_REVIEW_AUTHOR") or "").lower()
        if min_reviews > 0:  # 0 disables both review legs
            marked = [
                c
                for c in (d.get("comments") or [])
                if marker in (c.get("body") or "").lower()
                and (
                    not author
                    or ((c.get("author") or {}).get("login") or "").lower() == author
                )
            ]
            if len(marked) < min_reviews:
                blockers.append(f"reviews:{len(marked)}/{min_reviews}")
            if marked:  # zero markers already blocked above; skip double-reporting
                last_review = max(_ts(c["createdAt"]) for c in marked)
                nc = newest_commit(repo, pr)  # pagination-safe last:1 date pair
                dates = [
                    _ts(v) for v in (nc.get("committedDate"), nc.get("pushedDate")) if v
                ]
                if not dates:  # a commit with no usable timestamp -> fail closed
                    raise ValueError("newest-commit-dates-missing")
                last_commit = max(dates)  # server-set pushedDate beats backdating
                if last_review < last_commit:  # the FINAL branch state was not reviewed
                    blockers.append("review-stale")
    except Exception as exc:  # malformed data/env -> fail closed, never crash the gate
        blockers.append(f"process-check-error: {exc}")
    try:
        blockers += threads(repo, pr)  # 'unresolved' and/or 'threads-unpaginated'
    except Exception as exc:
        blockers.append(f"threads-error: {exc}")
    try:
        if not merge_ref_verify(repo, pr, local_verify):
            blockers.append("merge-ref-verify-failed")
    except (
        Exception
    ) as exc:  # timeout/error anywhere in the merge-ref path -> fail closed
        blockers.append(f"merge-ref-error: {exc}")
    return {"ok": not blockers, "blockers": blockers}


def _resolve_repo(run: Any = subprocess.run) -> str:
    """The repo the gate runs against: CONDUCTOR_REPO if set, else `gh repo view` — time-bounded
    and fail-closed (a hung or failed autodiscovery raises instead of stalling/crashing)."""
    repo = os.environ.get("CONDUCTOR_REPO")
    if repo:
        return repo
    out = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"repo-discovery-failed: {(out.stderr or '').strip()}")
    repo = (out.stdout or "").strip()
    if not repo:
        raise RuntimeError("repo-discovery-empty")
    return repo


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: conductor merge-gate <pr>", file=sys.stderr)
        sys.exit(2)
    pr_num = int(sys.argv[1])
    try:
        repo = _resolve_repo()
    except (subprocess.TimeoutExpired, RuntimeError) as exc:  # bounded + fail closed
        print(f"repo-error: {exc}", file=sys.stderr)
        sys.exit(1)
    local_verify = os.environ.get("CONDUCTOR_MERGE_VERIFY", "pytest -q")
    result = check(repo, pr_num, local_verify=local_verify)
    ok: bool = result["ok"]
    blockers: list[str] = result["blockers"]
    for b in blockers:
        print(b, file=sys.stderr)
    sys.exit(0 if ok else 1)
