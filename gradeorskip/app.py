from __future__ import annotations

import hmac
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .config import BRAND, ROOT, SOURCE_URL, TAG_ENABLED
from .pipeline import CHECKS_DIR, run_check
from .providers.condition import get_provider
from .providers.prices import (CardLookup, PriceChartingProvider, comps_links, graded_prices, identify_card,
                               PC_SOURCE, priced_from_pricecharting, pretty_variant, pricecharting_for_card,
                               pricecharting_listing, scraping_enabled, TAG_NO_PRICES, tag_comps_for_card, tag_source,
                               variant_prices)
from .grading import GRADERS
from .pipeline import grade_for
from .roi import RoiInput, compute_roi, load_fees

app = FastAPI(title=BRAND)


@app.middleware("http")
async def access_gate(request: Request, call_next):
    """When GRADEORSKIP_ACCESS_KEY is set, requests arriving through a Cloudflare tunnel
    (they always carry a cf-ray header) need the key: once via ?key=..., then a cookie.
    Local and LAN requests never pass through Cloudflare, so they're unaffected."""
    key = os.environ.get("GRADEORSKIP_ACCESS_KEY")
    if not key or "cf-ray" not in request.headers or request.url.path == "/healthz":
        return await call_next(request)
    if hmac.compare_digest(request.query_params.get("key", ""), key):
        resp = RedirectResponse(request.url.path, status_code=303)
        resp.set_cookie("gos_key", key, max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
        return resp
    if hmac.compare_digest(request.cookies.get("gos_key", ""), key):
        return await call_next(request)
    return HTMLResponse("<h1>Grade or Skip</h1><p>This is a private preview. Open the link with the access key.</p>",
                        status_code=401)

app.mount("/static", StaticFiles(directory=ROOT / "gradeorskip" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "gradeorskip" / "templates")
lookup = CardLookup()
MAX_UPLOAD = 15 * 1024 * 1024
GRADES = [10, 9, 8, 7]


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


templates.env.filters["ts"] = _fmt_time
templates.env.globals["brand"] = BRAND
templates.env.globals["source_url"] = SOURCE_URL
templates.env.filters["isodates"] = lambda s: re.sub(r"(\d{4}-\d{2}-\d{2})T[\d:.]+Z?", r"\1", s or "")
# Full cents: a rounded value would come back from the form looking like a price the user typed.
templates.env.filters["amount"] = lambda v: "" if v is None else f"{v:.2f}".rstrip("0").rstrip(".")
templates.env.filters["day"] = lambda ts: time.strftime("%b %-d", time.localtime(ts))
# Cache-busting for /static: phones hold on to old CSS/JS otherwise.
templates.env.globals["asset_v"] = str(int(max(f.stat().st_mtime for f in (ROOT / "gradeorskip" / "static").iterdir())))
templates.env.filters["money"] = lambda v: "—" if v is None else f"{'−' if v < 0 else ''}${abs(v):,.2f}"
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
templates.env.filters["variant"] = lambda v: pretty_variant(v) if v else ""
templates.env.tests["search_any"] = lambda text, words: any(w in (text or "") for w in words)


def _status() -> dict:
    p = get_provider()
    return {
        "llm": f"{p.name} ({p.model})" if p else None,
        "pricecharting": PriceChartingProvider().available() or scraping_enabled(),
    }


async def _read(f: UploadFile | None) -> bytes | None:
    if f is None or not f.filename:
        return None
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large (15 MB max)")
    return data or None


def _default_prices(card: dict | None) -> dict:
    fees = load_fees()
    prices = {
        "raw": None, "raw_source": None, "graded": {}, "graded_source": None,
        "tier": _default_tier(fees)["id"], "shipping": fees["default_shipping_per_card"]["value"],
        "sell_fee": 0.0, "fee_override": None,
    }
    if card:
        prices["raw"] = card.get("raw_price")
        if card.get("raw_price"):
            prices["raw_source"] = f"{card['raw_source']}, {pretty_variant(card.get('raw_variant') or '')}, updated {str(card.get('raw_updated'))[:10]}"
        graded, src = graded_prices(card)
        prices["graded"] = {str(k): v for k, v in graded.items()}
        prices["graded_source"] = src
        if not prices["raw"]:
            # The card databases have no market price for some cards (new promos, Japanese):
            # take the ungraded price from the same PriceCharting listing.
            pc = pricecharting_for_card(card) if not card.get("pc_graded") else None
            if pc and pc["prices"].get("raw"):
                prices["raw"] = pc["prices"]["raw"]
                prices["raw_source"] = f"{PC_SOURCE}, ungraded: {pc['url']}"
    else:
        prices["graded_source"] = graded_prices(None)[1]
    return prices


def _default_tier(fees: dict) -> dict:
    """The cheapest service PSA is currently taking (what most people would pick)."""
    return min((t for t in fees["tiers"] if t.get("available", True)), key=lambda t: t["fee"])


def _grader(check: dict) -> dict:
    return GRADERS[(check["prices"] or {}).get("grader", "psa") if TAG_ENABLED else "psa"]


def _active(prices: dict) -> dict:
    """Comps and tier for the chosen grader. A TAG slab sells at its own price, so PSA comps aren't reused."""
    return _tag_prices(prices) if prices.get("grader") == "tag" else prices


def _tag_prices(prices: dict) -> dict:
    return prices.setdefault("tag", {"graded": {}, "graded_source": TAG_NO_PRICES,
                                     "tier": _default_tier(load_fees("tag"))["id"]})


def _fill_tag(card: dict | None, prices: dict) -> None:
    """Fill TAG comps from sold listings. Runs on switching to TAG, never on a page render, and the
    result is stored on the check. Typed prices stay, and a failed fetch keeps any comps already here."""
    tag = _tag_prices(prices)
    if not card or tag.get("graded_source") == "entered by you" or not scraping_enabled():
        return
    found = tag_comps_for_card(card)
    if found is None:
        if not tag.get("graded"):
            tag["graded_source"] = f"Couldn't reach PriceCharting just now. {TAG_NO_PRICES}"
        return
    comps = {g: c for g, c in found["comps"].items() if c["price"] is not None}
    tag.update(graded={str(g): c["price"] for g, c in comps.items()}, comps={str(g): c for g, c in comps.items()},
               graded_source=tag_source(found["comps"], found["url"]) or TAG_NO_PRICES)


def _roi(check: dict, tier_id: str | None = None) -> tuple[dict | None, dict]:
    """ROI for the check's chosen tier, or for `tier_id` (ignoring any fee override)."""
    grader = _grader(check)
    fees = load_fees(grader["id"])
    prices = check["prices"] or _default_prices(check["card"])
    active = _active(prices)
    tier = next((t for t in fees["tiers"] if t["id"] == (tier_id or active.get("tier")) and t.get("available", True)),
                None) or _default_tier(fees)
    grade = grade_for(check["result"], grader["id"])
    if not grade:
        return None, tier
    override = None if tier_id else prices.get("fee_override")
    fee = override if override is not None else tier["fee"]
    inp = RoiInput(
        probs={int(k): v for k, v in grade["probs"].items()},
        raw_price=prices.get("raw"),
        graded_prices={int(k): float(v) for k, v in (active.get("graded") or {}).items() if v not in (None, "")},
        grading_fee=float(fee), shipping=float(prices.get("shipping") or 0),
        selling_fee_pct=float(prices.get("sell_fee") or 0),
        max_declared_value=tier.get("max_declared_value"), grader=grader["name"],
    )
    r = compute_roi(inp).to_dict()
    r["fee"] = fee
    r["shipping"] = inp.shipping
    card = check.get("card") or {}
    if card.get("printing_unconfirmed") and r["verdict"] in ("Grade it", "Marginal"):
        # These prices are for a different printing, so a positive answer proves nothing.
        r.update(ok=False, verdict="Need prices",
                 reason=f"This card looks stamped ({card.get('stamp')}), and these prices aren't for that printing. "
                        "Pick the right printing above before deciding.")
    elif r["verdict"] == "Grade it" and len(card.get("pc_options") or []) > 1 and not card.get("printing_confirmed"):
        r["warnings"] = [*r.get("warnings", []), "Several printings share this number and prices differ. "
                         "Confirm the printing above before you pay for grading."]
    return r, tier


def _tier_options(check: dict, current: dict) -> list[dict]:
    """Every service the grader is taking, with what grading this card would gain or lose at its fee:
    the service is the biggest lever in the math, so it's shown up front."""
    opts = []
    for t in load_fees(_grader(check)["id"])["tiers"]:
        if not t.get("available", True):
            continue
        r, _ = _roi(check, t["id"])
        opts.append({"id": t["id"], "name": t["name"].split(" (")[0], "fee": t["fee"],
                     "current": t["id"] == current["id"],
                     "gain": r["gain"] if r and r.get("ok") else None,
                     "verdict": r["verdict"] if r else None})
    return opts


VERDICT_CLASS = {"Grade it": "v-grade", "Marginal": "v-marginal", "Don't grade": "v-dont"}


def _thumb(check: dict) -> str:
    """The user's own straightened scan when there is one, else what they uploaded, else the database image."""
    for name in ("front_card.jpg", "front_input.jpg"):
        if (CHECKS_DIR / check["id"] / name).exists():
            return f"/files/{check['id']}/{name}"
    return (check.get("card") or {}).get("image") or ""


def _display_label(check: dict) -> str:
    """Batch checks fall back to the upload's filename, which isn't a card name."""
    label = check.get("label") or ""
    return "Unidentified card" if not label or label.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")) else label


def _front_centering(check: dict) -> str | None:
    cen = ((check["result"].get("sides") or {}).get("front") or {}).get("centering")
    return f"{cen['lr_label']} · {cen['tb_label']}" if cen and cen.get("lr_label") else None


def _row(check: dict) -> dict:
    """One check as a row in a list."""
    roi, _ = _roi(check)
    verdict = roi["verdict"] if roi else None
    return {"id": check["id"], "label": _display_label(check), "verdict": verdict or "No answer",
            "vclass": VERDICT_CLASS.get(verdict, "v-need"), "image": _thumb(check), "batch_id": check.get("batch_id"), "centering": _front_centering(check),
            "gain": roi["gain"] if roi and roi.get("ok") else None, "created_at": check["created_at"],
            "grade": (grade_for(check["result"], _grader(check)["id"]) or {}).get("label")}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "status": _status(), "recent": [_row(c) for c in db.list_checks(5)]})


