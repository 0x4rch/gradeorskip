"""Regression set: real iPhone scans. Expected values are repeat-scan consensus and overlays
checked by eye, not graded ground truth. `null` means the card must be reported as unmeasurable."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from gradeorskip.centering import CardNotFound, Borders, corners_intact, measure_centering, verify_borders

REAL = Path(__file__).parent / "fixtures" / "real"
if not (REAL / "labels.json").exists():  # real scans aren't published: card art is copyrighted
    pytest.skip("no real-scan fixtures", allow_module_level=True)
LABELS = json.loads((REAL / "labels.json").read_text())


@pytest.mark.parametrize("name", sorted(LABELS))
def test_real_scan(name):
    img = cv2.imread(str(REAL / f"{name}.jpg"))
    want = LABELS[name]
    try:
        res, _, _ = measure_centering(img, guided=True)
    except CardNotFound:
        assert want is None
        return
    if want is None:
        assert res.confidence < 0.6, f"{name}: a bad read must not be trusted ({res.lr[0]:.0f}/{res.tb[0]:.0f})"
    else:
        assert res.confidence >= 0.6 and res.crop_ok and res.verified
        assert abs(res.lr[0] - want[0]) <= 2 and abs(res.tb[0] - want[1]) <= 2


def test_same_card_scanned_twice_agrees():
    for side in ("front", "back"):
        a, _, _ = measure_centering(cv2.imread(str(REAL / f"oricorio1_{side}.jpg")), guided=True)
        b, _, _ = measure_centering(cv2.imread(str(REAL / f"oricorio2_{side}.jpg")), guided=True)
        assert abs(a.lr[0] - b.lr[0]) <= 2 and abs(a.tb[0] - b.tb[0]) <= 2


def _card(border=25):
    """A card on a dark mat, warped: rounded corners show the mat."""
    h, w = 880, 630
    card = np.full((h, w, 3), 40, np.uint8)
    cv2.rectangle(card, (20, 0), (w - 21, h - 1), (60, 200, 230), -1)
    cv2.rectangle(card, (0, 20), (w - 1, h - 21), (60, 200, 230), -1)
    for x, y in ((20, 20), (w - 21, 20), (20, h - 21), (w - 21, h - 21)):
        cv2.circle(card, (x, y), 20, (60, 200, 230), -1)
    cv2.rectangle(card, (border, border), (w - border, h - border), (90, 90, 200), -1)
    return card


def test_corner_check_spots_a_clipped_crop():
    card = _card()
    assert corners_intact(card)
    clipped = card.copy()
    clipped[-14:, -14:] = (60, 200, 230)  # the outline ran through the card here
    assert not corners_intact(clipped)


def test_a_frame_that_cannot_exist_fails_verification():
    card = _card()
    assert verify_borders(card, Borders(25, 25, 25, 25)) == []
    assert "balance" in verify_borders(card, Borders(25, 25, 80, 25))  # top latched onto a name bar


def test_rejected_read_shows_no_numbers(monkeypatch):
    from fastapi.testclient import TestClient
    from gradeorskip import app as app_module
    client = TestClient(app_module.app)
    monkeypatch.setattr(app_module.lookup, "get", lambda cid: (None, ["offline"]))
    files = {"front": ("f.jpg", (REAL / "mew1_front.jpg").read_bytes(), "image/jpeg")}
    url = client.post("/check", files=files, data={"guided": "front"}, follow_redirects=False).headers["location"]
    page = client.get(url).text
    assert "Not measured." in page and "front_overlay.jpg" not in page and "Left / right" not in page
