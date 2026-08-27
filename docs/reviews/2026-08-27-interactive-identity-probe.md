# Interactive worker identity — empirical probe

**Date:** 2026-08-27
**Branch:** `probe/interactive-identity`
**Status:** Findings only. No product code was modified.

## Why

The cron driver's deleted `pgrep -f 'claude'` guard must not come back, and no process-name
matching may replace it. The replacement is a Conductor-owned ownership record that an
interactive worker registers before doing product work. The owner's constraint:

> A PID from a short-lived `conductor` CLI call is not sufficient — it must represent the
> actual live Claude/Codex session across skill turns.

This document establishes, empirically, what durable identity each host makes available, and
whether that constraint can be met.

**Headline:** it can be met on both hosts, but by *different* mechanisms. Claude offers a
session PID and nothing else. Codex offers no PID at all but offers something strictly
better — a kernel-held `flock` keyed by session id. A symmetric *contract* is achievable; a
symmetric *identity value* is not, and forcing one would discard Codex's stronger proof.

## Method

Every claim below is a command run on this machine (WSL2, Ubuntu 22.04, kernel
6.18.33.1-microsoft-standard-WSL2, single PID namespace `pid:[4026532221]`, `/proc` mounted
without `hidepid`). Codex work used a scratch `CODEX_HOME` at `/tmp/idprobe/codexhome` with a
copy of `auth.json`; the operator's real `~/.codex` was only ever read. Integrity check is at
the end.

---

## Part 1 — Claude Code

Observed host version: `2.1.227` (interactive session under the Happy launcher), with
`2.1.241` and `2.1.246` sessions also live on the box.

### The identity that exists

`CLAUDE_PID` — an environment variable Claude Code injects into the environment of every
subprocess it spawns for a tool call. It names the **top-level Claude Code CLI process**.

```
$ env | grep -E '^(CLAUDE|CLAUDECODE)'
CLAUDECODE=1
CLAUDE_CODE_CHILD_SESSION=1
CLAUDE_CODE_ENTRYPOINT=sdk-ts
CLAUDE_CODE_EXECPATH=/home/danie906/.local/share/claude/versions/2.1.227
CLAUDE_CODE_MESSAGING_SOCKET=/mnt/wslg/runtime-dir/cc-socks/3059099.sock
CLAUDE_CODE_SESSION_ID=e84a3f94-e8f2-4cf1-9563-3da8f06549af
CLAUDE_PID=3059099
CLAUDE_PLUGIN_ROOT=.claude
```

`CLAUDE_PID` names a real, long-lived process:

```
$ tr '\0' ' ' < /proc/3059099/cmdline
/home/danie906/.local/share/claude/versions/2.1.227 --append-system-prompt … --mcp-config … --allow-dangerously-skip-permissions …
$ cat /proc/3059099/comm
2.1.227
```

### 1. Is `CLAUDE_PID` stable across separate Bash tool calls?

**Yes.** Each Bash tool call is a fresh `zsh` process; `CLAUDE_PID` was identical in all of
them, and each shell's own `$PPID` was that same value.

```
call#1 pid=3059099 sid=e84a3f94-… shell=2129847 ppid=3059099
call#2 pid=3059099 sid=e84a3f94-… shell=2131971
```

A **nested subagent** (a second Agent dispatch, two levels below the top-level session) was
asked to report the same values twice:

```
PID=3059099 SID=e84a3f94-e8f2-4cf1-9563-3da8f06549af CHILD=1 SOCK=/mnt/wslg/runtime-dir/cc-socks/3059099.sock SHELL_PPID=3059099 START=21633630
PID=3059099 SID=e84a3f94-e8f2-4cf1-9563-3da8f06549af CHILD=1 SOCK=… SHELL_PPID=3059099 START=21633630
```

Identical. **Subagents run in the same OS process as the top-level session.** See §"Subagent
vs top-level" for what this means for the contract.

A short-lived `conductor`-CLI-shaped process reads the *session's* identity, not its own:

```
$ python3 -c "import os; print(os.getpid(), os.environ['CLAUDE_PID'])"
2150052 3059099          # left: this process, useless.  right: the live session.
```

This is precisely the owner's constraint, satisfied.

### 2. Does it outlive an individual skill turn?

**Yes, by a wide margin.** Reconstructing the process start from `/proc/stat`'s `btime` plus
field 22:

```
process start: 2026-08-11T02:29:33
now:           2026-08-27T09:11:43
age:           16 days, 6:42:10
```

One PID has spanned this session's entire history — thousands of tool calls and every skill
turn in it.

### 3. Can an unrelated later process prove the identity live or dead?

