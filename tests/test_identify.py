from gradeorskip.providers.prices import _split_number, identify_card


class FakeLookup:
    def __init__(self, cards):
        self.cards = cards

    def search(self, q):
        return list(self.cards), []


def card(id, number, total, set_name, prices=None):
    return {"id": id, "name": "Charizard", "number": number, "printed_total": total, "set": set_name,
            "raw_price": 100.0, "raw_variant": "holofoil", "all_prices": prices or {}}


def G(**kw):
    base = {"name_printed": "Charizard", "name_en": "Charizard", "number": "4/102", "set_name": "",
            "language": "en", "first_edition": False, "confidence": 0.9}
    return {**base, **kw}


def test_split_number():
    assert _split_number("004/102") == ("4", "102")
    assert _split_number("TG23/TG30") == ("TG23", "TG30")
    assert _split_number("No.006") == ("6", None)
    assert _split_number("") == (None, None)


def test_exact_number_and_total_match():
    lk = FakeLookup([card("a", "4", 102, "Base"), card("b", "4", 110, "Legendary Collection")])
    r = identify_card(G(), lk)
    assert r["status"] == "matched" and r["card"]["id"] == "a"


def test_name_alone_is_never_enough():
    lk = FakeLookup([card("a", "6", 102, "Base")])
    r = identify_card(G(number="4/102"), lk)
    assert r["status"] == "unmatched" and r["card"] is None


def test_ambiguous_printings_are_flagged():
    lk = FakeLookup([card("a", "4", 102, "Base"), card("b", "4", 102, "Base Set 2")])
    r = identify_card(G(), lk)
    assert r["status"] == "ambiguous" and "Check it" in r["note"]


def test_set_name_breaks_ties():
    lk = FakeLookup([card("a", "4", 102, "Base"), card("b", "4", 102, "Base Set 2")])
    r = identify_card(G(set_name="Base Set 2"), lk)
    assert r["card"]["id"] == "b"


def test_non_english_cards_get_no_english_price():
    r = identify_card(G(language="ja", number="No.006", name_printed="リザードン"), FakeLookup([card("a", "6", 102, "Base")]))
    assert r["status"] == "non_english" and r["card"] is None


def test_low_confidence_is_unreadable():
    assert identify_card(G(confidence=0.2), FakeLookup([]))["status"] == "unreadable"
    assert identify_card(None, FakeLookup([]))["status"] == "unreadable"


def test_first_edition_stamp_picks_the_right_price():
    prices = {"1stEditionHolofoil": {"market": 133.0}, "unlimitedHolofoil": {"market": 44.0}}
    lk = FakeLookup([card("a", "4", 102, "Base", prices)])
    assert identify_card(G(first_edition=True), lk)["card"]["raw_price"] == 133.0
    assert identify_card(G(first_edition=False), lk)["card"]["raw_price"] == 44.0


def test_pricecharting_page_parsing():
    from gradeorskip.providers.prices import _pc_prices
    html = """
      <td id="used_price">
        <span class="price js-price">
        $19.27
        </span></td>
      <td id="complete_price"><span class="price js-price">$18.00</span></td>
      <td id="new_price"><span class="price js-price">$22.63</span></td>
      <td id="graded_price" class="tablet-portrait-hidden"><span class="price js-price">$1,029.15</span></td>
      <td id="manual_only_price" class="tablet-portrait-hidden"><span class="price js-price" title="x">$60.00</span></td>
    """
    assert _pc_prices(html) == {"raw": 19.27, 7: 18.0, 8: 22.63, 9: 1029.15, 10: 60.0}


