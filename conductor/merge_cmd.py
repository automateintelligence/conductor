"""`conductor merge <pr>` — the one command a worker uses to merge a phase PR.

Why this exists (review B-1): `conductor merge-gate` is a *separate* command from the merge
itself, so the recipe was two prose steps ("run the gate" then "gh pr merge") and only the first
had code behind it. A negligent worker that ran `gh pr merge` directly bypassed the gate — and on
a free-plan private repo there is no server-side backstop. This fuses the two: the merge IS the
gate. It also refuses to merge to the DEFAULT branch, so a confused fire can never land the final
owner PR (base=default) on main — that PR is the owner's alone. `CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE=1`
is the explicit escape hatch for legacy 0.4.x direct-to-main runs (previously a documented env var
with no code consumer — review B-5).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from conductor.merge_gate import _resolve_repo, check

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))


def _default_branch(repo: str, run: Any = subprocess.run) -> str:
    out = run(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "defaultBranchRef",
            "-q",
            ".defaultBranchRef.name",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"default-branch-lookup-failed: {(out.stderr or '').strip()}"
        )
    name = (out.stdout or "").strip()
    if not name:
        raise RuntimeError("default-branch-empty")
    return name


def _pr_base(repo: str, pr: int, run: Any = subprocess.run) -> str:
    out = run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "-R",
            repo,
            "--json",
            "baseRefName",
            "-q",
            ".baseRefName",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"pr-base-lookup-failed: {(out.stderr or '').strip()}")
    base = (out.stdout or "").strip()
    if not base:
        raise RuntimeError("pr-base-empty")
    return base


def _pr_head(repo: str, pr: int, run: Any = subprocess.run) -> str:
    out = run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "-R",
            repo,
            "--json",
            "headRefOid",
            "-q",
            ".headRefOid",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"pr-head-lookup-failed: {(out.stderr or '').strip()}")
    head = (out.stdout or "").strip()
    if not head:
        raise RuntimeError("pr-head-empty")
    return head


def merge(
    repo: str,
    pr: int,
    *,
    local_verify: str,
    run: Any = subprocess.run,
    check_fn: Any = check,
) -> dict[str, Any]:
    """Merge PR only if (a) its base is not the default branch (unless explicitly allowed) and
    (b) the merge gate is clean. Returns {ok, merged, blockers}. Never force-merges."""
    allow_direct = os.environ.get("CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE") == "1"
    try:
        base = _pr_base(repo, pr, run)
        default = _default_branch(repo, run)
        head = _pr_head(
            repo, pr, run
        )  # bind the merge to the exact commit we gate (anti-TOCTOU)
    except Exception as exc:  # gh failure/timeout -> fail closed, never merge blind
        return {"ok": False, "merged": False, "blockers": [f"lookup-error: {exc}"]}
    if base == default and not allow_direct:
        return {
            "ok": False,
            "merged": False,
            "blockers": [
                f"base-is-default:{base} — the final owner PR is owner-only; conductor never "
                "merges to the default branch (set CONDUCTOR_ALLOW_DIRECT_MAIN_MERGE=1 for a "
                "legacy 0.4.x direct-merge run)"
            ],
        }
    gate = check_fn(repo, pr, local_verify=local_verify)
    if not gate["ok"]:
        return {"ok": False, "merged": False, "blockers": gate["blockers"]}
    # --match-head-commit binds the merge to the SHA we gated: if the PR head moved between the
    # gate and here (a push in the TOCTOU window), GitHub rejects the merge instead of landing an
    # ungated commit. never --squash, never --admin, never force.
    merged = run(
        [
            "gh",
            "pr",
            "merge",
            str(pr),
            "-R",
            repo,
            "--merge",
            "--match-head-commit",
            head,
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    if merged.returncode != 0:
        return {
            "ok": False,
            "merged": False,
            "blockers": [f"merge-failed: {(merged.stderr or '').strip()}"],
        }
    return {"ok": True, "merged": True, "blockers": []}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print(
            "usage: conductor merge <pr>  (runs merge-gate, merges only if clean; refuses base=default)",
            file=sys.stderr,
        )
        return 0 if args and args[0] in ("-h", "--help") else 64
    try:
        pr = int(args[0])
    except ValueError:
        print(
            f"usage: conductor merge <pr> — expected an integer PR number, got {args[0]!r}",
            file=sys.stderr,
        )
        return 64
    try:
        repo = _resolve_repo()
    except (subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"repo-error: {exc}", file=sys.stderr)
        return 1
    local_verify = os.environ.get("CONDUCTOR_MERGE_VERIFY", "pytest -q")
    result = merge(repo, pr, local_verify=local_verify)
    for b in result["blockers"]:
        print(b, file=sys.stderr)
    if result["merged"]:
        print(
            f"merged PR #{pr} into its base via --merge (gate clean)", file=sys.stderr
        )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