@app.post("/check")
async def create_check(front: UploadFile = File(...), back: UploadFile | None = File(None),
                       card_id: str = Form(""), label: str = Form(""), guided: str = Form(""),
                       capture_debug: str = Form(""), front_alt: list[UploadFile] | None = File(None),
                       back_alt: list[UploadFile] | None = File(None), tilt: UploadFile | None = File(None)):
    f, b = await _read(front), await _read(back)
    if not f:
        raise HTTPException(400, "A front photo is required")
    alts = {"front": [x for x in [await _read(u) for u in (front_alt or [])[:4]] if x],
            "back": [x for x in [await _read(u) for u in (back_alt or [])[:4]] if x] if b else []}
    result = run_check(f, b, guided={x for x in guided.split(",") if x in ("front", "back")}, alts=alts, tilt=await _read(tilt))
    if capture_debug:  # camera diagnostics from the phone, kept for tuning the capture flow
        try:
            result["capture_debug"] = json.loads(capture_debug[:50_000])
        except ValueError:
            pass
    card = None
    if card_id:
        card, _ = lookup.get(card_id)
    else:
        card = _auto_identify(result)
    db.save_check(result, card=card, prices=_default_prices(card), label=label or _card_label(card, result))
    return RedirectResponse(f"/check/{result['id']}", status_code=303)


