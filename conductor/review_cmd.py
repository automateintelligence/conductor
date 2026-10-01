"""``conductor review <pr>`` — launch the run's reviewer host, record its token usage.

The worker used to ask an external wrapper for the opposite-host review of a phase PR, and the
wrapper returned no usage. This verb launches the reviewer itself: read-only, wall-clock bounded,
cold start (no session resume). It records one ``reviewer`` dispatch on the run and prints the
review text; posting it with the configured marker stays with the worker, so the merge gate's
provenance rules (``CONDUCTOR_REVIEW_AUTHOR``) are untouched. The record is derived data: a
failure to write it is reported on stderr and never changes the review or the exit status.

Only the record needs the run. The run is ``--run``'s, else the one this checkout belongs to
(``resolve.run_for_worktree``); the reviewer is that run's ``reviewer_host``, else the opposite
of the recorded run host (``.conductor/host``). When the run cannot be resolved or read (a lock
timeout, a schema error, no run or several bound to this checkout) the review runs anyway with
that fallback host and nothing is recorded: one ``usage-unrecorded reason=...`` line on stderr.

The reviewer reads the change from a diff file this verb writes to a private temp directory
(``<mkdtemp>/pr-<n>.diff``, always removed afterwards), so the Claude reviewer needs no shell at
all. A TERM, HUP or INT while the reviewer runs (the worker's shell-tool limit, the fire
watchdog) kills the reviewer's process group before this verb exits: a review never outlives
the call that launched it.

``--timeout`` (``$CONDUCTOR_REVIEW_TIMEOUT_S``, default 540 s) is the WHOLE command's wall-clock
budget, one deadline taken at entry: each ``git``/``gh`` helper gets at most 60 s of what is
left, the reviewer gets the rest, and ``_RESERVE_S`` is held back from both for the kill grace
and cleanup. A slow preflight therefore shortens the review instead of pushing the command past
the worker's 600 s shell limit.

Exit status: 0 review printed; 2 refused before any host was launched (stale checkout, oversized
brief, bad timeout, ``gh``/``git`` missing, failing or not executable, reviewer host unresolved,
missing or not startable); 3 the host ran and failed; 4 it timed out (``review-timeout``,
including a budget spent in preflight, when no host was launched and nothing is recorded) or
this verb was interrupted by a signal (``review-interrupted``); 64
usage. Every refusal is one line on stderr, never a traceback.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import time

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
#: The WHOLE command's wall-clock budget: preflight helpers, the reviewer and cleanup. Under
#: Claude's 600 s Bash-tool maximum (the worker runs this verb from its shell tool) and the fire
#: watchdog's 1800 s idle window.
_DEFAULT_REVIEW_TIMEOUT_S = 540.0
#: Held back from every helper and from the reviewer for what follows them: ``run_bounded``'s
#: TERM/KILL grace (2 x 5 s), removing the temp dir and appending the dispatch.
_RESERVE_S = 15.0
#: The budget's clock (a seam for tests).
_clock = time.monotonic
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
The full change is in {diff_path} (git diff {base_sha}..{head_sha}). Read it, and read any file in this checkout you need for context.

Review it against the phase brief below: correctness, spec and ADR conformance, tests that
prove the behaviour, security. Report findings by severity (P0 blocker, P1 must-fix, P2
should-fix, P3 nit) with file:line and a one-line fix. End with exactly one line:
VERDICT: APPROVE | VERDICT: CHANGES REQUESTED

--- phase brief ---
{brief}
"""


class Refused(Exception):
    """A precondition failed; no host was launched and nothing was recorded."""


class BudgetSpent(Exception):
    """The command's wall-clock budget ran out before the reviewer was launched."""


class _Budget:
    """One deadline for the whole command, taken at entry."""

    def __init__(self, total: float) -> None:
        self.total = total
        self.deadline = _clock() + total
        self.host: str | None = None

    def left(self) -> float:
        """Seconds a helper or the reviewer may still take, the cleanup reserve held back."""
        return self.deadline - _clock() - _RESERVE_S

    def spent(self, what: str) -> BudgetSpent:
        return BudgetSpent(
            f"review-timeout after {self.total:g}s ({self.host or 'reviewer unresolved'}): "
            f"the budget ran out in preflight at {what}; no host was launched"
        )


