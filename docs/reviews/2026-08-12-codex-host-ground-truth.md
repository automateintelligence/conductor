# Codex CLI ground truth for the HostAdapter layer

**Date:** 2026-08-12
**Audience:** whoever writes Plan 04 of the dual-host design.

## Purpose

Plan 04 of the dual-host design specifies a `HostAdapter` layer but never names Codex's
actual CLI vocabulary. This document supplies it, verified against the installed binary
rather than against documentation, so the plan writer does not have to guess.

Referenced documents:

- Design: [`docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md`](../superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md)
- Roadmap: [`docs/superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md`](../superpowers/plans/2026-08-10-codex-dual-host-ROADMAP.md) — Plan 04 at lines 294-326

Everything in the sections below marked as verified was established by running the binary
on this machine on 2026-08-12. Everything not established is collected in
[Things NOT determined](#things-not-determined) at the end. Nothing here is inferred from
Codex's published documentation.

## Version

```
$ codex --version
codex-cli 0.147.0
```

Pin the plan's claims to this version. Codex's CLI surface has moved quickly; a flag list
without a version attached ages badly.

## Non-interactive invocation

```
codex exec [OPTIONS] [PROMPT]
```

- Alias: `codex e`
- Subcommands of `exec`: `resume`, `review`, `help`

Prompt delivery:

- Passed as a positional argument, **or**
- Read from stdin when the argument is omitted, or when the argument is `-`
- If stdin is piped **and** a prompt argument is given, stdin is appended as a `<stdin>` block

Claude equivalent: `claude -p "<prompt>"`.

## Flags that matter to a HostAdapter

All of the following come from `codex exec --help` on codex-cli 0.147.0.

### Model and configuration

| Flag | Meaning |
| --- | --- |
| `-m, --model <MODEL>` | Model selection |
| `-c, --config <key=value>` | Overrides `~/.codex/config.toml`. Dotted paths supported; the value is parsed as TOML. Help's own example: `-c model="o3"` |
| `-p, --profile <CONFIG_PROFILE_V2>` | Layers `$CODEX_HOME/<name>.config.toml` over the base config |

Note the collision: `-p` means *profile* in Codex and *print/non-interactive* in Claude. An
adapter that builds argv by string templating across hosts will get this wrong exactly once,
silently, and in a way that looks like a model-selection bug.

### Sandbox and approvals

| Flag | Meaning |
| --- | --- |
| `-s, --sandbox <SANDBOX_MODE>` | Values: `read-only`, `workspace-write`, `danger-full-access` |
| `--approve-for-me` | Routes approval requests through automatic review using the workspace-write sandbox |
| `--dangerously-bypass-approvals-and-sandbox` | Skip all confirmation prompts, no sandboxing |
| `--dangerously-bypass-hook-trust` | Run enabled hooks without persisted hook trust |

`--dangerously-bypass-approvals-and-sandbox` is the analogue of Claude's
`--dangerously-skip-permissions`. It is the flag a conductor worker needs, and it is the
flag whose presence the preflight should be able to report on.

Codex's sandbox is a graded axis (`read-only` / `workspace-write` / `danger-full-access`)
where Claude's permission posture is a mode plus a settings file. These do not map one to
one; the adapter has to define its own posture vocabulary and project it onto each host,
not pass a shared string through.

### Filesystem scope

| Flag | Meaning |
| --- | --- |
| `-C, --cd <DIR>` | Agent working root |
| `--add-dir <DIR>` | Additional writable directories |
| `--skip-git-repo-check` | Do not require the working root to be a git repo |

### Session and config isolation

| Flag | Meaning |
| --- | --- |
| `--ephemeral` | Do not persist session files |
| `--ignore-user-config` | Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME` |
| `--ignore-rules` | Do not load user or project execpolicy `.rules` files |

### Output

| Flag | Meaning |
| --- | --- |
| `--json` | Print events to stdout as JSONL |
| `-o, --output-last-message <FILE>` | Write the agent's last message to a file |
| `--output-schema <FILE>` | JSON Schema describing the model's final response shape |
| `--color <always\|never\|auto>` | Color control |

**This is the asymmetry the plan should exploit.** `--json`, `-o/--output-last-message`, and
`--output-schema` together give Codex a materially richer machine-readable result surface
than Claude's `-p` offers. An adapter should use `-o` to capture a worker's final result
rather than parsing stdout. Parsing stdout is the Claude-side compromise; it should not be
generalized into the shared interface just because it is what the Claude path does today.

Concretely, the `HostAdapter` result contract should be "the host writes its final message
somewhere the caller names," with the Claude implementation doing whatever stdout capture it
must, and the Codex implementation passing `-o`. Defining the contract the other way around —
"the adapter returns captured stdout" — throws away the better surface.

## Session resume — corrects the design

**Verified:** `codex exec resume` exists. Its help text: "Resume a previous session by id or
pick the most recent with `--last`." Top-level `codex resume` and `codex fork` also exist.

The design assumes resume is purely durable-state-based and host-agnostic, with no Codex
session-continuation semantics to consider. That assumption is false. Codex has native
session continuation.

The plan writer must decide this deliberately rather than inherit it. Two coherent positions:

1. **Ignore Codex sessions.** Continue to reconcile from durable state on every fire, as the
   Claude path does. Every fire is a cold start; the ledger and the gate are the only memory.
   This keeps one reconciliation model across both hosts.
2. **Use Codex sessions.** Cheaper context re-establishment on the Codex side, at the cost of
   two different resumption models and a class of bug where the two disagree.

Position 1 is probably right for a first cut, because conductor's whole correctness story is
that durable state is authoritative and the worker is disposable. But it should be written
down as an **explicit non-goal**, with the reason, not left unmentioned. An unstated
non-goal reads as an oversight to the next person, who will then "fix" it.

## Codex plugin system — new, not in the design

The design does not mention that Codex has a plugin system. It does.

```
codex plugin  {add, list, marketplace, remove}
codex plugin marketplace add <SOURCE>
```

`<SOURCE>` accepts "a local path, `owner/repo[@ref]`, HTTPS Git URL, or SSH Git URL".
Options: `--ref <REF>`, `--sparse <PATH>` (sparse checkout path, repeatable), `--json`.

Currently configured marketplace on this machine: `openai-curated`, rooted at
`/home/danie906/.codex/.tmp/plugins`.

### Layout, verified from that catalog

- **Catalog manifest:** `.agents/plugins/marketplace.json`
- **Its top-level keys:** `name`, `interface`, `plugins` (180 entries in this catalog)
- **A catalog entry:**

  ```json
  {
    "name": "linear",
    "source": { "source": "local", "path": "./plugins/linear" },
    "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
    "category": "Productivity"
  }
  ```

- **Per-plugin manifest:** `.codex-plugin/plugin.json`
- **A plugin directory** (the `supabase` plugin) contains: `.codex-plugin/plugin.json`,
  `skills/<name>/` (several), `assets/`, `README.md`, `LICENSE`, `CHANGELOG.md`, `.app.json`

### Claude vs Codex

| | Claude | Codex |
| --- | --- | --- |
| Plugin manifest path | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` |
| Catalog manifest path | `.claude-plugin/marketplace.json` † | `.agents/plugins/marketplace.json` |
| Skills location | `skills/<name>/SKILL.md` | `skills/<name>/` |
| Catalog entry shape | † not verified here | `{name, source: {source, path}, policy: {installation, authentication}, category}` |
| Marketplace add | `/plugin marketplace add <source>` | `codex plugin marketplace add <SOURCE>` |
| File scoping | none | `--sparse <PATH>`, repeatable |

Two differences carry weight:

- Codex's catalog entry has a **`policy` block** (`installation`, `authentication`) that
  Claude's has no counterpart for. Publishing conductor means deciding what those values are,
  not just transliterating the existing manifest.
- Codex has **`--sparse`** for scoping which files come across on a marketplace add. Claude's
  plugin format has no file-control mechanism at all. For a repo like conductor — which
  carries tests, docs, plans, and reviews alongside the skills — this is the difference
  between shipping a skill bundle and shipping the whole repository.

### Consequence, stated plainly

Publishing conductor to a Codex catalog requires two additions:

1. `.codex-plugin/plugin.json` in the **conductor repo**
2. `.agents/plugins/marketplace.json` in the **automateintelligence/marketplace repo**

Conductor and spec-craft are **not** currently present in `~/.codex/skills/`.

> **Warning — do not package before Plan 04 lands.**
> Packaging conductor for Codex *without* Plan 04's host adapter produces a plugin that
> installs cleanly and then tries to spawn `claude`. The failure is not at install time; it is
> at first fire, on a machine that may not have Claude installed at all.
>
> The two specific sites:
> - `conductor/resume_script.py:261` runs `"$CLAUDE_BIN" -p "/conductor:autodev"`
> - `conductor/resume_script.py:214` guards with `pgrep -f 'claude'` — so on a Codex host the
>   double-drive guard is not merely wrong, it never matches anything and therefore never
>   fires. Two Codex workers could drive the same run.
>
> Sequence the work so the adapter precedes the packaging.

## Skill invocation under Codex — resolved, and it is not a host primitive

**Verified** from `~/.codex/AGENTS.md:103-106`:

```
<invocation_conventions>
- `$name` — invoke a workflow skill or role keyword
- `/skills` — browse available skills
</invocation_conventions>
```

A dispatch table follows (header at `AGENTS.md:140-141`, entries from `:142` onward — the
table continues past the `:149` originally cited). Every entry expands the same way:

```
| "analyze", "investigate"          | `$analyze` | Read `./.codex/skills/analyze/SKILL.md`, run deep analysis        |
| "plan this", "plan the"           | `$plan`    | Read `./.codex/skills/plan/SKILL.md`, start planning workflow     |
```

The consequences, which are the part that matters to Plan 04:

1. **`$conductor:autodev` is not resolved by the Codex host.** Under Claude,
   `claude -p "/conductor:autodev"` is dispatched by the host itself. Under Codex, `$name` is
   literal prompt text that the *model* interprets by reading a convention table, and it
   expands to "read this `SKILL.md` path and execute it." The dispatch is a prompting
   convention, not a CLI feature.

2. **The convention is third-party and unowned.** It lives in `~/.codex/AGENTS.md`, which on
   this machine is oh-my-codex's file — not a documented Codex-native feature. (The installed
   skills corroborate this: `~/.codex/skills/code-review/SKILL.md` carries the description
   prefix `[OMX]`.) If conductor ships nothing that establishes the convention on a given
   machine, `$conductor:autodev` resolves to nothing at all.

