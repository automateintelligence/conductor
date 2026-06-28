#!/usr/bin/env python3
"""Conductor assertion runner — the done-gate (design §5.1-5.2).

Reads assertions/manifest.yaml, runs each assertion's optional `setup` then its
`command` under a hard per-assertion timeout, and reports per-assertion + aggregate
results. Writes machine-readable assertions/run/results.json (id -> {pass, rc,
duration}) for the ledger/handoff.

FAIL-CLOSED (critical, design §5.2): a missing/unparseable manifest, a command that
cannot execute (missing dep / crash), or a timeout is treated as NOT done. The gate
is NEVER green-by-default on an indeterminate/unrunnable result.

Exit codes:
    0  all assertions green (done)
    1  >=1 assertion red
    2  manifest missing            (distinct code; clear message)
    3  manifest unparseable / wrong shape

YAML loading uses pyyaml when available but does not hard-depend on it: a minimal
built-in parser handles the flat manifest schema as a fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

ASSERTIONS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(ASSERTIONS_DIR)
MANIFEST = os.path.join(ASSERTIONS_DIR, "manifest.yaml")
RUN_DIR = os.path.join(ASSERTIONS_DIR, "run")
RESULTS = os.path.join(RUN_DIR, "results.json")

EXIT_OK = 0
EXIT_RED = 1
EXIT_NO_MANIFEST = 2
EXIT_BAD_MANIFEST = 3

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


def _run(cmd: str, timeout: int):
    """Run a shell command at repo root. Returns (rc, reason). FAIL-CLOSED on any
    non-zero/timeout/exception (never silently passes)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=REPO_ROOT,
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


def main() -> int:
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

    results: dict = {}
    passed = 0
    for a in assertions:
        aid = str(a["id"])
        command = str(a["command"])
        setup = str(a.get("setup", "") or "")
        timeout = int(a.get("timeout", DEFAULT_TIMEOUT))

        start = time.monotonic()
        if setup:
            rc, reason = _run(setup, timeout)
            if rc != 0:
                reason = f"setup-failed({reason})"
                duration = round(time.monotonic() - start, 3)
                results[aid] = {
                    "pass": False,
                    "rc": rc,
                    "duration": duration,
                    "reason": reason,
                }
                print(f"[FAIL] {aid} (rc={rc}, reason={reason})")
                continue
        rc, reason = _run(command, timeout)
        duration = round(time.monotonic() - start, 3)
        ok = rc == 0
        results[aid] = {"pass": ok, "rc": rc, "duration": duration, "reason": reason}
        if ok:
            passed += 1
            print(f"[PASS] {aid}")
        else:
            print(f"[FAIL] {aid} (rc={rc}, reason={reason})")

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
