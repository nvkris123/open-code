#!/usr/bin/env python3
"""Deterministic points valuation + price comparison for the hotel-booking skill.

Self-contained (pure stdlib) so it runs from anywhere by absolute path. Claude
drives the browser and calls this over Bash while shopping; state lives in
offers.json in the working directory so it survives across invocations.

  python compare.py reset
  python compare.py add '{"site":"hyatt.com","hotel":"Hyatt Place","room_type":"Standard King","points":20000,"points_program":"hyatt"}'
  python compare.py add '{"site":"booking.com","hotel":"Hyatt Place","room_type":"Standard King","cash_usd":295}'
  python compare.py compare --room-type "Standard King"
  python compare.py list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ─── Point valuations ────────────────────────────────────────────────────────
# Cents-per-point used to convert loyalty/credit-card points to a USD-equivalent.
# These are *your* chosen valuations, not market rates. EDIT FREELY.
DEFAULT_CENTS_PER_POINT = 1.5  # 1,000 points = $15
POINT_VALUES_CENTS: dict[str, float] = {
    "hyatt": 1.5,
    "chase_ur": 1.5,      # Chase Ultimate Rewards via Chase Travel (Sapphire Reserve)
    "amex_mr": 1.5,
    "capital_one": 1.5,
    "marriott": 0.7,
    "hilton": 0.5,
    "ihg": 0.5,
}
# Order to gather/compare prices in, per the workflow.
SITE_PRIORITY = ["chain", "chase_travel", "hotel_direct", "kayak", "booking"]


def cents_per_point(program: Optional[str]) -> float:
    if not program:
        return DEFAULT_CENTS_PER_POINT
    return POINT_VALUES_CENTS.get(
        program.strip().lower().replace(" ", "_"), DEFAULT_CENTS_PER_POINT
    )


def points_to_dollars(points: float, program: Optional[str] = None) -> float:
    return round(points * cents_per_point(program) / 100.0, 2)


# ─── Offers ──────────────────────────────────────────────────────────────────
def normalize_room_type(s: str) -> str:
    """Canonicalize a room/rate name for like-for-like comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _num(x) -> Optional[float]:
    if x is None or x == "":
        return None
    return float(str(x).replace(",", "").replace("$", "").strip())


@dataclass
class Offer:
    site: str
    hotel: str
    room_type: str
    cash_usd: Optional[float] = None
    points: Optional[float] = None
    points_program: Optional[str] = None
    refundable: Optional[bool] = None
    notes: str = ""

    @property
    def effective_usd(self) -> float:
        if self.cash_usd is not None:
            return self.cash_usd
        if self.points is not None:
            return points_to_dollars(self.points, self.points_program)
        return float("inf")

    def summary(self) -> dict:
        eff = self.effective_usd
        return {
            "site": self.site,
            "hotel": self.hotel,
            "room_type": self.room_type,
            "cash_usd": self.cash_usd,
            "points": self.points,
            "points_program": self.points_program,
            "effective_usd": None if eff == float("inf") else round(eff, 2),
            "refundable": self.refundable,
            "notes": self.notes,
        }


class OfferBook:
    def __init__(self):
        self.offers: list[Offer] = []

    def record(
        self,
        site: str,
        hotel: str,
        room_type: str,
        cash_usd=None,
        points=None,
        points_program: Optional[str] = None,
        refundable: Optional[bool] = None,
        notes: str = "",
    ) -> Offer:
        offer = Offer(
            site=site,
            hotel=hotel,
            room_type=room_type,
            cash_usd=_num(cash_usd),
            points=_num(points),
            points_program=points_program,
            refundable=refundable,
            notes=notes or "",
        )
        self.offers.append(offer)
        return offer

    def compare(self, room_type: Optional[str] = None) -> dict:
        pool = self.offers
        if room_type:
            key = normalize_room_type(room_type)
            pool = [o for o in pool if normalize_room_type(o.room_type) == key]

        ranked = sorted(pool, key=lambda o: o.effective_usd)
        room_types_seen = sorted(
            {normalize_room_type(o.room_type) for o in self.offers}
        )

        warning = None
        if not room_type and len(room_types_seen) > 1:
            warning = (
                "Offers span different room types: "
                + ", ".join(room_types_seen)
                + ". Pass --room-type to compare like-for-like, or confirm these "
                "rooms are equivalent before choosing."
            )

        return {
            "offers": [o.summary() for o in ranked],
            "cheapest": ranked[0].summary() if ranked else None,
            "room_types_seen": room_types_seen,
            "warning": warning,
        }


# ─── CLI ─────────────────────────────────────────────────────────────────────
STORE = Path("offers.json")
ALLOWED = {
    "site",
    "hotel",
    "room_type",
    "cash_usd",
    "points",
    "points_program",
    "refundable",
    "notes",
}
REQUIRED = {"site", "hotel", "room_type"}


def _load() -> list[dict]:
    return json.loads(STORE.read_text()) if STORE.exists() else []


def _save(rows: list[dict]) -> None:
    STORE.write_text(json.dumps(rows, indent=2))


def _book(rows: list[dict]) -> OfferBook:
    book = OfferBook()
    for row in rows:
        book.record(**{k: v for k, v in row.items() if k in ALLOWED})
    return book


def cmd_add(args) -> None:
    try:
        offer = json.loads(args.offer)
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON: {e}")
    missing = REQUIRED - offer.keys()
    if missing:
        sys.exit(f"missing required field(s): {', '.join(sorted(missing))}")
    unknown = offer.keys() - ALLOWED
    if unknown:
        sys.exit(f"unknown field(s): {', '.join(sorted(unknown))}")

    rows = _load()
    rows.append(offer)
    _save(rows)
    print(
        json.dumps(
            {"added": _book([offer]).offers[0].summary(), "total_offers": len(rows)},
            indent=2,
        )
    )


def cmd_compare(args) -> None:
    rows = _load()
    if not rows:
        sys.exit("no offers recorded yet — use: compare.py add '<json>'")
    print(json.dumps(_book(rows).compare(args.room_type), indent=2))


def cmd_list(args) -> None:
    print(json.dumps(_load(), indent=2))


def cmd_reset(args) -> None:
    STORE.unlink(missing_ok=True)
    print("cleared offers.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="record one offer (JSON)")
    p_add.add_argument("offer", help="offer as a JSON object")
    p_add.set_defaults(func=cmd_add)

    p_cmp = sub.add_parser("compare", help="rank offers by USD-equivalent")
    p_cmp.add_argument("--room-type", default=None, help="compare like-for-like")
    p_cmp.set_defaults(func=cmd_compare)

    sub.add_parser("list", help="dump recorded offers").set_defaults(func=cmd_list)
    sub.add_parser("reset", help="clear recorded offers").set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