**Yes.** `/proc/<pid>/stat` is world-readable and field 22 (`starttime`, ticks since boot) is
the PID-reuse defence. An observer was run under `setsid` — new session, new process group,
stdin/stdout severed, no relationship to the target — knowing only the identity string:

```
$ setsid /tmp/idprobe/observer.sh "claude:3059099:21633630" </dev/null
observer pid=2149354 sid=2149354  (unrelated to target)
identity='claude:3059099:21633630' -> host=claude pid=3059099 want_starttime=21633630
observed starttime=21633630
VERDICT: LIVE (pid+starttime match)

$ setsid /tmp/idprobe/observer.sh "claude:2134678:162234015" </dev/null
VERDICT: PROVABLY EXITED (no /proc entry)

$ setsid /tmp/idprobe/observer.sh "claude:3059099:99999999" </dev/null    # simulated PID reuse
observed starttime=21633630
VERDICT: PROVABLY EXITED (pid reused: starttime 21633630 != 99999999)
```

All three verdicts correct. `starttime` is readable, stable, and discriminating.

Permission model on this box:

```
-r--r--r--  /proc/3059099/stat      # world-readable — what liveness needs
-r--r--r--  /proc/3059099/cmdline
-r--------  /proc/3059099/environ   # owner-only — anything needing environ is same-uid only
proc /proc proc rw,nosuid,nodev,noexec,noatime 0 0      # no hidepid
```

### 4. What happens when the session exits?

**Provably dead, not merely absent.** Demonstrated on a disposable process:

```
victim pid=2134678 starttime=162234015
after kill: /proc/2134678 exists? NO
stat readable? awk: cannot open /proc/2134678/stat: No such file or directory
os.kill(pid,0) -> ProcessLookupError (PROVABLY EXITED)
```

Absence of `/proc/<pid>` plus a recorded `starttime` gives a positive exit proof rather than a
timeout. This is the sufficiency half of "expiry is necessary but never sufficient."

There is **no other artifact** that dies with a Claude session. Claude holds no advisory locks
at all:

```
$ awk '$5=="3059099"' /proc/locks | wc -l
0
```

and its only non-anonymous open file descriptors are `/dev/urandom` and its own `statm`. The
messaging socket looks promising but is not reliable: 7 live Claude CLI processes on this box,
2 socket files.

```
live claude version-binaries: 7
cc-socks files: 2      (118252.sock, 3059099.sock)
```

**The PID is the only durable Claude identity.**

### 5. Are these variables documented?

**Mostly not.** They are deliberate — the 304 MB binary contains them as literals:

```
$ grep -aoE 'CLAUDE_(PID|CODE_SESSION_ID|CODE_CHILD_SESSION|CODE_MESSAGING_SOCKET|CODE_ENTRYPOINT)' \
    /home/danie906/.local/share/claude/versions/2.1.227 | sort | uniq -c
     26 CLAUDE_CODE_CHILD_SESSION
     86 CLAUDE_CODE_ENTRYPOINT
     19 CLAUDE_CODE_MESSAGING_SOCKET
     21 CLAUDE_CODE_SESSION_ID
     15 CLAUDE_PID
```

But against `https://code.claude.com/docs/en/env-vars`:

| Variable | Documented |
|---|---|
| `CLAUDECODE` | **yes** — "Set to `1` in subprocesses Claude Code spawns" |
| `CLAUDE_CODE_CHILD_SESSION` | **yes** (prose, not in the table) |
| `CLAUDE_PID` | **no** |
| `CLAUDE_CODE_SESSION_ID` | **no** |
| `CLAUDE_CODE_MESSAGING_SOCKET` | **no** |

Corroborating that these are recent and informal: anthropics/claude-code issue #47018 is an
open request to *expose* a session id as an environment variable in tool execution context.

**Verdict: `CLAUDE_PID` is usable but undocumented.** It can disappear or change meaning in any
release with no deprecation. Mitigation is stated in §Risks.

### Correcting the `CLAUDE_CODE_CHILD_SESSION` hypothesis

The probe brief guessed `CLAUDE_CODE_CHILD_SESSION=1` might mean "this is a subagent, not the
top-level session." **It does not.** The documentation says verbatim:

> "To check whether the current process was spawned directly by a tool call or hook, rather
> than inside a stdio MCP server that Claude Code started, use `CLAUDE_CODE_CHILD_SESSION`
> instead"

It distinguishes *tool-call subprocess* from *MCP-server subprocess*. It says nothing about
agent nesting. The empirical result agrees: it was `1` at every depth tested.

### Subagent vs top-level

`CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID` are **identical** at the top level and at every
subagent depth — subagents are not separate OS processes and do not get separate session ids.

Consequences for the contract:

