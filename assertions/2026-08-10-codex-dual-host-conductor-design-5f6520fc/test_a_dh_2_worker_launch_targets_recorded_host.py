"""A-DH-2 — the worker launch targets the run's recorded host and only that host (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: for a run whose recorded worker host is H, the fire spawns H's executable exactly once
with a real argument vector, and spawns the opposite host's executable zero times.

WHAT A FIRE IS. The assertion says "fire each run once and read both logs", and in this
codebase a fire is the generated Tier-B cron driver executing — not a Python call. Track A
deliberately rejected Plan 04's ``worker_argv``/``launch_prompt`` members (they are declared on
the ``HostAdapter`` Protocol and implemented by neither adapter) because they resolve the
executable and the prompt at GENERATION time, and the 2026-07-05 production stall was a
generation-time path that rotted. Track A resolves at FIRE time, through the driver. So this
test renders the real driver with the product's own entry point
(``conductor resume-script write``, the same path ``/conductor:start`` reconcile uses) and then
EXECUTES it. A test that called an unimplemented Protocol member would be measuring a mechanism
the product does not have.

Nothing here names a host when it builds a launch. The test writes a host into the run's durable
record with ``runhost.record`` and then asks the product to render the driver for the RECORDED
host; "which executable got spawned" is Conductor's answer and not the test's. The two recording
fakes on ``PATH`` are what read that answer back: each appends its raw ``argv``, working
directory, environment and stdin to a distinct log, so a wrong-host launch shows up as a record
in the log nobody should have written to.

BYTE IDENTITY, AND WHERE IT IS MEASURED. The run's own directory name deliberately carries
shell-hostile bytes — a ``$``-prefixed token, a backtick pair, a semicolon, an embedded double
quote and a newline. It is the DIRECTORY name and not the prompt because the A1 driver carries a
FIXED prompt for each host: Claude's is the literal ``/conductor:autodev`` and Codex's is a
sentence with the skill path interpolated into it. The bytes therefore have to be planted where
the driver actually interpolates — the worktree path and the Conductor source root — which is
also where a shell string would do its damage. Those bytes must arrive intact in a SINGLE
recorded field: a shell would expand the ``$`` token, consume the backticks, and split on the
semicolon and the newline. For Codex the interpolation lands INSIDE the prompt argv element, so
the prompt itself is checked byte-for-byte; for Claude it lands in the working directory and in
``CONDUCTOR_HOME``, and the fixed prompt is checked only for being one discrete element. No
separate check for ``/bin/sh`` in the spawn chain is made, and none is specified — inspecting
the spawn chain is platform-specific and brittle where this is neither.
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

SKILL = "autodev"

#: Shell-hostile bytes carried by the run's own directory name. Every character class here is
#: one a shell would destroy: `$TOKEN` expands to nothing, the backticks run a command, the
#: semicolon and the newline both terminate a command, and the double quote ends a quoted
#: string. Surviving intact in one field is therefore only possible through an argv element.
HOSTILE = '$CONDUCTOR_NOT_A_VARIABLE `echo x` ; say "hi"\nsecond-line'

#: The signature a shell leaves behind: the `$`-token gone, the backtick pair consumed. If any
#: recorded field contains THIS instead of HOSTILE, the launch went through a shell string.
EXPANDED = " ; say hi\nsecond-line"

#: Distinctive substrings of the fixture. A field carrying one of these but NOT the whole
#: fixture has been word-split — the other half of the byte-identity proof.
FRAGMENTS = ("CONDUCTOR_NOT_A_VARIABLE", "second-line")

#: Codex native session continuation must never become Conductor's continuation mechanism:
#: every fire is a cold start reconciled from durable state (spec M3, folded into this
#: assertion rather than given its own launch harness).
FORBIDDEN_CODEX_ARGS = ("resume", "--last", "fork")

#: Recording fake: raw argv, cwd, environment and stdin appended to this host's own log.
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
    """One run fired once through its generated driver, plus both hosts' recording logs."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.records: dict[str, list[dict]] = {}
        self.workdir = pathlib.Path()
        self.project = pathlib.Path()
        self.worktree = pathlib.Path()
        self.package = pathlib.Path()
        self.driver_rc: int | None = None
        self.driver_log = ""

    @property
    def diagnosis(self) -> str:
        return (
            f"\nhost={self.host_id} driver rc={self.driver_rc}\n"
            f"driver log:\n{self.driver_log or '(empty)'}\n"
            f"worktree={self.worktree!r}\npackage={self.package!r}"
        )


