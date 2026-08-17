# Recorded host artefacts

Fixtures in this directory are **recorded from a real host binary**, not hand-written. A
hand-written dict can only express states its author already believed possible, which is exactly
how three findings hid at once: every previous `codex plugin list --json` fixture hardcoded
`enabled: true`, made `pluginId == name@openai-curated`, and aliased `source.path` to the
installed root. Production code that assumed all three could not be contradicted by any test.

## `codex-plugin-list-0.147.0.json`

Verbatim stdout of `codex plugin list --json` on **codex-cli 0.147.0**, with two literal paths
replaced by placeholders so the file is machine-independent:

* `__CODEX_HOME__` — the `$CODEX_HOME` of the sandbox it was recorded in
* `__FIXTURE_ROOT__` — the sandbox root holding the two marketplace source trees

It deliberately captures a **non-happy-path** install, in three ways at once:

| plugin | state | which assumption it breaks |
| --- | --- | --- |
| `superpowers@trusted-market` | enabled, uniquely named | the control — nothing broken |
| `spec-craft@trusted-market` | `"enabled": false` | "everything in `installed[]` is usable" |
| `conductor@trusted-market` + `conductor@evil-market` | one bare `name`, two `pluginId`s | "`name` identifies a plugin" |
| all four | `source.path` ≠ installed root | "`source.path` is the plugin root" |

`source.path` names the **marketplace source tree** the plugin was copied *from*; the installed
copy lives somewhere else entirely and **no field in this JSON names it**. Codex prints the real
one on install (`Installed plugin root: …`) and it is
`$CODEX_HOME/plugins/cache/<marketplaceName>/<name>/<version>` for every entry above —
which is what `conductor.hosts.codex.plugin_roots_from_json` derives and then checks on disk.

### How to re-record it

Everything below runs against a throwaway `$CODEX_HOME`; it never touches the operator's real
Codex config. `openai-curated` is a reserved marketplace name and cannot be added from a local
source, hence `trusted-market`.

```sh
W=$HOME/.cache/codex-fixture; rm -rf "$W"; export CODEX_HOME="$W/home"; mkdir -p "$CODEX_HOME"

# Two marketplaces. A marketplace root is a directory holding
# .agents/plugins/marketplace.json -> {"name": …, "plugins": [{"name", "source", "policy"}…]}.
# Each plugin is <root>/plugins/<name>/ with .codex-plugin/plugin.json and skills/<skill>/SKILL.md.
# `trusted-market` ships conductor 1.0.0, spec-craft 1.0.0, superpowers 2.3.1;
# `evil-market` ships a same-named conductor 1.0.9 carrying a copied `start` skill.

codex plugin marketplace add "$W/markets/trusted-market"
codex plugin marketplace add "$W/markets/evil-market"
codex plugin add conductor@trusted-market
codex plugin add conductor@evil-market
codex plugin add spec-craft@trusted-market
codex plugin add superpowers@trusted-market

# Disable one: there is no `codex plugin disable` on 0.147.0, and `-c plugins."x@y".enabled=false`
# does NOT take effect. The enabled bit lives in $CODEX_HOME/config.toml:
#     [plugins."spec-craft@trusted-market"]
#     enabled = false

codex plugin list --json </dev/null \
  | sed "s|$W/home|__CODEX_HOME__|g; s|$W|__FIXTURE_ROOT__|g" \
  > tests/conductor/fixtures/codex-plugin-list-0.147.0.json
```
