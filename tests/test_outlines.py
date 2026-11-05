"""The live camera sends where it saw the card; the server searches for the edges right there."""
import json

from fastapi.testclient import TestClient

from gradeorskip import app as app_module
from gradeorskip import pipeline
from gradeorskip.centering import outline_prior

from conftest import FIXTURES

SQUARE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


def test_outline_prior_orders_corners_and_rejects_junk():
    # Any corner order, slight rotation: each side sits at the midpoint of its two corners.
    box = outline_prior([[0.9, 0.08], [0.1, 0.09], [0.11, 0.92], [0.91, 0.91]])
    assert [round(float(v), 3) for v in box] == [0.105, 0.085, 0.905, 0.915]
    assert outline_prior("junk") is None
    assert outline_prior([[0, 0], [1, 1], [0, 1], [5, 5]]) is None  # far outside the photo
    assert outline_prior([[0.4, 0.4], [0.5, 0.4], [0.5, 0.5], [0.4, 0.5]]) is None  # too small to be the card


def test_parse_outlines_keeps_only_well_formed_frames():
    raw = json.dumps({"front": [SQUARE, None, "x", [[1, 2]]], "back": "no", "other": [SQUARE]})
    assert pipeline.parse_outlines(raw) == {"front": [SQUARE, None, None, None]}
    assert pipeline.parse_outlines("not json") == {} and pipeline.parse_outlines("[1, 2]") == {}
    assert pipeline.parse_outlines(json.dumps({"front": [[[True, 0], [1, 0], [1, 1], [0, 1]]]})) == {"front": [None]}


def test_check_route_passes_outlines_to_the_pipeline(monkeypatch):
    seen = {}
    real = app_module.run_check

    def spy(*a, **k):
        seen.update(k.get("outlines") or {})
        return real(*a, **{**k, "use_llm": False})

    monkeypatch.setattr(app_module, "run_check", spy)
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg")}
    r = TestClient(app_module.app).post("/check", files=files, data={"outlines": json.dumps({"front": [SQUARE]})},
                                        follow_redirects=False)
    assert r.status_code == 303 and seen == {"front": [SQUARE]}


def test_in_hand_condition_regrades_and_resets(monkeypatch):
    from gradeorskip import db
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    client = TestClient(app_module.app)
    files = {"front": ("f.jpg", (FIXTURES / "lr_60_40_front.jpg").read_bytes(), "image/jpeg")}
    cid = client.post("/check", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/check/{cid}").text
    assert "What you see in hand" in page and 'value="" selected' in page
    r = client.post(f"/check/{cid}/condition", data={"corners": "major", "edges": "", "surface": "none"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#condition")
    assert db.get_check(cid)["prices"]["in_hand"] == {"corners": "major", "surface": "none"}
    page = client.get(f"/check/{cid}").text
    assert "Corners (seen in hand): major" in page and 'value="major" selected' in page
    client.post(f"/check/{cid}/condition", data={})
    assert db.get_check(cid)["prices"]["in_hand"] == {}
    assert client.post(f"/check/{cid}/condition", data={"corners": "pristine"}).status_code == 400