def _seed_package_root(root: pathlib.Path) -> None:
    """A Conductor package tree at a test-chosen path: the real autodev ``SKILL.md`` plus the
    real ``bin/conductor``, because the Codex driver derives its source root from whichever
    conductor bin resolves at fire time and then refuses to fire unless the skill file is under
    it. Nothing else is copied: the driver's remaining use of that bin is the done-gate probe
    (``conductor assert run --level spec``), which must come back NOT-green or the fire is a
    no-op, and a package with no ``assertions/run.py`` answers exactly that, immediately."""
    (root / "bin").mkdir(parents=True)
    shutil.copyfile(ROOT / "bin" / "conductor", root / "bin" / "conductor")
    (root / "bin" / "conductor").chmod(0o755)
    skill_dir = root / "skills" / SKILL
    skill_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / "skills" / SKILL / "SKILL.md", skill_dir / "SKILL.md")


def _write_fakes(bindir: pathlib.Path, logdir: pathlib.Path) -> dict[str, pathlib.Path]:
    """A recording fake for EVERY supported host, each with its own log."""
    logs = {}
    for host_id in base.HOST_IDS:
        log = logdir / f"{host_id}.jsonl"
        log.write_text("", encoding="utf-8")
        fake = bindir / host_id
        fake.write_text(_FAKE.format(log=str(log)), encoding="utf-8")
        fake.chmod(0o755)
        logs[host_id] = log
    return logs


