import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES
from gradeorskip import app as app_module
from gradeorskip.providers import prices as prices_module
from gradeorskip.providers.prices import (TAG_MAX_AGE_DAYS, TAG_MAX_SALES, TAG_MIN_SALES, TAG_NO_PRICES,
                                       tag_comps_from_page, tag_source, title_tag_grade)

client = TestClient(app_module.app)
TODAY = datetime.date(2026, 10, 31)
TAG10 = "completed-auctions-grade-twenty-one"
URL = "https://www.pricecharting.com/game/pokemon-x/y-199"


def _row(title, price="$100.00", date="2026-10-01", rid=None, listed=False):
    """A row as PriceCharting prints it, best-offer list prices included."""
    rid = rid or f"ebay-{abs(hash((title, price, date)))}"
    extra = '<br><span class="js-price listed-price-inline" title="best offer list price">$999.00</span>' if listed else ""
    return f'''<tr id="{rid}">
      <td class="date">{date}</td>
      <td class="image"><div class="image"></div></td>
      <td class="title"><a target="_blank" class="js-ebay-completed-sale" href="https://www.ebay.com/itm/1">
        {title}</a> [eBay]</td>
      <td class="numeric"><span class="js-price" {'title="best offer accepted price"' if listed else ''}>{price}</span>{extra}</td>
      <td class="numeric listed-price">{'<span class="js-price" title="best offer list price">$999.00</span>' if listed else '&nbsp;'}</td>
      <td class="thumb-down"></td></tr>'''


def _page(**tables):
    """tables: Grade table -> rows. A TAG 10 option only appears when the page has that table."""
    options = f'<option value="{TAG10}">TAG 10 (30)</option>' if "tag10" in tables else ""
    names = {"tag10": TAG10, "grade9": "completed-auctions-graded", "grade8": "completed-auctions-new",
             "grade7": "completed-auctions-cib", "ace10": "completed-auctions-grade-twenty-two"}
    body = "".join(f'<div class="{names[k]}"><table><tbody>{"".join(v)}</tbody></table></div>' for k, v in tables.items())
    return f'<select>{options}</select><div class="tab available {TAG10}"></div>{body}'


def _comps(page, number=None):
    return tag_comps_from_page(page, number, TODAY)


@pytest.mark.parametrize("title,grade", [
    ("2023 Pokemon Charizard ex 199/165 SIR TAG 10 (983) 199/165", 10),
    ("TAG GEM MINT 10 [976] Mega Gengar ex 230/193", 10),
    ("Oricorio ex 024 Promo Tag 10 #024", 10),
    ("pokemon prismatic evolutions pikachu ex 179/131 tag 10", 10),
    ("Pokemon Heliolisk 💎TAG 9💎 236/217", 9),
    ("Pikachu ex 179/131 TAG 9 179/131", 9),
    ("Charizard Base Set Unlimited Holo 4 TAG 8", 8),
    ("TAG NM MT 8 Psyduck AR 199/193", 8),
    ("Charizard ex TAG 7 199/165", 7),
    ("Charizard ex TAG 8.5 #199", 8.5),  # returned as is, so it can't count as 8
    ("Mew VMAX Tag 7.5 114/264", 7.5),
])
def test_title_states_a_tag_grade(title, grade):
    assert title_tag_grade(title) == grade


@pytest.mark.parametrize("title", [
    "SM9 Tag Bolt Charmander 095/095",  # set names
    "Tag All Stars 9 Mewtwo",
    "TAG TEAM 9 Reshiram & Charizard GX",
    "Tag Bolt 8 Charmander PSA 9",
    "Tag 9/173 Pikachu",  # a collector number, not a grade
    "TAG 8/10 centering",
    "TAG 9 of 10",
    "TAG 9+",
    "TAG 983 Charizard",  # a score, not a grade
    "TAG 9. Charizard",
    "TAG9 Charizard",
    "TAG MINT 8 Charizard",  # label and number disagree
    "TAG 9 TAG 10 pair",
    "PSA 9 TAG 9 Charizard",
    "CGC 10 Charizard ex",
    "ACE 10 and TAG 10 Charizard",
    "TAG PRISTINE 10 Mega Gengar ex",  # our TAG 10 is Gem Mint
    "Charizard TAG 10 (995)",
    "Charizard raw, potential TAG 10",
    "TAG 10 ready Charizard",
    "Lot of 3 TAG 9 Charizard",
    "Price tag 9 Charizard",
    "Not TAG 9 Charizard",
    "Charizard ex 199/165 Near Mint",
])
def test_title_without_a_clear_tag_grade_is_ignored(title):
    assert title_tag_grade(title) is None


