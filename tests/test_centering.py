import json

import cv2
import numpy as np
import pytest

from conftest import FIXTURES
from gradeorskip.centering import CardNotFound, _ratio, measure_borders, measure_centering
from gradeorskip.synth import make_card, photograph

LABELS = json.loads((FIXTURES / "labels.json").read_text())
TOL = 2.5  # percentage points


def test_ratio_math():
    assert _ratio(30, 30) == (50.0, 50.0)
    assert _ratio(36, 24) == (60.0, 40.0)
    assert _ratio(0, 0) == (50.0, 50.0)
    assert sum(_ratio(17, 4)) == pytest.approx(100.0)


def test_flat_card_exact():
    """On an unwarped, noise-free render the borders are recovered almost exactly."""
    card = make_card(40, 20, 25, 35, "front", seed=3)
    b, conf, warnings = measure_borders(card)
    assert abs(b.left - 40) <= 1 and abs(b.right - 20) <= 1
    assert abs(b.top - 25) <= 1 and abs(b.bottom - 35) <= 1
    assert conf >= 0.9 and not warnings


@pytest.mark.parametrize("name", sorted(LABELS))
@pytest.mark.parametrize("side", ["front", "back"])
def test_fixture_photos(name, side):
    label = LABELS[name][side]
    img = cv2.imread(str(FIXTURES / label["file"]))
    res, card, overlay = measure_centering(img)
    assert res.detection == "contour"
    assert abs(res.lr[0] - label["lr"][0]) <= TOL, (res.lr, label["lr"])
    assert abs(res.tb[0] - label["tb"][0]) <= TOL, (res.tb, label["tb"])
    assert card.shape[:2] == (880, 630)
    assert overlay.shape == card.shape


def test_landscape_photo_is_rotated_upright():
    img = photograph(make_card(36, 24, 30, 30, "front", seed=9), angle=90, seed=9, canvas=(1200, 1600))
    res, card, _ = measure_centering(img)
    assert card.shape[:2] == (880, 630)
    # Rotated 90°, so the 60/40 offset now shows up on one of the two axes.
    assert max(abs(res.lr[0] - 50), abs(res.tb[0] - 50)) == pytest.approx(10, abs=TOL)


def test_no_card_raises():
    noise = np.random.default_rng(0).integers(0, 255, (600, 1200, 3), dtype=np.uint8)
    with pytest.raises(CardNotFound):
        measure_centering(noise)


def _guided_crop(borders, side="front", sleeve=True):
    """What the live camera uploads: the card on the guide lines (8% margins) on a busy
    background, optionally inside a penny sleeve whose faint edge sits just outside the card."""
    rng = np.random.default_rng(5)
    W, H = 1000, 1400
    bg = cv2.resize(rng.integers(60, 140, (H // 10, W // 10, 3), dtype=np.uint8), (W, H), interpolation=cv2.INTER_CUBIC)
    x0, y0, x1, y1 = int(0.08 * W), int(0.08 * H), int(0.92 * W), int(0.92 * H)
    if sleeve:  # slightly lighter plastic, 2.5% bigger than the card
        sx, sy = int(0.025 * W), int(0.025 * H)
        bg[y0 - sy:y1 + sy, x0 - sx:x1 + sx] = np.clip(bg[y0 - sy:y1 + sy, x0 - sx:x1 + sx].astype(int) + 18, 0, 255)
    card = cv2.resize(make_card(*borders, side=side, seed=2), (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)
    bg[y0:y1, x0:x1] = card
    return cv2.GaussianBlur(bg, (3, 3), 0.8)


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("borders", [(36, 24, 30, 30), (30, 30, 37, 23), (27, 33, 30, 30)])
def test_guided_capture_in_sleeve_on_busy_background(borders, side):
    res, card, _ = measure_centering(_guided_crop(borders, side), guided=True)
    assert res.detection == "lines-guided"
    assert abs(res.lr[0] - _ratio(borders[0], borders[1])[0]) <= TOL, res.lr
    assert abs(res.tb[0] - _ratio(borders[2], borders[3])[0]) <= TOL, res.tb