- A subagent **cannot** wrongly register a narrower identity than its session, and cannot
  wrongly fail to block. There is exactly one identity per session. This is the *right*
  granularity for "don't fire while a human has a session open."
- But two agents inside one session are indistinguishable. Re-entrancy must be handled by the
  *record* (the existing `acquire` already treats a matching `wrapper_identity` as not-busy),
  never by the identity.
- `CLAUDE_CODE_SESSION_ID` adds nothing over `CLAUDE_PID` for exclusion: it is the same
  1-per-session value, it has no liveness semantics, and its transcript file at
  `~/.claude/projects/<slug>/<session-id>.jsonl` persists long after the session exits. Record
  it as a diagnostic breadcrumb only.

### `$PPID` is not a substitute for `CLAUDE_PID`

In the interactive session, a Bash tool shell's `$PPID` happened to equal `CLAUDE_PID`. In
headless `claude -p` it does **not**:

```
HEADLESS: CLAUDE_PID=2153514
HEADLESS: shell_ppid=2157024        # different — an intermediate process
HEADLESS: SESSION_ID=98c8d91b-6f61-48c1-9a03-4b55962cb902
HEADLESS: CHILD=1
HEADLESS: pid_comm=claude start=162318974
HEADLESS: pid_cmdline=claude -p --model haiku --allowedTools Bash …
```

Two further facts from that run: headless `claude -p` **does** export `CLAUDE_PID` naming
itself, so a Conductor fire can register its own identity the same way; and its `comm` is
`claude`, whereas the interactive session's `comm` is `2.1.227`. See the next section.

---

## Part 2 — Why name matching cannot be resurrected in any form

This is not a style objection. Both directions of name matching are empirically broken *for
Claude specifically*.

**`pgrep -f 'claude'` over-matches catastrophically.** On this box, right now:

```
$ pgrep -f 'claude' | wc -l
22
```

Those 22 include six unrelated Claude sessions in other repositories, the Happy launcher
processes, and — the fatal case — Conductor's own shells. A synthetic driver-shaped process
was checked against the guard and the guard matched **the probe's own Bash tool shell**,
because every Claude Code Bash call sources a snapshot under `/home/danie906/.claude/`:

```
synthetic driver pid=2151309 argv: /usr/bin/zsh -c source /home/danie906/.claude/shell-snapshots/snapshot-zsh-…
pgrep -f 'claude' matches it? 1  (1 = SELF-MATCH)
```

Any process whose argv or working path mentions `~/.claude/…` — which is where this repo
lives — matches. The guard could never have worked here.

**`pgrep claude` (comm match) under-matches to zero.** The Claude Code binary is *named after
its version*, so `comm` and the `exe` basename are a version string:

```
pid=3059099 comm=2.1.227  exe_basename=2.1.227
pid=118252  comm=2.1.227  exe_basename=2.1.227
pid=1767245 comm=2.1.241  exe_basename=2.1.241
pid=3738709 comm=2.1.246  exe_basename=2.1.246
pid=3688089 comm=claude   exe_basename=2.1.241      # shim launch; comm and exe disagree
$ pgrep -x claude
3688089                    # one shim. Zero of the six real sessions.
```

### This breaks the currently planned `process_alive`

`docs/superpowers/plans/2026-08-10-plan-04-host-adapters.md` line 2230 specifies:

> "each adapter matches a live process by its own executable basename, which is `adapter.id`"

For Codex that predicate holds (`exe_basename=codex`). **For Claude it is false for every real
session.** `ClaudeAdapter.process_alive` implemented that way returns `False` for a live human
session — a **fail-open**: the driver concludes nobody is there and fires into an occupied
worktree. The bug is silent on one host and data-losing on the other, which is the worst
possible shape. It is also name matching, which is banned outright.

**Recommendation: delete the basename predicate from Plan 04 before implementation.**
`(pid, starttime)` needs no name predicate — `starttime` already answers "is this the same
process I recorded", which is the only question that matters. Asking "is it one of *my* host's
processes" is answered by the `host` field already in `OwnerRecord`, not by the process table.

---

## Part 3 — Codex

Observed: `codex-cli 0.147.0`, binary at
`…/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex`.

### The identity that exists

Two artifacts, both undocumented:

1. **`CODEX_THREAD_ID`** — a UUIDv7-shaped string exported into the environment of every shell
   command the agent runs. Equal to the "session id" printed in the `codex exec` banner.
2. **`$CODEX_HOME/thread-writer-locks/<CODEX_THREAD_ID>.lock`** — a zero-byte file on which the
   live Codex process holds an **exclusive `flock`** for the session's lifetime.

There is **no `CODEX_PID`**, and no PID is recorded anywhere in Codex's own state:

