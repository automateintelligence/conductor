import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def _gh_json(repo: str, pr: int, fields: str) -> Any:
    out = subprocess.run(
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", fields],
        capture_output=True,
        text=True,
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
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _remote_for(repo: str, run: Any = subprocess.run) -> str:
    """Pick the git remote whose URL points at <owner/repo>; fall back to 'origin'."""
    out = run(["git", "remote", "-v"], capture_output=True, text=True)
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and repo in parts[1]:
            return parts[0]
    return "origin"


def _merge_ref_verify(
    repo: str, pr: int, local_verify: str, run: Any = subprocess.run
) -> bool:
    """§6.2: re-verify on the ACTUAL merge ref (base+PR merged), not the current workspace."""
    remote = _remote_for(repo, run)
    wt = tempfile.mkdtemp(prefix=f"mergeref-{pr}-")
    try:
        if (
            run(
                f"git fetch {remote} refs/pull/{pr}/merge && git worktree add --detach {wt} FETCH_HEAD",
                shell=True,
            ).returncode
            != 0
        ):
            return False
        return run(local_verify, shell=True, cwd=wt).returncode == 0
    finally:
        run(f"git worktree remove --force {wt}", shell=True)
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
    d: Any = gh_json(repo, pr, "mergeStateStatus,mergeable,reviewDecision,isDraft")
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
    blockers += threads(repo, pr)  # 'unresolved' and/or 'threads-unpaginated'
    if not merge_ref_verify(repo, pr, local_verify):
        blockers.append("merge-ref-verify-failed")
    return {"ok": not blockers, "blockers": blockers}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: conductor merge-gate <pr>", file=sys.stderr)
        sys.exit(2)
    pr_num = int(sys.argv[1])
    repo = (
        os.environ.get("CONDUCTOR_REPO")
        or subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    local_verify = os.environ.get("CONDUCTOR_MERGE_VERIFY", "pytest -q")
    result = check(repo, pr_num, local_verify=local_verify)
    ok: bool = result["ok"]
    blockers: list[str] = result["blockers"]
    for b in blockers:
        print(b, file=sys.stderr)
    sys.exit(0 if ok else 1)
