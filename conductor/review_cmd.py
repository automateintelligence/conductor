"""``conductor review <pr>`` — launch the run's reviewer host, record its token usage.

The worker used to ask an external wrapper for the opposite-host review of a phase PR, and the
wrapper returned no usage. This verb launches the reviewer itself: read-only, wall-clock bounded,
cold start (no session resume). It records one ``reviewer`` dispatch on the run and prints the
review text; posting it with the configured marker stays with the worker, so the merge gate's
provenance rules (``CONDUCTOR_REVIEW_AUTHOR``) are untouched. The record is derived data: a
failure to write it is reported on stderr and never changes the review or the exit status.

Exit status: 0 review printed; 2 refused before any host was launched (stale checkout, oversized
brief, ``gh``/``git`` failure, reviewer host missing, unusable run); 3 the host failed; 4 it
timed out; 64 usage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from conductor import dispatches, merge_gate, remote
from conductor.core import resolve
from conductor.hosts import base as hostbase
from conductor.hosts import bounded, runhost

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_HOST_FAILED = 3
EXIT_TIMEOUT = 4
EXIT_USAGE = 64

_TOOL_TIMEOUT_S = 60.0
_DEFAULT_REVIEW_TIMEOUT_S = 2400.0
#: Argv strings are capped at 128 KiB by the kernel; the whole prompt (template and brief, as
#: UTF-8) is held to half of that.
_PROMPT_MAX_BYTES = 64 * 1024
_TAIL_LINES = 40

PROMPT = """\
You are the {reviewer} reviewer for a Conductor phase PR, reviewing work another host wrote.
Read-only: do not modify any file. Do not run the project's full test suite.

PR #{number}: {title}
URL: {url}
Head: {head_sha}   Base: {base_sha}
Review the full change with: git diff {base_sha}..{head_sha}
Read any file in this checkout you need for context.

Review it against the phase brief below: correctness, spec and ADR conformance, tests that
prove the behaviour, security. Report findings by severity (P0 blocker, P1 must-fix, P2
should-fix, P3 nit) with file:line and a one-line fix. End with exactly one line:
VERDICT: APPROVE | VERDICT: CHANGES REQUESTED

