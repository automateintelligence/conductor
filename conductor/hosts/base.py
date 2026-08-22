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
# and bounds itself with a timeout. The probes that do so live in the adapter that owns the
# subcommand being probed (`codex.installed_plugins`, `CodexAdapter.plugin_list_lookup`); this
# module carries no shared probe helper, because the only one it ever had had no callers.

# --- how long a WORKER LAUNCH may be silent -------------------------------------------------
#
# The generated cron driver holds `.conductor/resume.lock` for the whole fire, so a host that
# never answers holds it forever: every later twenty-minute tick fails `flock -n` and exits 0,
# and a permanently blocked run becomes indistinguishable from a healthy idle one.
#
# The bound below is deliberately NOT elapsed time, and that is the whole design decision. A
# legitimate phase runs for HOURS — one live fire in this project's own history ran 2h58m and
# wrote nothing at all to the driver log until its final second — so a wall-clock ceiling short
# enough for an operator to act on would kill working phases, and one long enough to be safe
# would bound nothing. What is bounded is SILENCE: the fire's own progress, sampled as the
# whole seconds of CPU consumed by every process in its process group plus the bytes it has
# appended to the driver log. Either one moving is progress and resets the clock; elapsed time
# never expires anything on its own.
#
# Two windows, because "has not started" and "has stopped" are different claims about a fire
# and deserve different patience. They live HERE, beside `PLUGIN_LIST_TIMEOUT_S`, because this
# package is where the host layer declares every bound it puts on a host process; the driver's
# shell is where one of them is enforced.

#: How long the fire may show NO progress at all before it is killed. A host launch is a
#: runtime start plus config/plugin load — seconds of CPU for a node-based Claude, streamed
#: events for `codex exec` — so a launch still at zero after two minutes has not begun doing
#: work; it is blocked before it. That is the known instance (a host subcommand blocking on an
#: unredirected stdin, ground truth §"Codex help hangs") and the shape the hanging-host probe
#: in A-DH-4 reproduces.
FIRE_STARTUP_TIMEOUT_S = 120

#: How long a fire that HAS shown progress may then go silent. Much longer, because it has
#: proven it is doing work and the cost of killing a real phase is an hour of lost work against
#: a stall an operator sees one tick later. Longer than the twenty-minute tick on purpose: a
#: fire quiet for longer than a full tick and a half, having produced neither CPU nor output,
#: is not working.
FIRE_IDLE_TIMEOUT_S = 1800

#: Seconds between the TERM that asks the fire to stop and the KILL that makes it. Same reason
#: as `PLUGIN_LIST_KILL_GRACE_S`: a bound expressed only as a TERM is not a bound, because a
#: child that traps or ignores the signal keeps running and its supervisor keeps waiting.
FIRE_KILL_GRACE_S = 5

#: How often the supervisor samples progress. Deliberately not named `*_TIMEOUT_S`/`*_GRACE_S`:
#: it is a sampling interval, not a bound, and it is the resolution of the two above rather
#: than a third ceiling.
FIRE_POLL_S = 5


class UnknownHost(ValueError):
    """A host id outside ``HOST_IDS``."""


class HostUnavailable(RuntimeError):
    """The host's executable, source root, or version could not be resolved."""


class HostProbeTimeout(HostUnavailable):
    """A bounded host probe was killed for exceeding its timeout: the host could not be asked.

    Its own class rather than a plain ``HostUnavailable`` because the two are different facts
    with opposite remedies, and a caller that cannot tell them apart gives the wrong advice: an
    absent executable means the host is not installed, while an expired probe means the host IS
    installed and did not answer. Reporting the second as the first sends an owner to reinstall
    something they already have.

    Raised at the probe, where the timeout is the only thing known. What an unanswerable probe
    MEANS for a run is policy and belongs to the caller — ``conductor.preflight`` degrades it to
    ``unverified`` rather than to ``missing``. A probe that swallowed the expiry itself would
    take that decision away from the layer that owns it, and would hand back an answer
    indistinguishable from "asked, and the host reported nothing".

    ``partial`` carries whatever the probe's caller had already established before it asked the
    host, so degrading does not also discard facts that were never in doubt.
    """

    def __init__(self, message: str, *, partial: object = None) -> None:
        super().__init__(message)
        self.partial = partial


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

    # --- the generated cron driver (A1) ----------------------------------------------------
    #
    # The nineteen members above launch a host from Python. The Tier-B driver does not: it is
    # a bash script cron fires, so what it needs from an adapter is SHELL TEXT plus the two
    # variable names that text uses. Those cannot be expressed as argv, which is why they are
    # their own group rather than a reinterpretation of ``worker_argv``.
    #
    # The no-shared-templating rule from this module's docstring applies unchanged, and
    # ``tests/conductor/hosts/test_driver_text.py`` enforces it structurally: every member
    # below must be defined in its own adapter module.

    #: Shell variable holding this host's resolved executable (``CLAUDE_BIN`` / ``CODEX_BIN``).
    BIN_VAR: str

    #: The owner-flags variable this host's driver reads. One per host, deliberately: the
    #: VALUE is that host's flag vocabulary, and a single shared name is how an owner's
    #: ``--dangerously-skip-permissions`` would reach a ``codex exec`` argv.
    FLAGS_VAR: str

    #: posture name -> the flags an owner writes in ``resume-env.sh`` to choose it.
    POSTURE_EXAMPLES: dict[str, str]

    #: posture name -> one line of prose explaining what that posture grants.
    POSTURE_NOTES: dict[str, str]

    def resume_bin_resolution(self) -> str: ...
    def resume_unresolved_guard(self) -> str: ...
    def resume_posture_arms(self) -> str: ...
    def resume_fire_command(self) -> str: ...
    def posture_of(self, args: list[str]) -> str: ...
    def session_posture(self, mode: str) -> str: ...
    def scheduled_tasks_file(self) -> str | None: ...


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