def test_tag10_comes_from_its_own_table_and_lower_grades_from_titles():
    page = _page(tag10=[_row("Charizard TAG 10 (983)", f"${p}.00") for p in (100, 200, 900)],
                 grade9=[_row("Charizard TAG 9", f"${p}.00") for p in (50, 60, 70, 80)] + [_row("Charizard PSA 9", "$500.00")],
                 grade8=[_row("Charizard TAG 8", "$40.00"), _row("Charizard TAG 8", "$41.00"),
                         _row("Charizard TAG 8.5", "$42.00"), _row("Charizard TAG 8.5", "$43.00")],
                 grade7=[_row("Charizard TAG 7", f"${p}.00") for p in (20, 30, 40)])
    c = _comps(page)
    assert c[10]["price"] == 200 and c[10]["n"] == 3
    assert c[9]["price"] == 65 and c[9]["n"] == 4 and c[9]["newest"] == "2026-10-01"
    assert c[8]["price"] is None and c[8]["n"] == 2  # half grades aren't 8s, so too few
    assert c[7]["price"] == 30


def test_tag10_table_rows_must_agree_and_titles_elsewhere_cannot_make_a_tag10():
    page = _page(tag10=[_row("Charizard TAG 10", "$100.00"), _row("Charizard TAG 10", "$101.00"),
                        _row("Charizard PSA 10", "$900.00"),  # filed in the TAG table on a real page
                        _row("Charizard ex 199/193 Japanese", "$800.00"),  # no grade at all
                        _row("TAG PRISTINE 10 Charizard", "$700.00"),
                        _row("Charizard TAG 9", "$20.00")],
                 grade9=[_row("Charizard TAG 10", "$600.00", rid="ebay-1")])
    c = _comps(page)
    assert c[10]["n"] == 2 and c[10]["price"] is None and c[9]["n"] == 0


def test_ace10_table_is_not_tag10():
    page = _page(tag10=[], ace10=[_row("Charizard ACE 10", "$100.00") for _ in range(3)])
    assert _comps(page)[10]["n"] == 0


def test_a_page_without_a_tag10_option_has_no_tag10():
    assert _comps(_page(grade9=[_row("Charizard TAG 10", "$100.00")]))[10]["n"] == 0


def test_sale_price_not_list_price_and_best_offer_counted():
    page = _page(tag10=[_row("Charizard TAG 10", "$1,200.00", listed=True), _row("Charizard TAG 10", "$1,100.00"),
                        _row("Charizard TAG 10", "$1,000.00")])
    assert _comps(page)[10]["price"] == 1100


def test_old_future_and_duplicate_sales_are_dropped():
    old = (TODAY - datetime.timedelta(days=TAG_MAX_AGE_DAYS + 1)).isoformat()
    edge = (TODAY - datetime.timedelta(days=TAG_MAX_AGE_DAYS)).isoformat()
    page = _page(grade9=[_row("Charizard TAG 9", "$10.00", edge), _row("Charizard TAG 9", "$20.00", "2026-09-01", rid="ebay-7"),
                         _row("Charizard TAG 9", "$500.00", old), _row("Charizard TAG 9", "$500.00", "2026-12-01")],
                 grade8=[_row("Charizard TAG 9", "$20.00", "2026-09-01", rid="ebay-7")])
    c = _comps(page)[9]
    assert c["n"] == 2 and c["oldest"] == edge and c["newest"] == "2026-09-01"


