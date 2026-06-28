# E5 recovery — two tiers, for two different situations

The **primary driver** is Option 1: a local `/loop` firing `/autodev` in this machine's
session (validated E2E — it reached green unattended and self-stopped). "Recovery" just
answers: *if that driver stops, what restarts it?* There are two situations, and they need
different answers. **They are complementary, not two ways to build the same thing.**

Both restart paths re-invoke the **reconcile-first `/conductor`**, which reads the durable
state (pushed git + GitHub issues + handoff) and continues from the **last pushed point** —
so unpushed in-flight work is re-derived, not trusted from memory. That is why every
iteration commits + pushes.

---

## Tier B — your machine is still available (reboot, Claude crashed, terminal closed)

A local OS trigger restarts Claude and re-runs `/conductor`. **No cloud involved.**

```cron
# @reboot crontab
@reboot sleep 30 && cd /path/to/checkout && \
  claude -p "/conductor resume <spec>" --permission-mode bypassPermissions \
  --no-session-persistence </dev/null >> ~/conductor-resume.log 2>&1
```
(or a systemd user service / login agent running the same `claude -p` line.)

**Tested:** a fresh `claude -p` process re-ran reconcile-first `/conductor` and skipped
every already-done step — clean resume, no double-work. The launcher + reconcile-resume are
proven; the literal `@reboot`/systemd trigger is provided as a snippet, not reboot-tested.

If you only need recovery when your machine is on, **Tier B is the whole story** — no cloud.

---

## Tier A — your machine is off / unreachable, but you want work to keep going

A cloud `/schedule` watchdog fires on a cadence; each fire is a fresh **cloud** container
that runs `/conductor` (which can start an in-cloud `/loop`) — i.e. **Option 1, in the
cloud**. It clones the repo from GitHub and continues from the last pushed state.

```
/schedule every 6h "Ensure /conductor is progressing <spec>; done only when
`conductor assert run` exits 0. If the local loop died, take over (reconcile-first)."
```

**When you return and resume locally, two things keep it safe:**
1. **Correctness — the lease/claim (§7).** Local and cloud share one done-gate and one
   ledger. Whoever holds the fresh lease on a unit owns it; the other backs off. No
   double-work even if both run briefly.
2. **Cost — stand the cloud down.** To stop paying for the cloud once local is back, local
   resume should delete the `/schedule` routine (a one-line prompt confirmation is fine).

**NOT tested — design only.** Tier A rests on substrate that *is* validated (pushed git +
issues + handoff + reconcile-first `/conductor`), but the cloud spawn, the cloud agent's
access to the private repo + `gh` credentials, and the takeover itself would be validated
in **E7** (cross-session durability); the parallel-lease handling is **E8**. Neither is in
Stage 0.

---

## Security note
`--permission-mode bypassPermissions` lets an unattended worker run git/gh/edits without
prompts. In production prefer a **scoped tool allowlist** in `settings.json` over a blanket
bypass, and run in a trusted/sandboxed checkout.