```
$ grep -roE '"pid"[^,]*' /tmp/idprobe/codexhome
(no output)
```

The rollout file's `session_meta` records `session_id`, `id`, `parent_thread_id`, `timestamp`,
`cwd`, `originator`, `cli_version`, `source`, `thread_source`, `model_provider` — no process id.

### 1. Is `CODEX_THREAD_ID` stable across separate tool calls?

**Yes.** A single `codex exec` was instructed to make three separate shell calls; the first and
third recorded:

```
---- INVOCATION … label=first ----
CODEX_THREAD_ID=01a043f7-4760-7783-89f3-6a3f78a5d398
self=2136479 ppid=2135801
---- INVOCATION … label=second ----
CODEX_THREAD_ID=01a043f7-4760-7783-89f3-6a3f78a5d398
self=2137067 ppid=2135801
```

Stable, and `$PPID` (the Codex binary) was stable too.

### 2. Does it outlive an individual turn?

**Yes.** The `flock` and the open rollout descriptor are held by the Codex binary for the whole
session, across turns.

### 3. Can an unrelated later process prove it live or dead?

**Yes, and better than on Claude.** A live session was started and observed from outside, with
no relationship to it:

```
=== external observer, no relationship to the codex session ===
lock files present:
-rw-r--r-- 1 danie906 danie906 0 Aug 27 09:09 01a043fc-1084-77e0-adb1-036af578324e.lock

file=…/thread-writer-locks/01a043fc-1084-77e0-adb1-036af578324e.lock inode=227807
   /proc/locks: 28: FLOCK  ADVISORY  WRITE 2142720 08:30:227807 0 EOF

thread id from the session banner:
session id: 01a043fc-1084-77e0-adb1-036af578324e
```

The chain is: `CODEX_THREAD_ID` → lock file path → `stat -c %i` → `/proc/locks` → **the live
PID**, with zero name matching and zero process-table scanning. From the PID,
`/proc/<pid>/stat` field 22 gives `starttime` as usual.

This holds for the operator's real, long-running **interactive TUI** sessions too — all six
lock files in `~/.codex/thread-writer-locks/` resolve to live Codex processes:

```
019feab3-05f0-7081-90fb-18b96bc27db3.lock  HELD by pid=1232793 starttime=12479017 comm=codex
019fff30-890c-7be2-96dc-8bf7bd3f7d4f.lock  HELD by pid=831492  starttime=46856038 comm=codex
019fff5c-905e-7883-8a9f-be11fe6d43b9.lock  HELD by pid=831492  starttime=46856038 comm=codex
019fff67-11ac-77c2-b14e-e770f1db66c3.lock  HELD by pid=887320  starttime=47213385 comm=codex
01a00e30-c09c-7371-bfa9-cd7e6bb7d4cf.lock  HELD by pid=512483  starttime=43240835 comm=codex
01a00e90-55c5-7ec0-9553-4a4597ba61da.lock  HELD by pid=3132039 starttime=72648742 comm=codex
```

Six locks, five distinct PIDs — those five are exactly the five live interactive Codex
processes on the box. Note **`pid=831492` holds two thread locks**: one Codex process can own
several threads. `thread_id → pid` is many-to-one, so ownership must never be keyed on the
Codex PID.

### 4. What happens when the session exits?

Two distinct, both-provable cases.

**Clean exit — the lock file is deleted.** After the observed session finished:

```
=== after exit: lock dir ===
total 0
```

**Abrupt kill — the file survives, but the kernel drops the lock.** Tested with `SIGKILL`, so
the process had no chance to clean up:

```
while live -> /proc/locks: 28: FLOCK  ADVISORY  WRITE 2158828 08:30:138858 0 EOF
holder pid=2158828 starttime=162334268
--- SIGKILL the codex process ---
lock file still on disk? YES
still in /proc/locks?     0        (kernel released it)
process alive?            no
```

So on Codex, "provably exited" has two positive forms: **lock file absent**, or **lock file
present but not in `/proc/locks`**. Both are kernel-backed facts, not timeouts. This is a
cleaner recovery story than Claude's, which has only the `/proc/<pid>` disappearance.

### 5. Documented?

**No.** `https://learn.chatgpt.com/docs/config-file/environment-variables` documents only what
Codex *reads* — `CODEX_HOME`, `CODEX_SQLITE_HOME`, `CODEX_NON_INTERACTIVE`, `CODEX_INSTALL_DIR`,
`CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `OPENAI_FEDERATION_RULE_ID`,
`OPENAI_IDENTITY_TOKEN_FILE`, `OPENAI_WORKLOAD_IDENTITY_CONTEXT`, `CODEX_CA_CERTIFICATE`,
`SSL_CERT_FILE`, `RUST_LOG` — and states it "does not list internal development variables, test
variables, or provider-specific secret names." `CODEX_THREAD_ID` is absent, and no exported
variable is documented at all.

It is deliberate but singular in the binary:

```
$ grep -aoE 'CODEX_[A-Z0-9_]{2,30}' …/bin/codex | sort | uniq -c | sort -rn | head -3
     64 CODEX_HOME
     21 CODEX_AUTHAPI_BASE_URL
      1 CODEX_THREAD_ID