def test_minimum_sample_is_exact():
    rows = [_row("Charizard TAG 9", "$10.00", f"2026-10-{d:02d}") for d in range(1, TAG_MIN_SALES)]
    assert _comps(_page(grade9=rows))[9]["price"] is None
    rows.append(_row("Charizard TAG 9", "$10.00", "2026-10-20"))
    assert _comps(_page(grade9=rows))[9]["price"] == 10


def test_median_uses_only_the_newest_sales():
    newest = [_row("Charizard TAG 10", "$50.00", f"2026-10-{d:02d}") for d in range(1, TAG_MAX_SALES + 1)]
    older = [_row("Charizard TAG 10", "$900.00", "2026-06-01", rid=f"ebay-{i}") for i in range(TAG_MAX_SALES)]
    c = _comps(_page(tag10=older + newest))[10]
    assert c["price"] == 50 and c["n"] == TAG_MAX_SALES and c["oldest"] == "2026-10-01"


def test_listing_for_another_collector_number_is_dropped():
    page = _page(grade9=[_row("Heliolisk #236/217💎TAG 9💎 236/217", "$25.00"), _row("Heliolisk TAG 9 #229", "$30.00"),
                         _row("Heliolisk 229/217 TAG 9", "$31.00"), _row("Heliolisk promo TAG 9", "$32.00")])
    assert _comps(page, "229")[9]["n"] == 3
    assert _comps(page)[9]["n"] == 4


def test_html_in_titles_is_read_as_text():
    page = _page(grade9=[_row("Brock&#39;s Rhydon Scarlet &amp; Violet TAG 9", f"${p}.00") for p in (1, 2, 3)])
    assert _comps(page)[9]["price"] == 2


def test_empty_or_unrelated_pages_give_nothing():
    for page in ("", "<html>changed layout</html>", _page(tag10=[])):
        assert all(c["price"] is None and c["n"] == 0 for c in _comps(page).values())


def test_source_says_what_was_found_and_what_was_not():
    c = _comps(_page(tag10=[_row("Charizard TAG 10", f"${p}.00", f"2026-10-{p:02d}") for p in (1, 2, 3)]))
    text = tag_source(c, URL)
    assert "TAG 10: median of 3 sales, 2026-10-01 to 2026-10-03." in text
    assert "TAG 9, 8, 7: blank" in text and URL in text
    assert tag_source(_comps(""), URL) is None


# ------------------------------------------------------------- through the app

def _check(monkeypatch, card, pages=None, fetch=None):
    """A check with PSA comps, and a stand-in for the network that counts page fetches."""
    monkeypatch.setenv("GRADEORSKIP_SCRAPE_PRICECHARTING", "1")
    calls = []

    def get_html(url, params=None):
        calls.append(url)
        if fetch:
            fetch()
        return pages, URL

    monkeypatch.setattr(prices_module, "_get_html", get_html)
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_55_45_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    client.post(f"/check/{cid}/prices", data={"raw": "40", "psa10": "900", "psa9": "400", "psa8": "300", "psa7": "200",
                                              "tier": "standard", "shipping": "8", "sell_fee": "0", "fee_override": ""})
    app_module.db.update_check(cid, card=card)
    calls.clear()
    return cid, calls


CARD = {"name": "Charizard ex", "set": "151", "number": "199", "pc_slug": "pokemon-x/y-199"}
ONLY_TAG10 = _page(tag10=[_row("Charizard TAG 10", f"${p}.50", datetime.date.today().isoformat(), rid=f"ebay-{p}")
                          for p in (300, 310, 320)])


def _tag(cid):
    return client.get(f"/api/check/{cid}").json()["prices"]["tag"]