def test_pricecharting_match_requires_language_and_number(monkeypatch):
    from gradeorskip.providers import prices
    search = ('<a href="/game/pokemon-korean-mega-dream-ex/mega-gengar-ex-230">k</a>'
              '<a href="https://www.pricecharting.com/game/pokemon-japanese-mega-dream-ex/mega-gengar-ex-230">j</a>'
              '<a href="/game/pokemon-japanese-mega-dream-ex/mega-gengar-ex-23">wrong number</a>')
    page = '<title>Mega Gengar ex #230 | x</title><td id="used_price"><span class="price js-price">$19.27</span></td>'
    def fake(url, params=None):
        return (search, url) if "search" in url else (page, url)
    monkeypatch.setattr(prices, "_get_html", fake)
    r = prices.pricecharting_lookup("Mega Gengar ex", "230/193", "ja")
    assert r["slug"] == "pokemon-japanese-mega-dream-ex/mega-gengar-ex-230" and r["prices"]["raw"] == 19.27
    assert prices.pricecharting_lookup("Mega Gengar ex", "230/193", "zh") is None
    assert prices.pricecharting_lookup("Mega Gengar ex", "231/193", "ja") is None


def test_pricecharting_match_requires_the_name(monkeypatch):
    """A number-only match once priced an Oricorio ex promo as M Blastoise ex #22."""
    from gradeorskip.providers import prices
    search = ('<a href="/game/pokemon-evolutions/m-blastoise-ex-22">b</a>'
              '<a href="/game/pokemon-promo/oricorio-ex-24">o</a>'
              '<a href="/game/pokemon-phantasmal-flames/oricorio-ex-110">o2</a>'
              '<a href="/game/pokemon-japanese-inferno-x/oricorio-ex-111">j</a>')
    page = '<title>x</title><td id="used_price"><span class="price js-price">$9.99</span></td>'
    monkeypatch.setattr(prices, "_get_html", lambda url, params=None: (search, url) if "search" in url else (page, url))
    # Misread number: no Oricorio #22, so fall back to name + set ("Promo") -> the one promo listing.
    assert prices.pricecharting_lookup("Oricorio ex", "022", "en", "Promo")["slug"] == "pokemon-promo/oricorio-ex-24"
    # Same misread with no set hint: two English Oricorio ex listings -> refuse to guess.
    assert prices.pricecharting_lookup("Oricorio ex", "022", "en") is None
    assert prices.pricecharting_lookup("Oricorio ex", "24", "en")["slug"] == "pokemon-promo/oricorio-ex-24"


MEWTWO_SEARCH = ('<a href="/game/pokemon-brilliant-stars/mewtwo-56">a</a>'
                 '<a href="/game/pokemon-brilliant-stars/mewtwo-reverse-holo-56">b</a>'
                 '<a href="/game/pokemon-trick-or-trade-2022/mewtwo-holo-56">c</a>'
                 '<a href="/game/pokemon-brilliant-stars/mewtwo-stamped-56">d</a>')


def _mewtwo_site(monkeypatch):
    from gradeorskip import providers
    page = '<title>x</title><td id="used_price"><span class="price js-price">$1.67</span></td>'
    monkeypatch.setattr(providers.prices, "_get_html",
                        lambda url, params=None: (MEWTWO_SEARCH, url) if "search" in url else (page, url))
    return providers.prices


def test_stamp_picks_the_stamped_printing(monkeypatch):
    prices = _mewtwo_site(monkeypatch)
    r = prices.pricecharting_lookup("Mewtwo", "056/172", "en", "Brilliant Stars", False, "Trick or Trade pumpkin")
    assert r["slug"] == "pokemon-trick-or-trade-2022/mewtwo-holo-56" and r["stamp_matched"]


def test_no_stamp_prefers_the_plain_printing_and_offers_the_rest(monkeypatch):
    prices = _mewtwo_site(monkeypatch)
    r = prices.pricecharting_lookup("Mewtwo", "056/172", "en", "Brilliant Stars")
    assert r["slug"] == "pokemon-brilliant-stars/mewtwo-56" and not r["stamp_matched"]
    assert len(r["options"]) == 4 and any("Trick Or Trade" in o["label"] for o in r["options"])


def test_unknown_stamp_is_not_claimed_as_matched(monkeypatch):
    prices = _mewtwo_site(monkeypatch)
    assert not prices.pricecharting_lookup("Mewtwo", "56", "en", "Brilliant Stars", False, "Worlds 2022")["stamp_matched"]
