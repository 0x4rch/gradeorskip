"""Synthetic card photos with known border widths, for tests and demos.

These are not real Pokémon cards: just a coloured border around a busy
"art" area, photographed (simulated) at a slight angle on a background.
"""
from __future__ import annotations

import cv2
import numpy as np

from .centering import CARD_W, CARD_H

FRONT_BORDER = (40, 205, 245)  # BGR yellow
BACK_BORDER = (150, 70, 25)    # BGR blue


def make_card(left: int, right: int, top: int, bottom: int, side: str = "front", seed: int = 0) -> np.ndarray:
    """Render a flat CARD_W x CARD_H card whose printed border widths are exactly as given (px)."""
    rng = np.random.default_rng(seed)
    border = FRONT_BORDER if side == "front" else BACK_BORDER
    card = np.full((CARD_H, CARD_W, 3), border, dtype=np.uint8)
    x0, y0, x1, y1 = left, top, CARD_W - right, CARD_H - bottom
    if side == "front":
        inner = np.full((y1 - y0, x1 - x0, 3), (200, 200, 205), dtype=np.uint8)  # silver frame
        ih, iw = inner.shape[:2]
        art = rng.integers(0, 255, size=(ih // 8, iw // 8, 3), dtype=np.uint8)
        art = cv2.resize(art, (iw - 24, int(ih * 0.45)), interpolation=cv2.INTER_LINEAR)
        inner[30:30 + art.shape[0], 12:12 + art.shape[1]] = art
        for k in range(6):  # text lines
            y = int(ih * 0.55) + k * 40
            cv2.line(inner, (20, y), (iw - 20, y), (40, 40, 40), 3)
    else:
        inner = np.full((y1 - y0, x1 - x0, 3), (110, 40, 10), dtype=np.uint8)
        ih, iw = inner.shape[:2]
        cv2.circle(inner, (iw // 2, ih // 2), min(iw, ih) // 3, (30, 30, 200), -1)
        cv2.circle(inner, (iw // 2, ih // 2), min(iw, ih) // 9, (240, 240, 240), -1)
        noise = rng.normal(0, 12, inner.shape)
        inner = np.clip(inner + noise, 0, 255).astype(np.uint8)
    card[y0:y1, x0:x1] = inner
    # mild print noise on the border
    card = np.clip(card.astype(np.float32) + rng.normal(0, 3, card.shape), 0, 255).astype(np.uint8)
    return card


def photograph(card: np.ndarray, angle: float = 4.0, perspective: float = 0.03, seed: int = 0,
               canvas: tuple[int, int] = (1600, 1200)) -> np.ndarray:
    """Place a flat card on a textured background with rotation, perspective, blur and JPEG noise."""
    rng = np.random.default_rng(seed + 1000)
    H, W = canvas
    bg = rng.integers(20, 60, size=(H // 20, W // 20, 3), dtype=np.uint8)
    bg = cv2.resize(bg, (W, H), interpolation=cv2.INTER_CUBIC)
    h, w = card.shape[:2]
    scale = 0.75 * H / h
    cw, ch = w * scale, h * scale
    cx, cy = W / 2, H / 2
    base = np.array([[-cw / 2, -ch / 2], [cw / 2, -ch / 2], [cw / 2, ch / 2], [-cw / 2, ch / 2]])
    t = np.deg2rad(angle)
    rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    dst = base @ rot.T
    dst += rng.uniform(-perspective, perspective, size=(4, 2)) * np.array([cw, ch])
    dst += np.array([cx, cy])
    src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
    warped = cv2.warpPerspective(card, m, (W, H), flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), m, (W, H))
    out = np.where(mask[..., None] > 127, warped, bg)
    out = cv2.GaussianBlur(out, (3, 3), 0.8)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)
