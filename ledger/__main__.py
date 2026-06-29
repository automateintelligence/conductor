"""CLI entry point: python3 -m ledger <subcommand> [args]"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

from ledger import gh as _gh
from ledger import reconcile as _reconcile
from ledger import sync as _sync


def _derive_repo(override: str | None) -> str:
    if override:
        return override
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Cannot determine repo: {result.stderr.strip()}")
    return result.stdout.strip()


def cmd_generate(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    with open(args.plan_json) as f:
        plan: dict[str, Any] = json.load(f)
    result = _sync.generate(repo, plan, _gh)
    print(json.dumps(result))


def cmd_convert(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    result = _sync.convert(repo, args.plan_md, _gh)
    print(json.dumps(result))


def cmd_reconcile(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    result = _reconcile.reconcile(
        repo,
        args.issue,
        tests_red=args.tests_red,
        pr_merged=args.pr_merged,
        commits_since_baseline=args.commits,
        retries=args.retries,
        R=args.R,
        gh=_gh,
        now_ts=args.now_ts,
        L=args.L,
    )
    print(json.dumps(result))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m ledger",
        description="Conductor ledger CLI — generate/convert/reconcile GitHub issue hierarchy",
    )
    p.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="GitHub repo (default: current repo from gh cli)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # generate
    g = sub.add_parser(
        "generate", help="Generate GitHub hierarchy from a JSON plan file"
    )
    g.add_argument("plan_json", metavar="plan.json", help="Path to JSON plan dict")
    g.set_defaults(func=cmd_generate)

    # convert
    c = sub.add_parser("convert", help="Parse plan.md and generate GitHub hierarchy")
    c.add_argument("plan_md", metavar="plan.md", help="Path to Markdown plan file")
    c.set_defaults(func=cmd_convert)

    # reconcile
    r = sub.add_parser("reconcile", help="Reconcile an issue against §7 rules")
    r.add_argument("issue", type=int, metavar="ISSUE#", help="GitHub issue number")
    r.add_argument("--tests-red", action="store_true", default=False)
    r.add_argument("--pr-merged", action="store_true", default=False)
    r.add_argument("--commits", type=int, default=0, metavar="N")
    r.add_argument("--retries", type=int, default=0, metavar="N")
    r.add_argument("-R", type=int, default=3, metavar="N", help="Retry cap (default 3)")
    r.add_argument(
        "--now-ts", type=int, default=None, metavar="N", help="Current unix timestamp"
    )
    r.add_argument(
        "-L", type=int, default=900, metavar="N", help="Lease TTL seconds (default 900)"
    )
    r.set_defaults(func=cmd_reconcile)

    return p


def main() -> None:
    p = _build_parser()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
