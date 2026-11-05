"""Card lookup and market prices.

- Card search + raw (TCGplayer) prices: pokemontcg.io, falling back to TCGdex
  when pokemontcg.io is down (it returned 503s intermittently during development).
- Graded prices: PriceCharting, only if PRICECHARTING_API_KEY is set (paid).
  Otherwise the user enters graded comps by hand. Nothing is ever estimated.
- TAG graded prices: medians of the TAG sold listings on the PriceCharting card page (POC).
"""
from __future__ import annotations

import datetime
import hashlib
import html as htmllib
import json
import os
import re
import statistics
import threading
import time
from urllib.parse import quote

import httpx

from ..config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache" / "http"
TTL = 6 * 3600
UA = {"User-Agent": "GradeOrSkip/1.0 (+https://github.com/0x4rch/gradeorskip)"}


def _get_json(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 12.0):
    key = hashlib.sha256((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < TTL:
        return json.loads(path.read_text())
    r = httpx.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


# ------------------------------------------------------------- raw prices

def _pick_variant(prices: dict, key: str) -> tuple[str | None, float | None]:
    """Choose the most relevant TCGplayer variant's market price."""
    order = ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil", "unlimitedHolofoil", "1stEditionNormal"]
    for v in order + list(prices):
        p = prices.get(v)
        if isinstance(p, dict) and p.get(key):
            return v, float(p[key])
    return None, None


class PokemonTCGProvider:
    name = "pokemontcg.io"
    base = "https://api.pokemontcg.io/v2"

    def _headers(self):
        k = os.environ.get("POKEMONTCG_API_KEY")
        return {"X-Api-Key": k} if k else {}

    def search(self, q: str, limit: int = 12) -> list[dict]:
        terms = []
        for tok in q.split():
            if tok.replace("/", "").isdigit():
                terms.append(f'number:{tok.split("/")[0].lstrip("0") or "0"}')
            else:
                tok = "".join(ch for ch in tok if ch.isalnum() or ch in "-'")
                if tok:
                    terms.append(f"name:{tok}*")
        data = _get_json(f"{self.base}/cards", {"q": " ".join(terms), "pageSize": limit,
                                                "orderBy": "-set.releaseDate"}, self._headers(), timeout=8)
        return [self._norm(c) for c in data.get("data", [])]

    def get(self, card_id: str) -> dict:
        return self._norm(_get_json(f"{self.base}/cards/{card_id}", None, self._headers(), timeout=8)["data"])

    def _norm(self, c: dict) -> dict:
        tp = c.get("tcgplayer") or {}
        variant, market = _pick_variant(tp.get("prices") or {}, "market")
        return {
            "id": c["id"], "name": c["name"], "set": c.get("set", {}).get("name"),
            "number": c.get("number"), "printed_total": c.get("set", {}).get("printedTotal"),
            "rarity": c.get("rarity"), "image": (c.get("images") or {}).get("small"),
            "raw_price": market, "raw_variant": variant, "raw_source": "TCGplayer market (via pokemontcg.io)",
            "raw_updated": tp.get("updatedAt"), "raw_url": tp.get("url"),
            "all_prices": tp.get("prices") or {},
        }


class TCGdexProvider:
    name = "tcgdex"
    base = "https://api.tcgdex.net/v2/en"

    def search(self, q: str, limit: int = 12) -> list[dict]:
        name = " ".join(t for t in q.split() if not t.replace("/", "").isdigit())
        number = next((t.split("/")[0].lstrip("0") for t in q.split() if t.replace("/", "").isdigit()), None)
        params = {"name": name, "pagination:itemsPerPage": 50}
        if number:
            params["localId"] = number
        rows = _get_json(f"{self.base}/cards", params)
        if number:
            rows = [r for r in rows if str(r.get("localId", "")).lstrip("0") == number]
        out = []
        for r in rows[:limit]:
            try:
                out.append(self.get(r["id"]))
            except httpx.HTTPError:
                continue
        return out

    def get(self, card_id: str) -> dict:
        card_id = card_id.removeprefix("tcgdex:")
        c = _get_json(f"{self.base}/cards/{card_id}")
        tp = (c.get("pricing") or {}).get("tcgplayer") or {}
        prices = {k: v for k, v in tp.items() if isinstance(v, dict)}
        variant, market = _pick_variant(prices, "marketPrice")
        img = c.get("image")
        return {
            "id": "tcgdex:" + c["id"], "name": c["name"], "set": (c.get("set") or {}).get("name"),
            "number": c.get("localId"), "printed_total": ((c.get("set") or {}).get("cardCount") or {}).get("official"),
            "rarity": c.get("rarity"), "image": f"{img}/low.webp" if img else None,
            "raw_price": market, "raw_variant": variant, "raw_source": "TCGplayer market (via TCGdex)",
            "raw_updated": tp.get("updated"), "raw_url": None, "all_prices": prices,
        }


class CardLookup:
    """pokemontcg.io first, TCGdex if it fails."""

    COOLDOWN = 600  # seconds to skip a source after it fails, so a down API doesn't cost a timeout per lookup

    def __init__(self):
        self.providers = [PokemonTCGProvider(), TCGdexProvider()]
        self._down_until: dict[str, float] = {}

    def _live(self, providers):
        now = time.time()
        live = [p for p in providers if self._down_until.get(p.name, 0) <= now]
        return live or providers[-1:]

    def _failed(self, p, errors, e):
        errors.append(f"{p.name}: {type(e).__name__}")
        self._down_until[p.name] = time.time() + self.COOLDOWN

    def search(self, q: str) -> tuple[list[dict], list[str]]:
        errors = []
        total = next((t.split("/")[1] for t in q.split() if "/" in t and t.replace("/", "").isdigit()), None)
        for p in self._live(self.providers):
            try:
                cards = p.search(q)
                if total:  # "58/102": prefer the set whose printed total matches
                    cards.sort(key=lambda c: str(c.get("printed_total")) != total.lstrip("0"))
                return cards, errors
            except (httpx.HTTPError, ValueError, KeyError) as e:
                self._failed(p, errors, e)
        return [], errors

    def get(self, card_id: str) -> tuple[dict | None, list[str]]:
        # IDs differ between sources; TCGdex ones carry a "tcgdex:" prefix.
        errors = []
        providers = self.providers[1:] if card_id.startswith("tcgdex:") else self.providers
        for p in self._live(providers):
            try:
                return p.get(card_id), errors
            except (httpx.HTTPError, ValueError, KeyError) as e:
                self._failed(p, errors, e)
        return None, errors


# ---------------------------------------------------------- graded prices

_MIN_GAP = 1.1  # PriceCharting allows one call a second and can revoke access beyond that
_LISTINGS: dict[str, tuple[float, dict]] = {}  # query -> (when, answer): one call serves graded and raw
_pace_lock = threading.Lock()
_last_call = 0.0


def _pace() -> None:
    global _last_call
    with _pace_lock:
        time.sleep(max(0.0, _last_call + _MIN_GAP - time.monotonic()))
        _last_call = time.monotonic()


def is_same_card(card: dict, data: dict) -> bool:
    """The API's search answers with its single closest match, even when that's another card, and a
    wrong card's price is a made-up price. So the match must name this card and carry its number."""
    if data.get("status") not in (None, "success"):
        return False
    title = re.sub(r"[^a-z0-9#]+", " ", (data.get("product-name") or "").lower())
    console = (data.get("console-name") or "").lower()
    words = re.findall(r"[a-z0-9]+", (card.get("name") or "").lower())
    number = str(card.get("number") or "").lstrip("0").lower()
    if not words or not number or not title or "pokemon" not in console:
        return False
    if (card.get("language") or "en") == "en" and any(w in console for w in ("japanese", "korean", "chinese")):
        return False
    return all(w in title.split() for w in words) and re.search(rf"#0*{re.escape(number)}\b", title) is not None


class PriceChartingProvider:
    """Graded prices from PriceCharting's paid API (https://www.pricecharting.com/api-documentation).

    Field mapping for trading cards: loose = ungraded, cib = grade 7, new = grade 8, graded =
    grade 9, box-only = grade 9.5, manual-only = PSA 10. Prices are in pennies. Checked against
    the live API on 2026-11-05: four cards matched and every price equalled PriceCharting's page.
    Calls are paced to one a second, and a match that isn't this card counts as no price.
    """
    name = "pricecharting"
    FIELDS = {10: "manual-only-price", 9: "graded-price", 8: "new-price", 7: "cib-price"}

    def available(self) -> bool:
        return bool(os.environ.get("PRICECHARTING_API_KEY"))

    def listing(self, card: dict) -> dict | None:
        """This card's prices in dollars: "raw" (ungraded) and grades 7-10. None if the API's
        match isn't this card."""
        token = os.environ["PRICECHARTING_API_KEY"]
        q = f"pokemon {card['name']} {card.get('set') or ''} {card.get('number') or ''}".strip()
        hit = _LISTINGS.get(q)
        if hit and time.monotonic() - hit[0] < 600:
            data = hit[1]
        else:
            _pace()
            data = _get_json("https://www.pricecharting.com/api/product", {"t": token, "q": q})
            _LISTINGS[q] = (time.monotonic(), data)
        if not is_same_card(card, data):
            return None
        out = {grade: round(int(data[fld]) / 100.0, 2) for grade, fld in self.FIELDS.items() if data.get(fld)}
        if data.get("loose-price"):
            out["raw"] = round(int(data["loose-price"]) / 100.0, 2)
        return out

    def graded(self, card: dict) -> dict[int, float]:
        return {g: v for g, v in (self.listing(card) or {}).items() if g != "raw"}


def _pc_match(card: dict) -> dict | None:
    return pricecharting_lookup(card["name"], str(card.get("number") or ""), "en", card.get("set") or "",
                                "1st" in (card.get("raw_variant") or "").lower(), card.get("stamp") or "")


def pricecharting_for_card(card: dict) -> dict | None:
    """PriceCharting listing (raw + graded) for a database card, from its public page."""
    if not scraping_enabled():
        return None
    try:
        return _pc_match(card)
    except httpx.HTTPError:
        return None


def api_raw_price(card: dict | None) -> float | None:
    """The ungraded price from the PriceCharting API, when there's a key and it matches this card."""
    pc = PriceChartingProvider()
    if not card or not pc.available():
        return None
    try:
        return (pc.listing(card) or {}).get("raw")
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def graded_prices(card: dict | None) -> tuple[dict[int, float], str]:
    """(prices by grade, source description). Empty dict means manual entry is needed."""
    pc = PriceChartingProvider()
    if card and pc.available():
        try:
            prices = pc.graded(card)
            if prices:
                return prices, "PriceCharting API"
        except (httpx.HTTPError, ValueError, KeyError):
            return {}, "PriceCharting lookup failed. Enter comps manually."
    if card and card.get("pc_graded"):
        return dict(card["pc_graded"]), PC_SOURCE
    if card:
        pc = pricecharting_for_card(card)
        if pc:
            graded = {g: v for g, v in pc["prices"].items() if g != "raw"}
            if graded:
                return graded, f"{PC_SOURCE}: {pc['url']}"
    return {}, "No graded-price source configured. Enter recent sold comps (eBay sold listings, 130point.com, etc.)."


# ------------------------------------------------------- auto-identification

def _split_number(raw: str) -> tuple[str | None, str | None]:
    """'004/102' -> ('4', '102'); 'TG23/TG30' -> ('TG23', 'TG30'); 'No.006' -> ('6', None)."""
    raw = (raw or "").strip().upper().replace("NO.", "").replace("#", "").strip()
    if not raw:
        return None, None
    num, _, total = raw.partition("/")
    norm = lambda x: (x.lstrip("0") or "0") if x.isdigit() else x  # noqa: E731
    return norm(num.strip()), (norm(total.strip()) if total.strip() else None)


def identify_card(guess: dict | None, lookup: "CardLookup") -> dict:
    """Match the vision model's reading of the card to a database entry.

    Returns {"card": card or None, "status": "matched" | "ambiguous" | "unmatched" | "unreadable"
    | "non_english", "note": str, "guess": guess}. Only an exact collector-number match counts;
    the name alone is never enough (dozens of cards share it).
    """
    out = {"card": None, "guess": guess}
    if not guess or float(guess.get("confidence") or 0) < 0.4 or not (guess.get("name_en") or "").strip():
        return {**out, "status": "unreadable", "note": "Couldn't read the card's name and number from the photo."}
    name, lang = guess["name_en"].strip(), (guess.get("language") or "en").lower()
    label = f"{guess.get('name_printed') or name} {guess.get('number') or ''}".strip()
    if lang not in ("en", "english"):
        return {**out, "status": "non_english",
                "note": f"Looks like a non-English ({lang}) card: {label}. The price sources here only cover English "
                        "printings, which sell for different prices, so prices must be entered by hand."}
    num, total = _split_number(guess.get("number", ""))
    if not num:
        return {**out, "status": "unreadable", "note": f"Read the name ({name}) but not the collector number."}
    cards, _ = lookup.search(f"{name} {num}/{total}" if total else f"{name} {num}")
    hits = [c for c in cards if (_split_number(str(c.get("number") or ""))[0] or "") == num]
    if total:
        exact = [c for c in hits if str(c.get("printed_total") or "") == total]
        hits = exact or hits
    set_name = (guess.get("set_name") or "").lower().strip()
    if set_name and len(hits) > 1:
        exact = [c for c in hits if (c.get("set") or "").lower() == set_name]
        by_set = exact or [c for c in hits if set_name in (c.get("set") or "").lower()
                           or (c.get("set") or "").lower() in set_name]
        hits = by_set or hits
    if not hits:
        return {**out, "status": "unmatched", "note": f"Read it as {label}, but found no matching card in the database."}
    card = _choose_edition(dict(hits[0]), bool(guess.get("first_edition")))
    if len(hits) > 1:
        sets = ", ".join(sorted({c.get('set') or '?' for c in hits})[:4])
        return {**out, "card": card, "status": "ambiguous",
                "note": f"Read it as {label}; several printings match ({sets}). Picked {card.get('set')}. Check it."}
    return {**out, "card": card, "status": "matched", "note": f"Identified from the photo as {label}."}


def _choose_edition(card: dict, first_edition: bool) -> dict:
    """Re-pick the raw price variant to match the 1st Edition stamp (or its absence)."""
    prices = card.get("all_prices") or {}
    def market(v):
        return v.get("market") or v.get("marketPrice") if isinstance(v, dict) else None
    keyed = {k: market(v) for k, v in prices.items() if market(v)}
    first = {k: v for k, v in keyed.items() if "1st" in k.lower()}
    other = {k: v for k, v in keyed.items() if "1st" not in k.lower()}
    pool = first if first_edition else other
    if not pool:
        return card
    # Prefer holo variants within the pool, the same order _pick_variant uses.
    order = sorted(pool, key=lambda k: (0 if "holo" in k.lower() and "reverse" not in k.lower() else 1, k))
    card["raw_variant"], card["raw_price"] = order[0], float(pool[order[0]])
    if first_edition:
        card["raw_variant"] += " (1st Edition stamp seen in photo)"
    return card


def variant_prices(card: dict | None) -> list[tuple[str, float]]:
    """[(variant key, market price)] for the printing picker, e.g. 1st Edition vs Unlimited."""
    out = []
    for k, v in ((card or {}).get("all_prices") or {}).items():
        if isinstance(v, dict):
            m = v.get("market") or v.get("marketPrice")
            if m:
                out.append((k, float(m)))
    return sorted(out, key=lambda kv: -kv[1])


def pretty_variant(key: str) -> str:
    """'1stEditionHolofoil' / '1st-edition-holofoil' -> '1st Edition Holofoil'."""
    import re
    k = key.split(" (")[0].replace("-", " ")
    k = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", k)
    return " ".join(w if w.isupper() else w.capitalize() for w in k.split()).replace("1St", "1st")


def comps_links(card: dict | None, guess: dict | None = None, grader: str = "PSA") -> list[tuple[str, str]]:
    """Search links for recent graded sales. We link out rather than scrape."""
    from urllib.parse import quote_plus
    if card:
        base = f"{card['name']} {card.get('number') or ''}{'/' + str(card['printed_total']) if card.get('printed_total') else ''}"
        set_name = card.get("set") or ""
    elif guess and guess.get("name_en"):
        base, set_name = f"{guess['name_en']} {guess.get('number') or ''}", ""
    else:
        return []
    q = f"pokemon {base} {set_name}".strip()
    return [
        (f"eBay sold: {grader} 10", f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(f'{q} {grader} 10')}&LH_Sold=1&LH_Complete=1"),
        (f"eBay sold: {grader} 9", f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(f'{q} {grader} 9')}&LH_Sold=1&LH_Complete=1"),
        ("PriceCharting", f"https://www.pricecharting.com/search-products?type=prices&q={quote_plus(q)}"),
    ]


# ------------------------------- PriceCharting public pages (opt-in, see README)

PC_BASE = "https://www.pricecharting.com"
# Table cell ids on a PriceCharting card page -> grade (their API docs use the same mapping).
PC_FIELDS = {"used_price": "raw", "complete_price": 7, "new_price": 8, "graded_price": 9, "manual_only_price": 10}
PC_SOURCE = "PriceCharting public page"
LANG_PREFIX = {"ja": "pokemon-japanese-", "ko": "pokemon-korean-", "zh": "pokemon-chinese-"}


def _get_html(url: str, params: dict | None = None) -> tuple[str, str]:
    """(html, final url), cached like _get_json. Final url matters: a unique search redirects to the card."""
    key = hashlib.sha256(("html|" + url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.html.json"
    if path.exists() and time.time() - path.stat().st_mtime < TTL:
        d = json.loads(path.read_text())
        return d["html"], d["url"]
    r = httpx.get(url, params=params, headers=UA, timeout=15, follow_redirects=True)
    r.raise_for_status()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"html": r.text, "url": str(r.url)}))
    return r.text, str(r.url)


def scraping_enabled() -> bool:
    return os.environ.get("GRADEORSKIP_SCRAPE_PRICECHARTING") == "1"


def _pc_prices(html: str) -> dict:
    import re
    out = {}
    for fid, grade in PC_FIELDS.items():
        m = re.search(rf'<td id="{fid}"[^>]*>\s*<span class="price js-price"[^>]*>\s*\$([0-9,]+\.[0-9]{{2}})', html)
        if m:
            out[grade] = float(m.group(1).replace(",", ""))
    return out


def _stamp_words(stamp: str) -> set[str]:
    """Words of a stamp that would appear in a listing's slug."""
    import re
    s = (stamp or "").lower()
    words = {w for w in re.findall(r"[a-z]+", s) if len(w) > 2} - {"stamp", "stamped", "the", "logo", "mark", "card", "and"}
    if "pumpkin" in words:  # the Trick or Trade stamp is a pumpkin; listings name the product
        words = (words - {"pumpkin"}) | {"trick", "trade"}
    return words


def _printing_label(slug: str) -> str:
    """'pokemon-trick-or-trade-2022/mewtwo-holo-56' -> 'Trick Or Trade 2022 · Holo'."""
    import re
    console, card = slug.split("/", 1)
    extra = re.sub(r"-[a-z]*\d+[a-z]*$", "", card).split("-")[1:]
    words = [w for w in extra if w not in ("ex", "gx", "v", "vmax", "vstar")]
    label = console.removeprefix("pokemon-").replace("-", " ").title()
    return f"{label} · {' '.join(words).title()}" if words else label


def pricecharting_lookup(name_en: str, number: str | None, language: str = "en", set_name: str = "",
                         first_edition: bool = False, stamp: str = "") -> dict | None:
    """Find a card's PriceCharting page and read its raw + graded prices.

    Matching: right language (slug prefix), collector number at the end of the slug,
    then set-name and 1st Edition overlap as tie-breaks. Returns None rather than guess.
    """
    import re
    num, _ = _split_number(number or "")
    if not name_en:
        return None
    from urllib.parse import unquote
    import html as htmllib

    def search(q):
        html, url = _get_html(f"{PC_BASE}/search-products", {"type": "prices", "q": q})
        if "/game/" in url:  # a unique match redirects straight to the card page
            return [unquote(url.split("/game/")[1].split("?")[0])]
        return [unquote(htmllib.unescape(s)) for s in
                dict.fromkeys(re.findall(r'href="(?:https?://www\.pricecharting\.com)?/game/([^"?#]+)"', html))]

    prefix = LANG_PREFIX.get(language)
    def lang_ok(slug):
        if prefix:
            return slug.startswith(prefix)
        return slug.startswith("pokemon-") and not any(slug.startswith(p) for p in LANG_PREFIX.values())
    # The listing must be this card: its slug is "<name>-<number>" (a number-only match once
    # priced an Oricorio ex as M Blastoise ex #22).
    name_slug = "-".join(re.findall(r"[a-z0-9]+", name_en.lower()))
    def card_of(slug):
        return slug.split("/")[-1].lower()
    def named(slug):
        return re.fullmatch(rf"{re.escape(name_slug)}(-[a-z-]*)?-[a-z]*\d+[a-z]*", card_of(slug)) is not None
    set_words = {w for w in re.findall(r"[a-z0-9]+", set_name.lower()) if len(w) > 2}
    stamp_words = _stamp_words(stamp)

    cands = []
    if num:
        num_l = num.lower().lstrip("0") or "0"
        found = search(f"pokemon {name_en} {num}")
        if stamp_words:  # a stamped printing may sit in its own product, off the first page
            found += search(f"pokemon {name_en} {num} {' '.join(sorted(stamp_words))}")
        cands = [s for s in dict.fromkeys(found) if lang_ok(s) and named(s)
                 and re.search(rf"-0*{re.escape(num_l)}$", card_of(s))]
    if not cands:
        # Number unreadable or misread: accept a name match only if it's unambiguous (after
        # narrowing by set name, e.g. "Promo").
        cands = [s for s in search(f"pokemon {name_en}") if lang_ok(s) and named(s)]
        if len(cands) > 1 and set_words:
            cands = [s for s in cands if set_words & set(s.split("/")[0].split("-"))] or cands
        if len(cands) != 1:
            return None
    def stamp_hits(slug):
        return len(stamp_words & set(re.findall(r"[a-z0-9]+", slug)))
    def score(slug):
        console, card = slug.split("/", 1)
        overlap = len(set_words & set(console.split("-")))
        edition = ("1st-edition" in card) == first_edition
        generic = bool(stamp_words) and "stamped" in card
        return (stamp_hits(slug), generic, edition, overlap, -len(card))  # plain listing beats variants
    ranked = sorted(cands, key=score, reverse=True)
    best = ranked[0]
    from urllib.parse import quote
    page, final = _get_html(f"{PC_BASE}/game/{quote(best, safe='/')}")
    prices = _pc_prices(page)
    if not prices:
        return None
    title = re.search(r"<title>([^<|]+)", page)
    console = best.split("/")[0]
    return {"slug": best, "url": final, "prices": prices, "title": title.group(1).strip() if title else best,
            "set": console.removeprefix("pokemon-").replace("-", " ").title(), "ambiguous": len(cands) > 1,
            "options": [{"slug": s, "label": _printing_label(s)} for s in ranked[:6]],
            "stamp_matched": stamp_hits(best) > 0}


def pricecharting_listing(slug: str) -> dict | None:
    """Prices for one specific listing, e.g. the printing someone picked."""
    from urllib.parse import quote
    if not scraping_enabled():
        return None
    try:
        page, final = _get_html(f"{PC_BASE}/game/{quote(slug, safe='/')}")
    except httpx.HTTPError:
        return None
    prices = _pc_prices(page)
    return {"slug": slug, "url": final, "prices": prices} if prices else None


def priced_from_pricecharting(guess: dict) -> dict | None:
    """A card record (same shape as the database ones) for cards only PriceCharting covers, e.g. Japanese."""
    if not scraping_enabled():
        return None
    try:
        pc = pricecharting_lookup(guess.get("name_en", ""), guess.get("number"), (guess.get("language") or "en").lower(),
                                  guess.get("set_name") or "", bool(guess.get("first_edition")), guess.get("stamp") or "")
    except httpx.HTTPError:
        return None
    if not pc:
        return None
    num, total = _split_number(guess.get("number") or "")
    import re
    listed = re.search(r"(\d+)[a-z]*$", pc["slug"])
    if listed and listed.group(1).lstrip("0") != num.lstrip("0"):
        num, total = listed.group(1), None  # matched by name: trust the listing's number over the misread one
    graded = {g: v for g, v in pc["prices"].items() if g != "raw"}
    return {
        "id": "pc:" + pc["slug"],
        "name": guess.get("name_en") if guess.get("name_printed") in (None, "", guess.get("name_en"))
        else f"{guess.get('name_printed')} ({guess.get('name_en')})",
        "set": pc["set"], "number": num, "printed_total": total, "rarity": None, "image": None,
        "raw_price": pc["prices"].get("raw"), "raw_variant": "ungraded", "raw_source": PC_SOURCE,
        "raw_updated": time.strftime("%Y-%m-%d"), "raw_url": pc["url"], "all_prices": {},
        "pc_graded": graded, "pc_url": pc["url"], "language": guess.get("language"),
        "pc_slug": pc["slug"], "pc_options": pc["options"], "stamp_matched": pc["stamp_matched"],
    }


# ------------------------------------------- TAG sold listings on the card page (POC)
#
# TAG 10 has its own table of sales on the page. TAG 9, 8 and 7 don't: a few turn up in the shared
# grade tables, named only in the title. Those tables hold about 30 sales across all graders, so
# TAG sales drop off quickly on a popular card.

TAG_GRADES = (10, 9, 8, 7)
TAG_MIN_SALES = 3  # below this, one odd sale would set the price
TAG_MAX_AGE_DAYS = 180  # TAG sales are thin, so 90 days would empty most cards
TAG_MAX_SALES = 15  # newest sales only: a busy card's six-month median lags a moving market by 15-30%
TAG_NO_PRICES = "No TAG prices yet. Enter recent sold prices."
TAG_PRISTINE_SCORE = 990  # our TAG 10 is Gem Mint; a Pristine 10 (this score or more) sells far higher

_TAG_LABELS = {"gem": 10, "gem mint": 10, "gem mt": 10, "mint": 9, "nm mt": 8, "nm": 7}
# "TAG 9", or TAG's own wording ("TAG NM MT 8"). A number must follow TAG directly, which rules out
# the set names (Tag Bolt, Tag All Stars, Tag Team) and collector numbers ("Tag 95/95").
_TAG_GRADE = re.compile(r"(?<![a-z0-9])tag\s+(?:(gem[\s-]+mint|gem[\s-]+mt|gem|mint|nm[\s-]+mt|nm)\s+)?"
                        r"(\d+(?:\.\d+)?)(?![\d.+/-]|\s*/|\s*(?:of|out)\b)", re.I)
_TAG_SCORE = re.compile(r"[(\[]\s*(\d{3,4})\s*[)\]]")
_OTHER_GRADER = re.compile(r"\b(?:psa|bgs|beckett|cgc|sgc|gma|hga|csg|ars|ksa|pca)\b|\bace\s+(?:gem\s+mint\s+)?\d", re.I)
_NOT_ONE_SLAB = re.compile(r"\b(?:lots?|bundle|bulk|set of|x[2-9]|raw|ungraded|not graded|potential|possible|candidate"
                           r"|ready|maybe|worthy|proxy|custom|reprint|replica|fake|empty|reholder(?:ed)?|cracked"
                           r"|pristine|(?:no|not|non|like|price|gift)\s+tag)\b", re.I)
_PC_TABLE = re.compile(r'<div class="(completed-auctions-[a-z-]+)"[^>]*>')
_PC_ROW = re.compile(r'<tr id="([a-z]+-[^"]+)">(.*?)</tr>', re.S)
_PC_DATE = re.compile(r'<td class="date">\s*(\d{4}-\d{2}-\d{2})\s*</td>')
_PC_TITLE = re.compile(r'<td class="title">(.*?)</td>', re.S)
# The sale price opens this cell; list prices follow it, and have a cell of their own.
_PC_PRICE = re.compile(r'<td class="numeric">\s*<span class="js-price"(?: title="best offer accepted price")?\s*>'
                       r'\s*\$([0-9,]+\.[0-9]{2})')


def title_tag_grade(title: str) -> float | None:
    """The one TAG grade a listing title clearly claims, else None. Half grades come back as they are."""
    if _NOT_ONE_SLAB.search(title) or _OTHER_GRADER.search(title):
        return None
    grades = set()
    for m in _TAG_GRADE.finditer(title):
        grade = float(m.group(2))
        label = " ".join(re.split(r"[\s-]+", (m.group(1) or "").lower()))
        if label and _TAG_LABELS[label] != grade:
            return None
        grades.add(grade)
    if len(grades) != 1:
        return None
    grade = grades.pop()
    if grade > 10 or grade == 10 and any(int(s) >= TAG_PRISTINE_SCORE for s in _TAG_SCORE.findall(title)):
        return None
    return grade


def _title_names_other_card(title: str, number: str | None) -> bool:
    """A listing filed on the wrong page: its title shows collector numbers, and none is this card's."""
    if not number:
        return False
    shown = set(re.findall(r"#\s*0*(\d+)", title)) | set(re.findall(r"(?<![\w/])0*(\d+)/\d", title))
    return bool(shown) and number not in shown


def _pc_sales(page: str) -> list[tuple[str, str, str, str, float]]:
    """(table, row id, date, title, price) for every sold listing on a card page."""
    found = list(_PC_TABLE.finditer(page))
    out = []
    for m, nxt in zip(found, found[1:] + [None]):
        for rid, row in _PC_ROW.findall(page[m.end():nxt.start() if nxt else len(page)]):
            date, title, price = _PC_DATE.search(row), _PC_TITLE.search(row), _PC_PRICE.search(row)
            if date and title and price:
                text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", title.group(1))).split())
                out.append((m.group(1), rid, date.group(1), text, float(price.group(1).replace(",", ""))))
    return out


def tag_comps_from_page(page: str, number: str | None = None, today: datetime.date | None = None) -> dict[int, dict]:
    """TAG comps from a card page, per grade: {price, n, oldest, newest}. n counts recent sales, and
    price stays None below TAG_MIN_SALES. `number` is the collector number, to drop misfiled listings."""
    today = today or datetime.date.today()
    since = (today - datetime.timedelta(days=TAG_MAX_AGE_DAYS)).isoformat()
    number = (number or "").lstrip("0") or None
    option = re.search(r'<option value="(completed-auctions-[a-z-]+)">\s*TAG 10\b', page)
    tag10_table = option.group(1) if option else None
    sales, seen = {g: [] for g in TAG_GRADES}, set()
    for table, rid, date, title, price in _pc_sales(page):
        grade = title_tag_grade(title)
        own_table = grade == 10 and table == tag10_table or grade in (9, 8, 7) and table != tag10_table
        if own_table and rid not in seen and since <= date <= today.isoformat() and price > 0 \
                and not _title_names_other_card(title, number):
            seen.add(rid)
            sales[int(grade)].append((date, price))
    out = {}
    for grade, rows in sales.items():
        rows = sorted(rows, reverse=True)[:TAG_MAX_SALES]
        dates = [d for d, _ in rows]
        out[grade] = {"price": round(statistics.median(p for _, p in rows), 2) if len(rows) >= TAG_MIN_SALES else None,
                      "n": len(rows), "oldest": min(dates, default=None), "newest": max(dates, default=None)}
    return out


def tag_source(comps: dict[int, dict], url: str) -> str | None:
    """What the TAG comps are and aren't, in words. None if no grade has one."""
    parts = []
    for g, c in comps.items():
        if c["price"] is not None:
            span = c["newest"] if c["oldest"] == c["newest"] else f"{c['oldest']} to {c['newest']}"
            parts.append(f"TAG {g}: median of {c['n']} sales, {span}.")
    if not parts:
        return None
    empty = [str(g) for g, c in comps.items() if c["price"] is None]
    if empty:
        parts.append(f"TAG {', '.join(empty)}: blank, under {TAG_MIN_SALES} sales in the last {TAG_MAX_AGE_DAYS} days.")
    return " ".join(parts) + f" Sold listings from {PC_SOURCE}: {url}"


def tag_comps_for_card(card: dict) -> dict | None:
    """{comps, url} from the card's PriceCharting page, with no comps if PriceCharting doesn't list it.
    None if the fetch failed. The page is the one _get_html caches, so this is at most one fetch in TTL
    once the listing is known."""
    try:
        slug = card.get("pc_slug") or (_pc_match(card) or {}).get("slug")
        if not slug:
            return {"comps": {}, "url": None}
        page, url = _get_html(f"{PC_BASE}/game/{quote(slug, safe='/')}")
        listed = re.search(r"-0*(\d+)$", slug)
        return {"comps": tag_comps_from_page(page, listed.group(1) if listed else None), "url": url}
    except Exception:  # reading someone else's page must never take the check page down with it
        return None
