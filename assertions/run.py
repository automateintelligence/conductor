#!/usr/bin/env python3
"""Conductor assertion runner — the done-gate (design §5.1-5.2).

Reads assertions/manifest.yaml, runs each assertion's optional `setup` then its
`command` under a hard per-assertion timeout, and reports per-assertion + aggregate
results. Writes machine-readable assertions/run/results.json (id -> {pass, rc,
duration, kind, reason}) for the ledger/handoff.

FAIL-CLOSED (critical, design §5.2): a missing/unparseable manifest, a command that
cannot execute (missing dep / crash), or a timeout is treated as NOT done. The gate
is NEVER green-by-default on an indeterminate/unrunnable result.

Exit codes:
    0  all assertions green (done)
    1  >=1 assertion red
    2  manifest missing            (distinct code; clear message)
    3  manifest unparseable / wrong shape
    4  overall wall-clock timeout exceeded
    5  no assertions match the requested level
    6  done-gate tampered (a frozen assertion was changed/removed)

YAML loading uses pyyaml when available but does not hard-depend on it: a minimal
built-in parser handles the flat manifest schema as a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ASSERTIONS_DIR = os.path.dirname(os.path.abspath(__file__))
# PLUGIN_ROOT holds the TOOL CODE (imports); kept only for sys.path / module imports.
PLUGIN_ROOT = os.path.dirname(ASSERTIONS_DIR)


def _project_root() -> str:
    """The PROJECT that owns run state + the done-gate: ``$CONDUCTOR_HOME``, else the git
    repo of the current directory, else cwd. ``bin/conductor`` resolves this once and exports
    ``CONDUCTOR_HOME`` so the runner and the freeze guard agree. Distinct from PLUGIN_ROOT
    (the installed tool code), which must not hold a project's gate/state."""
    home = os.environ.get("CONDUCTOR_HOME")
    if home:
        return home
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return os.getcwd()


PROJECT = _project_root()
MANIFEST = os.environ.get(
    "CONDUCTOR_MANIFEST", os.path.join(PROJECT, "assertions", "manifest.yaml")
)
OVERALL_TIMEOUT = float(os.environ.get("CONDUCTOR_OVERALL_TIMEOUT", "0"))  # 0 = none
ISOLATE = os.environ.get("CONDUCTOR_ISOLATE", "") not in ("", "0")
RUN_DIR = os.path.join(PROJECT, "assertions", "run")
RESULTS = os.path.join(RUN_DIR, "results.json")

EXIT_OK = 0
EXIT_RED = 1
EXIT_NO_MANIFEST = 2
EXIT_BAD_MANIFEST = 3
EXIT_OVERALL_TIMEOUT = 4
EXIT_NO_MATCH = 5
EXIT_TAMPERED = 6

DEFAULT_TIMEOUT = 60


class ManifestMissing(Exception):
    pass


class ManifestInvalid(Exception):
    pass


def _coerce(v: str):
    if v == "":
        return ""
    if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
        return v[1:-1]
    if v.lstrip("-").isdigit():
        return int(v)
    return v


def _parse_flat_yaml(text: str) -> dict:
    """Minimal fallback parser for the flat manifest schema (no pyyaml dependency).

    Handles exactly:
        topkey:
          - key: value
            key: value
    Values may be double/single quoted; bare integers are coerced to int.
    """
    result: dict = {}
    cur_list = None
    cur_item = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0 and stripped.endswith(":"):
            cur_list = []
            result[stripped[:-1].strip()] = cur_list
            cur_item = None
            continue
        if stripped.startswith("- "):
            if cur_list is None:
                continue
            cur_item = {}
            cur_list.append(cur_item)
            stripped = stripped[2:].strip()
        if cur_item is not None and ":" in stripped:
            k, _, v = stripped.partition(":")
            cur_item[k.strip()] = _coerce(v.strip())
    return result


def load_assertions(path: str) -> list:
    if not os.path.exists(path):
        raise ManifestMissing(path)
    with open(path) as f:
        text = f.read()
    try:
        import yaml  # optional; not a hard dependency

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            raise ManifestInvalid(f"YAML parse error: {exc}")
    except ImportError:
        data = _parse_flat_yaml(text)

    if not isinstance(data, dict) or "assertions" not in data:
        raise ManifestInvalid("manifest has no top-level 'assertions' key")
    items = data["assertions"]
    if not isinstance(items, list) or not items:
        raise ManifestInvalid("'assertions' must be a non-empty list")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "id" not in item or "command" not in item:
            raise ManifestInvalid(f"assertion #{i} missing required 'id' or 'command'")
    return items


