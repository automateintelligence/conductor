"""A-DH-4 — every host invocation is time-bounded and reports on expiry (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: every Conductor-initiated host subprocess — version probe, preflight, capability check,
worker launch, reviewer launch — terminates within its configured timeout and, when the child
never exits on its own, Conductor kills it and returns a non-zero actionable failure.

A hang is not a failure: nothing errors, nothing reports, and an unattended fire leaves a stuck
owner instead of a recoverable one. ``codex --help`` hanging without stdin redirection is the
known instance.

THE ENTRY-POINT LIST IS DERIVED, NOT HAND-LISTED. Every public callable on the loaded adapter,
plus every public module-level callable in the adapter package, is invoked once against a
RESPONSIVE fake host; whichever ones the fake recorded are, by definition, the entry points
that spawn a host process. Those same entry points are then run against a HANGING fake. A
hand-listed set would silently stop covering the next entry point somebody adds, which is the
whole failure mode.

THE HANGING FAKE IGNORES TERMINATION BY POLITENESS. It traps SIGTERM and SIGINT and then
blocks forever, so a bound expressed only as a TERM is not a bound: only an escalation to KILL
ends it. ``conductor/hosts/codex.py`` records the probe that established this.

Both fake sets are exercised. Against the responsive set the same entry points must return
within the same bound WITHOUT reporting a timeout — otherwise "always time out immediately"
satisfies the check. It is deliberately NOT "completes successfully": an entry point may still
fail against a responsive fake for unrelated state reasons, and requiring success would make
this assertion red for causes it does not govern.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.hosts import base  # noqa: E402  (needs ROOT on sys.path)

ADAPTER_PACKAGE = pathlib.Path(base.__file__).resolve().parent

#: Wall-clock room on top of the adapter layer's own configured bound. Generous: the point is
#: to distinguish "bounded" from "unbounded", not to measure the bound.
MARGIN_S = 15.0

#: What a timeout report looks like in any of the adapter layer's voices.
TIMEOUT_REPORT = re.compile(
    r"did not answer within|timed out|timeout|TimeoutExpired|DispatchTimeout", re.I
)

#: The four things §"Failure handling" requires of an actionable failure, as recognisers over
#: the report text. The run key and the state are one clause; the rest are their own.
ACTIONABLE_CLAUSES = {
    "the run key or current state": re.compile(
        r"run[ _-]?key|current state|state=", re.I
    ),
    "the failed operation": re.compile(
        r"--version|plugin list|exec|dispatch|probe", re.I
    ),
    "whether any write occurred": re.compile(
        r"no write occurred|wrote|write occurred", re.I
    ),
    "an exact recovery command": re.compile(
        r"command -v |conductor \w|codex \w|claude \w"
    ),
}

_HANGING = """#!/usr/bin/env python3
import os, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
with open({pids!r}, "a", encoding="utf-8") as handle:
    handle.write(str(os.getpid()) + "\\n")
try:
    sys.stdin.read()
except Exception:
    pass
while True:
    signal.pause()
"""

_RESPONSIVE = """#!/usr/bin/env python3
import json, sys
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv) + "\\n")
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print({version!r})
elif args[:3] == ["plugin", "list", "--json"]:
    print(json.dumps({{"installed": []}}))
else:
    print("ok")
"""

VERSIONS = {"claude": "2.1.224 (Claude Code)", "codex": "codex-cli 0.147.0"}

#: The child harness. Embedded as text rather than shipped as a sibling module on purpose: the
#: freeze guard digests ``test_*.py`` and ``conftest.py``, so a helper module beside this file
#: could be weakened without tripping it.
_CHILD = r"""
import inspect, json, os, pathlib, sys, traceback

ROOT = sys.argv[1]
HOST = sys.argv[2]
KIND = sys.argv[3]          # "module:<module>:<name>" or "adapter:<name>"
SCRATCH = pathlib.Path(sys.argv[4])
sys.path.insert(0, ROOT)

from conductor.hosts import base

adapter = base.load(HOST)
project = SCRATCH / "project"
project.mkdir(parents=True, exist_ok=True)

BANK = {
    "adapter": adapter,
    "args": [],
    "command": ["true"],
    "entry": {},
    "executable": HOST,
    "head_sha": "0" * 40,
    "home": str(SCRATCH),
    "host_id": HOST,
    "identity": "1:0",
    "mode": "read-only",
    "name": "conductor",
    "pid": os.getpid(),
    "posture": "supervised",
    "pr": 1,
    "profile": {"posture": "supervised", "argv": []},
    "project": str(project),
    "project_root": str(project),
    "prompt": "no-op",
    "repo_root": str(project),
    "result_path": str(SCRATCH / "result.txt"),
    "roots": [str(project)],
    "root": str(project),
    "run_key": "a-dh-4-0000000f",
    "skill": "autodev",
    "spec_path": "docs/specs/x.md",
    "state_root": str(project / ".conductor"),
    "text": "{}",
    "timeout": float(os.environ.get("A_DH_4_TIMEOUT", "3")),
}


