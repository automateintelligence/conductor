"""A-DH-2 — the worker launch targets the run's recorded host and only that host (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: for a run whose recorded worker host is H, the fire spawns H's executable exactly once
with a real argument vector, and spawns the opposite host's executable zero times.

The invariant is the SEPARATION between the two recorded hosts, so both directions are fired.
A single-host case passes while the inversion is broken, which is the failure mode this
assertion exists to catch.

Nothing here names a host when it builds a launch. The test writes a host into the run's
durable record and then asks Conductor — ``runhost.adapter`` -> ``worker_argv`` /
``worker_env`` / ``launch_prompt`` — for the vector, so "which executable got spawned" is
Conductor's answer and not the test's. The two recording fakes on ``PATH`` are what read that
answer back: each appends its raw ``argv``, working directory, environment and stdin to a
distinct log, so a wrong-host launch shows up as a record in the log nobody should have
written to.

BYTE IDENTITY. The run's paths deliberately carry shell-hostile bytes — a ``$``-prefixed
token, a backtick pair, a semicolon, an embedded double quote and a newline. Those bytes must
arrive intact in a SINGLE recorded field. That is what proves the launch used an argument
vector rather than an interpolated shell string: a shell would expand the ``$`` token, consume
the backticks, and split on the semicolon and the newline. No separate check for ``/bin/sh``
in the spawn chain is made, and none is specified — inspecting the spawn chain is
platform-specific and brittle where this is neither.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.hosts import base, runhost  # noqa: E402  (needs ROOT on sys.path)

RUN_KEY = "2026-08-10-codex-dual-host-conductor-design-5f6520fc"
SKILL = "autodev"

#: Shell-hostile bytes carried by the run's own directory name. Every character class here is
#: one a shell would destroy: `$TOKEN` expands to nothing, the backticks run a command, the
#: semicolon and the newline both terminate a command, and the double quote ends a quoted
#: string. Surviving intact in one field is therefore only possible through an argv element.
HOSTILE = '$CONDUCTOR_NOT_A_VARIABLE `echo x` ; say "hi"\nsecond-line'

#: The signature a shell leaves behind: the `$`-token gone, the backtick pair consumed. If any
#: recorded field contains THIS instead of HOSTILE, the launch went through a shell string.
EXPANDED = " ; say hi\nsecond-line"

#: Codex native session continuation must never become Conductor's continuation mechanism:
#: every fire is a cold start reconciled from durable state (spec M3, folded into this
#: assertion rather than given its own launch harness).
FORBIDDEN_CODEX_ARGS = ("resume", "--last", "fork")

_FAKE = """#!/usr/bin/env python3
import json, os, sys
record = {{
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": dict(os.environ),
    "stdin": sys.stdin.read(),
}}
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
"""


class Fire:
    """One run fired once, plus both hosts' recording logs."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.records: dict[str, list[dict]] = {}
        self.prompt = ""
        self.project = ""


def _write_fakes(bindir: pathlib.Path, logdir: pathlib.Path) -> dict[str, pathlib.Path]:
    """A recording fake for EVERY supported host, each with its own log."""
    logs = {}
    bindir.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)
    for host_id in base.HOST_IDS:
        log = logdir / f"{host_id}.jsonl"
        log.write_text("", encoding="utf-8")
        fake = bindir / host_id
        fake.write_text(_FAKE.format(log=str(log)), encoding="utf-8")
        fake.chmod(0o755)
        logs[host_id] = log
    return logs


def _seed_package_root(root: pathlib.Path) -> None:
    """A Conductor package tree at a test-chosen path: the real autodev SKILL.md, plus the
    ``bin/conductor`` entry point, so a launch that resolves through installed artifacts finds
    a real one rather than a placeholder."""
    skill_dir = root / "skills" / SKILL
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "skills" / SKILL / "SKILL.md", skill_dir / "SKILL.md")
    (root / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "bin" / "conductor", root / "bin" / "conductor")
    (root / "bin" / "conductor").chmod(0o755)


