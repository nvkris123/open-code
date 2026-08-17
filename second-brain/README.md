# second-brain

**Skills and a subagent that turn Claude Code or Codex into a disciplined second-brain wiki maintainer, shared by every brain repo instead of copied into each one.**

## The short version

- Ships three skills — `ingest`, `query`, `lint` — plus the `wiki-researcher` subagent.
- Packaged as a plugin whose **repo root is the plugin** (`"source": "."`), so `skills/` and `agents/` sit at the top level.
- One `skills/` directory serves **both** Claude Code and Codex, via parallel manifests. No mirroring.
- Brain repos enable it per project, so the skills never leak into unrelated code repos.
- [`second-brain-setup-instructions.md`](second-brain-setup-instructions.md) is the prompt that generates a brain's `CLAUDE.md` from scratch.

## Layout

```
.claude-plugin/marketplace.json   # catalog; lists this repo as one plugin
.claude-plugin/plugin.json        # Claude Code manifest
.codex-plugin/plugin.json         # Codex manifest, same skills/
.agents/plugins/marketplace.json  # Codex catalog
skills/{ingest,query,lint}/SKILL.md
agents/wiki-researcher.md
commands/{ingest,query,lint}.md
```

## Use it in a brain

```bash
claude plugin marketplace add ~/Documents/WORK/repos/open-code/second-brain
cd /path/to/your-brain
claude plugin install second-brain@second-brain --scope project
```

That writes `enabledPlugins` into the brain's `.claude/settings.json`. Add the marketplace there too so a fresh clone resolves it without user-level configuration:

```json
{
  "extraKnownMarketplaces": {
    "second-brain": {
      "source": {
        "source": "directory",
        "path": "/Users/vinay/Documents/WORK/repos/open-code/second-brain"
      }
    }
  },
  "enabledPlugins": { "second-brain@second-brain": true }
}
```

Commands are namespaced: `/second-brain:ingest`, `/second-brain:query`, `/second-brain:lint`.

## Editing

The marketplace is registered as a `directory` source, so its install location **is** this working copy. Edit a `SKILL.md` and the next session picks it up — no reinstall, no cache to bust.

## What lives here versus in a brain

| Here | In the brain's `CLAUDE.md` |
|---|---|
| How to ingest, query, and lint | What the brain is *about* |
| Layout rules, link conventions, page shape | Domain rules, index groupings, sensitivity policy |

Anything true of every second brain belongs here. Anything true of one belongs there.