def _auto_identify(result: dict) -> dict | None:
    """Attach the card the vision model read off the front, if it matches the database exactly."""
    if not result.get("identified"):
        return None
    guess = result["identified"]
    stamp = (guess.get("stamp") or "").strip()
    ident = identify_card(guess, lookup)
    unknown = ident["status"] in ("non_english", "unmatched")
    if unknown or stamp:
        # Japanese (and anything the card databases lack): try PriceCharting's listings. A stamped
        # card too: the databases price the plain printing (a Trick or Trade Mewtwo came back
        # as plain Brilliant Stars, and a wrong "Grade it").
        pc = priced_from_pricecharting(guess)
        if pc and (unknown or pc["stamp_matched"]):
            if ident["card"]:
                pc["image"] = ident["card"].get("image")
            lang = "Japanese " if pc.get("language") == "ja" else ""
            read = result["identified"].get("number") or "no number"
            ident = {**ident, "card": pc, "status": "matched",
                     "note": f"Read from the photo as {result['identified'].get('name_printed')} ({read}); "
                             f"priced from PriceCharting's {lang}listing: {pc['set']} #{pc['number']}. "
                             "Check it's the right card."}
    result["identification"] = {k: ident[k] for k in ("status", "note", "guess")}
    card = ident["card"]
    if card:
        card["auto"] = ident["status"]
        card["stamp"] = stamp
        if "pc_options" not in card:
            pc = pricecharting_for_card(card)
            card["pc_options"], card["pc_slug"] = (pc["options"], pc["slug"]) if pc else ([], None)
            card["stamp_matched"] = bool(pc and pc["stamp_matched"])
        card["printing_unconfirmed"] = bool(stamp) and not card["stamp_matched"]
    return card


