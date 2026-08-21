"""A-DH-5 — relocation refuses while any run artifact lives under the old checkout (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: the relocation safety check refuses, before mutating anything, when a live process, a
registered linked worktree, or an installed schedule resolves beneath the checkout being
relocated.

RED ON PURPOSE, AND FOR TWO NAMED REASONS.

1. THERE IS NO RELOCATION SAFETY CHECK IN THE TREE. `bin/conductor` registers no verb that
   scans a checkout before relocating it. Roadmap Plan 00 ("Source relocation and quarantine")
   produces it — described there as "a `conductor doctor relocation` style scan that fails when
   any cron entry, hook, process, worktree registration, or runtime config still names the old
   path". Plan 00 additionally carries a BLOCKING PRECONDITION: it must not start without the
   owner's explicit go-ahead, because the owner has a live run executing out of the checkout it
   would move. Nothing here starts any part of Plan 00; this assertion only measures its
   absence.

2. VARIANT (b) CANNOT BE CONSTRUCTED YET. It needs a run "whose owner record names a process
   identity that is currently alive". `conductor/core/runstate.py` single-sources the
   `owner.lock` PATH and says in its own docstring that the record's semantics belong to Plan
   02; `conductor/core/locks.py` `hold()` writes no content at all — an empty flocked file names
   no process identity. Roadmap Plan 02 produces `conductor/core/ownership.py` with
   `acquire(...)` and an `OwnerRecord` carrying `wrapper_identity`. Until it exists, this module
   REFUSES TO INVENT A RECORD FORMAT: `test_a_live_owner_process_blocks_relocation` fails naming
   the missing prerequisite rather than fabricating a file and calling the case covered.

Variants (a), (c) and (d) ARE constructed, from shipped mechanisms only: a real `git worktree
add` nested under the checkout, and the real crontab line `conductor/resume_script.py` installs
(`cron_marker` + `driver_script_path`, served through a stub `crontab` on `PATH` so nothing
touches the developer's real crontab).

WHAT THE CHECK'S INTERFACE IS TAKEN TO BE. Nothing to introspect exists, so this assertion
PINS a contract rather than deriving one: a `conductor` CLI verb matching `relocat`/`doctor`,
invoked with `--checkout <path>`; exit 0 means clear-to-proceed, non-zero means refusal and must
print the blocking artifact's own path and an exact recovery command. Plan 00 either matches
that shape or updates this assertion deliberately.

NO REAL RELOCATION HAPPENS. Every fixture is a self-contained temporary directory. No test
performs or simulates a relocation of a real Conductor installation, and the byte-level manifest
below is what proves the check mutated nothing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor import resume_script  # noqa: E402  (needs ROOT on sys.path first)

CONDUCTOR_BIN = ROOT / "bin" / "conductor"

#: Plan 02's declared module for execution-ownership records. Variant (b) needs it.
OWNERSHIP_MODULE = "conductor.core.ownership"

#: What owns each unbuilt prerequisite, so a red result names the plan rather than "missing".
PLAN_00 = (
    "roadmap Plan 00 — Source relocation and quarantine "
    "(docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md), which is additionally "
    "gated on an explicit owner go-ahead"
)
PLAN_02 = (
    "roadmap Plan 02 — Ownership, leases, takeover, prune, rebind, which produces "
    f"{OWNERSHIP_MODULE}.acquire() and the OwnerRecord format"
)

SPEC_RELPATH = "docs/fixture-spec.md"


def _registered_verbs() -> list[str]:
    """The CLI's registered verb list: the top-level `case` arm labels of `bin/conductor`."""
    depth = 0
    verbs: list[str] = []
    for raw in CONDUCTOR_BIN.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if re.match(r"^case\b.*\bin$", line):
            depth += 1
            continue
        if line.startswith("esac"):
            depth -= 1
            continue
        if depth != 1:
            continue
        match = re.match(r"^([A-Za-z0-9_|\-\s]+)\)", line)
        if not match:
            continue
        for label in match.group(1).split("|"):
            label = label.strip()
            if label and label not in verbs:
                verbs.append(label)
    return verbs