```

`thread-writer-locks/` is a pure implementation detail with no documentation whatsoever, on a
`0.x` CLI. Treat both as more fragile than Claude's `CLAUDE_PID`.

### Codex answers the earlier "no host detection analogue" question differently

Prior work in this repo found no exported Codex analogue for **host detection**. That remains
true and is now sharper: a Codex session started from inside a Claude session inherits Claude's
variables wholesale. Directly observed in the environment of a shell inside `codex exec`:

```
CLAUDECODE=1
CLAUDE_CODE_CHILD_SESSION=1
CLAUDE_CODE_SESSION_ID=e84a3f94-e8f2-4cf1-9563-3da8f06549af
CLAUDE_PID=3059099
CODEX_THREAD_ID=01a043f5-721a-7ce1-acab-db7b456bd507
```

Both sets are present. **Host must never be inferred from environment-variable presence.** It
must come from the invoking skill's own host id. But **identity** is a different question, and
for identity Codex is well served.

---

## Part 4 — Verdict on symmetry

**A symmetric contract is possible. A symmetric identity value is not, and should not be
forced.**

| | Claude | Codex |
|---|---|---|
| Exported session handle | `CLAUDE_PID` (a PID) | `CODEX_THREAD_ID` (a UUID) |
| Exported PID | yes | **no** |
| Kernel-held liveness artifact | **none** | `flock` on `thread-writer-locks/<id>.lock` |
| Liveness proof | `/proc/<pid>` + `starttime` | lock file + `/proc/locks`; also `/proc/<pid>` + `starttime` |
| Exit proof on abrupt kill | `/proc` entry vanishes | lock file present, `/proc/locks` entry gone |
| Survives PID reuse | via `starttime` | via thread id (a PID is never the identity) |
| Documented | no | no |

The three verbs — **register**, **consult**, **prove-exited** — are identical on both hosts.
The identity *string* and the liveness *procedure* differ. That is exactly the split
`HostAdapter` exists for.

Forcing Claude's `"<host>:<pid>:<start-ticks>"` onto Codex would mean recording the Codex
binary's PID, which is many-to-one against threads and only obtainable via a fragile `$PPID`
walk. It would throw away the `flock` — the single strongest primitive found in this probe —
in exchange for uniformity. Do not do it.

### Recommended identity tuples

**Claude:**

```
claude:<CLAUDE_PID>:<starttime-ticks>:<boot_id>
```

- `CLAUDE_PID` from the environment of the registering process. Never `os.getpid()`.
- `starttime` = field 22 of `/proc/<pid>/stat`. Defeats PID reuse.
- `boot_id` = `/proc/sys/kernel/random/boot_id`. Ticks-since-boot are meaningless across a
  reboot; a differing boot id is itself a positive exit proof.

**Codex:**

```
codex:<CODEX_THREAD_ID>:<boot_id>
```

- The thread id is authoritative. Liveness = lock file exists **and** appears in `/proc/locks`.
- `<pid>:<starttime>` may be recorded as a derived, diagnostic cache — never as the identity.

Both should additionally carry, as **non-authoritative diagnostics** for the operator:
`CLAUDE_CODE_SESSION_ID` (Claude), the worktree path, and the registering process's own PID.

### Where it should live

**`conductor/hosts/claude.py` and `conductor/hosts/codex.py`, behind the existing
`HostAdapter` Protocol** — with shared `/proc` mechanics in a `conductor/hosts/proc.py`
(declared by Plan 04's design notes in `base.py`; **does not exist on this branch**). This is
the correct home and matches `base.py`'s own rule that sharing process-table mechanics is fine
while sharing host-specific construction is not.

It must **not** live in `conductor/core/ownership.py`. That module's job is the record and the
mutex; it must stay host-agnostic and consume the adapter.

### But the declared signature is wrong for this use

`conductor/hosts/base.py:191-192`:

```python
    def process_identity(self, pid: int) -> str: ...
    def process_alive(self, identity: str) -> bool: ...
