---
name: hotel-booking
description: Find or book a hotel by driving Chrome (chrome-devtools MCP) and comparing prices across the chain site, Chase Travel, the hotel's direct site, Kayak, and Booking.com, with loyalty/credit-card points valued deterministically via compare.py. Use whenever the user asks to find, compare, or book a hotel.
---

# Hotel-booking workflow

You operate the user's own Chrome through the **chrome-devtools** MCP tools
(`take_snapshot`, `navigate_page`, `click`, `fill`, `fill_form`, etc.).
Deterministic price math is done by the
self-contained `compare.py` in this skill's folder. Run it over Bash by its path —
`python3 .claude/skills/hotel-booking/compare.py <args>` when this repo is the project,
or the skill's absolute path if it's installed elsewhere (e.g.
`~/.claude/skills/hotel-booking/compare.py`). It writes state to `offers.json` in the
working directory. Never value points in your head.

## Step 0 — Verify user is logged in to relevant portals
Before proceeding use the  **chrome-devtools** MCP tool to visit the below sites:
1. hyatt.com
2. chase.com
3. kayak.com
4. booking.com
Verify that they are logged in to an account.
If they are not, stop and ask user to log in first.

## Step 1 — classify the request
**A) Broad search** — a city, or a zip code + radius, possibly with criteria
(traveler profile; "near a coffee shop with good latte art"):
1. Research the **top 3** hotels first. If a constraint needs live data (coffee shop
   with good latte art → find such shops, then hotels within 0.5 mi), use the browser
   / WebSearch to gather it. Otherwise weigh space, location/safety, kid-appropriate
   amenities, and value. Consider spawning a subagent for this research.
2. Prefer Hyatt hotels first.
3. Present the 3 with a one-line reason each, then **ask which to book**. Do not start
   price comparison until the user picks one.

**B) Specific** — a chain + city, or one named hotel: skip research, go straight to
price comparison.

Before booking, make sure you know **destination, check-in / check-out dates, and the
number and ages of guests**. Ask if any are missing.

## Step 2 — gather prices (after a hotel is chosen)
Run `python3 .claude/skills/hotel-booking/compare.py reset` first. Then find the price for the **same hotel and an
equivalent room type** from each reachable source, in this order, recording every one.

**Always check every source below, including for independent, boutique, and B&B
properties — not just chains.** Only the chain site (#1) is chain-specific and may not
apply. Chase Travel, Kayak, and Booking.com are Expedia-/aggregator-powered and routinely
list independent inns, often with points value, so they must be checked regardless of
brand. Do not skip a source on the assumption that "it only carries chain hotels." Only
skip a source after you've actually searched it and confirmed the hotel isn't listed —
and say so when reporting.

1. **Chain site** (e.g. Hyatt.com) — *only if the hotel belongs to a chain.* Log in;
   prefer booking with **points**; record `points` + `points_program` (e.g. `"hyatt"`).
2. **Chase Travel** (Sapphire Reserve) — **always check, chain or independent.** Log in;
   record cash and/or points (`points_program: "chase_ur"`).
3. The hotel's **direct** website (if not the chain site / not on Chase Travel).
4. **Kayak.com** — always check.
5. **Booking.com** — always check. It lists many room types at different prices; record
   the one that **matches** the room you're comparing, not just its cheapest row.

Record each with:
```
python3 .claude/skills/hotel-booking/compare.py add '{"site":"hyatt.com","hotel":"<name>","room_type":"Standard King","points":20000,"points_program":"hyatt"}'
python3 .claude/skills/hotel-booking/compare.py add '{"site":"booking.com","hotel":"<name>","room_type":"Standard King","cash_usd":295,"refundable":true}'
```

## Step 3 — compare and choose
```
python3 .claude/skills/hotel-booking/compare.py compare --room-type "Standard King"
```
It returns each offer's USD-equivalent (points valued per the table at the top of
`compare.py`, 1.5¢ default),
the cheapest, and a warning if room types differ. Always compare **like-for-like** —
pass `--room-type`. Pick the cheapest equivalent option.

## Step 4 — drive to checkout, then STOP
Open the winning site and fill everything up to — but **NOT including** — the final
payment/booking step.

### Hard rule — never finalize
NEVER click a button that completes a purchase, confirms a non-refundable booking, or
spends money/points ("Book", "Reserve", "Pay", "Confirm booking", "Complete
reservation", etc.). Instead stop and report: winning site, hotel, dates, room type,
total price (cash and/or points + USD-equivalent), cancellation policy, and the exact
button the **user** must click. Wait for explicit confirmation.

## Logins
If you cannot log into Hyatt, Chase, or any required site, **stop and ask the user to
log in in their browser**, then continue. Never guess or enter credentials.

## Browser hygiene
Take a snapshot before acting; act by `uid` from the latest snapshot; re-snapshot
after anything that changes the page. On captcha / unexpected page / ambiguity, stop
and ask.