def resolve():
    scope, _, rest = KIND.partition(":")
    if scope == "adapter":
        return getattr(adapter, rest)
    module_name, _, name = rest.rpartition(":")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def kwargs_for(func):
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    call = {}
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.name in ("self", "cls"):
            continue
        if parameter.name in BANK:
            call[parameter.name] = BANK[parameter.name]
        elif parameter.default is not parameter.empty:
            continue
        else:
            return None
    return call


target = resolve()
call = kwargs_for(target)
if call is None:
    print(json.dumps({"status": "not-invocable"}))
    sys.exit(0)
try:
    target(**call)
except BaseException:
    sys.stderr.write(traceback.format_exc())
    print(json.dumps({"status": "raised"}))
    sys.exit(1)
print(json.dumps({"status": "returned"}))
"""


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rc: int | None = None
        self.output = ""
        self.wall = 0.0
        self.expired = False
        self.spawned = False


def _configured_bound() -> float:
    """The adapter layer's own longest configured bound, read out of the adapter package's
    module constants rather than restated here."""
    seconds = [0.0]
    for path in ADAPTER_PACKAGE.rglob("*.py"):
        for name, value in re.findall(
            r"^([A-Z][A-Z0-9_]*(?:TIMEOUT|GRACE)[A-Z0-9_]*)\s*=\s*([0-9.]+)",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            del name
            seconds.append(float(value))
    return sum(sorted(seconds)[-2:])


BOUND_S = _configured_bound()
CAP_S = BOUND_S + MARGIN_S


class Harness:
    def __init__(self) -> None:
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="a-dh-4-"))
        self.child = self.workdir / "child.py"
        self.child.write_text(_CHILD, encoding="utf-8")
        self.pids = self.workdir / "hanging.pids"
        self.pids.write_text("", encoding="utf-8")
        self.sets: dict[str, pathlib.Path] = {}
        self.logs: dict[str, pathlib.Path] = {}
        for kind in ("responsive", "hanging"):
            bindir = self.workdir / kind
            bindir.mkdir()
            self.sets[kind] = bindir
            for host_id in base.HOST_IDS:
                log = self.workdir / f"{kind}-{host_id}.jsonl"
                log.write_text("", encoding="utf-8")
                self.logs[f"{kind}-{host_id}"] = log
                fake = bindir / host_id
                fake.write_text(
                    _HANGING.format(pids=str(self.pids))
                    if kind == "hanging"
                    else _RESPONSIVE.format(log=str(log), version=VERSIONS[host_id]),
                    encoding="utf-8",
                )
                fake.chmod(0o755)

    def entry_points(self) -> list[str]:
        """Every candidate, derived from the adapter interface and its package."""
        names = set()
        for host_id in base.HOST_IDS:
            adapter = base.load(host_id)
            for name in dir(adapter):
                if not name.startswith("_") and callable(getattr(adapter, name, None)):
                    names.add(f"adapter:{name}")
        for path in sorted(ADAPTER_PACKAGE.rglob("*.py")):
            module_name = "conductor.hosts" + (
                "" if path.stem == "__init__" else f".{path.stem}"
            )
            module = __import__(module_name, fromlist=["*"])
            for name in dir(module):
                value = getattr(module, name)
                if (
                    not name.startswith("_")
                    and callable(value)
                    and getattr(value, "__module__", "") == module_name
                    and not isinstance(value, type)
                ):
                    names.add(f"module:{module_name}:{name}")
        return sorted(names)

    def run(self, kind: str, host_id: str, entry: str) -> Result:
        result = Result(entry)
        log = self.logs[f"{kind}-{host_id}"]
        before = log.stat().st_size
        scratch = self.workdir / f"scratch-{kind}-{host_id}-{abs(hash(entry))}"
        scratch.mkdir(parents=True, exist_ok=True)
        environ = dict(os.environ)
        environ["PATH"] = f"{self.sets[kind]}{os.pathsep}{environ['PATH']}"
        environ["HOME"] = str(scratch)
        environ["CODEX_HOME"] = str(scratch)
        environ["CLAUDE_CONFIG_DIR"] = str(scratch)
        environ["A_DH_4_TIMEOUT"] = "3"
        environ.pop("CONDUCTOR_HOST", None)
        argv = [
            sys.executable,
            str(self.child),
            str(ROOT),
            host_id,
            entry,
            str(scratch),
        ]
        started = os.times().elapsed
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=CAP_S,
                check=False,
                start_new_session=True,
                env=environ,
                cwd=str(scratch),
            )
            result.rc = proc.returncode
            result.output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as expiry:
            result.expired = True
            result.output = (expiry.stdout or b"").decode("utf-8", "replace") + (
                expiry.stderr or b""
            ).decode("utf-8", "replace")
        result.wall = os.times().elapsed - started
        result.spawned = log.stat().st_size > before
        return result

    def survivors(self) -> list[int]:
        pids = [
            int(line)
            for line in self.pids.read_text(encoding="utf-8").split()
            if line.strip()
        ]
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            alive.append(pid)
        return alive

    def reap(self) -> None:
        for pid in self.survivors():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


_HARNESS: dict[str, Harness] = {}
_SPAWNING: dict[str, list[str]] = {}
_HANG_RESULTS: dict[str, dict[str, Result]] = {}
_ORPHANS: dict[str, list[int]] = {}
_LIVE_RESULTS: dict[str, dict[str, Result]] = {}


def harness() -> Harness:
    if "h" not in _HARNESS:
        _HARNESS["h"] = Harness()
    return _HARNESS["h"]


def spawning_entry_points(host_id: str) -> list[str]:
    """Derived: the entry points a responsive fake host actually recorded a call from."""
    if host_id not in _SPAWNING:
        harness_ = harness()
        found = {}
        for entry in harness_.entry_points():
            result = harness_.run("responsive", host_id, entry)
            if result.spawned:
                found[entry] = result
        _SPAWNING[host_id] = sorted(found)
        _LIVE_RESULTS[host_id] = found
    return _SPAWNING[host_id]


def hanging_results(host_id: str) -> dict[str, Result]:
    """The hanging-set sweep, plus the orphans it left BEFORE this harness cleans up.

    The order is load-bearing. ``reap()`` SIGKILLs every surviving fake, so a test that called
    it and then asked ``survivors()`` would be asking a question whose answer the call it just
    made had already forced to "none" — green with the behaviour deleted. The census is taken
    first and stored; the reap that follows exists only so the sweep does not leak processes
    into the rest of the run.
    """
    if host_id not in _HANG_RESULTS:
        harness_ = harness()
        _HANG_RESULTS[host_id] = {
            entry: harness_.run("hanging", host_id, entry)
            for entry in spawning_entry_points(host_id)
        }
        _ORPHANS[host_id] = harness_.survivors()
        harness_.reap()
    return _HANG_RESULTS[host_id]


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    """Module teardown. Autouse, so pytest runs it whether or not a test names it: the
    hanging fakes ignore SIGTERM, so anything this module started and did not KILL would
    outlive the whole gate run."""
    yield
    if "h" in _HARNESS:
        _HARNESS["h"].reap()
        shutil.rmtree(_HARNESS["h"].workdir, ignore_errors=True)


def test_the_adapter_layer_declares_a_bound_at_all() -> None:
    assert BOUND_S > 0, (
        "no TIMEOUT/GRACE constant anywhere in the adapter package — nothing declares a bound"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_enumeration_reaches_the_host_spawning_layer(host_id: str) -> None:
    """Anti-stub: an enumeration that finds no host-spawning entry point would satisfy every
    clause below vacuously."""
    assert spawning_entry_points(host_id), (
        f"no {host_id} entry point spawned the host executable; the enumeration never reached "
        "the launch layer"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_no_entry_point_outlives_its_bound_against_a_hanging_host(host_id: str) -> None:
    """Must-not-contain: any entry point still running after the bound."""
    unbounded = [
        entry for entry, result in hanging_results(host_id).items() if result.expired
    ]
    assert not unbounded, (
        f"unbounded against a hanging {host_id} (still running after {CAP_S}s): {unbounded}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_every_entry_point_fails_non_zero_against_a_hanging_host(host_id: str) -> None:
    """Must-not-contain: any entry point exiting zero against the hanging set — a hang
    reported as success is the failure this assertion exists for."""
    silent = [
        entry
        for entry, result in hanging_results(host_id).items()
        if not result.expired and result.rc == 0
    ]
    assert not silent, f"hang reported as success by {host_id} entry points: {silent}"


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_every_expiry_reports_actionably(host_id: str) -> None:
    """Must-contain: a failure report naming the run key and current state, the failed
    operation, whether any write occurred, and an exact recovery command."""
    missing: dict[str, list[str]] = {}
    for entry, result in hanging_results(host_id).items():
        absent = [
            clause
            for clause, pattern in ACTIONABLE_CLAUSES.items()
            if not pattern.search(result.output)
        ]
        if absent:
            missing[entry] = absent
    report = "\n".join(f"  {entry}: missing {gaps}" for entry, gaps in missing.items())
    assert not missing, f"unactionable expiry reports from {host_id}:\n{report}"


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_no_fake_host_process_is_orphaned(host_id: str) -> None:
    """Must-not-contain: any orphaned fake process. The fake ignores SIGTERM, so a caller that
    only asks politely leaves it behind."""
    hanging_results(host_id)
    assert _ORPHANS[host_id] == [], (
        f"fake host processes survived the {host_id} sweep: {_ORPHANS[host_id]}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_responsive_set_is_never_reported_as_a_timeout(host_id: str) -> None:
    """Must-contain (anti-stub): against a responsive host the same entry points return within
    the same bound WITHOUT reporting a timeout. Deliberately not "completes successfully" — an
    entry point may fail for unrelated state reasons this assertion does not govern."""
    spawning_entry_points(host_id)
    wrong = {
        entry: result.output.strip()[-200:]
        for entry, result in _LIVE_RESULTS[host_id].items()
        if result.expired or TIMEOUT_REPORT.search(result.output)
    }
    assert not wrong, f"timeout reported against a RESPONSIVE {host_id}: {wrong}"