def _card_label(card: dict | None, result: dict) -> str | None:
    if card:
        return f"{card['name']} · {card.get('set')} #{card.get('number')}"
    guess = (result.get("identification") or {}).get("guess") or {}
    if guess.get("name_printed") and float(guess.get("confidence") or 0) >= 0.4:
        return f"{guess['name_printed']} {guess.get('number') or ''}".strip()
    return None


@app.get("/check/{check_id}", response_class=HTMLResponse)
def show_check(request: Request, check_id: str, msg: str = ""):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    roi, tier = _roi(check)
    grader = _grader(check)
    check["prices"] = check["prices"] or _default_prices(check["card"])
    return templates.TemplateResponse(request, "check.html", {
        "c": check, "r": check["result"], "roi": roi, "tier": tier, "fees": load_fees(grader["id"]),
        "grader": grader, "graders": GRADERS.values() if TAG_ENABLED else [], "active": _active(check["prices"]),
        "grade": grade_for(check["result"], grader["id"]),
        "card_image": _thumb(check),
        "tier_opts": _tier_options(check, tier),
        "grades": GRADES, "status": _status(), "msg": msg,
        "variants": variant_prices(check["card"]),
        "current_variant": ((check["card"] or {}).get("raw_variant") or "").split(" (")[0],
        "comps": comps_links(check["card"], (check["result"].get("identification") or {}).get("guess"),
                             grader["name"]),
    })


@app.get("/api/check/{check_id}")
def api_check(check_id: str):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    roi, tier = _roi(check)
    return {**check, "roi": roi, "tier": tier}


