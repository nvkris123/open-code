# Hotel-booking agent

Find and book hotels by **driving a real Chrome browser**, comparing prices across
the chain site, Chase Travel, the hotel's direct site, Kayak, and Booking.com — with
loyalty/credit-card points valued on a common USD basis. It does everything up to the
final checkout step, then **stops and hands the browser back to you** to click
"Pay / Confirm" yourself. It never spends money or points on its own.

It runs entirely on your Claude Pro/Max subscription — **Claude Code is the agent**, so
there's no Anthropic API key, no per-token billing, and no Python dependencies. A
project skill encodes the workflow, the `chrome-devtools` MCP server drives Chrome, and
`compare.py` does the deterministic points math (called over Bash).

```
┌──────────────┐  MCP  ┌──────────────────────┐  CDP  ┌────────┐
│  Claude Code │ ────▶ │ chrome-devtools-mcp  │ ────▶ │ Chrome │
│ (your plan)  │       └──────────────────────┘       └────────┘
│   + skill    │  Bash  ┌──────────────┐
│              │ ─────▶ │  compare.py  │  points → USD, like-for-like compare
└──────────────┘        └──────────────┘
```

## Setup
Requirements: Claude Code logged into your Pro/Max plan, Node.js (for `npx`), Python 3.9+.

The `chrome-devtools` browser server is configured **globally at user scope** (in
`~/.claude.json`), so it's available in every project. If it's not set up yet:

```bash
claude mcp add chrome-devtools --scope user -- npx -y chrome-devtools-mcp@latest --autoConnect
```

Then:

```bash
cd hotel-agent
unset ANTHROPIC_API_KEY     # IMPORTANT: if set, Claude Code bills the API instead of your plan
claude                      # start Claude Code in this directory
```

On first use, Claude Code asks to approve the `chrome-devtools` server. Approve it. It
launches a Chrome window with a persistent profile — log into Google / Hyatt / Chase
there once; the session persists.

## Use
Just ask, e.g.:

> Find a hotel in Chicago for a family of 4, kids 6 and 8, June 12–15.

The `hotel-booking` skill (in `.claude/skills/`) drives the flow: research top 3 →
you pick → gather prices from each site → `compare.py` picks the cheapest like-for-like
room → fill to checkout → stop for you to finalize.

### compare.py (used by the skill, handy standalone too)
Lives in the skill folder; run from the project root:
```bash
S=.claude/skills/hotel-booking/compare.py
python "$S" reset
python "$S" add '{"site":"hyatt.com","hotel":"Hyatt Place","room_type":"Standard King","points":20000,"points_program":"hyatt"}'
python "$S" add '{"site":"booking.com","hotel":"Hyatt Place","room_type":"Standard King","cash_usd":295}'
python "$S" compare --room-type "Standard King"   # ranks by USD-equivalent
```

### Safety
The "never finalize" rule is enforced by the **skill prompt**, not code (Claude Code is
the host, so there's no in-process click interceptor). For a hard gate, run Claude Code
so it **prompts you to approve each `chrome-devtools` tool call** — then you eyeball
every click. Always review the agent's final summary before clicking Pay yourself.

## Files

The skill is a self-contained folder (`.claude/skills/hotel-booking/`) — `SKILL.md`
plus one stdlib script:

| File | Role |
|------|------|
| `.claude/skills/hotel-booking/SKILL.md` | The workflow skill (request types, site order, points rule, never-finalize). |
| `.claude/skills/hotel-booking/compare.py` | Self-contained CLI: record offers + compare; deterministic points→USD. Holds the point-value table. |

The `chrome-devtools` browser server is **not** in a project `.mcp.json` — it's
configured globally at user scope (see Setup).

## Point valuations

The `POINT_VALUES_CENTS` table at the top of `compare.py` holds *your* chosen
cents-per-point (1.5¢ default; Hyatt/Chase UR 1.5¢, Marriott 0.7¢, Hilton/IHG 0.5¢).
These are placeholders — edit them to your real valuations.
