"""Review packet for the final run-branch → main PR (0.5.0 run-branch topology).

Phase PRs merge into `conductor/run-<slug>`; when the done-gate is green the
worker opens ONE owner-reviewed PR run-branch → main whose body is this packet:
phase PR links, changed-files summary, gate evidence, deferrals, verification.
The owner merges that PR — conductor never does.

Trust note: unlike the merge gate, this module is DISPLAY, not enforcement, so
it fails OPEN by design — any subprocess failure (gh down, git error, timeout)
renders an explicit "unavailable: <reason>" line in the affected section and the
packet still renders. Withholding the packet would block the owner's review
without protecting anything; the fail-closed gate already ran separately.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

_GH_TIMEOUT = float(os.environ.get("CONDUCTOR_GH_TIMEOUT", "60"))
# The spec-level done-gate can legitimately run for minutes (it executes the
# conducted project's verify commands), so it gets the merge-gate verify bound.
_GATE_TIMEOUT = float(os.environ.get("CONDUCTOR_GATE_TIMEOUT", "900"))
_DIFF_CAP = 200
_GATE_TAIL = 30
_GATE_CMD = "conductor assert run --level spec"


def _reason(exc_or_proc: Any) -> str:
    """One-line failure reason from an exception or a failed CompletedProcess."""
    if isinstance(exc_or_proc, BaseException):
        return str(exc_or_proc) or type(exc_or_proc).__name__
    return (getattr(exc_or_proc, "stderr", "") or "").strip() or "exit non-zero"


def _phase_pr_lines(run_branch: str, runner: Any) -> list[str]:
    try:
        out = runner(
            [
                "gh",
                "pr",
                "list",
                "--base",
                run_branch,
                "--state",
                "merged",
                "--json",
                "number,title,url,mergedAt",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if out.returncode != 0:
            return [f"unavailable: {_reason(out)}"]
        prs = json.loads(out.stdout)
    except Exception as exc:  # fail open: the packet is evidence, not enforcement
        return [f"unavailable: {_reason(exc)}"]
    if not prs:
        return ["None recorded"]
    return [
        f"- #{p['number']} {p['title']} (merged {(p.get('mergedAt') or '')[:10]})"
        f" — {p['url']}"
        for p in prs
    ]


def _diff_stat_lines(run_branch: str, base: str, runner: Any) -> list[str]:
    try:
        out = runner(
            ["git", "diff", "--stat", f"{base}...{run_branch}"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if out.returncode != 0:
            return [f"unavailable: {_reason(out)}"]
    except Exception as exc:
        return [f"unavailable: {_reason(exc)}"]
    stat = str(out.stdout or "").rstrip("\n")
    lines: list[str] = stat.splitlines() if stat else ["(no changes)"]
    if len(lines) > _DIFF_CAP:
        extra = len(lines) - _DIFF_CAP
        lines = lines[:_DIFF_CAP] + [f"… truncated ({extra} more lines)"]
    return lines


def build_packet(
    run_branch: str,
    base: str = "main",
    *,
    gate_output: str | None = None,
    gate_exit: int | None = None,
    deferrals: list[str] | None = None,
    runner: Any = subprocess.run,
) -> str:
    """Render the full review packet as markdown. Pure over its subprocess seam:
    the CLI runs the gate and collects deferrals; this function only formats."""
    gate_body = (
        gate_output.rstrip("\n")
        if gate_output
        else f"Gate output not supplied — run `{_GATE_CMD}` and attach."
    )
    exit_line = f"exit status: {gate_exit if gate_exit is not None else 'not run'}"
    deferral_lines = [f"- {d}" for d in (deferrals or [])] or ["None"]
    parts = [
        f"# Conductor run review packet — {run_branch} → {base}",
        "",
        "This PR is reviewed and merged by the OWNER — conductor never merges it.",
        "",
        f"## Phase PRs merged into {run_branch}",
        "",
        *_phase_pr_lines(run_branch, runner),
        "",
        f"## Changed files vs {base}",
        "",
        "```",
        *_diff_stat_lines(run_branch, base, runner),
        "```",
        "",
        "## Done-gate evidence",
        "",
        gate_body,
        "",
        "## Known deferrals / open items",
        "",
        *deferral_lines,
        "",
        "## Verification",
        "",
        "```",
        f"$ {_GATE_CMD}",
        exit_line,
        "```",
    ]
    return "\n".join(parts) + "\n"


def _collect_deferrals(milestone: str | None, *, runner: Any) -> list[str]:
    """Open conductor-debt issues, plus open issues in the run's milestone if
    given; deduped by issue number, order preserved."""
    queries = [["--label", "conductor-debt"]]
    if milestone:
        queries.append(["--milestone", milestone])
    seen: set[int] = set()
    lines: list[str] = []
    for extra in queries:
        try:
            out = runner(
                ["gh", "issue", "list", "--state", "open", *extra]
                + ["--json", "number,title"],
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT,
            )
            if out.returncode != 0:
                lines.append(f"unavailable: {_reason(out)}")
                continue
            for issue in json.loads(out.stdout):
                if issue["number"] not in seen:
                    seen.add(issue["number"])
                    lines.append(f"#{issue['number']} {issue['title']}")
        except Exception as exc:  # fail open: render the reason, keep going
            lines.append(f"unavailable: {_reason(exc)}")
    return lines


def _run_gate(runner: Any) -> tuple[str, int | None]:
    """Run the spec-level done-gate via the sibling assertions/run.py and return
    (last-30-lines of combined output, exit code). Failure to run at all yields
    an unavailable line and no exit code — the packet still renders."""
    gate = Path(__file__).resolve().parent.parent / "assertions" / "run.py"
    try:
        out = runner(
            ["python3", str(gate), "--level", "spec"],
            capture_output=True,
            text=True,
            timeout=_GATE_TIMEOUT,
        )
    except Exception as exc:
        return f"unavailable: {_reason(exc)}", None
    combined = ((out.stdout or "") + (out.stderr or "")).rstrip("\n")
    tail = combined.splitlines()[-_GATE_TAIL:]
    return "\n".join(tail), out.returncode


def main(
    argv: list[str] | None = None,
    *,
    runner: Any = subprocess.run,
    stdout: TextIO = sys.stdout,
) -> int:
    p = argparse.ArgumentParser(
        prog="conductor run-packet",
        description="Render the owner-review packet for the final "
        "run-branch → main PR body",
    )
    p.add_argument("run_branch", metavar="run-branch")
    p.add_argument("--base", default="main")
    p.add_argument("--milestone", default=None, metavar="TITLE")
    args = p.parse_args(argv)
    gate_output, gate_exit = _run_gate(runner)
    deferrals = _collect_deferrals(args.milestone, runner=runner)
    print(
        build_packet(
            args.run_branch,
            args.base,
            gate_output=gate_output,
            gate_exit=gate_exit,
            deferrals=deferrals,
            runner=runner,
        ),
        end="",
        file=stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