--- phase brief ---
{brief}
"""


class Refused(Exception):
    """A precondition failed; no host was launched and nothing was recorded."""


def _tool(argv: list[str], *, cwd: str, what: str) -> str:
    """Run a short helper (``git``/``gh``) bounded; its stdout, or ``Refused`` naming ``what``."""
    try:
        done = bounded.run_bounded(argv, cwd=cwd, timeout=_TOOL_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise Refused(f"{what} failed: {exc}") from exc
    if done.timed_out or done.returncode != 0:
        detail = "timed out" if done.timed_out else done.stderr.strip()
        raise Refused(f"{what} failed: {detail}")
    return done.stdout.strip()


def _pull_request(pr: str, root: str) -> dict:
    out = _tool(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--json",
            "number,title,body,url,baseRefName,headRefOid",
        ],
        cwd=root,
        what=f"gh pr view {pr}",
    )
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Refused(f"gh pr view {pr} failed: not JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise Refused(f"gh pr view {pr} failed: unexpected answer")
    return doc


def _read_brief(path: str) -> str:
    # Decoding with replacement never shrinks the bytes, so a raw read past the cap is already
    # too large; the exact check runs on the finished prompt.
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_PROMPT_MAX_BYTES + 1)
    except OSError as exc:
        raise Refused(f"brief unreadable: {exc}") from exc
    if len(raw) > _PROMPT_MAX_BYTES:
        raise Refused(f"brief-too-large: over {_PROMPT_MAX_BYTES} bytes ({path})")
    return raw.decode("utf-8", errors="replace")


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-_TAIL_LINES:])


def _reason(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


def _review(args: argparse.Namespace) -> int:
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        raise Refused(f"bad timeout {args.timeout!r}: must be a finite number > 0")
    top = _tool(["git", "rev-parse", "--show-toplevel"], cwd=os.getcwd(), what="git")
    root = os.path.realpath(top)
    resolve.recover_pending(resolve.state_root(root))
    try:
        found = resolve.resolve(run_key=args.run, start=root)
    except (resolve.RunNotFound, resolve.RunAmbiguous) as exc:
        raise Refused(str(exc)) from exc
    try:
        reviewer = found.run.get("reviewer_host") or hostbase.opposite(
            runhost.resolve(root)
        )
        adapter = hostbase.load(reviewer)
    except Exception as exc:  # noqa: BLE001 — unknown/conflicting host: refuse, never a traceback
        raise Refused(f"reviewer host unresolved: {_reason(exc)}") from exc

    pr = _pull_request(args.pr, root)
    head_sha = str(pr.get("headRefOid") or "")
    local_head = _tool(
        ["git", "rev-parse", "HEAD"], cwd=root, what="git rev-parse HEAD"
    )
    if local_head != head_sha:
        raise Refused(
            f"checkout-stale: HEAD {local_head} is not PR #{pr.get('number')} head "
            f"{head_sha}; push or check out the PR head first"
        )

    base_ref = str(pr.get("baseRefName") or "")
    try:
        remote_name = remote.resolve(root)
    except Exception:  # noqa: BLE001 — same fail-open as `conductor remote`
        remote_name = "origin"
    _tool(["git", "fetch", remote_name, base_ref], cwd=root, what="git fetch")
    base_sha = _tool(
        ["git", "merge-base", "HEAD", f"{remote_name}/{base_ref}"],
        cwd=root,
        what="git merge-base",
    )

    brief = _read_brief(args.brief)
    prompt = PROMPT.format(
        reviewer=reviewer,
        number=pr.get("number"),
        title=pr.get("title"),
        url=pr.get("url"),
        head_sha=head_sha,
        base_sha=base_sha,
        brief=brief,
    )
    if len(prompt.encode("utf-8")) > _PROMPT_MAX_BYTES:
        raise Refused(
            f"brief-too-large: prompt over {_PROMPT_MAX_BYTES} bytes ({args.brief})"
        )
    try:
        argv = adapter.reviewer_argv(prompt, project_root=root)
    except (hostbase.HostUnavailable, ValueError) as exc:
        raise Refused(str(exc)) from exc

    # Claude's argv has no workspace flag, so the checkout is its cwd.
    try:
        result = bounded.run_bounded(argv, cwd=root, timeout=args.timeout)
    except (
        OSError
    ) as exc:  # executable vanished, E2BIG: no host ran, so nothing is recorded
        raise Refused(f"reviewer host not launched: {_reason(exc)}") from exc
    usage = adapter.usage_from_output(result.stdout)
    if result.timed_out:
        outcome = "timeout"
    elif result.returncode != 0 or usage.is_error or not usage.result_text:
        outcome = "error"
    else:
        outcome = "ok"
    try:
        entry = dispatches.make(
            host=reviewer,
            role="reviewer",
            phase_id=merge_gate.closes_issue(str(pr.get("body") or "")),
            head_sha=head_sha,
            usage=usage,
            wall_s=result.duration_s,
            outcome=outcome,
            note="no-usage-in-output" if usage.input_tokens is None else None,
        )
        dispatches.append(found.state_root, found.run_key, entry)
    except Exception as exc:  # noqa: BLE001 — derived data must never lose the review
        print(f"usage-unrecorded reason={_reason(exc)}", file=sys.stderr)

    if outcome == "ok":
        print(usage.result_text)
        return EXIT_OK
    if outcome == "timeout":
        print(f"review-timeout after {args.timeout:g}s ({reviewer})", file=sys.stderr)
        return EXIT_TIMEOUT
    print(
        f"review-failed ({reviewer}, exit {result.returncode})\n"
        f"--- stderr ---\n{_tail(result.stderr)}\n--- stdout ---\n{_tail(result.stdout)}",
        file=sys.stderr,
    )
    return EXIT_HOST_FAILED


def _default_timeout() -> float:
    raw = os.environ.get("CONDUCTOR_REVIEW_TIMEOUT_S")
    if not raw:
        return _DEFAULT_REVIEW_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        raise Refused(f"bad CONDUCTOR_REVIEW_TIMEOUT_S {raw!r}: not a number") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor review",
        description="Run the opposite-host review of a phase PR and record its usage.",
    )
    parser.add_argument("pr", help="pull request number")
    parser.add_argument(
        "--brief", required=True, help="file holding the phase's Spec sections and ADRs"
    )
    parser.add_argument("--run", help="run key (default: the one active run)")
    parser.add_argument(
        "--timeout",
        type=float,
        help="wall-clock bound in seconds (default: $CONDUCTOR_REVIEW_TIMEOUT_S or 2400)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _parser()
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.timeout is None:
            args.timeout = _default_timeout()
        return _review(args)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
