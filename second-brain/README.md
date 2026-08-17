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

Four steps. The plugin gives you the verbs — `ingest`, `query`, `lint`. Step 3 is
what makes it *your* brain, and it's the one that isn't a copy-paste.

### 1. Create the folder

```bash
mkdir -p ~/Documents/brains/my-brain && cd $_ && git init -q
```

That's all you make by hand. Step 3 creates `raw/`, `wiki/`, `index.md`, and
`log.md` for you.

Make it a git repo, though. Pages get moved and links get rewritten in bulk later,
and git is what makes that recoverable.

### 2. Install the plugin

From inside the new brain, point it at the marketplace:

```bash
claude plugin marketplace add ~/Documents/WORK/repos/open-code/second-brain --scope project
```

Then enable it:

```bash
claude plugin install second-brain@second-brain --scope project
```

`--scope project` on both is what matters. It writes `extraKnownMarketplaces` and
`enabledPlugins` into the brain's own `.claude/settings.json`, so the skills load in
this brain and nowhere else, and a fresh clone resolves the plugin with no
user-level setup. Nothing to hand-edit.

Check it worked — run this **from inside the brain**, since project scope reads as
disabled anywhere else:

```bash
claude plugin list
```

### 3. Generate `CLAUDE.md`

Start Claude **inside the new brain folder** and give it:

> Read `~/Documents/WORK/repos/open-code/second-brain/second-brain-setup-instructions.md`
> and follow the "Your task" section at the end for this repo.

It will ask what the brain is *for* before writing anything. That question is
deliberate: the answer shapes the `index.md` groupings, which is the one thing that
can't be templated. Have an answer ready — roughly, what subjects this brain covers,
which people and organizations recur, and what you'll want to ask it later.

It writes `CLAUDE.md`, then builds `raw/` and `wiki/` with `index.md` and `log.md`.

### 4. Feed it

Put documents in `raw/` — articles, transcripts, PDFs, statements, notes — then:

```
/second-brain:ingest raw/<filename>
```

Run `/lint` every so often once the wiki has a few dozen pages.

**On command names.** Plugin commands are always namespaced — `/second-brain:ingest`
— and Claude Code has no aliasing mechanism to shorten that. Step 3 therefore writes
three shims into the brain's `.claude/commands/`, giving you plain `/ingest`,
`/query`, and `/lint`. Each is three lines and holds only a pointer to the plugin
skill, never a procedure, so it can't drift the way a copied `SKILL.md` would. Both
forms work.

You can also just ask in plain English — "ingest raw/statement.pdf" — since the
skills are model-invoked and their descriptions match those phrasings.

## Add it to an existing brain

Step 2 only. The brain keeps its existing `CLAUDE.md`; the plugin replaces
whatever per-repo copies of the skills it had.

## Editing

The marketplace is registered as a `directory` source, so its install location **is** this working copy. Edit a `SKILL.md` and the next session picks it up — no reinstall, no cache to bust.

## What lives here versus in a brain

| Here | In the brain's `CLAUDE.md` |
|---|---|
| How to ingest, query, and lint | What the brain is *about* |
| Layout rules, link conventions, page shape | Domain rules, index groupings, sensitivity policy |

Anything true of every second brain belongs here. Anything true of one belongs there.
