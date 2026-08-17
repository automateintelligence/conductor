"""Host adapters — the only place Conductor knows a host by name.

Design §"System architecture" forbids the core from containing a Claude slash command, a Codex
dollar invocation, ``CLAUDE_PLUGIN_ROOT``, a host-specific permission flag, or an assumption
about one installation directory. This package is where all of those live instead.

The single most important rule in this package: **no argument vector is ever built by shared
code.** ``-p`` means ``--print`` to Claude and ``--profile`` to Codex (ground truth §"Model and
configuration"), so a shared argv template is wrong exactly once, silently, and presents as a
model-selection bug. Each adapter builds its own argv in its own module, and
``tests/conductor/hosts/test_argv.py`` enforces that structurally.

Sharing *validation* and *process-table mechanics* is fine and is done here and in ``proc``.
Sharing *argv construction* is not.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

HOST_IDS = ("claude", "codex")

# Least privileged first. The projection onto each host is the adapter's job: Claude's posture
# is a mode plus a settings file, Codex's is a graded sandbox axis, and they do not map one to
# one (ground truth §"Sandbox and approvals"). The shared vocabulary is the posture NAME only.
POSTURES = ("supervised", "scoped", "full-bypass")

# `codex --version` and `codex exec --help` HANG when stdin is an open pipe or a terminal
# (ground truth §"Codex help hangs without stdin redirection"). Under cron the symptom is a
# stuck worker, not a failed one, so every probe in this package redirects stdin from /dev/null
# and bounds itself with a timeout.
VERSION_PROBE_TIMEOUT = 20.0

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


class UnknownHost(ValueError):
    """A host id outside ``HOST_IDS``."""


class HostUnavailable(RuntimeError):
    """The host's executable, source root, or version could not be resolved."""


class HostVersionTooOld(RuntimeError):
    """The installed host is below the supported floor (design line 365)."""


class PermissionProfileError(ValueError):
    """A permission profile is malformed, uses an unknown posture, or belongs to another host."""


class HookContractUnverified(RuntimeError):
    """The host's PreCompact hook contract has not been verified on this host.

    Design line 306: a missing, untrusted, disabled, or ineffective required hook blocks
    unattended mode rather than allowing an unbounded session. Raising is that rule, not a stub.
    """


class DispatchTimeout(RuntimeError):
    """An implementation dispatch exceeded its timeout; the child was killed."""


@dataclass(frozen=True)
class DispatchResult:
    """The bounded result of one isolated implementation dispatch.

    ``result_path`` is load-bearing. Codex can write its final message to a file the caller
    names (``-o/--output-last-message``); Claude cannot, and its adapter captures stdout and
    writes that same file itself. Defining the contract the other way round — "the adapter
    returns captured stdout" — would generalise the Claude-side compromise and throw away the
    better surface (ground truth §"Output").
    """

    host: str
    argv: tuple[str, ...]
    returncode: int
    result_path: str
    result_text: str
    truncated: bool
    duration_s: float


class HostAdapter(Protocol):
    """Everything that genuinely differs between Claude Code and Codex.

    ``id`` is also the basename of the host's executable. Tasks 7 and 8 depend on that.
    """

    id: str

    def executable(self) -> str: ...
    def source_root(self) -> str: ...
    def version(self) -> tuple[int, ...]: ...
    def minimum_version(self) -> tuple[int, ...]: ...
    def upgrade_hint(self) -> str: ...
    def native_invocation(self, skill: str) -> str: ...
    def launch_prompt(self, skill: str, *, run_key: str | None = None) -> str: ...
    def worker_argv(
        self,
        *,
        state_root: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]: ...
    def worker_env(
        self, *, state_root: str, run_key: str, project_root: str
    ) -> dict[str, str]: ...
    def reviewer_argv(
        self,
        *,
        pr: int,
        head_sha: str,
        run_key: str,
        project_root: str,
        posture: str = "supervised",
    ) -> list[str]: ...
    def permission_profile(self, posture: str = "supervised") -> dict: ...
    def validate_permissions(self, profile: dict) -> None: ...
    def process_identity(self, pid: int) -> str: ...
    def process_alive(self, identity: str) -> bool: ...
    def processes_under(self, roots: list[str]) -> list[int]: ...
    def install_hooks(
        self, state_root: str, run_key: str, *, command: list[str]
    ) -> str: ...
    def hook_installed(self, state_root: str, run_key: str) -> bool: ...
    def dispatch_implementation(
        self,
        prompt: str,
        *,
        timeout: float,
        result_path: str | None = None,
        posture: str = "scoped",
    ) -> DispatchResult: ...


