"""A-DH-4 — every host invocation is time-bounded and reports on expiry (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: every Conductor-initiated host subprocess — version probe, preflight, capability check,
worker launch, reviewer launch — terminates within its configured timeout and, when the child
never exits on its own, Conductor kills it and returns a non-zero actionable failure.

A hang is not a failure: nothing errors, nothing reports, and an unattended fire leaves a stuck
owner instead of a recoverable one. ``codex --help`` hanging without stdin redirection is the
known instance.

THE ENTRY-POINT LIST IS DERIVED, NOT HAND-LISTED. Every public callable on the loaded adapter,
plus every public module-level callable in the adapter package, PLUS the generated Tier-B cron
driver, is invoked once against a RESPONSIVE fake host; whichever ones the fake recorded are,
by definition, the entry points that spawn a host process. Those same entry points are then run
against a HANGING fake. A hand-listed set would silently stop covering the next entry point
somebody adds, which is the whole failure mode.

THE DRIVER IS IN THE SET BECAUSE IT IS THE WORKER LAUNCH. The Claim governs "every
Conductor-initiated host subprocess — version probe, preflight, capability check, worker launch,
reviewer launch". A Python-only enumeration cannot see the worker launch at all: in this codebase
the worker launch is not a Python call, it is ``conductor/resume_script.render`` emitting
``<adapter>.resume_fire_command()`` into ``<project>/.conductor/resume-autodev.sh`` and cron
executing that. ``worker_argv``/``launch_prompt`` are declared on the ``HostAdapter`` Protocol
and implemented by neither adapter — Track A rejected resolving the executable and the prompt at
GENERATION time, which is the shape the 2026-07-05 stall had — so an enumeration that walked the
Protocol would be measuring a mechanism the product does not have. This leg therefore renders the
real driver through the product's own entry point (``conductor resume-script write``, the same
path ``/conductor:start`` reconcile uses) and EXECUTES it, exactly as A-DH-2 and A-DH-3 do.

Its name is derived too: ``resume_script.driver_script_path`` is the product's own declaration of
where its cron driver lives, so a rename moves this entry point rather than dropping it.

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

from conductor import resume_script  # noqa: E402  (needs ROOT on sys.path)
from conductor.hosts import base, runhost  # noqa: E402  (needs ROOT on sys.path)

ADAPTER_PACKAGE = pathlib.Path(base.__file__).resolve().parent

#: The generated cron driver, named from the product's own path declaration rather than from a
#: literal here, so a rename moves this entry point instead of silently dropping it.
DRIVER_ENTRY = "driver:" + os.path.basename(resume_script.driver_script_path(""))

SKILL = "autodev"

#: The directories the generated driver prepends to ``PATH`` before it resolves anything. The
#: scratch ``$HOME/.local/bin`` holding this module's fakes is the FIRST of them, so nothing here
#: can shadow the fakes — but a real ``claude``/``codex``/``conductor`` in one of the system dirs
#: would be spawned instead of a fake, and the sweep would then be reporting on the wrong binary.
#: ``test_no_system_bin_dir_shadows_the_driver_fakes`` refuses loudly rather than measure it.
DRIVER_SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")

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
            self._write_fakes(bindir, kind)

    def _fake_text(self, kind: str, host_id: str) -> str:
        if kind == "hanging":
            return _HANGING.format(pids=str(self.pids))
        return _RESPONSIVE.format(
            log=str(self.logs[f"{kind}-{host_id}"]), version=VERSIONS[host_id]
        )

    def _write_fakes(self, bindir: pathlib.Path, kind: str) -> None:
        """One fake per supported host in ``bindir``, all writing back to this harness's own
        logs and pid census — so a driver fire and a Python call are read through the same
        instruments and a wrong-host spawn shows up either way."""
        for host_id in base.HOST_IDS:
            fake = bindir / host_id
            fake.write_text(self._fake_text(kind, host_id), encoding="utf-8")
            fake.chmod(0o755)

    def entry_points(self) -> list[str]:
        """Every candidate, derived from the adapter interface and its package."""
        names = {DRIVER_ENTRY}
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

    def _seed_driver_fixture(
        self, kind: str, host_id: str
    ) -> tuple[pathlib.Path, pathlib.Path, dict[str, str]]:
        """A project whose recorded host is ``host_id``, with this sweep's fakes where the
        driver will look for them. Returns (project, script, env).

        NO HOST NAME APPEARS IN THESE PATHS, and that is load-bearing rather than tidy. The
        Claude driver's no-double-drive guard is ``pgrep -f 'claude'`` filtered by whether the
        matched process's cwd is under ``$PROJECT``/``$WORKTREE`` — and the driver's own cmdline
        is its script path. A fixture directory called ``claude`` therefore makes the driver
        match itself and ``exit 0`` before it ever fires, which would record no spawn, drop this
        entry point out of the derived set, and leave every clause below passing on a sweep that
        never reached the launch layer. Host slots are indexed instead.

        The fakes go in ``$HOME/.local/bin`` because that is the FIRST directory the generated
        driver prepends to ``PATH``; putting them anywhere else lets a real host binary on this
        machine win, deterministically rather than by luck.
        """
        slot = self.workdir / f"driver-{kind}-{base.HOST_IDS.index(host_id)}"
        project = slot / "project"
        worktree = slot / "worktree"
        package = slot / "package"
        home = slot / "home"
        bindir = home / ".local" / "bin"
        for directory in (project, worktree, bindir):
            directory.mkdir(parents=True)
        # A package root at a path chosen at test time: the real `bin/conductor` and the real
        # autodev SKILL.md, because the Codex driver derives its source root from whichever
        # conductor bin resolves at fire time and refuses to fire unless the skill file is under
        # it. Nothing else is copied — the driver's other use of that bin is the done-gate probe
        # (`conductor assert run --level spec`), which must come back NOT-green or the fire is a
        # no-op, and a package with no `assertions/run.py` answers exactly that, immediately.
        (package / "bin").mkdir(parents=True)
        shutil.copyfile(ROOT / "bin" / "conductor", package / "bin" / "conductor")
        (package / "bin" / "conductor").chmod(0o755)
        (package / "skills" / SKILL).mkdir(parents=True)
        shutil.copyfile(
            ROOT / "skills" / SKILL / "SKILL.md",
            package / "skills" / SKILL / "SKILL.md",
        )
        self._write_fakes(bindir, kind)
        os.symlink(package / "bin" / "conductor", bindir / "conductor")

        environ = {
            key: value
            for key, value in os.environ.items()
            if key not in {runhost.HOST_ENV, "CONDUCTOR_HOME", "CODEX_PLUGIN_ROOT"}
        }
        environ["HOME"] = str(home)
        environ["CODEX_HOME"] = str(home)
        environ["CLAUDE_CONFIG_DIR"] = str(home)

        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(environ)
        try:
            runhost.record(str(project), host_id)
        finally:
            os.environ.clear()
            os.environ.update(previous)

        script = pathlib.Path(resume_script.driver_script_path(str(project)))
        render = subprocess.run(
            [
                str(ROOT / "bin" / "conductor"),
                "resume-script",
                "write",
                "--project",
                str(project),
                "--worktree",
                str(worktree),
                "--out",
                str(script),
            ],
            env=environ,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert render.returncode == 0, (
            f"the product refused to render the {host_id} driver "
            f"(rc={render.returncode}):\n{render.stderr}"
        )
        return project, script, environ

    def run_driver(self, kind: str, host_id: str) -> Result:
        """Fire the generated driver once and read it the same way as every other entry point.

        ``result.output`` is the driver's own log as well as its stdout/stderr: the driver
        reports into ``<project>/.conductor/resume-autodev.log``, so a report clause that read
        only the pipes would be asking the wrong file and would fail for the wrong reason.
        """
        result = Result(DRIVER_ENTRY)
        log = self.logs[f"{kind}-{host_id}"]
        before = log.stat().st_size
        project, script, environ = self._seed_driver_fixture(kind, host_id)
        started = os.times().elapsed
        try:
            proc = subprocess.run(
                [str(script)],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=CAP_S,
                check=False,
                start_new_session=True,
                env=environ,
                cwd=str(self.workdir),
            )
            result.rc = proc.returncode
            result.output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as expiry:
            result.expired = True
            result.output = (expiry.stdout or b"").decode("utf-8", "replace") + (
                expiry.stderr or b""
            ).decode("utf-8", "replace")
        result.wall = os.times().elapsed - started
        driver_log = project / ".conductor" / "resume-autodev.log"
        if driver_log.is_file():
            result.output += driver_log.read_text(encoding="utf-8", errors="replace")
        result.spawned = log.stat().st_size > before
        return result

    def run(self, kind: str, host_id: str, entry: str) -> Result:
        if entry == DRIVER_ENTRY:
            return self.run_driver(kind, host_id)
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
_LIVE_DRIVER_DIAGNOSIS: dict[str, str] = {}


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
            if entry == DRIVER_ENTRY:
                _LIVE_DRIVER_DIAGNOSIS[host_id] = (
                    f"spawned={result.spawned} rc={result.rc} "
                    f"expired={result.expired} output={result.output.strip()[-400:]!r}"
                )
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


def test_no_system_bin_dir_shadows_the_driver_fakes() -> None:
    """Setup guarantee. The generated driver prepends the system bin dirs to ``PATH``, so a real
    ``claude``, ``codex`` or ``conductor`` in one of them would be what the fire spawned and this
    whole sweep would be reporting on the wrong binary. Refuse loudly rather than measure it."""
    shadowing = [
        os.path.join(directory, name)
        for directory in DRIVER_SYSTEM_PATH
        for name in (*base.HOST_IDS, "conductor")
        if os.path.exists(os.path.join(directory, name))
    ]
    assert not shadowing, (
        f"a real host/conductor binary sits ahead of this module's fakes on the driver's PATH: "
        f"{shadowing}"
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
def test_the_enumeration_reaches_the_worker_launch(host_id: str) -> None:
    """Anti-stub for the driver leg specifically, and the sharper half of the one above.

    The derived set is "whatever the responsive fake recorded", so an entry point that quietly
    declines to spawn simply vanishes from it — and the driver has four ways to do that (the
    no-double-drive guard, a green done-gate, ``flock``, an unresolved bin), each of which exits
    0 and writes nothing. Without this clause, any of them would silently return this assertion
    to measuring Python helpers only, which is the exact defect this leg was added to close.
    """
    assert DRIVER_ENTRY in spawning_entry_points(host_id), (
        f"the generated {host_id} driver spawned no host executable — it exited early instead "
        f"of firing, so the worker launch is not in the measured set. "
        f"driver result: {_LIVE_DRIVER_DIAGNOSIS.get(host_id, '(not run)')}"
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
