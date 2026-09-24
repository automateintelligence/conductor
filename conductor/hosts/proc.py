"""Process-table mechanics. **This module knows no host name, and must not learn one.**

``base``'s docstring already reserves this split: "Sharing *validation* and *process-table
mechanics* is fine and is done here and in ``proc``." What each adapter keeps for itself is
where its session handle comes from and which kernel artifact proves it live. What they share
is the reading of ``/proc`` — and sharing that is not a convenience, it is the reason no part
of Conductor needs to look at a process NAME.

NO NAME MATCHING LIVES HERE, OR ANYWHERE. The deleted cron guard asked "is a process called
``claude`` running", which on this class of machine is wrong in both directions at once:
``pgrep -f claude`` matches every shell whose argv mentions ``~/.claude`` (including
Conductor's own), while ``pgrep -x claude`` matches nothing, because the Claude Code binary is
named after its VERSION (``2.1.227``) rather than after the product. The predicates below ask
instead "is the process I RECORDED still the process at that pid", which needs no name:
``starttime`` answers it, and ``boot_id`` answers it across a reboot.

THE THREE ANSWERS. Every liveness predicate here returns ``True`` / ``False`` / ``None`` and
never collapses the third into either of the others. ``False`` is a POSITIVE EXIT PROOF — the
``/proc`` entry is gone, or the pid was recycled and its ``starttime`` no longer matches, or
the machine has rebooted since the record was written. ``None`` means the question could not
be answered. The difference is the whole safety argument: ``False`` clears an ownership record
and lets a cron fire into a checkout, so anything short of proof must be ``None``, which every
caller here treats as occupied.

WHY ``starttime`` AND ``boot_id``. A bare pid has no defence against reuse: ``pid_max`` is
4194304 on this machine, so wrap is slow but not impossible, and a recycled pid reads as
"live" for a holder that exited — or, read the other way by reuse-aware code, as a false clear.
Field 22 of ``/proc/<pid>/stat`` is ticks-since-boot at process start; it is world-readable,
stable for the life of the process, and discriminating. It is also meaningless across a reboot,
because the clock it counts from restarts — so ``boot_id`` rides along, and a differing boot id
is itself a positive exit proof.

LINUX ONLY, DELIBERATELY VISIBLE. Everything here reads ``/proc``. On a kernel without it the
functions return ``None`` rather than raising or guessing, so an unsupported platform presents
as "cannot tell" — which fails closed — instead of as "nobody is there".
"""

from __future__ import annotations

import os

#: The machine's boot identity. Ticks-since-boot are uninterpretable across a reboot, so an
#: identity that carries no boot id cannot tell "the process I recorded" from "a process that
#: happens to hold that pid with that tick count on a later boot".
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

#: The kernel's lock table. Codex's session proof resolves through it; nothing else does.
LOCKS_PATH = "/proc/locks"

MOUNTS_PATH = "/proc/self/mounts"

#: Scheme prefix for the identity of a PLAIN LOCAL PROCESS — one this machine forked and whose
#: pid the recorder actually holds, such as the ``conductor heartbeat`` wrapper that supervises
#: a fire for its whole life. It is deliberately NOT a host id: a wrapper is a wrapper on both
#: hosts, and minting ``claude:<wrapper-pid>:…`` would put a value in the record that looks like
#: a Claude SESSION identity and is not one.
LOCAL_SCHEME = "proc"

#: How many parents an ancestry walk will follow before giving up. A cycle is impossible in a
#: well-formed process tree, but the walk reads ``/proc`` entries that can be replaced under it,
#: so the bound is what makes the loop terminate rather than an assumption about the tree.
ANCESTRY_LIMIT = 64