def test_switching_to_tag_fills_comps_once_and_rendering_never_fetches(monkeypatch):
    cid, calls = _check(monkeypatch, CARD, ONLY_TAG10)
    assert "tag" not in client.get(f"/api/check/{cid}").json()["prices"]  # an older check
    assert client.post(f"/check/{cid}/grader", data={"grader": "tag"}).status_code == 200
    assert len(calls) == 1 and calls[0].endswith("/game/pokemon-x/y-199")
    tag = _tag(cid)
    assert tag["graded"] == {"10": 310.5} and tag["comps"]["10"]["n"] == 3 and "median of 3 sales" in tag["graded_source"]
    assert tag["tier"]
    for _ in range(3):
        page = client.get(f"/check/{cid}").text
        assert "310.5" in page and "median of 3 sales" in page
    client.get("/history")
    client.get("/")
    assert len(calls) == 1


def test_psa_comps_are_untouched_and_psa_never_fetches(monkeypatch):
    cid, calls = _check(monkeypatch, CARD, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "psa"})
    assert not calls
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert client.get(f"/api/check/{cid}").json()["prices"]["graded"]["10"] == 900


def _odds(monkeypatch, probs):
    real = app_module.grade_for
    monkeypatch.setattr(app_module, "grade_for", lambda result, grader: {**real(result, grader), "probs": probs})


def test_only_tag10_filled_and_a_lower_likely_grade_never_says_grade_it(monkeypatch):
    cid, _ = _check(monkeypatch, CARD, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    _odds(monkeypatch, {"10": 0.1, "9": 0.6, "8": 0.3})
    d = client.get(f"/api/check/{cid}").json()
    assert d["roi"]["verdict"] == "Need prices" and not d["roi"]["ok"]
    assert "No sold price for TAG 9, TAG 8, the grade you'll likely get" in d["roi"]["reason"]
    page = client.get(f"/check/{cid}").text
    assert "TAG 9, 8, 7: blank" in page and "the most it could fetch" in page and "eBay sold: TAG 9" in page


def test_only_tag10_filled_still_lets_a_dont_grade_through(monkeypatch):
    cid, _ = _check(monkeypatch, CARD, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    client.post(f"/check/{cid}/prices", data={"raw": "400", "tag10": "310.5", "shipping": "8", "sell_fee": "0", "fee_override": ""})
    _odds(monkeypatch, {"10": 0.1, "9": 0.6, "8": 0.3})
    d = client.get(f"/api/check/{cid}").json()
    assert d["roi"]["verdict"] == "Don't grade" and "valued at the next grade up" in " ".join(d["roi"]["assumptions"])


def test_only_tag10_filled_with_a_likely_10_values_the_rest_at_raw_and_says_so(monkeypatch):
    cid, _ = _check(monkeypatch, CARD, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    _odds(monkeypatch, {"10": 0.6, "9": 0.2, "8": 0.2})
    d = client.get(f"/api/check/{cid}").json()
    assert d["roi"]["verdict"] == "Grade it"  # the one case a positive answer rests on an assumption
    assert "No comps entered for TAG 9, TAG 8" in " ".join(d["roi"]["assumptions"])
    assert "No comps entered for TAG 9, TAG 8" in client.get(f"/check/{cid}").text


def test_typed_tag_prices_are_never_overwritten(monkeypatch):
    cid, calls = _check(monkeypatch, CARD, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    client.post(f"/check/{cid}/prices", data={"raw": "40", "tag10": "555", "shipping": "8", "sell_fee": "0", "fee_override": ""})
    assert _tag(cid)["graded_source"] == "entered by you" and "comps" not in _tag(cid)
    calls.clear()
    client.post(f"/check/{cid}/grader", data={"grader": "psa"})
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded"] == {"10": 555} and _tag(cid)["graded_source"] == "entered by you" and not calls


def test_saving_the_form_unchanged_keeps_filled_comps_as_they_are(monkeypatch):
    page = _page(tag10=[_row("Charizard TAG 10", f"${p}", datetime.date.today().isoformat(), rid=f"ebay-{i}")
                        for i, p in enumerate(("12,345.67", "12,345.67", "12,345.67"))])
    cid, _ = _check(monkeypatch, CARD, page)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    shown = client.get(f"/check/{cid}").text
    assert 'value="12345.67"' in shown
    client.post(f"/check/{cid}/prices", data={"raw": "40", "tag10": "12345.67", "shipping": "9", "sell_fee": "0", "fee_override": ""})
    assert "median of 3 sales" in _tag(cid)["graded_source"] and _tag(cid)["graded"] == {"10": 12345.67}


def test_nothing_found_keeps_the_manual_message_and_links(monkeypatch):
    cid, _ = _check(monkeypatch, CARD, _page(tag10=[]))
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded"] == {} and _tag(cid)["graded_source"] == TAG_NO_PRICES
    page = client.get(f"/check/{cid}").text
    assert TAG_NO_PRICES in page and "eBay sold: TAG 10" in page


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("down"), httpx.HTTPStatusError("403", request=None, response=None),
                                   ValueError("bad cache"), RuntimeError("anything")])
def test_a_failed_fetch_breaks_nothing(monkeypatch, error):
    def fail():
        raise error
    cid, _ = _check(monkeypatch, CARD, fetch=fail)
    r = client.post(f"/check/{cid}/grader", data={"grader": "tag"}, follow_redirects=False)
    assert r.status_code == 303
    d = client.get(f"/api/check/{cid}").json()
    assert d["prices"]["grader"] == "tag" and d["prices"]["tag"]["graded"] == {}
    assert TAG_NO_PRICES in d["prices"]["tag"]["graded_source"] and "Couldn't reach" in d["prices"]["tag"]["graded_source"]
    assert client.get(f"/check/{cid}").status_code == 200


def test_a_failed_refresh_keeps_earlier_comps_and_retrying_works(monkeypatch):
    state = {"down": True}

    def maybe():
        if state["down"]:
            raise httpx.ConnectError("down")
    cid, calls = _check(monkeypatch, CARD, ONLY_TAG10, fetch=maybe)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    state["down"] = False
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})  # pressing TAG again retries
    assert _tag(cid)["graded"] == {"10": 310.5}
    state["down"] = True
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded"] == {"10": 310.5} and "median of 3 sales" in _tag(cid)["graded_source"]