def _fire(host_id: str) -> Fire:
    """Record ``host_id`` for a fresh run, ask Conductor for the launch, and dispatch it."""
    fire = Fire(host_id)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="a-dh-2-"))
    hostile = workdir / HOSTILE
    project = hostile / "project"
    package = hostile / "package"
    project.mkdir(parents=True)
    _seed_package_root(package)
    fire.project = str(project)
    state_root = str(project / ".conductor")

    logs = _write_fakes(workdir / "bin", workdir / "log")
    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in {runhost.HOST_ENV, "CONDUCTOR_HOME"}
    }
    environ["PATH"] = f"{workdir / 'bin'}{os.pathsep}{environ.get('PATH', '')}"
    # Both hosts' source roots point at the SAME test-chosen package root, so neither host's
    # launch can resolve through an installation that happens to exist on this machine.
    for var in (
        "CLAUDE_PLUGIN_ROOT",
        "CODEX_PLUGIN_ROOT",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        environ[var] = str(package)

    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        runhost.record(str(project), host_id)
        adapter = runhost.adapter(str(project))
        argv = adapter.worker_argv(
            state_root=state_root,
            run_key=RUN_KEY,
            project_root=str(project),
            posture="supervised",
        )
        worker_env = adapter.worker_env(
            state_root=state_root, run_key=RUN_KEY, project_root=str(project)
        )
        fire.prompt = adapter.launch_prompt(SKILL, run_key=RUN_KEY)
    finally:
        os.environ.clear()
        os.environ.update(previous)

    subprocess.run(
        argv,
        cwd=str(project),
        env={**environ, **worker_env},
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    fire.records = {
        host: [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        for host, log in logs.items()
    }
    return fire


_FIRES: dict[str, Fire] = {}


def fire_for(host_id: str) -> Fire:
    if host_id not in _FIRES:
        _FIRES[host_id] = _fire(host_id)
    return _FIRES[host_id]


def _fields(record: dict) -> list[str]:
    """Every discrete recorded field: each argv element, the cwd, each environment value, and
    stdin. "A single field" is the unit byte identity has to survive in."""
    return [
        *record["argv"],
        record["cwd"],
        *record["env"].values(),
        record["stdin"],
    ]


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_exactly_one_launch_of_the_recorded_host(host_id: str) -> None:
    fire = fire_for(host_id)
    assert len(fire.records[host_id]) == 1, (
        f"run recorded host={host_id} produced {len(fire.records[host_id])} launches of "
        f"{host_id}; expected exactly one"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_opposite_host_is_never_spawned(host_id: str) -> None:
    """Must-not-contain, and the half that fails today's hardcoded fire. Both directions are
    parametrized because an invariant checked in one direction is not an invariant."""
    other = base.opposite(host_id)
    fire = fire_for(host_id)
    assert fire.records[other] == [], (
        f"run recorded host={host_id} spawned {other} {len(fire.records[other])} time(s); "
        f"argv={[r['argv'] for r in fire.records[other]]}"
    )


def test_the_codex_launch_is_a_headless_exec() -> None:
    record = fire_for("codex").records["codex"][0]
    assert record["argv"][1:2] == ["exec"], record["argv"]


def test_the_codex_launch_carries_no_session_continuation_token() -> None:
    """Must-not-contain: Codex native session resume is not Conductor's continuation
    mechanism — every fire is a cold start reconciled from durable state."""
    record = fire_for("codex").records["codex"][0]
    found = [arg for arg in record["argv"] if arg in FORBIDDEN_CODEX_ARGS]
    assert not found, (
        f"session-continuation token(s) {found} in codex argv {record['argv']}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_prompt_arrives_as_one_discrete_element_byte_identical(
    host_id: str,
) -> None:
    """The prompt Conductor computed must arrive as exactly ONE argv element (or on stdin),
    byte-for-byte. Any expansion, stripping, word-splitting or requoting proves the launch
    went through a shell string rather than an argument vector."""
    fire = fire_for(host_id)
    record = fire.records[host_id][0]
    exact = [element for element in record["argv"] if element == fire.prompt]
    assert len(exact) == 1 or record["stdin"] == fire.prompt, (
        f"prompt {fire.prompt!r} is not one discrete argv element nor stdin; "
        f"argv={record['argv']!r} stdin={record['stdin']!r}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_shell_hostile_bytes_survive_intact_in_one_field(host_id: str) -> None:
    """The `$`-token, backtick pair, semicolon, double quote and newline must all arrive
    together in a single recorded field."""
    fire = fire_for(host_id)
    record = fire.records[host_id][0]
    intact = [field for field in _fields(record) if HOSTILE in field]
    assert intact, (
        f"no recorded field carries the fixture bytes intact.\nfixture={HOSTILE!r}\n"
        f"argv={record['argv']!r}\ncwd={record['cwd']!r}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_no_recorded_field_shows_shell_expansion(host_id: str) -> None:
    """Must-not-contain the shell's fingerprint: the `$`-token expanded away and the backtick
    pair consumed. A field carrying that instead of the fixture is an interpolated string."""
    fire = fire_for(host_id)
    record = fire.records[host_id][0]
    mangled = [
        field for field in _fields(record) if EXPANDED in field and HOSTILE not in field
    ]
    assert not mangled, f"shell-expanded fixture bytes in {mangled!r}"


def test_the_codex_launch_carries_the_hostile_bytes_in_argv_itself() -> None:
    """Codex names its workspace and its skill file in argv, so for that host the byte-identity
    proof lands in an argv element specifically rather than in the environment."""
    record = fire_for("codex").records["codex"][0]
    carriers = [element for element in record["argv"] if HOSTILE in element]
    assert carriers, (
        f"no codex argv element carries the fixture bytes: {record['argv']!r}"
    )