def boot_id() -> str | None:
    """This boot's identity, or ``None`` when it cannot be read."""
    try:
        with open(BOOT_ID_PATH, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


def hidepid_active() -> bool:
    """Is ``/proc`` mounted with a ``hidepid`` that can hide another user's processes?

    Load-bearing, and the one place absence must not be believed. Under ``hidepid=1``/``=2`` a
    process belonging to another user has no readable ``/proc/<pid>`` at all, so "the entry is
    gone" stops meaning "the process exited" and starts meaning "you may not look" — which,
    read as an exit proof, clears a live owner's record. Unreadable mounts count as active for
    the same reason: not knowing is not evidence.
    """
    try:
        with open(MOUNTS_PATH, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return True
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[2] != "proc":
            continue
        for option in fields[3].split(","):
            if not option.startswith("hidepid="):
                continue
            if option.split("=", 1)[1] not in ("0", "off"):
                return True
    return False


def _read_stat(pid: int) -> tuple[str, str]:
    """``("ok", raw)`` / ``("absent", "")`` / ``("unreadable", "")`` for ``/proc/<pid>/stat``.

    Three outcomes rather than an optional, because "there is no such process" and "I was not
    allowed to look" are the two facts a liveness answer must never merge.
    """
    if pid <= 0:
        return "absent", ""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="surrogateescape") as h:
            return "ok", h.read()
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return ("unreadable", "") if hidepid_active() else ("absent", "")
    except OSError:
        return "unreadable", ""


def _parse_ticks(raw: str) -> int | None:
    """Field 22 of a ``/proc/<pid>/stat`` line.

    Split from the LAST ``)``, never on whitespace from the left: field 2 is the executable's
    ``comm`` in parentheses and may itself contain spaces and parentheses, so a naive
    ``split()[21]`` silently reads a different field for any process whose name has a space in
    it. After the split the remaining fields start at field 3, so field 22 is index 19.
    """
    _, _, tail = raw.rpartition(")")
    fields = tail.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def start_ticks(pid: int) -> int | None:
    """``starttime`` (ticks since boot) for ``pid``, or ``None`` if it cannot be read."""
    outcome, raw = _read_stat(pid)
    if outcome != "ok":
        return None
    return _parse_ticks(raw)


def pid_identity(scheme: str, pid: int) -> str | None:
    """``"<scheme>:<pid>:<starttime-ticks>:<boot-id>"``, or ``None``.

    ``None`` whenever any component is unavailable, and every caller treats that as "no
    identity is available" and REFUSES TO CLAIM OWNERSHIP. Minting a partial identity — a bare
    pid, or a pid with no boot id — would write a record that a later reader cannot check for
    reuse, and an unverifiable record is worse than no record: it reads as a live owner forever,
    or as a dead one wrongly.
    """
    if pid <= 0 or ":" in scheme or not scheme:
        return None
    ticks = start_ticks(pid)
    if ticks is None:
        return None
    boot = boot_id()
    if boot is None:
        return None
    return f"{scheme}:{pid}:{ticks}:{boot}"


def local_identity(pid: int) -> str | None:
    """The identity of a plain local process — see ``LOCAL_SCHEME``."""
    return pid_identity(LOCAL_SCHEME, pid)


def parse_pid_identity(identity: str, *, scheme: str) -> tuple[int, int, str] | None:
    """``(pid, ticks, boot_id)`` from a ``<scheme>:<pid>:<ticks>:<boot>`` string.

    ``None`` for anything else, INCLUDING a well-formed identity belonging to a different
    scheme. A caller that guessed at another scheme's string would be answering a question
    about a process it cannot identify.
    """
    parts = identity.split(":")
    if len(parts) != 4 or parts[0] != scheme:
        return None
    try:
        pid, ticks = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if pid <= 0 or ticks < 0 or not parts[3]:
        return None
    return pid, ticks, parts[3]


def pid_identity_liveness(identity: str, *, scheme: str) -> bool | None:
    """Is the process a ``<scheme>:<pid>:<ticks>:<boot>`` identity names still running?

    ``True`` live, ``False`` PROVABLY exited, ``None`` cannot tell. The three exit proofs, in
    the order they are cheapest to establish:

    * the recorded boot id differs from this boot's — the machine restarted, so nothing the
      record named survived;
    * ``/proc/<pid>`` is absent on a ``/proc`` that is not hiding anything;
    * ``/proc/<pid>`` is present but its ``starttime`` differs — the pid was recycled, and the
      process that held it is gone.

    Everything else is ``None``: an unparseable identity, an unreadable boot id, a ``/proc``
    with ``hidepid`` where absence proves nothing, an unreadable ``stat``.
    """
    parsed = parse_pid_identity(identity, scheme=scheme)
    if parsed is None:
        return None
    pid, ticks, recorded_boot = parsed
    current_boot = boot_id()
    if current_boot is None:
        return None
    if current_boot != recorded_boot:
        return False
    outcome, raw = _read_stat(pid)
    if outcome == "absent":
        return False
    if outcome != "ok":
        return None
    observed = _parse_ticks(raw)
    if observed is None:
        return None
    return observed == ticks


def ancestor_pids(pid: int, *, limit: int = ANCESTRY_LIMIT) -> list[int]:
    """``pid`` and its parents, nearest first. Stops at pid 1, at an unreadable entry, or at
    ``limit`` — the walk reads ``/proc`` entries that can be replaced under it, so the bound is
    what terminates the loop rather than an assumption about the tree."""
    chain: list[int] = []
    seen: set[int] = set()
    current = pid
    while current > 0 and len(chain) < limit and current not in seen:
        seen.add(current)
        chain.append(current)
        outcome, raw = _read_stat(current)
        if outcome != "ok":
            break
        _, _, tail = raw.rpartition(")")
        fields = tail.split()
        if len(fields) < 2:
            break
        try:
            current = int(fields[1])
        except ValueError:
            break
    return chain


def lock_holders() -> dict[tuple[int, int, int], tuple[int, ...]] | None:
    """``(major, minor, inode) -> holder pids`` from the kernel's lock table, or ``None``.

    ``None`` on an unreadable ``/proc/locks``, never an empty mapping: "the kernel holds no
    locks" and "I could not ask" are different facts, and the second read as the first turns a
    live lock holder into a proven exit.

    Format note, verified on this kernel: the device is written ``MAJOR:MINOR`` in HEX while
    the inode that follows it is DECIMAL (``08:30:227807`` for major 8, minor 48, inode
    227807). The holder pid is the token immediately before that triple, which survives the
    ``->`` prefix the kernel inserts on a blocked waiter's line where a fixed column index
    would not.
    """
    try:
        with open(LOCKS_PATH, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    found: dict[tuple[int, int, int], list[int]] = {}
    for line in lines:
        tokens = line.split()
        for index, token in enumerate(tokens):
            parts = token.split(":")
            if len(parts) != 3:
                continue
            try:
                key = (int(parts[0], 16), int(parts[1], 16), int(parts[2]))
            except ValueError:
                continue
            holder = -1
            if index:
                try:
                    holder = int(tokens[index - 1])
                except ValueError:
                    holder = -1
            found.setdefault(key, []).append(holder)
            break
    return {key: tuple(pids) for key, pids in found.items()}


def path_lock_state(path: str) -> tuple[str, tuple[int, ...]]:
    """``(state, holder pids)`` for advisory locks on ``path``.

    ``state`` is one of:

    * ``"held"`` — the file exists and its (device, inode) appears in the kernel's lock table;
    * ``"unheld"`` — the file exists and does not appear, which is a POSITIVE exit proof for a
      lock a live process would still be holding: a killed holder leaves the file behind but
      the kernel releases its lock;
    * ``"absent"`` — no such file, the clean-exit proof;
    * ``"unknown"`` — the file could not be stat'd, or the lock table could not be read.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "absent", ()
    except OSError:
        return "unknown", ()
    table = lock_holders()
    if table is None:
        return "unknown", ()
    key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    holders = table.get(key)
    if holders is None:
        return "unheld", ()
    return "held", holders


def holds_flock(pid: int, st: os.stat_result) -> bool:
    """Does one of ``pid``'s open file DESCRIPTIONS hold a ``flock`` on the file ``st`` names?

    Having the file open is not holding the lock: ``flock`` belongs to the open file
    description. The kernel says which description holds it in ``/proc/<pid>/fdinfo/<fd>``: a
    ``lock:`` line carrying ``FLOCK`` and the file's ``major:minor:inode`` appears only on the
    description the lock belongs to — including one inherited from an ancestor's
    ``exec 9>"$LOCK"; flock -n 9``, whose locking ``flock`` helper has long exited. That helper's
    exit is also why ``/proc/locks`` cannot be relied on here: it omits a lock whose recorded
    pid no longer exists. Anything unreadable answers no."""
    want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    directory = f"/proc/{pid}/fd"
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    for name in names:
        try:
            opened = os.stat(os.path.join(directory, name))
        except OSError:
            continue
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            continue
        try:
            with open(f"/proc/{pid}/fdinfo/{name}", encoding="utf-8") as handle:
                info = handle.read().splitlines()
        except OSError:
            continue
        for line in info:
            if not line.startswith("lock:") or "FLOCK" not in line.split():
                continue
            for token in line.split():
                parts = token.split(":")
                if len(parts) != 3:
                    continue
                try:
                    found = (int(parts[0], 16), int(parts[1], 16), int(parts[2]))
                except ValueError:
                    continue
                if found == want:
                    return True
    return False


def flock_holder_pids(st: os.stat_result) -> list[int]:
    """Every process whose open file descriptions hold a ``flock`` on the file ``st`` names,
    per ``holds_flock``. Read-only: the lock is never taken to test it. Processes whose
    descriptor tables this user cannot read are not seen."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    return sorted(
        int(entry)
        for entry in entries
        if entry.isdigit() and holds_flock(int(entry), st)
    )
