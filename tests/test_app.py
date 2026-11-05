import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES
from gradeorskip import app as app_module

client = TestClient(app_module.app)


def test_end_to_end_centering_only(monkeypatch):
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {
        "front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg"),
        "back": ("b.jpg", (FIXTURES / "lr_60_40_back.jpg").read_bytes(), "image/jpeg"),
    }
    r = client.post("/check", files=files, data={"label": "test card"}, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    page = client.get(url)
    assert page.status_code == 200 and "Estimated grade" in page.text and "Centering" in page.text
    data = client.get("/api/check/" + url.rsplit("/", 1)[1]).json()
    assert data["result"]["grade"]["assessed"] is False
    assert data["roi"]["verdict"] == "Need prices"

    cid = data["id"]
    r = client.post(f"/check/{cid}/prices", data={"raw": "40", "psa10": "250", "psa9": "90", "psa8": "55",
                                                  "psa7": "", "tier": "standard", "shipping": "8", "sell_fee": "0",
                                                  "fee_override": ""}, follow_redirects=False)
    assert r.status_code == 303
    data = client.get(f"/api/check/{cid}").json()
    assert data["roi"]["ok"] and data["roi"]["verdict"] in {"Grade it", "Marginal", "Don't grade"}
    assert "What you paid doesn't change this" in client.get(f"/check/{cid}").text
    assert client.get(f"/files/{cid}/front_overlay.jpg").status_code == 200


def test_mock_provider_flow(monkeypatch):
    monkeypatch.setenv("GRADEORSKIP_LLM_PROVIDER", "mock")
    files = {"front": ("f.jpg", (FIXTURES / "perfect_front.jpg").read_bytes(), "image/jpeg")}
    r = client.post("/check", files=files, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    data = client.get(f"/api/check/{cid}").json()
    assert data["result"]["grade"]["assessed"] is True
    assert "DEMO MODE" in client.get(f"/check/{cid}").text


def test_bad_image():
    r = client.post("/check", files={"front": ("x.jpg", b"not an image", "image/jpeg")}, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    data = client.get(f"/api/check/{cid}").json()
    assert data["result"]["grade"] is None and data["result"]["errors"]


def test_file_route_rejects_traversal():
    assert client.get("/files/abc/..%2F..%2Fetc.jpg").status_code == 404


def test_unreliable_centering_does_not_cap_grade(monkeypatch):
    from gradeorskip import pipeline
    real = pipeline.measure_centering

    def shaky(img, guided=False, outline=None):
        res, card, ov = real(img, guided)
        res.confidence = 0.3
        return res, card, ov

    monkeypatch.setattr(pipeline, "measure_centering", shaky)
    files = {"front": ("f.jpg", (FIXTURES / "lr_75_25_front.jpg").read_bytes(), "image/jpeg")}
    r = client.post("/check", files=files, follow_redirects=False)
    g = client.get("/api/check/" + r.headers["location"].rsplit("/", 1)[1]).json()["result"]["grade"]
    assert g["centering_cap"]["grade"] == 10  # a 75/25 reading would cap at 7 if trusted
    assert any("unreliable" in e for e in g["explanation"])


def test_use_this_card_without_a_pick_redirects_with_message():
    files = {"front": ("f.jpg", (FIXTURES / "perfect_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/check/{cid}/card", data={"card_id": ""}, follow_redirects=False)
    assert r.status_code == 303 and "msg=pick" in r.headers["location"]
    assert "Tap a card in the results first" in client.get(r.headers["location"]).text


def test_condition_still_runs_when_card_outline_not_found(monkeypatch):
    """Regression: a back photo with no detectable outline used to skip the condition read."""
    import numpy as np
    from gradeorskip import pipeline
    from gradeorskip.centering import CardNotFound
    monkeypatch.setenv("GRADEORSKIP_LLM_PROVIDER", "mock")
    real = pipeline.measure_centering
    calls = {"n": 0}

    def flaky(img, guided=False, outline=None):
        calls["n"] += 1
        if calls["n"] == 2:  # the back
            raise CardNotFound("no outline")
        return real(img, guided)

    monkeypatch.setattr(pipeline, "measure_centering", flaky)
    f = (FIXTURES / "perfect_front.jpg").read_bytes()
    b = (FIXTURES / "perfect_back.jpg").read_bytes()
    r = pipeline.run_check(f, b)
    assert "condition" in r["sides"]["back"] and "error" in r["sides"]["back"]
    assert any("outline wasn't found in the back" in e for e in r["grade"]["explanation"])


def test_picking_a_printing_updates_the_raw_price(monkeypatch):
    card = {"id": "x", "name": "Brock's Rhydon", "set": "Gym Heroes", "number": "2", "printed_total": 132,
            "raw_price": 43.76, "raw_variant": "unlimitedHolofoil", "raw_source": "TCGplayer market",
            "raw_updated": "2026/10/15", "image": None,
            "all_prices": {"1stEditionHolofoil": {"market": 133.36}, "unlimitedHolofoil": {"market": 43.76}}}
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (card, []))
    files = {"front": ("f.jpg", (FIXTURES / "perfect_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, data={"card_id": "x"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/check/{cid}").text
    assert "1st Edition Holofoil: $133.36" in page and "Unlimited Holofoil: $43.76" in page
    client.post(f"/check/{cid}/variant", data={"variant": "1stEditionHolofoil"})
    d = client.get(f"/api/check/{cid}").json()
    assert d["prices"]["raw"] == 133.36 and "picked by you" in d["prices"]["raw_source"]
    assert d["roi"]["breakeven"] > 133.36


def test_access_gate_only_applies_to_tunnel_traffic(monkeypatch):
    monkeypatch.setenv("GRADEORSKIP_ACCESS_KEY", "s3cret")
    tunnel = {"cf-ray": "abc123"}
    assert client.get("/").status_code == 200                       # local / LAN: no cf-ray
    assert client.get("/", headers=tunnel).status_code == 401
    assert client.get("/history", headers=tunnel).status_code == 401
    assert client.get("/?key=wrong", headers=tunnel).status_code == 401
    r = client.get("/?key=s3cret", headers=tunnel, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "gos_key=s3cret" in r.headers["set-cookie"]
    assert client.get("/", headers=tunnel, cookies={"gos_key": "s3cret"}).status_code == 200
    assert client.get("/healthz", headers=tunnel).status_code == 200


def test_missing_raw_price_comes_from_pricecharting(monkeypatch):
    """TCGdex knows the MEP Oricorio ex promo but has no market price: the check said
    'need prices' although PriceCharting had the ungraded price."""
    from gradeorskip import app as app_mod
    listing = {"url": "https://example.test/oricorio-ex-24", "prices": {"raw": 9.99, 7: 11.0, 10: 78.15}}
    monkeypatch.setattr(app_mod, "pricecharting_for_card", lambda card: listing)
    monkeypatch.setattr(app_mod, "graded_prices", lambda card: ({7: 11.0, 10: 78.15}, "PriceCharting"))
    card = {"id": "tcgdex:mep-024", "name": "Oricorio ex", "set": "MEP Black Star Promos", "number": "024",
            "raw_price": None}
    p = app_mod._default_prices(card)
    assert p["raw"] == 9.99 and "ungraded" in p["raw_source"]


def test_burst_frames_use_the_one_that_measures_best(monkeypatch):
    """The phone uploads its best few burst frames; a frame where the card can't be found
    (here: a blank one sent as the primary) must not win over a good one."""
    import cv2
    import numpy as np
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    blank = cv2.imencode(".jpg", np.full((900, 650, 3), 40, np.uint8))[1].tobytes()
    good = (FIXTURES / "lr_60_40_front.jpg").read_bytes()
    files = [("front", ("f.jpg", blank, "image/jpeg")), ("front_alt", ("a.jpg", good, "image/jpeg"))]
    r = client.post("/check", files=files, follow_redirects=False)
    data = client.get("/api/check/" + r.headers["location"].rsplit("/", 1)[1]).json()
    front = data["result"]["sides"]["front"]
    assert front["frames"] == {"used": 1, "of": 2}
    assert abs(front["centering"]["lr"][0] - 60) <= 2


def test_service_picker_shows_each_tier_and_switches(monkeypatch):
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    client.post(f"/check/{cid}/prices", data={"raw": "40", "psa10": "400", "psa9": "120", "psa8": "60", "psa7": "45",
                                              "shipping": "8", "sell_fee": "0", "fee_override": ""})
    page = client.get(f"/check/{cid}").text
    assert 'class="tiers"' in page and "Standard" in page and "Express" in page
    assert client.get(f"/api/check/{cid}").json()["tier"]["id"] == "value"  # cheapest open tier by default
    cheap = client.get(f"/api/check/{cid}").json()["roi"]["gain"]
    client.post(f"/check/{cid}/tier", data={"tier": "express"})
    data = client.get(f"/api/check/{cid}").json()
    assert data["tier"]["id"] == "express" and data["roi"]["gain"] < cheap
    assert client.post(f"/check/{cid}/tier", data={"tier": "bogus"}).status_code == 400


def _priced_check(monkeypatch, card):
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_55_45_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    client.post(f"/check/{cid}/prices", data={"raw": "1", "psa10": "900", "psa9": "400", "psa8": "300", "psa7": "200",
                                              "tier": "standard", "shipping": "8", "sell_fee": "0", "fee_override": ""})
    app_module.db.update_check(cid, card=card)
    return cid


def test_unmatched_stamp_cannot_say_grade_it(monkeypatch):
    card = {"name": "Mewtwo", "set": "x", "number": "56", "stamp": "Worlds", "printing_unconfirmed": True,
            "pc_options": [{"slug": "a/b-56", "label": "A"}, {"slug": "c/d-56", "label": "C"}], "pc_slug": "a/b-56"}
    cid = _priced_check(monkeypatch, card)
    roi = client.get(f"/api/check/{cid}").json()["roi"]
    assert roi["verdict"] == "Need prices" and "stamped" in roi["reason"]
    assert "looks stamped" in client.get(f"/check/{cid}").text


def test_picking_a_printing_reprices_the_check(monkeypatch):
    card = {"name": "Mewtwo", "set": "x", "number": "56", "printing_unconfirmed": True,
            "pc_options": [{"slug": "a/b-56", "label": "A"}, {"slug": "c/d-56", "label": "C"}], "pc_slug": "a/b-56"}
    cid = _priced_check(monkeypatch, card)
    monkeypatch.setattr(app_module, "pricecharting_listing", lambda slug: {
        "slug": slug, "url": "https://example.test/x", "prices": {"raw": 1.67, 7: 12.0, 8: 14.37, 9: 25.88, 10: 240.0}})
    assert client.post(f"/check/{cid}/printing", data={"slug": "not/offered-1"}).status_code == 400
    client.post(f"/check/{cid}/printing", data={"slug": "c/d-56"})
    data = client.get(f"/api/check/{cid}").json()
    assert data["prices"]["raw"] == 1.67 and data["prices"]["graded"]["10"] == 240.0
    assert data["card"]["printing_confirmed"] and data["roi"]["verdict"] == "Don't grade"


def test_paused_service_cannot_be_chosen(monkeypatch):
    cid = _priced_check(monkeypatch, {"name": "x", "set": "y", "number": "1"})
    assert client.post(f"/check/{cid}/tier", data={"tier": "value_bulk"}).status_code == 400
    client.post(f"/check/{cid}/prices", data={"raw": "1", "tier": "value_bulk", "shipping": "8", "sell_fee": "0"})
    assert client.get(f"/api/check/{cid}").json()["tier"]["id"] != "value_bulk"
    assert "Value Bulk" not in client.get(f"/check/{cid}").text


def test_tag_is_off_by_default(monkeypatch):
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    assert client.post(f"/check/{cid}/grader", data={"grader": "tag"}).status_code == 400
    assert 'class="seg"' not in client.get(f"/check/{cid}").text


def test_switching_to_tag_recalculates_with_its_own_fees_and_comps(monkeypatch):
    monkeypatch.setattr(app_module, "TAG_ENABLED", True)
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    client.post(f"/check/{cid}/prices", data={"raw": "40", "psa10": "400", "psa9": "120", "psa8": "60", "psa7": "45",
                                              "shipping": "8", "sell_fee": "0", "fee_override": ""})
    client.post(f"/check/{cid}/grader", data={"grader": "tag"})
    d = client.get(f"/api/check/{cid}").json()
    assert d["tier"]["id"] == "standard" and d["tier"]["fee"] == 59.95
    assert d["roi"]["verdict"] == "Need prices"  # PSA comps don't price a TAG slab
    page = client.get(f"/check/{cid}").text
    assert "TAG service" in page and "TAG 10" in page and 'name="tag10"' in page
    client.post(f"/check/{cid}/prices", data={"raw": "40", "tag10": "300", "tag9": "100", "tag8": "60", "tag7": "45",
                                              "shipping": "8", "sell_fee": "0", "fee_override": ""})
    d = client.get(f"/api/check/{cid}").json()
    assert d["roi"]["ok"] and d["prices"]["graded"]["10"] == 400  # PSA comps kept
    assert client.post(f"/check/{cid}/grader", data={"grader": "bogus"}).status_code == 400


def _priced(monkeypatch, **prices):
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg"),
             "back": ("b.jpg", (FIXTURES / "lr_60_40_back.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    form = {"raw": "", "psa10": "", "psa9": "", "psa8": "", "psa7": "", "tier": "value", "shipping": "8", "sell_fee": "0",
            "fee_override": "", **prices}
    client.post(f"/check/{cid}/prices", data=form, follow_redirects=False)
    return client.get(f"/check/{cid}").text


def test_yes_panel_explains_a_yes(monkeypatch):
    page = _priced(monkeypatch, raw="10", psa10="1000", psa9="500", psa8="300", psa7="200")
    assert "Why this is a yes" in page and "Stays a yes until total costs reach" in page


def test_yes_panel_says_when_no_service_helps(monkeypatch):
    page = _priced(monkeypatch, raw="40", psa10="45", psa9="42", psa8="41", psa7="40")
    assert "What would make this a yes" in page and "No service makes this a yes" in page


def test_yes_view_offers_a_cheaper_open_service():
    roi = {"ok": True, "verdict": "Marginal", "cost": 67.99, "shipping": 8, "yes_path": {"max_cost": 50, "pays_at": 9, "top_grade": 10, "top_adds": 90}}
    opts = [{"id": "standard", "name": "Standard", "fee": 59.99, "verdict": "Marginal"}, {"id": "value", "name": "Value", "fee": 39.99, "verdict": "Grade it"}]
    view = app_module._yes_view({"prices": {}}, roi, {"id": "standard"}, opts)
    assert view["state"] == "switch" and view["tier"]["id"] == "value"


def test_yes_view_names_a_paused_service_that_would_work():
    roi = {"ok": True, "verdict": "Marginal", "cost": 47.99, "shipping": 8, "yes_path": {"max_cost": 33.5, "pays_at": 10, "top_grade": 10, "top_adds": 90}}
    opts = [{"id": "value", "name": "Value", "fee": 39.99, "verdict": "Marginal"}]
    view = app_module._yes_view({"prices": {"graded_source": "PriceCharting"}}, roi, {"id": "value"}, opts)
    assert view["state"] == "cheaper" and view["paused"]["id"] == "value_bulk" and view["check_sold"]
    assert view["cheapest"] == "Value" and view["cheapest_total"] == pytest.approx(47.99)


def test_yes_view_treats_a_tiny_budget_as_no_route():
    roi = {"ok": True, "verdict": "Don't grade", "cost": 47.99, "shipping": 8, "yes_path": {"max_cost": 3, "pays_at": None, "top_grade": 10, "top_adds": 40}}
    assert app_module._yes_view({"prices": {}}, roi, {"id": "value"}, [])["state"] == "none"