class Interrupted(BaseException):
    """A trapped signal arrived. A ``BaseException``, like ``KeyboardInterrupt``, so no
    ``except Exception`` on the way out can swallow it before the reviewer group is killed."""

    def __init__(self, signame: str) -> None:
        super().__init__(signame)
        self.signame = signame


_TRAPPED = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGHUP", "SIGINT")
    if hasattr(signal, name)
)


def _raise_interrupted(signum: int, _frame: object) -> None:
    raise Interrupted(signal.Signals(signum).name)


def _trap_signals() -> dict:
    """Turn TERM/HUP/INT into ``Interrupted``; the previous handlers, for ``_restore_signals``.
    Handlers can only be set from the main thread; elsewhere nothing is trapped."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {sig: signal.signal(sig, _raise_interrupted) for sig in _TRAPPED}


def _restore_signals(previous: dict) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _tool(argv: list[str], *, cwd: str, what: str, budget: _Budget) -> str:
    """Run a short helper (``git``/``gh``) for at most ``_TOOL_TIMEOUT_S`` or what the budget
    has left; its stdout, or ``Refused`` naming ``what``. ``BudgetSpent`` when the budget is gone
    before it starts, or it timed out on the budget's cut rather than its own limit."""
    left = budget.left()
    if left <= 0:
        raise budget.spent(what)
    limit = min(_TOOL_TIMEOUT_S, left)
    try:
        done = bounded.run_bounded(argv, cwd=cwd, timeout=limit)
    except OSError as exc:  # missing, not executable: it never started
        raise Refused(f"{what} failed: {_reason(exc)}") from exc
    if done.timed_out and limit < _TOOL_TIMEOUT_S:
        raise budget.spent(what)
    if done.timed_out or done.returncode != 0:
        detail = "timed out" if done.timed_out else done.stderr.strip()
        raise Refused(f"{what} failed: {detail}")
    return done.stdout.strip()


def _pull_request(pr: str, root: str, budget: _Budget) -> dict:
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
        budget=budget,
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


def _find_run(
    run_key: str | None, root: str
) -> tuple[resolve.RunResolution | None, str]:
    """The run to record on, or ``None`` and why not. Never raises: a run-state failure costs
    the record, never the review."""
    try:
        resolve.recover_pending(resolve.state_root(root))
        if run_key is not None:
            return resolve.resolve(run_key=run_key, start=root), ""
        return resolve.run_for_worktree(root), ""
    except Exception as exc:  # noqa: BLE001 — lock timeout, schema error, no/several runs
        return None, _reason(exc)


def _record(
    found: resolve.RunResolution | None,
    unresolved: str,
    *,
    reviewer: str,
    phase_id: str | None,
    head_sha: str,
    usage: hostbase.Usage,
    wall_s: float,
    outcome: str,
    note: str | None,
) -> None:
    """Append the ``reviewer`` dispatch. Derived data: a failure is reported, never raised."""
    if found is None:
        print(f"usage-unrecorded reason={unresolved}", file=sys.stderr)
        return
    try:
        entry = dispatches.make(
            host=reviewer,
            role="reviewer",
            phase_id=phase_id,
            head_sha=head_sha,
            usage=usage,
            wall_s=wall_s,
            outcome=outcome,
            note=note,
        )
        dispatches.append(found.state_root, found.run_key, entry)
    except Exception as exc:  # noqa: BLE001 — derived data must never lose the review
        print(f"usage-unrecorded reason={_reason(exc)}", file=sys.stderr)


