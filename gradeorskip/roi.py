"""Expected value of grading vs. selling raw. Pure arithmetic, every step shown."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_fees(grader: str = "psa") -> dict:
    return json.loads((CONFIG_DIR / f"{grader}_fees.json").read_text())


@dataclass
class RoiInput:
    probs: dict[int, float]  # grade -> probability
    raw_price: float | None
    graded_prices: dict[int, float]  # grade -> price; only what's known
    grading_fee: float
    shipping: float
    selling_fee_pct: float = 0.0  # marketplace fees, applied to both raw and graded sales
    fill_missing_with_raw: bool = True  # value un-priced grades at the raw price (shown as an assumption)
    max_declared_value: float | None = None
    grader: str = "PSA"


@dataclass
class RoiResult:
    ok: bool
    verdict: str  # "Grade it" | "Marginal" | "Don't grade" | "Need prices"
    reason: str
    rows: list = field(default_factory=list)
    ev_graded_gross: float | None = None
    ev_graded_net: float | None = None
    raw_net: float | None = None
    cost: float | None = None
    gain: float | None = None
    p_loss: float | None = None
    assumptions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    # Graded sale price at which one graded copy merely matches selling raw (after fees).
    breakeven: float | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compute_roi(inp: RoiInput) -> RoiResult:
    if inp.raw_price is None or inp.raw_price <= 0:
        return RoiResult(False, "Need prices", "A raw (ungraded) price is needed to compare against.",
                         missing=["raw"])
    keep = 1.0 - inp.selling_fee_pct / 100.0
    cost = inp.grading_fee + inp.shipping
    breakeven = round((inp.raw_price * keep + cost) / keep, 2)
    rows, missing, assumptions = [], [], []
    ev = 0.0
    for grade in sorted(inp.probs, reverse=True):
        p = inp.probs[grade]
        if p <= 0:
            continue
        if grade in inp.graded_prices and inp.graded_prices[grade] is not None:
            price, source = inp.graded_prices[grade], "comp"
        elif inp.fill_missing_with_raw and p < 0.25:
            # A lower grade shouldn't be worth more than a higher one with a known comp.
            higher = [v for g, v in inp.graded_prices.items() if g > grade and v is not None]
            nearest = inp.graded_prices[min(g for g in inp.graded_prices if g > grade)] if higher else None
            if nearest is not None and nearest < inp.raw_price:
                price, source = nearest, f"assumed ≤ {inp.grader} {min(g for g in inp.graded_prices if g > grade)}"
            else:
                price, source = inp.raw_price, "assumed = raw"
        elif any(g > grade and v is not None for g, v in inp.graded_prices.items()):
            # Likely grade, no comp: it can't sell for more than the next grade up, so value it
            # at that ceiling. Only a "don't grade" answer survives this (checked below).
            up = min(g for g, v in inp.graded_prices.items() if g > grade and v is not None)
            price, source = inp.graded_prices[up], f"at most {inp.grader} {up}"
        else:
            missing.append(grade)
            continue
        contrib = p * price
        ev += contrib
        rows.append({"grade": grade, "p": round(p, 4), "price": round(price, 2),
                     "source": source, "contribution": round(contrib, 2)})
    if missing:
        likely = ", ".join(f"{inp.grader} {g}" for g in missing)
        return RoiResult(False, "Need prices",
                         f"Grading only pays if a graded copy at the grade you'll likely get ({likely}) sells for "
                         f"more than ${breakeven:,.2f}: the ${inp.raw_price:,.2f} raw price plus ${cost:,.2f} "
                         "in grading costs. Check recent sold prices, or enter them below for the full math.",
                         rows=rows, missing=[f"psa{g}" for g in missing], cost=round(cost, 2),
                         breakeven=breakeven)
    ceiling = [f"{inp.grader} {r['grade']}" for r in rows if r["source"].startswith("at most")]
    if any(r["source"] != "comp" and not r["source"].startswith("at most") for r in rows):
        assumed = [f"{inp.grader} {r['grade']}" for r in rows if r["source"] != "comp" and not r["source"].startswith("at most")]
        assumptions.append(f"No comps entered for {', '.join(assumed)}; valued at the lower of the raw price and "
                           "the next-higher grade's comp. Low grades often sell for less, so this may flatter grading.")
    if inp.selling_fee_pct:
        assumptions.append(f"Marketplace fees of {inp.selling_fee_pct:g}% deducted from both raw and graded sales.")

    ev_net = ev * keep - cost
    raw_net = inp.raw_price * keep
    gain = ev_net - raw_net
    breakeven = raw_net + cost
    p_loss = sum(r["p"] for r in rows if r["price"] * keep < breakeven)
    warnings = []
    top_price = max((r["price"] for r in rows), default=0)
    if inp.max_declared_value and top_price > inp.max_declared_value:
        warnings.append(f"The best-case graded value (${top_price:,.0f}) exceeds this tier's max declared value "
                        f"(${inp.max_declared_value:,.0f}). {inp.grader} may upcharge to a higher tier.")

    threshold = max(15.0, 0.25 * cost)
    if gain >= threshold:
        verdict = "Grade it"
        reason = f"Expected gain of ${gain:,.2f} after ${cost:,.2f} in grading costs."
    elif gain < 0:
        verdict = "Don't grade"
        reason = f"Grading loses ${-gain:,.2f} on average compared with selling raw."
    else:
        verdict = "Marginal"
        reason = (f"Expected gain of ${gain:,.2f} is small next to the ${cost:,.2f} cost and the uncertainty "
                  f"in the grade (threshold ${threshold:,.2f}).")
    if verdict == "Grade it" and p_loss > 0.5:
        # Positive on average, but only because of a long-shot top grade.
        verdict = "Marginal"
        reason = (f"Positive on average (+${gain:,.2f}), but the grade comes back too low to cover the "
                  f"${cost:,.2f} cost {p_loss:.0%} of the time. It only pays off with a top grade.")
    if ceiling:
        if verdict != "Don't grade":
            # The ceilings flatter grading, so a positive answer isn't proven.
            likely = ", ".join(ceiling)
            return RoiResult(False, "Need prices",
                             f"No sold price for {likely}, the grade you'll likely get. Valued at the next grade up "
                             f"(the most it could fetch), grading looks {'marginal' if gain >= 0 else 'unprofitable'}, "
                             f"so it depends on what {likely} really sells for: it needs more than "
                             f"${breakeven:,.2f} to pay off.",
                             rows=rows, missing=[f"psa{g.split()[-1]}" for g in ceiling], cost=round(cost, 2),
                             breakeven=breakeven)
        assumptions.append(f"No sold price for {', '.join(ceiling)}; valued at the next grade up, the most it could "
                           "sell for. Even so grading loses money, so the answer holds.")
    return RoiResult(True, verdict, reason, rows, round(ev, 2), round(ev_net, 2), round(raw_net, 2),
                     round(cost, 2), round(gain, 2), round(p_loss, 4), assumptions, warnings, [], breakeven)