def opposite(host_id: str) -> str:
    """The default reviewer host for a run owned by ``host_id`` (design line 25)."""
    if host_id not in HOST_IDS:
        raise UnknownHost(f"unknown host {host_id!r}; supported hosts are {HOST_IDS}")
    return "codex" if host_id == "claude" else "claude"


def load(host_id: str) -> HostAdapter:
    """The adapter for ``host_id``. Imports are local so ``base`` stays leaf-level."""
    if host_id == "claude":
        from conductor.hosts.claude import ClaudeAdapter

        return ClaudeAdapter()  # type: ignore[return-value]  # conforming from Task 10
    if host_id == "codex":
        from conductor.hosts.codex import CodexAdapter

        return CodexAdapter()  # type: ignore[return-value]  # conforming from Task 10
    raise UnknownHost(f"unknown host {host_id!r}; supported hosts are {HOST_IDS}")


def reject_flaglike_prompt(prompt: str) -> str:
    """Refuse a prompt that would be parsed as an option.

    Both hosts take the prompt as a trailing positional argument. Neither adapter emits a
    ``--`` separator, because whether ``codex exec`` honours one was not verified and guessing
    at an unverified CLI contract is how the ``-p`` collision happened in the first place.
    Refusing the input instead is verifiable today.
    """
    if prompt.startswith("-"):
        raise ValueError(
            f"prompt would be parsed as an option: {prompt[:40]!r}. Prompts are passed as a "
            "trailing positional argument and must not start with '-'."
        )
    return prompt


def probe_version(
    executable: str, *, timeout: float = VERSION_PROBE_TIMEOUT
) -> tuple[int, ...]:
    """``<executable> --version`` parsed into a comparable tuple.

    ``stdin=DEVNULL`` is the whole point of this helper existing rather than each adapter
    calling ``subprocess.run``: without it ``codex --version`` hangs forever instead of
    answering (ground truth §"Codex help hangs without stdin redirection").
    """
    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HostUnavailable(f"{executable!r} is not executable") from exc
    except subprocess.TimeoutExpired as exc:
        raise HostUnavailable(
            f"{executable} --version did not answer within {timeout}s (no write occurred); "
            f"check the install with: command -v {executable}"
        ) from exc
    if proc.returncode != 0:
        raise HostUnavailable(
            f"{executable} --version exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
        )
    match = _VERSION_RE.search(proc.stdout) or _VERSION_RE.search(proc.stderr)
    if not match:
        raise HostUnavailable(
            f"{executable} --version printed no dotted version number: "
            f"{(proc.stdout or proc.stderr).strip()[:200]!r}"
        )
    return tuple(int(part) for part in match.group(1).split("."))


def assert_minimum_version(adapter: HostAdapter) -> tuple[int, ...]:
    """The installed version, or ``HostVersionTooOld`` naming the floor and the exact check."""
    found = adapter.version()
    floor = adapter.minimum_version()
    if found < floor:
        raise HostVersionTooOld(
            f"{adapter.id} {'.'.join(map(str, found))} is below the supported floor "
            f"{'.'.join(map(str, floor))} (no write occurred). {adapter.upgrade_hint()}"
        )
    return found
