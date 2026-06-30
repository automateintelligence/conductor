# E5 recovery — where the always-on loop lives, and how it restarts

The **primary driver** is Option 1: a local `/loop` firing `/autodev` (validated E2E —
reached green unattended, self-stopped).

**Hard precondition — conductor only runs where its skill stack exists.** Conductor
*orchestrates* other skills (superpowers `/writing-plans`, `/subagent-driven-development`,
`/code-review`, `/receiving-code-review`, `/document-release`; spec-kit; `/codex`). Any host
that runs `/conductor` must have **(a)** that skill/plugin stack, **(b)** `gh` credentials
for the repo, **(c)** model access. This holds locally; it does **not** hold in Anthropic's
cloud (see Tier A).

Both restart paths re-invoke the **reconcile-first `/conductor`** over the durable substrate
(pushed git + issues + handoff), resuming from the **last pushed point**.

## Tier B — same machine, just restart the driver (reboot / crash / closed terminal)

OS autostart → `claude -p "/conductor:start <spec>" </dev/null` (reconcile-first, so it resumes):
```cron
@reboot sleep 30 && cd /path/to/checkout && \
  claude -p "/conductor:start <spec>" --permission-mode bypassPermissions \
  --no-session-persistence </dev/null >> ~/conductor-resume.log 2>&1
```
(or a systemd user service / login agent running the same line.)

**Tested:** a fresh `claude -p` re-ran reconcile-first `/conductor` and skipped every
already-done step — clean resume, no double-work. The OS trigger is a snippet, not
reboot-tested. Skills are present locally, so this just works.

## Durable "walk away for days" tier — an always-on host YOU control

To keep progress going while your laptop is off, run Option 1 + Tier B on an **always-on
host you own** — a home server, your own cloud VM, or the workstation/WSL left on —
provisioned once with the same skill stack + gh creds. It is the **same code path as
local**; nothing new to validate beyond per-host provisioning. **This is the recommended
durable tier**, not Anthropic cloud.

## Tier A — Anthropic cloud `/schedule` — BLOCKED on skills-in-cloud

A cloud `/schedule` fire is a fresh Anthropic-cloud container that would run `/conductor`.
**Problem (confirmed):** those containers do **not** have superpowers / spec-kit, and
`/codex` needs a CLI binary that isn't there — so cloud `/conductor` dies at the first
`/writing-plans` / `/subagent-driven-development` call. Vendoring markdown skills into the
repo might cover some, not the plugin machinery or the codex binary; whether the stack is
installable in Anthropic cloud at all is **unverified**. So Tier A is **feasibility-gated**
(amendment E), not a tier we can assume. *If* it is ever unblocked, overlap with a resumed
local session is bounded by the ledger lease (§7, E8) for correctness + an explicit
cloud-stop on local resume for cost.

## Security note
`--permission-mode bypassPermissions` lets an unattended worker run git/gh/edits without
prompts. Prefer a scoped tool allowlist in `settings.json` over a blanket bypass; run in a
trusted/sandboxed checkout.