```

`process_identity(pid)` is correct for the case it was designed for — the driver forks a host
process and knows the child's PID (Plan 04 line 2330 uses exactly that). It **cannot** serve
the interactive case:

- **Claude:** the caller has no useful PID to pass. It has `CLAUDE_PID` in its environment.
  Passing `os.getpid()` records the short-lived CLI — the exact failure the owner named.
- **Codex:** the identity is not derived from a PID at all.

A **second** Protocol member is needed, distinct from `process_identity`:

```python
    def session_identity(self, env: Mapping[str, str]) -> str | None: ...
```

returning `None` when the host's variable is absent (not running under that host, or the
undocumented variable was removed). Taking `env` as a parameter rather than reading
`os.environ` keeps it testable and keeps the "am I nested inside the other host" ambiguity
visible at the call site.

`process_alive(identity: str) -> bool` is the right shape *except* that `bool` cannot express
"I cannot tell" — and `ownership.identity_is_live` already, correctly, has three answers.
Plan 02 line 1177 anticipates this by wrapping the call in `except Exception`. Cleaner to make
the tri-state explicit rather than routing it through exceptions, but that is a design call
outside this probe's scope; flagging it, not deciding it.

---

## Part 5 — Does `ownership.py`'s record shape accommodate this?

`OwnerRecord` is `(run_key, host, tier, wrapper_identity, acquired_at)` with
`wrapper_identity: str`. **The shape is fine — the interpreter is not.**

### Blocking bug: `identity_is_live` cannot parse a structured identity

```python
def identity_is_live(wrapper_identity: str) -> bool | None:
    try:
        pid = int(wrapper_identity)
    except (TypeError, ValueError):
        return None
