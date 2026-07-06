"""CLI entry point: python3 -m ledger <subcommand> [args]"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from conductor.paths import project_root
from ledger import align as _align
from ledger import gate_link
from ledger import gh as _gh
from ledger import phase_done as _phase_done
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


def _default_results_path() -> str:
    return os.path.join(project_root(), "assertions", "run", "results.json")


def _tests_red_from_gate(
    repo: str, issue: int, results_path: str | None, gh: Any = _gh
) -> bool:
    """Derive the phase's test state from the runner's ``results.json`` via the issue's
    ``conductor-assertions`` marker — ground truth instead of a caller-supplied flag
    (dogfood finding: model-reported truth decays). Every failure mode exits distinctly
    and fail-closed; no marker / no results / unresolved token can never read as green."""
    tokens = gate_link.read_assertion_tokens(gh.get_body(repo, issue))
    if not tokens:
        sys.exit(f"from-gate: no conductor-assertions marker on issue #{issue}")
    path = results_path or _default_results_path()
    try:
        results = gate_link.load_results(path)
    except (OSError, ValueError) as exc:
        sys.exit(
            f"from-gate: cannot read results at {path} ({exc}); "
            "run `conductor assert run --level spec` first"
        )
    state = gate_link.tests_red_from_results(tokens, results)
    if state["unresolved"]:
        sys.exit(
            f"from-gate: unresolved assertion tokens {state['unresolved']} "
            f"on issue #{issue}"
        )
    if state["ambiguous"]:
        sys.exit(
            f"from-gate: ambiguous assertion tokens {state['ambiguous']} "
            f"on issue #{issue} — fix the marker to name exact manifest ids"
        )
    return bool(state["red"])


def cmd_reconcile(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    tests_red = args.tests_red
    if args.from_gate:
        tests_red = _tests_red_from_gate(repo, args.issue, args.results)
    result = _reconcile.reconcile(
        repo,
        args.issue,
        tests_red=tests_red,
        pr_merged=args.pr_merged,
        commits_since_baseline=args.commits,
        R=args.R,
        gh=_gh,
        now_ts=args.now_ts,
        L=args.L,
    )
    print(json.dumps(result))


def cmd_phase_done(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    results: dict[str, Any] | None = None
    if not args.no_gate_check:
        path = args.results or _default_results_path()
        try:
            results = gate_link.load_results(path)
        except (OSError, ValueError) as exc:
            sys.exit(
                f"phase-done: cannot read results at {path} ({exc}); "
                "run `conductor assert run --level spec` first, "
                "or pass --no-gate-check"
            )
    result = _phase_done.phase_done(
        repo,
        args.issue,
        gh=_gh,
        results=results,
        plan_path=args.plan,
        no_gate_check=args.no_gate_check,
    )
    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


def cmd_align(args: argparse.Namespace) -> None:
    repo = _derive_repo(args.repo)
    with open(args.plan_md) as f:
        plan = _sync.parse_plan_md(f.read())
    result = _align.align(repo, plan, _gh, apply=args.apply)
    print(json.dumps(result))
    if result["ambiguous_phases"] or result["milestone"] == "ambiguous":
        sys.exit(1)  # ambiguity needs the owner; renames for those were withheld


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
    gate = r.add_mutually_exclusive_group()
    gate.add_argument("--tests-red", action="store_true", default=False)
    gate.add_argument(
        "--from-gate",
        action="store_true",
        default=False,
        help="Derive tests-red from assertions/run/results.json via the issue's "
        "conductor-assertions marker, instead of trusting the caller's flag",
    )
    r.add_argument(
        "--results",
        default=None,
        metavar="PATH",
        help="results.json path (with --from-gate; default: "
        "<project>/assertions/run/results.json)",
    )
    r.add_argument("--pr-merged", action="store_true", default=False)
    r.add_argument("--commits", type=int, default=-1, metavar="N")  # -1 = not reported (fail-safe; see reconcile §2)
    r.add_argument("-R", type=int, default=3, metavar="N", help="Retry cap (default 3)")
    r.add_argument(
        "--now-ts", type=int, default=None, metavar="N", help="Current unix timestamp"
    )
    r.add_argument(
        "-L", type=int, default=900, metavar="N", help="Lease TTL seconds (default 900)"
    )
    r.set_defaults(func=cmd_reconcile)

    # align
    al = sub.add_parser(
        "align",
        help="Match existing (paraphrased-title) phase issues to plan phases by "
        "assertion-id SET and rename issues + milestone to the canonical plan "
        "headings; dry-run by default, --apply executes",
    )
    al.add_argument("plan_md", metavar="plan.md", help="Path to Markdown plan file")
    al.add_argument("--apply", action="store_true", default=False)
    al.set_defaults(func=cmd_align)

    # phase-done
    pd = sub.add_parser(
        "phase-done",
        help="Atomic end-of-phase bookkeeping: verify the phase's assertions are "
        "green (fail-closed), then label status:done, close task sub-issues, strip "
        "lease/attempts, unassign, close the issue, tick the plan's checkboxes",
    )
    pd.add_argument("issue", type=int, metavar="ISSUE#", help="Phase issue number")
    pd.add_argument(
        "--plan",
        default=None,
        metavar="plan.md",
        help="Plan file whose matching phase-section checkboxes to tick",
    )
    pd.add_argument(
        "--results",
        default=None,
        metavar="PATH",
        help="results.json path (default: <project>/assertions/run/results.json)",
    )
    pd.add_argument("--no-gate-check", action="store_true", default=False)
    pd.set_defaults(func=cmd_phase_done)

    return p


def main() -> None:
    p = _build_parser()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