3. **The design's shell-expansion warning understates the risk.** It is correct that
   `$conductor:*` must not be shell-expanded — expansion yields an empty string. But the
   deeper problem is that even *unexpanded*, the token has no guaranteed resolver. Fixing the
   quoting does not make the launch work; it only makes it fail differently.

4. **Recommendation** (a recommendation, not a decision — the plan writer owns it): the Codex
   adapter's launch method should emit an explicit instruction naming the `SKILL.md` path,
   which is exactly what every `AGENTS.md` dispatch entry expands to anyway, rather than
   emitting `$conductor:autodev` and depending on an unowned convention. This makes launch
   deterministic and removes the shell-expansion hazard entirely, since no `$` token survives.

5. **The dispatch table references project-local `./.codex/skills/`**, not only
   `~/.codex/skills/`. The conductor repo root has an empty, untracked `.codex/` directory —
   a reserved slot alongside the empty `.agents/`. Both are invisible to git today (see
   [Verification status](#verification-status)).

### Skill file format is compatible across hosts

**Verified** at `~/.codex/skills/code-review/SKILL.md`: Codex skills use the **same
`SKILL.md` format** as Claude, with `name` + `description` YAML frontmatter.

Separately, `~/.codex/prompts/*.md` use `description` + `argument-hint` frontmatter and are
the Codex analogue of Claude's slash commands.

This format compatibility means conductor's existing `skills/*/SKILL.md` files are **likely
reusable across both hosts with no rewrite**. Worth stating plainly, because it materially
reduces Plan 04's estimated size: the Codex work concentrates in launch, argv construction,
result capture, and packaging — not in porting skill content.

## Environment notes

### Hooks are not disabled by `--ignore-user-config`

`~/.codex/hooks.json` registers two events:

- `SessionStart`, matcher `startup|resume|clear`
- `PreToolUse`

Both fire oh-my-codex's `codex-native-hook.js`. This repo's own session notes record that
hook denying `Stop` repeatedly.

`--ignore-user-config` does **not** disable hooks — it only skips `config.toml`. The
scratch-`CODEX_HOME` workaround therefore remains necessary for a clean-environment fire.
Do not let the flag's name talk the plan out of the workaround.

### Codex help hangs without stdin redirection

`codex --help` and `codex exec --help` **hang** unless stdin is redirected from `/dev/null`.

```bash
codex exec --help </dev/null
```

Any automation that captures Codex help output — preflight version checks, capability
probes, CI assertions about the flag surface — must use `</dev/null`. Without it the symptom
is a hang, not an error, which in a cron-driven fire means a stuck worker rather than a
failed one.

## Things NOT determined

These were **not** established and must be resolved by the plan writer. They are listed as
open questions, not as things assumed to be fine.

1. **Native subagent primitive.** Whether Codex has one, or whether the adapter must spawn a
   fresh `codex exec` child process per subagent. This changes the cost model of the recipe
   step and the shape of the adapter's "spawn a worker" method.

2. **Codex `PreCompact` hook contract.** The design cites
   `https://learn.chatgpt.com/docs/hooks`. Not verified locally.

3. **Scheduled-task equivalent.** Whether Codex has anything analogous to
   `~/.claude/scheduled_tasks.json`. The design says both hosts move to OS cron; the *absence*
   of a Codex native scheduler was not confirmed, only not observed.

Skill invocation syntax was previously open and is now resolved — see
[Skill invocation under Codex](#skill-invocation-under-codex--resolved-and-it-is-not-a-host-primitive).

## Verification status

| Section | Status |
| --- | --- |
| Version | Verified — `codex --version`, 2026-08-12 |
| Non-interactive invocation | Verified — `codex exec --help` |
| Flags | Verified — `codex exec --help` |
| Session resume | Verified — subcommand and help text present |
| Skill invocation (`$name`) | Verified — `~/.codex/AGENTS.md:103-106`, dispatch table from `:142` |
| SKILL.md format compatibility | Verified — `~/.codex/skills/code-review/SKILL.md` frontmatter |
| `~/.codex/prompts/*.md` frontmatter | Verified — `description` + `argument-hint` |
| Plugin system, layout, catalog entry | Verified — installed `openai-curated` catalog |
| Absence from `~/.codex/skills/` | Verified by inspection |
| `resume_script.py` line references | Verified — see below |
| `.agents/` at conductor repo root | Verified — see below |
| Things NOT determined | Explicitly unverified |

Post-write verification performed on 2026-08-12:

- `conductor/resume_script.py:214` is `for pid in $(pgrep -f 'claude' 2>/dev/null); do` —
  as claimed.
- `conductor/resume_script.py:261` is
  `"$CLAUDE_BIN" -p "/conductor:autodev" "$@" >> "$LOG" 2>&1` — as claimed.
  Both lines live inside a bash template embedded in a Python string literal; the line
  numbers refer to the `.py` file.
- `.agents/` exists at the conductor repo root and contains no files. It is not tracked
  (`git ls-files .agents/` is empty). One nuance worth carrying into the plan: because git
  does not track empty directories, `git status --porcelain .agents/` is *also* empty — the
  directory is invisible to git rather than reported as untracked. It will not appear in any
  status output until a file is added to it.
- `.codex/` at the conductor repo root is likewise present, empty, and untracked, with the
  same git-invisibility caveat.
- Claude-side layout was re-checked in this repo: `.claude-plugin/plugin.json` and
  `skills/<name>/SKILL.md` both confirmed. The two cells marked † in the comparison table
  were **not** verified — this repo's `.claude-plugin/` holds only `plugin.json`, and the
  Claude catalog manifest lives in the separate marketplace repo, which was not inspected.