```

`int("claude:3059099:21633630")` raises → returns `None` → in `acquire`, `live is not False`
→ `OwnerBusy`. **The moment a structured identity is written, every acquire refuses forever,
including by the process that wrote it.** This is not a latent risk; it is a certainty on the
first write.

The fix is not to widen the parser here but to delegate:
`base.load(record.host).process_alive(record.wrapper_identity)`, keeping the existing tri-state
return, which is exactly right and should be preserved verbatim. That makes `identity_is_live`
take the record (it needs `host`), not a bare string.

### Fields that would be needed

No new field is *strictly* required — `host` + `wrapper_identity: str` can carry everything.
Two are strongly advisable:

1. **A scheme discriminator, or a `RECORD_SCHEMA_VERSION` bump with refusal on the old value.**
   Today a bare `"3059099"` (what `acquire` writes now, via `str(os.getpid())`) and a future
   `"claude:3059099:21633630"` are both just strings. A newer Conductor reading an older
   record would fall through to the `int()` path and answer liveness about a PID with **no
   reuse defence** — a false clear-to-proceed, the direction that loses work. `read()` already
   ignores unknown fields for forward compatibility; it needs a matching *backward* refusal.

2. **`boot_id`.** Without it, `starttime` is uninterpretable across a reboot. Cheapest place is
   inside the identity string, as proposed above, which avoids touching `_FIELDS` at all.

Diagnostics worth adding as separate fields (not load-bearing): `worktree`/`cwd`,
`session_id`, and the registering process's own PID.

### The docstring's known gap is now closable

The module docstring says PID reuse "is real and Plan 02 answers it with an exit proof; until
then a recycled PID makes `identity_is_live` answer 'live' for a holder that has exited… a
false REFUSAL, never a false clear-to-proceed."

That reasoning is correct for the *current* bare-PID scheme. The `starttime` field closes the
gap outright, so the interim asymmetry does not need to persist — but note the docstring's
safety argument **inverts** if a bare-PID record is ever read by code that assumes reuse
protection is present. Hence the schema bump above.

---

## Part 6 — Staleness recovery

### What proves it safe to clear

**Claude** — clear iff any one of these holds:

- `/proc/<pid>` does not exist → process gone.
- `/proc/<pid>/stat` field 22 ≠ recorded `starttime` → PID reused; the recorded process is gone.
- recorded `boot_id` ≠ `/proc/sys/kernel/random/boot_id` → machine rebooted; nothing survived.

Otherwise **never clear**, regardless of the record's age. A 16-day-old record naming a live
process is a correct refusal, not staleness.

**Codex** — clear iff:

- `$CODEX_HOME/thread-writer-locks/<thread_id>.lock` does not exist (clean exit), **or**
- it exists but its inode appears in no `/proc/locks` entry (killed; kernel released), **or**
- recorded `boot_id` differs.

Both hosts: these are positive exit proofs. A lease/expiry timer may be layered on top as a
*necessary* condition (Plan 02's job) but must never be *sufficient*.

### Uninterpretable records must stay fail-closed

`OwnerAmbiguous` cases — foreign-host identity, unloadable adapter, unknown identity scheme,
`hidepid` hiding the target, a differing PID namespace — must **not** auto-clear. They need an
explicit operator action that prints the full record and requires confirmation, e.g. a
`conductor run disown --run <key> --force`. The existing `read()` error message already models
this tone ("remove it only once you have confirmed no process is still working on run …");
what is missing is a supported command that does the removal, so operators do not learn to
`rm` state files by hand.

### The residual gap neither host closes

Exit proof answers "did the owner die." It does not answer "did the owner *finish*." A human
who registers ownership, wanders off, and leaves the session open blocks the driver
indefinitely — on **both** hosts, correctly by the letter of the contract and probably wrongly
by intent. This is the lease's job, not the identity's, and it is the one place a timer is
legitimately load-bearing. Flagging it because a probe that only reports "identity works" would
leave the impression that the problem is fully solved by identity alone. It is not.

---

## Part 7 — Risks

**R1 — `CLAUDE_PID` is undocumented.** Present as a literal 15 times in the binary, absent from
`code.claude.com/docs/en/env-vars`. Can be removed or repurposed in any release.
*Mitigation:* `session_identity(env)` returns `None` on absence; the interactive-worker skill
refuses to start with an explicit message naming the variable, rather than silently
registering something weaker. Add a contract test asserting the variable exists and names a
live process, so an upgrade breaks CI rather than production.

**R2 — `CODEX_THREAD_ID` and `thread-writer-locks/` are undocumented.** Worse than R1: the lock
directory is an internal implementation detail of a `0.x` CLI (0.147.0 here), documented
nowhere, and the docs page explicitly disclaims listing internal variables.
*Mitigation:* same shape as R1, plus pin a tested Codex version floor and treat a missing lock
directory as "cannot register" rather than "not running."

**R3 — PID reuse.** `pid_max = 4194304` on this box (not the 32768 default), so wrap is slow,
but a long-lived machine will eventually wrap. Fully defeated by `starttime` — proven by the
simulated-reuse test. **Only** defeated if `starttime` is actually recorded; a bare-PID record
has no defence, which is why R7 matters.

**R4 — Reboot.** `starttime` is ticks since boot. Post-reboot, a new process can occupy the
same PID with the same tick count. Low probability, catastrophic direction (false "live" →
permanent block; or with a bare PID, false "dead" → fire into occupied worktree).
*Mitigation:* record `boot_id`.

**R5 — PID namespaces / containers. NOT TESTED.** All observations here were in a single
namespace (`readlink /proc/self/ns/pid` → `pid:[4026532221]` for both observer and target). If
the driver ever runs in a different PID namespace from the session — `docker exec`, a devcontainer,
`systemd-nspawn`, or a WSL distro restart — `/proc/<pid>` refers to a different process or none.
*Mitigation:* record the pid-namespace inode alongside `boot_id` and treat a mismatch as
**ambiguous**, never as dead.

**R6 — `/proc` visibility.** `stat` is world-readable and `environ` is owner-only, so any
scheme needing `environ` is same-uid only. `hidepid` is not set here; under `hidepid=2` another
user's `/proc/<pid>` is invisible and "absent" would be misread as "exited" — a fail-open.
*Mitigation:* check the `/proc` mount options at registration; if `hidepid` is active, treat
absence as ambiguous. `ownership.identity_is_live` already handles the `PermissionError` case
correctly ("The process exists; it belongs to another user. Alive is the honest answer.").

**R7 — Mixed identity schemes.** A record written by today's `acquire` is a bare PID string.
Read by tomorrow's reuse-aware code, it silently loses the reuse defence.
*Mitigation:* bump `RECORD_SCHEMA_VERSION` and refuse rather than guess.

**R8 — Environment inheritance downward.** Proven: a Codex session launched from a Claude Bash
call sees `CLAUDECODE=1`, `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID` *and* `CODEX_THREAD_ID`.
A worker that decides its host from variable presence will register the wrong process — on
Claude that means recording a PID that is alive but is not the worker, blocking fires for as
long as the outer Claude session lives.
*Mitigation:* host comes from the invoking skill's own host id, never from env sniffing.

**R9 — Name matching in Plan 04.** Line 2230's "match a live process by its executable
basename, which is `adapter.id`" is false for every Claude session (basename is the version
string) and would fail **open**. Must be removed before implementation. See Part 2.

**R10 — One session, many worktrees and many runs.** Claude's identity is per-session-process,
not per-worktree. One session that `cd`s between worktrees presents one identity everywhere;
two runs driven from one session share one identity. Per-run record namespacing (already in
place via `run_key`) handles exclusion correctly, but the identity **alone** cannot answer "is
someone working in *this* checkout." Record the worktree path as a diagnostic so an operator
looking at a refusal can see where the owner actually is.

**R11 — Codex: one process, many threads.** Observed `pid=831492` holding two thread locks.
Never key ownership on the Codex PID; the thread id is the identity.

**R12 — Causes of wrongly blocking a legitimate fire**, consolidated:
`identity_is_live` failing to parse a structured identity (Part 5, certain, not hypothetical);
`hidepid` or a cross-uid target yielding `None`; a foreign-host identity with no loadable
adapter; a PID-namespace mismatch; and the legitimate-but-unbounded case of a human leaving a
session open (Part 6). Every one of these lands on `OwnerBusy`/`OwnerAmbiguous`, which is the
safe direction — but with no supported clear-the-record command, the operator's only recourse
is deleting state files by hand.

---

## Part 8 — What could NOT be established

Stated as unknown, not assumed.

1. **Whether the interactive Codex TUI exports `CODEX_THREAD_ID` to its tool shells.** Two
   attempts to drive `codex` under a pty (`script -qec`, then `script -qc` with fed stdin)
   failed to reach the prompt; the TUI initialised and was killed by timeout without running
   the probe. Confirmed only for `codex exec`. Indirect evidence is strong that the *lock*
   mechanism covers the TUI — all six locks in the operator's real `~/.codex` are held by
   interactive `codex --dangerously-bypass-approvals-and-sandbox` processes — but the env
   export is unconfirmed for the TUI. **This matters:** the owner's scenario is a human's
   interactive session. If the TUI does not export it, the Codex half of the contract has no
   entry point and the lock is unreachable from inside the session. **Verify before designing.**
2. **Whether `CLAUDE_PID` survives an in-place Claude Code auto-update.** If the CLI re-execs
   into a new version binary, the session id would plausibly persist while the PID would not,
   turning a live owner into a "provably exited" one — a fail-open. Not tested.
3. **Cross-PID-namespace behaviour** (R5). Not tested; no container available in this
   environment.
4. **Whether `thread-writer-locks` exists on Codex versions other than 0.147.0**, and whether
   the path or naming is stable. Only one version was available.
5. **Whether Codex ever reuses a `thread_id`.** The values are UUIDv7-shaped (time-ordered), so
   collision is implausible, but this is inference from format, not a tested property.
6. **`openai-curated-remote`.** The probe brief asked to confirm the operator's real Codex
   marketplaces still list exactly `openai-curated` and `openai-curated-remote`. Post-probe,
   `codex plugin marketplace list` reports **only** `openai-curated`. No baseline was captured
   before the probe began, so it cannot be confirmed whether `openai-curated-remote` was
   present beforehand. Nothing in this probe wrote to the real `CODEX_HOME` — see the integrity
   check below — so this is reported as an unverifiable discrepancy, not a change caused here.

---

## Environment integrity check

Every `codex` invocation that ran a model used `CODEX_HOME=/tmp/idprobe/codexhome`. The only
commands run against the real `~/.codex` were the read-only `codex plugin list --json` and
`codex plugin marketplace list`.

```
$ stat -c '%y %s' ~/.codex/auth.json
2026-08-25 13:44:27.557213982 -0700 4304          # predates this probe (2026-08-27 09:01)