def _review(args: argparse.Namespace) -> int:
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        raise Refused(f"bad timeout {args.timeout!r}: must be a finite number > 0")
    budget = _Budget(args.timeout)
    top = _tool(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=os.getcwd(),
        what="git",
        budget=budget,
    )
    root = os.path.realpath(top)
    found, unresolved = _find_run(args.run, root)
    try:
        reviewer = (found.run.get("reviewer_host") if found else None) or (
            hostbase.opposite(runhost.resolve(root))
        )
        adapter = hostbase.load(reviewer)
    except Exception as exc:  # noqa: BLE001 — unknown/conflicting host: refuse, never a traceback
        raise Refused(f"reviewer host unresolved: {_reason(exc)}") from exc
    budget.host = reviewer

    pr = _pull_request(args.pr, root, budget)
    head_sha = str(pr.get("headRefOid") or "")
    local_head = _tool(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        what="git rev-parse HEAD",
        budget=budget,
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
    _tool(
        ["git", "fetch", remote_name, base_ref],
        cwd=root,
        what="git fetch",
        budget=budget,
    )
    base_sha = _tool(
        ["git", "merge-base", "HEAD", f"{remote_name}/{base_ref}"],
        cwd=root,
        what="git merge-base",
        budget=budget,
    )

    brief = _read_brief(args.brief)
    number = pr.get("number")
    diff_name = f"pr-{number}.diff" if isinstance(number, int) else "pr.diff"
    phase_id = merge_gate.closes_issue(str(pr.get("body") or ""))

    previous = _trap_signals()
    context_dir: str | None = None
    started: float | None = None
    result: bounded.Bounded | None = None
    interrupted: Interrupted | None = None
    try:
        try:
            context_dir = tempfile.mkdtemp(prefix="conductor-review-")
        except OSError as exc:
            raise Refused(f"no temp dir for the diff: {_reason(exc)}") from exc
        diff_path = os.path.join(context_dir, diff_name)
        prompt = PROMPT.format(
            reviewer=reviewer,
            number=number,
            title=pr.get("title"),
            url=pr.get("url"),
            head_sha=head_sha,
            base_sha=base_sha,
            diff_path=diff_path,
            brief=brief,
        )
        if len(prompt.encode("utf-8")) > _PROMPT_MAX_BYTES:
            raise Refused(
                f"brief-too-large: prompt over {_PROMPT_MAX_BYTES} bytes ({args.brief})"
            )
        try:
            argv = adapter.reviewer_argv(
                prompt, project_root=root, context_dir=context_dir
            )
        except (hostbase.HostUnavailable, ValueError) as exc:
            raise Refused(str(exc)) from exc
        _tool(
            [
                "git",
                "diff",
                "--no-color",
                "--no-ext-diff",
                f"--output={diff_path}",
                f"{base_sha}..{head_sha}",
            ],
            cwd=root,
            what="git diff",
            budget=budget,
        )
        review_limit = budget.left()
        if review_limit <= 0:
            raise budget.spent("the reviewer launch")
        started = time.monotonic()
        # Claude's argv has no workspace flag, so the checkout is its cwd.
        # An OSError here is Popen's (vanished, E2BIG): no host ran, so nothing is recorded.
        # One after the start comes back as a result with returncode None: exit 3, recorded.
        try:
            result = bounded.run_bounded(argv, cwd=root, timeout=review_limit)
        except OSError as exc:
            raise Refused(f"reviewer host not launched: {_reason(exc)}") from exc
    except Interrupted as stop:
        # run_bounded has already killed the reviewer group. Handlers are restored and the
        # temp dir removed (finally, below) before the attempt is recorded.
        interrupted = stop
    finally:
        _restore_signals(previous)
        if context_dir is not None:
            shutil.rmtree(context_dir, ignore_errors=True)

    if interrupted is not None:
        if started is not None:
            _record(
                found,
                unresolved,
                reviewer=reviewer,
                phase_id=phase_id,
                head_sha=head_sha,
                usage=adapter.usage_from_output(""),
                wall_s=time.monotonic() - started,
                outcome="error",
                note="interrupted",
            )
        print(f"review-interrupted ({interrupted.signame})", file=sys.stderr)
        return EXIT_TIMEOUT
    assert result is not None  # the try either set it or raised

    usage = adapter.usage_from_output(result.stdout)
    if result.timed_out:
        outcome = "timeout"
    elif result.returncode != 0 or usage.is_error or not usage.result_text:
        outcome = "error"
    else:
        outcome = "ok"
    _record(
        found,
        unresolved,
        reviewer=reviewer,
        phase_id=phase_id,
        head_sha=head_sha,
        usage=usage,
        wall_s=result.duration_s,
        outcome=outcome,
        note="no-usage-in-output" if usage.input_tokens is None else None,
    )

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
        help="wall-clock budget for the whole command, in seconds "
        "(default: $CONDUCTOR_REVIEW_TIMEOUT_S or 540)",
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
    except BudgetSpent as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_TIMEOUT


if __name__ == "__main__":
    sys.exit(main())