def _run(cmd: str, timeout: float, cwd: str = PROJECT):
    """Run a shell command at the given working directory. Returns (rc, reason).
    FAIL-CLOSED on any non-zero/timeout/exception (never silently passes)."""
    try:
        proc = subprocess.run(
            "set -o pipefail\n" + cmd,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.returncode, ("ok" if proc.returncode == 0 else "nonzero-exit")
    except subprocess.TimeoutExpired:
        return 124, f"timeout>{timeout}s"
    except Exception as exc:  # missing binary, OS error, etc. -> fail-closed
        return 127, f"exec-error: {exc}"


def write_results(results: dict) -> None:
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(RESULTS, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True)


def _overall_fail(results: dict) -> int:
    write_results(results)
    print("SUMMARY: overall wall-clock exceeded -> gate NOT done (exit 4)")
    return EXIT_OVERALL_TIMEOUT


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else (deadline - time.monotonic())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=["spec", "phase", "task"], default=None)
    try:
        args = ap.parse_args()
    except SystemExit as exc:
        # argparse exit 0/None == --help (standard, leave it). A non-zero usage
        # error (e.g. invalid --level value) must fail closed WITHOUT colliding
        # with EXIT_NO_MANIFEST(2): an invalid level matches no assertions -> 5.
        if exc.code in (0, None):
            raise
        write_results({})
        print("[GATE] FAIL: invalid command-line arguments")
        print("SUMMARY: gate NOT done (invalid arguments) -> exit 5")
        return EXIT_NO_MATCH
    try:
        assertions = load_assertions(MANIFEST)
    except ManifestMissing:
        write_results({})
        print(f"[GATE] FAIL: manifest missing at {MANIFEST}")
        print("SUMMARY: gate NOT done (manifest missing) -> exit 2")
        return EXIT_NO_MANIFEST
    except ManifestInvalid as exc:
        write_results({})
        print(f"[GATE] FAIL: manifest unparseable: {exc}")
        print("SUMMARY: gate NOT done (manifest unparseable) -> exit 3")
        return EXIT_BAD_MANIFEST

    # Done-gate integrity (§5): if /conductor:start froze a baseline, the manifest and the
    # test files its commands reference must be unchanged. Fail-closed, so the worker cannot
    # make a red gate green by weakening a check instead of satisfying it.
    _baseline = os.environ.get(
        "CONDUCTOR_FREEZE_BASELINE", os.path.join(PROJECT, "assertions", ".frozen")
    )
    if os.path.exists(_baseline):
        if PLUGIN_ROOT not in sys.path:
            sys.path.insert(0, PLUGIN_ROOT)
        try:
            from conductor import freeze

            fr = freeze.verify(MANIFEST, _baseline, PROJECT)
        except Exception as exc:  # cannot verify integrity -> fail closed
            fr = {"ok": False, "tampered": [f"freeze-check-error: {exc}"]}
        if not fr["ok"]:
            write_results({})
            print("[GATE] FAIL: done-gate tampered — " + "; ".join(fr["tampered"]))
            print("SUMMARY: gate NOT done (done-gate tampered) -> exit 6")
            return EXIT_TAMPERED
    if args.level:
        assertions = [
            a for a in assertions if str(a.get("level", "spec")) == args.level
        ]
        if not assertions:  # Codex #1: empty != done
            write_results({})
            print(f"[GATE] FAIL: no assertions at level '{args.level}'")
            print("SUMMARY: gate NOT done (no matching assertions) -> exit 5")
            return EXIT_NO_MATCH

    deadline = time.monotonic() + OVERALL_TIMEOUT if OVERALL_TIMEOUT else None
    results, passed = {}, 0
    overall_timed_out = False
    for a in assertions:
        aid, command = str(a["id"]), str(a["command"])
        setup = str(a.get("setup", "") or "")
        teardown = str(a.get("teardown", "") or "")
        kind = str(a.get("kind", "example"))
        try:
            timeout = float(a.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            write_results({})
            print(f"[GATE] FAIL: assertion {aid} has non-numeric timeout")
            print("SUMMARY: gate NOT done (manifest unparseable) -> exit 3")
            return EXIT_BAD_MANIFEST
        workdir = tempfile.mkdtemp(prefix=f"assert-{aid}-") if ISOLATE else PROJECT
        start = time.monotonic()
        try:
            # setup (if any) then command — each capped by the REAL remaining budget (no round-up),
            # with a deadline re-check AFTER every return (Codex #2: strict wall-clock).
            failed_reason = None
            for is_setup, cmd in ([(True, setup)] if setup else []) + [
                (False, command)
            ]:
                rem = _remaining(deadline)
                if rem is not None and rem <= 0:
                    return _overall_fail(results)
                eff = (
                    timeout if rem is None else min(timeout, rem)
                )  # float; NOT max(1, ...)
                rc, reason = _run(cmd, eff, workdir)
                if deadline is not None and time.monotonic() > deadline:
                    results[aid] = {
                        "pass": False,
                        "rc": 124,
                        "kind": kind,
                        "duration": round(time.monotonic() - start, 3),
                        "reason": "overall-timeout",
                    }
                    print(f"[FAIL] {aid} (reason=overall-timeout)")
                    return _overall_fail(results)
                if rc != 0:
                    failed_reason = f"setup-failed({reason})" if is_setup else reason
                    results[aid] = {
                        "pass": False,
                        "rc": rc,
                        "kind": kind,
                        "duration": round(time.monotonic() - start, 3),
                        "reason": failed_reason,
                    }
                    print(f"[FAIL] {aid} (rc={rc}, reason={failed_reason})")
                    break
            if failed_reason is None:
                results[aid] = {
                    "pass": True,
                    "rc": 0,
                    "kind": kind,
                    "duration": round(time.monotonic() - start, 3),
                    "reason": "ok",
                }
                print(f"[PASS] {aid}")
                passed += 1
        finally:
            if teardown:
                # teardown is wall-clock too (Codex): cap it by the remaining
                # overall budget so cleanup cannot run past the deadline.
                rem = _remaining(deadline)
                if rem is None or rem > 0:
                    _run(teardown, 5.0 if rem is None else min(5.0, rem), workdir)
            if ISOLATE and workdir != PROJECT:
                shutil.rmtree(workdir, ignore_errors=True)
            if deadline is not None and time.monotonic() > deadline:
                overall_timed_out = True

    if overall_timed_out:  # teardown (or cleanup) pushed past the overall budget
        return _overall_fail(results)
    write_results(results)
    total = len(assertions)
    failed = total - passed
    if failed == 0:
        print(f"SUMMARY: {passed}/{total} green -> gate DONE (exit 0)")
        return EXIT_OK
    print(f"SUMMARY: {passed}/{total} green, {failed} red -> gate NOT done (exit 1)")
    return EXIT_RED


if __name__ == "__main__":
    raise SystemExit(main())