def _check_argv(checkout: pathlib.Path) -> list[str] | None:
    """The relocation safety check's invocation, or None when the CLI registers no such verb."""
    for verb in _registered_verbs():
        if "relocat" in verb:
            return [str(CONDUCTOR_BIN), verb, "--checkout", str(checkout)]
        if "doctor" in verb:
            return [str(CONDUCTOR_BIN), verb, "relocation", "--checkout", str(checkout)]
    return None


def _git(cwd: pathlib.Path, *args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if out.returncode != 0:
        raise AssertionError(
            f"fixture git failed: git {' '.join(args)} (rc={out.returncode})\n{out.stderr}"
        )
    return out.stdout


def _manifest(root: pathlib.Path) -> dict[str, str]:
    """A byte-level manifest of one fixture: every path, its kind, its mode, and — for regular
    files — the sha256 of its contents. Symlinks record their target, never their destination's
    bytes, so a relocation that replaced a directory with a link to it would show up."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        try:
            info = path.lstat()
        except FileNotFoundError:  # vanished mid-walk = a mutation, recorded as one
            manifest[relative] = "vanished"
            continue
        mode = stat.filemode(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            manifest[relative] = f"symlink {mode} -> {os.readlink(path)}"
        elif stat.S_ISDIR(info.st_mode):
            manifest[relative] = f"dir {mode}"
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest[relative] = f"file {mode} {digest}"
        else:
            manifest[relative] = f"other {mode}"
    return manifest


class Variant:
    """One fixture checkout in one state, plus the result of running the check against it."""

    def __init__(self, name: str, blocker: str) -> None:
        self.name = name
        self.blocker = blocker
        self.error: str | None = None
        self.workdir = pathlib.Path()
        self.checkout = pathlib.Path()
        self.env: dict[str, str] = {}
        self.run_key = ""
        self.blocking_path = ""
        self.before: dict[str, str] = {}
        self.after: dict[str, str] = {}
        self.worktrees_before = ""
        self.worktrees_after = ""
        self.rc: int | None = None
        self.report = ""
        self.processes: list[subprocess.Popen[bytes]] = []

    def close(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def surviving_processes(self) -> list[int]:
        return [p.pid for p in self.processes if p.poll() is None]


def _base_env(home: pathlib.Path) -> dict[str, str]:
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CONDUCTOR_", "GH_", "GITHUB_"))
        },
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }


def _stub_crontab(bindir: pathlib.Path, lines: list[str]) -> None:
    """A `crontab` on PATH answering exactly these lines. The developer's real crontab is never
    read and never written."""
    body = "".join(f"echo {line!r}\n" for line in lines)
    script = bindir / "crontab"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-l" ]; then\n'
        + (body if lines else 'echo "no crontab for fixture" >&2\nexit 1\n')
        + "exit 0\nfi\n"
        'echo "the fixture crontab refuses writes: $*" >&2\nexit 1\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


def _seed_checkout(variant: Variant, status: str) -> dict[str, str]:
    """A self-contained checkout with one run in `status`. Returns the environment to use."""
    variant.workdir = pathlib.Path(
        tempfile.mkdtemp(prefix=f"a-dh-5-{variant.name}-")
    ).resolve()
    variant.checkout = variant.workdir / "conductor"
    home = variant.workdir / "home"
    bindir = home / ".local" / "bin"
    for directory in (variant.checkout, home, bindir):
        directory.mkdir(parents=True)

    env = _base_env(home)
    _git(variant.checkout, "init", "-b", "trunk", env=env)
    spec = variant.checkout / SPEC_RELPATH
    spec.parent.mkdir(parents=True)
    spec.write_text("# fixture spec\n", encoding="utf-8")
    _git(variant.checkout, "add", SPEC_RELPATH, env=env)
    _git(variant.checkout, "commit", "-m", "seed", env=env)

    env = {
        **env,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CONDUCTOR_HOME": str(variant.checkout),
        "TMPDIR": str(variant.workdir),
    }
    created = subprocess.run(
        [str(CONDUCTOR_BIN), "run", "new", SPEC_RELPATH],
        cwd=str(variant.checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert created.returncode == 0, (
        f"the product could not create the {variant.name} fixture run:\n"
        f"{created.stdout}\n{created.stderr}"
    )
    listed = subprocess.run(
        [str(CONDUCTOR_BIN), "run", "list", "--all", "--json"],
        cwd=str(variant.checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    variant.run_key = json.loads(listed.stdout)[0]["run_key"]
    promote = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from conductor.core import runstate; "
            "runstate.set_status(sys.argv[1], sys.argv[2], sys.argv[3])",
            str(variant.checkout / ".conductor"),
            variant.run_key,
            status,
        ],
        cwd=str(variant.checkout),
        env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert promote.returncode == 0, f"{promote.stdout}\n{promote.stderr}"
    return env


def _build(name: str) -> Variant:
    variant = Variant(name, blocker=name)
    if name == "worktree":
        env = _seed_checkout(variant, "checkpointed")
        _stub_crontab(pathlib.Path(env["HOME"]) / ".local" / "bin", [])
        nested = variant.checkout / ".worktrees" / "phase-1"
        _git(variant.checkout, "worktree", "add", "-b", "phase-1", str(nested), env=env)
        variant.blocking_path = str(nested)
    elif name == "process":
        env = _seed_checkout(variant, "checkpointed")
        _stub_crontab(pathlib.Path(env["HOME"]) / ".local" / "bin", [])
        if importlib.util.find_spec(OWNERSHIP_MODULE) is None:
            variant.error = (
                f"variant (b) cannot be constructed: {OWNERSHIP_MODULE} does not exist, so "
                "there is no owner-record format to write a live process identity into. "
                f"Owned by {PLAN_02}."
            )
            return variant
        holder = subprocess.Popen(  # noqa: S603 — fixture-owned, torn down in Variant.close
            [
                sys.executable,
                "-c",
                "import os, sys, time\n"
                "from conductor.core import ownership\n"
                "with ownership.acquire(sys.argv[1], sys.argv[2], host='claude',\n"
                "                       wrapper_identity=str(os.getpid())):\n"
                "    print('held', flush=True)\n"
                "    time.sleep(600)\n",
                str(variant.checkout / ".conductor"),
                variant.run_key,
            ],
            cwd=str(variant.checkout),
            env={**env, "PYTHONPATH": str(ROOT)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        variant.processes.append(holder)
        assert holder.stdout is not None
        if holder.stdout.readline().strip() != b"held":
            variant.error = (
                f"variant (b) cannot be constructed: {OWNERSHIP_MODULE}.acquire() did not "
                "take the owner record for a live process. Owned by " + PLAN_02
            )
            return variant
        variant.blocking_path = str(holder.pid)
    elif name == "schedule":
        env = _seed_checkout(variant, "checkpointed")
        # The product's OWN crontab line for this checkout, not a hand-typed approximation.
        launcher = resume_script.driver_script_path(str(variant.checkout))
        marker = resume_script.cron_marker(str(variant.checkout))
        _stub_crontab(
            pathlib.Path(env["HOME"]) / ".local" / "bin",
            [f"*/10 * * * * {launcher} {marker}"],
        )
        variant.blocking_path = launcher
    elif name == "control":
        env = _seed_checkout(variant, "checkpointed")
        _stub_crontab(pathlib.Path(env["HOME"]) / ".local" / "bin", [])
    else:  # pragma: no cover — the variant table above is the whole set
        raise AssertionError(f"unknown variant {name!r}")

    variant.env = env
    variant.before = _manifest(variant.workdir)
    variant.worktrees_before = _git(
        variant.checkout, "worktree", "list", "--porcelain", env=env
    )

    argv = _check_argv(variant.checkout)
    if argv is None:
        variant.error = (
            "no relocation safety check is registered by bin/conductor (no verb matching "
            f"relocat/doctor among {_registered_verbs()}). Owned by {PLAN_00}."
        )
    else:
        proc = subprocess.run(
            argv,
            cwd=str(variant.workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        variant.rc = proc.returncode
        variant.report = f"{proc.stdout}\n{proc.stderr}"
    variant.after = _manifest(variant.workdir)
    variant.worktrees_after = _git(
        variant.checkout, "worktree", "list", "--porcelain", env=env
    )
    return variant


_VARIANTS: dict[str, Variant] = {}


def variant(name: str) -> Variant:
    if name not in _VARIANTS:
        _VARIANTS[name] = _build(name)
    return _VARIANTS[name]


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    for built in _VARIANTS.values():
        built.close()


def _assert_untouched(built: Variant) -> None:
    """The post-check manifest must be byte-identical to the pre-check one — including that the
    checkout still exists under its original name and every worktree registration is
    unchanged."""
    changed = sorted(
        name
        for name in set(built.before) | set(built.after)
        if built.before.get(name) != built.after.get(name)
    )
    assert not changed, (
        f"the {built.name} variant was mutated by a check that must refuse before mutating "
        f"anything: {[(n, built.before.get(n), built.after.get(n)) for n in changed]}"
    )
    assert built.checkout.is_dir(), (
        f"the checkout no longer exists under its original name: {built.checkout}"
    )
    assert built.worktrees_after == built.worktrees_before, (
        "a worktree registration changed:\n"
        f"before:\n{built.worktrees_before}\nafter:\n{built.worktrees_after}"
    )


def _assert_refused(built: Variant) -> None:
    """Non-zero exit, the blocking artifact named WITH ITS PATH, and an exact recovery
    command."""
    assert built.error is None, built.error
    assert built.rc is not None and built.rc != 0, (
        f"the {built.name} blocker did not block: the check exited "
        f"{built.rc} — clear-to-proceed while the artifact is live:\n{built.report}"
    )
    assert built.blocking_path in built.report, (
        f"the refusal does not name the blocking artifact's path {built.blocking_path!r}; a "
        f"generic blocker name is not actionable:\n{built.report}"
    )
    assert "conductor" in built.report.lower(), (
        f"the refusal prints no recovery command an operator can run:\n{built.report}"
    )


def test_the_relocation_safety_check_is_registered() -> None:
    """The umbrella prerequisite: without an entry point, none of the four variants below can
    be observed at all."""
    argv = _check_argv(pathlib.Path("/nonexistent"))
    assert argv is not None, (
        "bin/conductor registers no relocation safety check, so relocating the canonical "
        "checkout is unguarded against a live process, a nested linked worktree, or an "
        f"installed schedule. Owned by {PLAN_00}."
    )


def test_a_registered_linked_worktree_under_the_checkout_blocks_relocation() -> None:
    """Variant (a), independently sufficient."""
    built = variant("worktree")
    _assert_refused(built)
    _assert_untouched(built)


def test_a_live_owner_process_blocks_relocation() -> None:
    """Variant (b), independently sufficient. Fails on the missing Plan 02 owner record rather
    than fabricating one.

    The liveness half is a real process this module started and still owns: if the check killed
    it — or it died on its own — the variant would prove nothing, so that is checked here rather
    than left to a separate test that passes vacuously while (b) is unconstructible."""
    built = variant("process")
    _assert_refused(built)
    assert len(built.surviving_processes()) == len(built.processes), (
        "the owner process this variant depends on is no longer alive, so the refusal above "
        "was not observed against a live process"
    )
    _assert_untouched(built)


def test_an_installed_schedule_entry_under_the_checkout_blocks_relocation() -> None:
    """Variant (c), independently sufficient."""
    built = variant("schedule")
    _assert_refused(built)
    _assert_untouched(built)


def test_the_control_is_cleared_to_proceed() -> None:
    """Must-contain (anti-stub): variant (d) — every run checkpointed, no live artifacts —
    reports clear-to-proceed. Otherwise a check that refuses unconditionally passes while
    proving nothing."""
    built = variant("control")
    assert built.error is None, built.error
    assert built.rc == 0, (
        "the control fixture has no live process, no nested worktree and no installed "
        f"schedule, yet the check refused (rc={built.rc}):\n{built.report}"
    )
    _assert_untouched(built)


def test_no_blocked_variant_was_renamed_or_partially_moved() -> None:
    """Must-not-contain, stated separately from the per-variant manifest clause: no blocked
    variant may show a rename, a partial move, or a modified worktree registration."""
    damage = []
    for name in ("worktree", "process", "schedule"):
        built = variant(name)
        if built.error is not None:
            damage.append(f"{name}: not observed — {built.error}")
            continue
        if not built.checkout.is_dir():
            damage.append(f"{name}: the checkout no longer exists at {built.checkout}")
        if built.before != built.after:
            damage.append(f"{name}: the fixture manifest changed under a refusal")
        if built.worktrees_before != built.worktrees_after:
            damage.append(f"{name}: a worktree registration changed under a refusal")
    assert not damage, "\n".join(damage)
