# second-brain

**Skills and a subagent that turn Claude Code or Codex into a disciplined second-brain wiki maintainer, shared by every brain repo instead of copied into each one.**

## The short version

- Ships three skills — `ingest`, `query`, `lint` — plus the `wiki-researcher` subagent.
- Packaged as a plugin whose **repo root is the plugin** (`"source": "."`), so `skills/` and `agents/` sit at the top level.
- One `skills/` directory serves **both** Claude Code and Codex, via parallel manifests. No mirroring.
- Brain repos enable it per project, so the skills never leak into unrelated code repos.
- [`second-brain-setup-instructions.md`](second-brain-setup-instructions.md) is the prompt that generates a brain's `CLAUDE.md` from scratch. It also checks the plugin is wired up and refuses to create local skill copies.
- To spin up a brain from nothing, see [Start a new second brain](#start-a-new-second-brain).

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

## Start a new second brain

Five steps. The plugin gives you the verbs — `ingest`, `query`, `lint`. Step 4 is
what makes it *your* brain, and it's the one that isn't a copy-paste.

### 1. Create the folder

```bash
mkdir -p ~/Documents/brains/my-brain && cd $_ && git init -q && mkdir -p raw wiki
```

Make it a git repo. Pages get moved and links get rewritten in bulk later, and git
is what makes that recoverable.

### 2. Install the plugin

Register the marketplace once per machine:

```bash
claude plugin marketplace add ~/Documents/WORK/repos/open-code/second-brain
```

Then, from inside the new brain:

```bash
claude plugin install second-brain@second-brain --scope project
```

Project scope means the skills load in this brain and nowhere else — they won't
clutter your code repos.

### 3. Make the settings portable

Step 2 writes only `enabledPlugins`, which leans on your user-level marketplace
registration. Add the marketplace to the brain's `.claude/settings.json` too, so a
fresh clone resolves it with no user-level setup:

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

### 4. Generate `CLAUDE.md`

Start Claude **inside the new brain folder** and give it:

> Read `~/Documents/WORK/repos/open-code/second-brain/second-brain-setup-instructions.md`
> and follow the "Your task" section at the end for this repo.

It will ask what the brain is *for* before writing anything. That question is
deliberate: the answer shapes the `index.md` groupings, which is the one thing that
can't be templated. Have an answer ready — roughly, what subjects this brain covers,
which people and organizations recur, and what you'll want to ask it later.

It writes `CLAUDE.md` and creates `wiki/index.md` and `wiki/log.md`.

### 5. Feed it

Put documents in `raw/` — articles, transcripts, PDFs, statements, notes — then:

```
/second-brain:ingest raw/<filename>
```

Commands are namespaced: `/second-brain:ingest`, `/second-brain:query`,
`/second-brain:lint`. Run `/second-brain:lint` every so often once the wiki has a
few dozen pages.

## Add it to an existing brain

Steps 2 and 3 only. The brain keeps its existing `CLAUDE.md`; the plugin replaces
whatever per-repo copies of the skills it had.

## Editing

The marketplace is registered as a `directory` source, so its install location **is** this working copy. Edit a `SKILL.md` and the next session picks it up — no reinstall, no cache to bust.

## What lives here versus in a brain

| Here | In the brain's `CLAUDE.md` |
|---|---|
| How to ingest, query, and lint | What the brain is *about* |
| Layout rules, link conventions, page shape | Domain rules, index groupings, sensitivity policy |

Anything true of every second brain belongs here. Anything true of one belongs there.