def test_card_without_a_stored_listing_is_looked_up(monkeypatch):
    card = {"name": "Charizard ex", "set": "151", "number": "199"}
    cid, _ = _check(monkeypatch, card, ONLY_TAG10)
    monkeypatch.setattr(prices_module, "pricecharting_lookup", lambda *a: {"slug": "pokemon-x/y-199"})
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded"] == {"10": 310.5}


def test_card_not_on_pricecharting_or_no_card_stays_manual(monkeypatch):
    cid, calls = _check(monkeypatch, {"name": "Unknown"}, ONLY_TAG10)
    monkeypatch.setattr(prices_module, "pricecharting_lookup", lambda *a: None)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded_source"] == TAG_NO_PRICES
    app_module.db.update_check(cid, card={})
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert _tag(cid)["graded_source"] == TAG_NO_PRICES and not calls


def test_scraping_off_makes_no_fetch(monkeypatch):
    cid, calls = _check(monkeypatch, CARD, ONLY_TAG10)
    monkeypatch.setenv("GRADEORSKIP_SCRAPE_PRICECHARTING", "0")
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    assert not calls and _tag(cid)["graded_source"] == TAG_NO_PRICES


def test_picking_another_printing_replaces_filled_comps(monkeypatch):
    card = {**CARD, "pc_options": [{"slug": "pokemon-x/z-199", "label": "Z"}]}
    cid, _ = _check(monkeypatch, card, ONLY_TAG10)
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    monkeypatch.setattr(app_module, "pricecharting_listing", lambda slug: {
        "slug": slug, "url": URL, "prices": {"raw": 5.0, 10: 50.0}})
    other = _page(tag10=[])
    monkeypatch.setattr(prices_module, "_get_html", lambda url, params=None: (other, URL))
    client.post(f"/check/{cid}/printing", data={"slug": "pokemon-x/z-199"})
    assert _tag(cid)["graded"] == {} and _tag(cid)["graded_source"] == TAG_NO_PRICES


@pytest.fixture(autouse=True)
def tag_on(monkeypatch):
    monkeypatch.setattr(app_module, "TAG_ENABLED", True)
