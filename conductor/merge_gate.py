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
    merge_ref_verify: Any = _merge_ref_verify,
) -> dict[str, Any]:
    try:
        d: Any = gh_json(
            repo,
            pr,
            "mergeStateStatus,mergeable,reviewDecision,isDraft,body,comments,commits",
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
        if not _CLOSES_RE.search(d.get("body") or ""):
            blockers.append("closes-missing")  # recipe: one PR per phase, Closes #issue
        # env read per call (tests monkeypatch); empty marker would match everything
        marker = (os.environ.get("CONDUCTOR_REVIEW_MARKER") or "Codex review").lower()
        min_reviews = int(os.environ.get("CONDUCTOR_MIN_REVIEWS", "2"))
        if min_reviews > 0:  # 0 disables both review legs
            marked = [
                c
                for c in (d.get("comments") or [])
                if marker in (c.get("body") or "").lower()
            ]
            if len(marked) < min_reviews:
                blockers.append(f"reviews:{len(marked)}/{min_reviews}")
            if marked:  # zero markers already blocked above; skip double-reporting
                last_review = max(_ts(c["createdAt"]) for c in marked)
                last_commit = max(
                    _ts(c["committedDate"]) for c in d.get("commits") or []
                )
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
