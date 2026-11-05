"""PriceCharting's API answers with its closest match even when that's another card: only this card's price counts."""
from gradeorskip.providers import prices

PIKA = {"status": "success", "product-name": "Pikachu ex #179", "console-name": "Pokemon Prismatic Evolutions",
        "loose-price": 4888, "cib-price": 4100, "new-price": 5100, "graded-price": 6832, "manual-only-price": 31000}


def test_fields_map_to_grades_and_a_different_card_counts_as_no_price(monkeypatch):
    monkeypatch.setenv("PRICECHARTING_API_KEY", "k")
    monkeypatch.setattr(prices, "_pace", lambda: None)
    monkeypatch.setattr(prices, "_LISTINGS", {})
    monkeypatch.setattr(prices, "_get_json", lambda *a, **k: PIKA)
    found, source = prices.graded_prices({"name": "Pikachu ex", "set": "Prismatic Evolutions", "number": "179"})
    assert found == {10: 310.0, 9: 68.32, 8: 51.0, 7: 41.0} and source == "PriceCharting API"
    found, _ = prices.graded_prices({"name": "Pikachu ex", "set": "Prismatic Evolutions", "number": "999"})
    assert found == {}


def test_same_card_rules():
    assert prices.is_same_card({"name": "Mega Charizard X ex", "number": "023"},
                               {"product-name": "Mega Charizard X EX #23", "console-name": "Pokemon Promo"})
    assert prices.is_same_card({"name": "Mega Gengar ex", "number": "230", "language": "ja"},
                               {"product-name": "Mega Gengar ex #230", "console-name": "Pokemon Japanese Mega Dream ex"})
    assert not prices.is_same_card({"name": "Mega Gengar ex", "number": "230"},  # an English card never takes a Japanese price
                                   {"product-name": "Mega Gengar ex #230", "console-name": "Pokemon Japanese Mega Dream ex"})
    assert not prices.is_same_card({"name": "Pikachu ex", "number": "17"}, PIKA)
    assert not prices.is_same_card({"name": "Pikachu ex", "number": "179"}, {**PIKA, "status": "error"})


def test_backfill_fills_missing_prices_and_never_touches_typed_ones(monkeypatch):
    from gradeorskip import backfill, db
    monkeypatch.setenv("PRICECHARTING_API_KEY", "k")
    monkeypatch.setattr(prices, "_pace", lambda: None)
    monkeypatch.setattr(prices, "_LISTINGS", {})
    monkeypatch.setattr(prices, "_get_json", lambda *a, **k: PIKA)
    seen = {"name_en": "Pikachu ex", "number": "179/131", "confidence": 0.95, "language": "en"}
    base = {"sides": {}, "errors": [], "identified": seen}
    db.save_check({**base, "id": "bf0000000001"}, card=None, prices=None)                 # never priced
    db.save_check({**base, "id": "bf0000000002"}, card=None,
                  prices={"raw": 50.0, "raw_source": "entered by you", "graded": {"10": 999.0}, "graded_source": "entered by you"})
    db.save_check({**base, "id": "bf0000000003", "identified": {}}, card=None, prices=None)  # nothing to look up
    counts = backfill.backfill(apply=False, log=lambda *_: None)
    assert counts["priced"] == 1 and db.get_check("bf0000000001")["prices"] in ({}, None)  # a preview writes nothing
    backfill.backfill(apply=True, log=lambda *_: None)
    p = db.get_check("bf0000000001")["prices"]
    assert p["graded"] == {"10": 310.0, "9": 68.32, "8": 51.0, "7": 41.0} and p["graded_source"] == "PriceCharting API"
    assert p["raw"] == 48.88 and p["tier"]  # a full price sheet, not just the prices
    typed = db.get_check("bf0000000002")["prices"]
    assert typed["raw"] == 50.0 and typed["graded"] == {"10": 999.0}
    assert db.get_check("bf0000000003")["prices"] in ({}, None)
