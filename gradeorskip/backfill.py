"""Put PriceCharting API prices on checks scanned before the key was set. Never touches prices
someone typed in ("entered by you").

    python -m gradeorskip.backfill            # preview: what would change
    python -m gradeorskip.backfill --apply    # write it

Graded prices are replaced unless typed in; an ungraded price is only filled where there's none
(a TCGplayer market price stays). A check whose card was never matched in a card database is
looked up by the name and number read off the card, and the API's same-card check still applies.
Calls are paced to one a second.
"""
from __future__ import annotations

import argparse

from . import db
from .providers.prices import PriceChartingProvider

TYPED = "entered by you"
SOURCE = "PriceCharting API"


def card_for(check: dict) -> dict | None:
    card = check.get("card")
    if card and card.get("name") and card.get("number"):
        return card
    seen = check["result"].get("identified") or {}
    if seen.get("name_en") and seen.get("number") and (seen.get("confidence") or 0) >= 0.8:
        return {"name": seen["name_en"], "number": str(seen["number"]).split("/")[0],
                "set": seen.get("set_name") or "", "language": seen.get("language") or "en"}
    return None


def backfill(apply: bool = False, log=print) -> dict:
    from . import app  # the default price sheet (tier, shipping, fees) for checks that never had one

    pc = PriceChartingProvider()
    if not pc.available():
        raise SystemExit("PRICECHARTING_API_KEY isn't set")
    counts = {"priced": 0, "no card": 0, "no match": 0, "typed": 0, "already": 0}
    for check in db.list_checks(limit=1_000_000):
        label = check.get("label") or check["id"]
        prices = check["prices"] or {}
        need_graded = prices.get("graded_source") != TYPED and (prices.get("graded_source") != SOURCE or not prices.get("graded"))
        need_raw = not prices.get("raw") and prices.get("raw_source") != TYPED
        if not need_graded and not need_raw:
            counts["typed" if prices.get("graded_source") == TYPED else "already"] += 1
            continue
        card = card_for(check)
        if not card:
            counts["no card"] += 1
            log(f"  skip {check['id']} {label}: card not identified")
            continue
        listing = pc.listing(card)
        if not listing:
            counts["no match"] += 1
            log(f"  skip {check['id']} {label}: PriceCharting has no listing for this exact card")
            continue
        if not prices:
            prices = app._default_prices(check.get("card"))
        before = (prices.get("raw"), dict(prices.get("graded") or {}))
        if need_graded and any(g in listing for g in (7, 8, 9, 10)):
            prices["graded"] = {str(g): v for g, v in listing.items() if g != "raw"}
            prices["graded_source"] = SOURCE
        if need_raw and listing.get("raw"):
            prices["raw"], prices["raw_source"] = listing["raw"], f"{SOURCE}, ungraded"
        if (prices.get("raw"), prices.get("graded")) == before:
            counts["already"] += 1
            continue
        counts["priced"] += 1
        log(f"  {'set ' if apply else 'would set'} {check['id']} {label}: raw {prices.get('raw')}, graded {prices.get('graded')}")
        if apply:
            db.update_check(check["id"], prices=prices)
    log(f"{'Done' if apply else 'Preview'}: {counts}")
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the prices (default: preview only)")
    backfill(ap.parse_args().apply)