$ md5sum ~/.codex/auth.json /tmp/idprobe/codexhome/auth.json
8b197c5e0675440dfd5e7649275e9fe1  /home/danie906/.codex/auth.json
8b197c5e0675440dfd5e7649275e9fe1  /tmp/idprobe/codexhome/auth.json   # byte-identical copy

~/.codex/config.toml   mtime 2026-08-14 00:28    (unchanged)
~/.codex/.tmp/         mtime 2026-07-04 11:27    (unchanged)

$ codex plugin marketplace list
MARKETPLACE     ROOT
openai-curated  /home/danie906/.codex/.tmp/plugins

$ codex plugin list --json | grep marketplaceName
  "marketplaceName": "openai-curated"      x4  (google-calendar, gmail, github, google-drive)
```

The scratch `CODEX_HOME` and its copy of `auth.json` are deleted at the end of this probe.

## Reference — files touched by this probe

- **Written:** this document only.
- **Read (product code, unmodified):** `conductor/hosts/base.py`,
  `conductor/core/ownership.py`, `conductor/hosts/codex.py`,
  `docs/superpowers/plans/2026-08-10-plan-04-host-adapters.md`,
  `docs/superpowers/plans/2026-08-10-plan-02-ownership-takeover.md`.
- **Scratch (deleted):** `/tmp/idprobe/`.

## Sources

- [Claude Code environment variables reference](https://code.claude.com/docs/en/env-vars)
- [Claude Code settings](https://code.claude.com/docs/en/settings)
- [anthropics/claude-code issue #47018 — expose session id as an environment variable](https://github.com/anthropics/claude-code/issues/47018)
- [Codex CLI environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables)