def _num(v: str) -> float | None:
    v = (v or "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


@app.post("/check/{check_id}/prices")
async def set_prices(request: Request, check_id: str):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    form = await request.form()
    prices = check["prices"] or _default_prices(check["card"])
    raw = _num(form.get("raw", ""))
    if raw != prices.get("raw"):
        prices["raw_source"] = "entered by you" if raw is not None else None
    prices["raw"] = raw
    grader = _grader(check)["id"]
    active = _active(prices)
    graded = {}
    for g in GRADES:
        v = _num(form.get(f"{grader}{g}", ""))
        if v is not None:
            graded[str(g)] = v
    if graded != (active.get("graded") or {}):
        active["graded_source"] = "entered by you"
        active.pop("comps", None)
    active["graded"] = graded
    if any(t["id"] == form.get("tier") and t.get("available", True) for t in load_fees(grader)["tiers"]):
        active["tier"] = form["tier"]
    prices["shipping"] = _num(form.get("shipping", "")) or 0.0
    prices["sell_fee"] = _num(form.get("sell_fee", "")) or 0.0
    prices["fee_override"] = _num(form.get("fee_override", ""))
    db.update_check(check_id, prices=prices)
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/printing")
async def set_printing(check_id: str, slug: str = Form(...)):
    """Price the check as the printing the user says they have."""
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    card = check["card"] or {}
    option = next((o for o in card.get("pc_options") or [] if o["slug"] == slug), None)
    if not option:
        raise HTTPException(400, "Unknown printing")
    listing = pricecharting_listing(slug)
    if not listing:
        return RedirectResponse(f"/check/{check_id}?msg=printing#answer", status_code=303)
    graded = {g: v for g, v in listing["prices"].items() if g != "raw"}
    card.update(pc_slug=slug, printing=option["label"], printing_confirmed=True, printing_unconfirmed=False,
                raw_price=listing["prices"].get("raw"), raw_source=PC_SOURCE, raw_variant="ungraded",
                pc_graded=graded, pc_url=listing["url"], raw_updated=time.strftime("%Y-%m-%d"))
    prices = check["prices"] or _default_prices(card)
    prices.update(raw=card["raw_price"], raw_source=f"{PC_SOURCE}: {listing['url']}",
                  graded={str(g): v for g, v in graded.items()}, graded_source=f"{PC_SOURCE}: {listing['url']}")
    if prices.get("grader") == "tag" or "tag" in prices:  # those comps were for the old printing
        _fill_tag(card, prices)
    db.update_check(check_id, card=card, prices=prices)
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/tier")
async def set_tier(check_id: str, tier: str = Form(...)):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    if not any(t["id"] == tier and t.get("available", True) for t in load_fees(_grader(check)["id"])["tiers"]):
        raise HTTPException(400, "That service isn't available")
    prices = check["prices"] or _default_prices(check["card"])
    _active(prices)["tier"] = tier
    prices["fee_override"] = None
    db.update_check(check_id, prices=prices)
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/grader")
def set_grader(check_id: str, grader: str = Form(...)):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    if not TAG_ENABLED or grader not in GRADERS:
        raise HTTPException(400, "Unknown grader")
    prices = check["prices"] or _default_prices(check["card"])
    prices["grader"] = grader
    prices["fee_override"] = None
    if grader == "tag":
        _fill_tag(check["card"], prices)
    db.update_check(check_id, prices=prices)
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/reidentify")
async def reidentify(check_id: str):
    """Re-run card matching and price lookup on a stored check (no new model call)."""
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    result = check["result"]
    card = _auto_identify(result)
    with db.conn() as c:
        c.execute("UPDATE checks SET result = ? WHERE id = ?", (json.dumps(result), check_id))
    db.update_check(check_id, card=card or {}, prices=_default_prices(card))
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/variant")
async def set_variant(check_id: str, variant: str = Form("")):
    """Pick the printing (e.g. the model missed a 1st Edition stamp) and take that printing's price."""
    check = db.get_check(check_id)
    if not check or not check["card"]:
        raise HTTPException(404)
    card, prices = check["card"], check["prices"] or _default_prices(check["card"])
    by_key = dict(variant_prices(card))
    if variant in by_key:
        card["raw_variant"], card["raw_price"] = variant, by_key[variant]
        prices["raw"] = by_key[variant]
        prices["raw_source"] = (f"{card['raw_source']}, {pretty_variant(variant)} (picked by you), "
                                f"updated {str(card.get('raw_updated'))[:10]}")
        db.update_check(check_id, card=card, prices=prices)
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.post("/check/{check_id}/card")
async def set_card(check_id: str, card_id: str = Form("")):
    check = db.get_check(check_id)
    if not check:
        raise HTTPException(404)
    if not card_id:
        return RedirectResponse(f"/check/{check_id}?msg=pick#roi", status_code=303)
    card, errors = lookup.get(card_id)
    if not card:
        raise HTTPException(502, f"Card lookup failed: {'; '.join(errors)}")
    db.update_check(check_id, card=card, prices=_default_prices(card))
    return RedirectResponse(f"/check/{check_id}#answer", status_code=303)


@app.get("/api/cards/search")
def search_cards(q: str):
    if len(q.strip()) < 2:
        return {"cards": [], "errors": []}
    cards, errors = lookup.search(q.strip())
    return {"cards": [{k: c[k] for k in ("id", "name", "set", "number", "printed_total", "image", "raw_price",
                                          "raw_variant")} for c in cards], "errors": errors}


@app.get("/files/{check_id}/{name}")
def files(check_id: str, name: str):
    if not check_id.isalnum() or "/" in name or ".." in name or not name.endswith(".jpg"):
        raise HTTPException(404)
    path = CHECKS_DIR / check_id / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return templates.TemplateResponse(request, "history.html", {
        "checks": [_row(c) for c in db.list_checks(200)], "status": _status()})


# ------------------------------------------------------------------ batch

def _batch_score(check: dict) -> tuple:
    g = check["result"].get("grade")
    if not g:
        return (-1, -1, -1)
    p = {int(k): v for k, v in g["probs"].items()}
    return (p.get(10, 0), sum(v for k, v in p.items() if k >= 9), g["top"])


@app.get("/batch", response_class=HTMLResponse)
def batch_form(request: Request):
    return templates.TemplateResponse(request, "batch.html", {"status": _status(), "ranked": None})


@app.post("/batch")
async def batch_run(fronts: list[UploadFile] = File(...), backs: list[UploadFile] = File(default=[])):
    fronts = [f for f in fronts if f.filename]
    backs = [b for b in backs if b.filename]
    if not fronts or len(fronts) > 20:
        raise HTTPException(400, "Upload between 1 and 20 front photos")
    batch_id = uuid.uuid4().hex[:10]
    for i, f in enumerate(fronts):
        fb = await _read(f)
        bb = await _read(backs[i]) if i < len(backs) else None
        result = run_check(fb, bb)
        card = _auto_identify(result)
        db.save_check(result, card=card, prices=_default_prices(card), batch_id=batch_id,
                      label=_card_label(card, result) or f.filename)
    return RedirectResponse(f"/batch/{batch_id}", status_code=303)


@app.get("/batch/{batch_id}", response_class=HTMLResponse)
def batch_show(request: Request, batch_id: str):
    checks = db.list_checks(batch_id=batch_id)
    if not checks:
        raise HTTPException(404)
    ranked = sorted(checks, key=_batch_score, reverse=True)
    return templates.TemplateResponse(request, "batch.html", {
        "status": _status(), "ranked": [{**c, "row": _row(c)} for c in ranked], "batch_id": batch_id})


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, **_status()})
