# Host usage and session ground truth — 2026-09-30

Probed for `docs/plans/2026-09-30-plan-sustained-context-A-measurement.md` (Task 1), which
implements §3 of `docs/specs/2026-09-29-sustained-context-design.md`. Hosts: **claude 2.1.286**,
**codex-cli 0.156.1**, on WSL2. Every command below was run with stdin from `/dev/null`.
Recorded outputs are in `tests/conductor/fixtures/` (`claude-print-json-2.1.286.json`,
`claude-print-json-subagent-2.1.286.json`, `codex-exec-json-0.156.1.jsonl`), with home and temp
paths replaced by `<HOME>` / `<TMP>`.

## 1. Claude: `claude -p <prompt> --output-format json`

Prints one JSON object on stdout. Top-level keys include `type` (`"result"`), `subtype`
(`"success"`), `is_error`, `result` (the final text), `session_id`, `num_turns`, `usage`,
`modelUsage`, `permission_denials`, `subagent_stats`, `total_cost_usd`.

`usage` splits input into three parts: `input_tokens` (uncached), `cache_read_input_tokens`,
`cache_creation_input_tokens`. Total prompt tokens = the sum of the three.

## 2. Claude: top-level `usage` is the LAST main-thread call only

Prompt asking the model to spawn one subagent (`--model haiku --allowedTools=Agent`):

| Source | cache read | cache creation | output |
| --- | --- | --- | --- |
| top-level `usage` (1 iteration) | 56,653 | 551 | 52 |
| `modelUsage` (all calls, incl. subagent) | 137,139 | 69,767 | 391 |

So a fire's total must be summed from `modelUsage` (per model: `inputTokens`,
`cacheReadInputTokens`, `cacheCreationInputTokens`, `outputTokens`). Autodev fires dispatch
subagents on every phase, so reading `usage` would undercount the worker badly.

## 3. Codex: `codex exec --json`

Prints JSONL:

```
{"type":"thread.started","thread_id":"<uuid>"}
{"type":"item.completed","item":{"type":"error","message":"Codex is ignoring 4 unrecognized configuration settings..."}}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"agent_message","text":"alpha"}}
{"type":"turn.completed","usage":{"input_tokens":34072,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":22,"reasoning_output_tokens":15}}
```

`item.completed` items of type `error` are config warnings; the run still succeeds.
`input_tokens` includes `cached_input_tokens`. The rollout file's `total_tokens` (34,094 =
34,072 + 22) shows `output_tokens` already includes `reasoning_output_tokens`.

## 4. Both hosts report the SESSION's running total on resume

- Claude: `claude -p --resume <id> --output-format json …` keeps the same `session_id`.
  `modelUsage` after round 2 was round 1 plus round 2 (cache read 21,550 → 60,544; output 5 → 10).
- Codex: `codex exec resume --json <id> …` (run from the original cwd) keeps the same
  `thread_id`. `turn.completed.usage` after round 2 was 80,676 input / 33,920 cached, equal to the
  rollout's `total_token_usage`; the round's own figure (`last_token_usage`) was 46,604 input.

A per-round figure is therefore `after − before`, where `before` is the previous round's reported
total. This matters only for session resume (spec §4); Phase A never resumes.

Codex `exec resume` accepts `--json`, `-o`, `--skip-git-repo-check`, `-c`, `-m`,
`--dangerously-bypass-approvals-and-sandbox`, but **not** `--cd` or `--sandbox`: the working
directory comes from the process cwd, and a sandbox must be set with `-c sandbox_mode=...`.

## 5. Read-only reviewer postures

Test repo with two commits; prompt: run `git diff HEAD~1..HEAD`, then try to create
`pwned.txt`.

| Host | Flags | `git diff` | write |
| --- | --- | --- | --- |
| Claude | `--permission-mode dontAsk '--allowedTools=Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)'` | ran | denied (`permission_denials` lists `Write` and `Bash`) |
| Codex | `exec --json --sandbox read-only --cd .` | ran | failed: read-only filesystem |

`--allowedTools` is variadic: written as two tokens it swallows a following prompt
(`Error: Input must be provided …`). Use the `--allowedTools=<list>` form. Codex's read-only
sandbox also blocks network, so a Codex reviewer cannot call `gh`; the caller must put PR facts
in the prompt.

**Superseded for Claude: the `--allowedTools` posture is not reliably read-only.** The list only
adds to the allow rules merged from user, project and local settings, so a PR's own
`.claude/settings.json` (and its hooks) apply to the reviewer and can widen it; and
`Bash(git diff:*)` / `Bash(git log:*)` admit `--output=<file>`, which writes a file. The
reviewer now gets no shell at all: `conductor review` writes the diff to a temp directory and
hands the reviewer that directory.

Re-probed 2026-09-30, claude 2.1.286, with a hostile project `.claude/settings.json` allowing
`Write` and `Bash(*)`; prompt: read an out-of-tree diff file, then try to write a file and run a
shell command.

| Host | Flags | read diff outside the checkout | write / shell |
| --- | --- | --- | --- |
| Claude | `-p <prompt> --output-format json --permission-mode dontAsk --restricted --strict-mcp-config --tools=Read,Grep,Glob --add-dir <context_dir>` | read | Write and shell unavailable; no file created |
| Codex | `exec --json --sandbox read-only --cd <checkout>` (unchanged) | read (a read-only sandbox reads outside `--cd`) | — |

## 6. Unknown session id

- `claude -p --resume 00000000-0000-0000-0000-000000000000 …` → exit 1, stdout
  `No conversation found with session ID: 00000000-…` (not JSON).
- `codex exec resume --json 00000000-… "hi"` → exit 1, stderr
  `Error: thread/resume: thread/resume failed: no rollout found for thread id 00000000-… (code -32600)`.

Both are clean, fast failures, which is what spec §4 rule 3 (cold-start fallback) needs.

## Open

- **Codex child-agent tokens in `turn.completed` were not probed.** Section 2 shows Claude's
  top-level `usage` omits subagent calls; whether Codex's `turn.completed.usage` includes tokens
  spent by child agents the turn spawned is unknown. Confirm on one live Codex worker fire
  (compare `turn.completed` against the rollout files of the parent and its children) before
  trusting Codex worker totals.