def _fire(host_id: str) -> Fire:
    """Record ``host_id`` for a fresh run, render its driver, and RUN the driver."""
    fire = Fire(host_id)
    fire.workdir = pathlib.Path(tempfile.mkdtemp(prefix="a-dh-2-")).resolve()
    hostile = fire.workdir / HOSTILE
    fire.project = hostile / "project"
    fire.worktree = hostile / "worktree"
    fire.package = hostile / "package"
    # HOME stays free of the fixture bytes on purpose: it is the driver's PATH root
    # (`$HOME/.local/bin` is prepended before every system bin dir), so putting the fakes there
    # is what makes them beat any real claude/codex installed on this machine — deterministically,
    # rather than by this machine happening not to have one.
    home = fire.workdir / "home"
    bindir = home / ".local" / "bin"
    logdir = fire.workdir / "log"
    for directory in (fire.project, fire.worktree, bindir, logdir):
        directory.mkdir(parents=True)
    _seed_package_root(fire.package)
    logs = _write_fakes(bindir, logdir)
    # `conductor` resolves through PATH to a SYMLINK; the Codex driver derives its source root
    # from `readlink -f` of the bin it resolved, so the symlink is what proves the source root
    # tracks the package tree rather than the PATH entry.
    os.symlink(fire.package / "bin" / "conductor", bindir / "conductor")

    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in {runhost.HOST_ENV, "CONDUCTOR_HOME"}
    }
    environ["HOME"] = str(home)

    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        runhost.record(str(fire.project), host_id)
    finally:
        os.environ.clear()
        os.environ.update(previous)

    script = fire.project / ".conductor" / "resume-autodev.sh"
    render = subprocess.run(
        [
            str(ROOT / "bin" / "conductor"),
            "resume-script",
            "write",
            "--project",
            str(fire.project),
            "--worktree",
            str(fire.worktree),
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

    proc = subprocess.run(
        [str(script)],
        env=environ,
        cwd=str(fire.workdir),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    fire.driver_rc = proc.returncode
    log_path = fire.project / ".conductor" / "resume-autodev.log"
    fire.driver_log = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else ""
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


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    for fire in _FIRES.values():
        shutil.rmtree(fire.workdir, ignore_errors=True)


def _fields(record: dict) -> list[str]:
    """Every discrete recorded field: each argv element, the cwd, each environment value, and
    stdin. "A single field" is the unit byte identity has to survive in."""
    return [
        *record["argv"],
        record["cwd"],
        *record["env"].values(),
        record["stdin"],
    ]


def _prompt_of(host_id: str, argv: list[str]) -> str:
    """The one argv element the fire hands the host as its instruction.

    Structural, not a copy of the product's prose: Claude takes the prompt as the VALUE of
    ``-p``, Codex takes it as the trailing positional. Those two shapes are the reason
    ``base`` forbids a shared argv template, and they are what makes "the prompt" locatable
    without restating what it says.
    """
    if host_id == "claude":
        assert argv.count("-p") == 1, f"no single -p in claude argv: {argv!r}"
        index = argv.index("-p") + 1
        assert index < len(argv), f"-p has no value in claude argv: {argv!r}"
        return argv[index]
    assert len(argv) >= 2, f"codex argv too short: {argv!r}"
    return argv[-1]


def test_the_fixture_carries_every_hostile_character_class() -> None:
    """Anti-stub for this module's own fixture: an emptied or softened HOSTILE would make every
    byte-identity clause below pass without measuring anything."""
    assert "$CONDUCTOR_NOT_A_VARIABLE" in HOSTILE
    assert HOSTILE.count("`") == 2
    assert ";" in HOSTILE
    assert '"' in HOSTILE
    assert "\n" in HOSTILE


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_exactly_one_launch_of_the_recorded_host(host_id: str) -> None:
    fire = fire_for(host_id)
    assert len(fire.records[host_id]) == 1, (
        f"run recorded host={host_id} produced {len(fire.records[host_id])} launches of "
        f"{host_id}; expected exactly one.{fire.diagnosis}"
    )
    argv = fire.records[host_id][0]["argv"]
    assert os.path.basename(argv[0]) == host_id, (
        f"the fire spawned {argv[0]!r}, not the recorded host's executable"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_opposite_host_is_never_spawned(host_id: str) -> None:
    """Must-not-contain, and the half a single-host test can never see. Both directions are
    parametrized because an invariant checked in one direction is not an invariant."""
    other = base.opposite(host_id)
    fire = fire_for(host_id)
    assert fire.records[other] == [], (
        f"run recorded host={host_id} spawned {other} {len(fire.records[other])} time(s); "
        f"argv={[r['argv'] for r in fire.records[other]]}{fire.diagnosis}"
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
def test_the_prompt_arrives_as_one_discrete_argv_element(host_id: str) -> None:
    """The instruction the fire hands the host must be ONE argv element, not a fragment of a
    shell string. Any splitting or requoting would leave it appearing more than once, appearing
    empty, or beginning with a dash (``base.reject_flaglike_prompt``'s refusal condition)."""
    fire = fire_for(host_id)
    argv = fire.records[host_id][0]["argv"]
    prompt = _prompt_of(host_id, argv)
    assert prompt, f"empty prompt element in {argv!r}"
    assert not prompt.startswith("-"), f"prompt would parse as an option: {prompt!r}"
    assert argv.count(prompt) == 1, (
        f"prompt {prompt!r} appears {argv.count(prompt)} times in {argv!r}"
    )


def test_the_codex_prompt_is_byte_identical_to_the_interpolated_skill_path() -> None:
    """Codex's fire interpolates Conductor's source root into the prompt, so for that host the
    byte-identity proof lands in the prompt argv element itself: the fixture bytes and the
    autodev ``SKILL.md`` path must both be present, whole, in that ONE element."""
    fire = fire_for("codex")
    argv = fire.records["codex"][0]["argv"]
    prompt = _prompt_of("codex", argv)
    expected = str(fire.package / "skills" / SKILL / "SKILL.md")
    assert expected in prompt, (
        f"the codex prompt does not name the autodev skill under the run's package root.\n"
        f"prompt={prompt!r}\nexpected substring={expected!r}"
    )
    assert HOSTILE in prompt, (
        f"the fixture bytes did not survive into the codex prompt element: {prompt!r}"
    )


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_the_run_worktree_arrives_as_a_whole_field(host_id: str) -> None:
    """The other path the driver interpolates. It must arrive EQUAL to a recorded field —
    Codex names it in ``--cd``, Claude reaches it as the process working directory — never as
    a prefix of one and never in pieces."""
    fire = fire_for(host_id)
    record = fire.records[host_id][0]
    whole = [field for field in _fields(record) if field == str(fire.worktree)]
    assert whole, (
        f"no recorded field equals the run worktree {str(fire.worktree)!r}\n"
        f"argv={record['argv']!r}\ncwd={record['cwd']!r}"
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
def test_no_recorded_field_carries_a_fragment_of_the_fixture(host_id: str) -> None:
    """Must-not-contain the shell's other fingerprint: word-splitting. A field holding a
    distinctive piece of the fixture without the whole of it is a split one."""
    fire = fire_for(host_id)
    record = fire.records[host_id][0]
    split = [
        field
        for field in _fields(record)
        if any(fragment in field for fragment in FRAGMENTS) and HOSTILE not in field
    ]
    assert not split, f"word-split fixture bytes in {split!r}"


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
