# E5 recovery wrappers — the cross-session tiers

**Option 1 (in-session `/loop /autodev`) is the primary driver** — validated E2E: it
reached green unattended and self-stopped on the machine gate. The wrappers below restart
it after a crash/restart. Both re-invoke the **reconcile-first `/conductor`** (amendment B)
over the durable substrate (pushed git + GitHub issues + handoff), so they never
double-work — demonstrated: a fresh `claude -p` re-ran `/conductor` and skipped every
already-done step.

## Local restart (amendment C) — OS autostart

`@reboot` crontab line:
```cron
@reboot sleep 30 && cd /path/to/checkout && \
  claude -p "/conductor resume <spec>" --permission-mode bypassPermissions \
  --no-session-persistence </dev/null >> ~/conductor-resume.log 2>&1
```

systemd user unit `~/.config/systemd/user/conductor-resume.service`
(`systemctl --user enable --now conductor-resume.service`):
```ini
[Unit]
Description=Conductor local autostart (reconcile-first resume)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/checkout
ExecStart=/home/USER/.local/bin/claude -p "/conductor resume <spec>" --permission-mode bypassPermissions --no-session-persistence
StandardInput=null

[Install]
WantedBy=default.target
```

## Cross-session / multi-day (Option 2) — cloud `/schedule`

A cloud routine that ensures `/conductor` is progressing; each fire is a fresh container
that clones from GitHub and reconciles:
```
/schedule every 6h "Ensure /conductor is progressing <spec>; done only when
`conductor assert run` exits 0. If the loop/container died, resume it (reconcile-first)."
```
The cloud fire is the cloud counterpart of the local autostart above: same reconcile-first
entry point, same durable substrate. Not soak-tested here (multi-hour; would create a
persistent cloud agent), but the substrate + entry point it relies on are validated by
E1/E3/E5 and the local `claude -p` autostart.

## Security note
`--permission-mode bypassPermissions` lets an unattended worker run git/gh/edits without
prompts. In production prefer a **scoped tool allowlist** in `settings.json` over a blanket
bypass, and run in a trusted/sandboxed checkout.
