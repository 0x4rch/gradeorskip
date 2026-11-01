"""Deterministic centering measurement.

Pipeline: find the card's outer edge in the photo, warp it to a canonical
upright rectangle, then measure the printed border width on each side by
scanning inward from each edge until pixels stop looking like the border.

Everything here is plain OpenCV/numpy so results are reproducible: the same
photo always yields the same numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import cv2
import numpy as np

# Canonical warped size. Pokémon cards are 63 x 88 mm.
CARD_W, CARD_H = 630, 880


class CardNotFound(Exception):
    pass


@dataclass
class Borders:
    left: float
    right: float
    top: float
    bottom: float

    @property
    def lr(self) -> tuple[float, float]:
        return _ratio(self.left, self.right)

    @property
    def tb(self) -> tuple[float, float]:
        return _ratio(self.top, self.bottom)


@dataclass
class CenteringResult:
    borders_px: dict
    lr: tuple[float, float]  # (left %, right %)
    tb: tuple[float, float]  # (top %, bottom %)
    worst: float  # the larger share on the worse axis, e.g. 58.0 for 58/42
    detection: str  # "contour" or "full-frame"
    confidence: float  # 0-1, heuristic quality of the measurement
    warnings: list = field(default_factory=list)
    quad: list | None = None  # card corners in the input photo (tl, tr, br, bl)
    crop_ok: bool = True  # all four rounded corners are inside the crop (outline is sound)
    verified: bool = True  # the borders passed the physical checks in verify_borders

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lr_label"] = f"{self.lr[0]:.0f}/{self.lr[1]:.0f}"
        d["tb_label"] = f"{self.tb[0]:.0f}/{self.tb[1]:.0f}"
        return d


def _ratio(a: float, b: float) -> tuple[float, float]:
    total = a + b
    if total <= 0:
        return (50.0, 50.0)
    pa = 100.0 * a / total
    return (round(pa, 1), round(100.0 - pa, 1))


# ---------------------------------------------------------------- detection

def _order_corners(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl, br = pts[np.argmin(s)], pts[np.argmax(s)]
    tr, bl = pts[np.argmin(d)], pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def find_card_quad(img: np.ndarray) -> np.ndarray | None:
    """Return the 4 corners (tl, tr, br, bl) of the largest card-like quad."""
    h, w = img.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
    s = scale if scale < 1 else 1.0
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    area_img = small.shape[0] * small.shape[1]

    candidates = []
    # Edges on every colour channel (a blue card back on a dark table has
    # little grey-level contrast), plus Otsu masks on grey and saturation.
    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    edges = np.zeros(gray.shape, np.uint8)
    for ch in cv2.split(blurred):
        edges |= cv2.Canny(ch, 30, 100)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    sat = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)[..., 1]
    masks = [edges]
    for chan in (gray, sat):
        _, otsu = cv2.threshold(chan, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        otsu = cv2.morphologyEx(otsu, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        masks += [otsu, 255 - otsu]
    for mask in masks:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if area < 0.15 * area_img or area > 0.98 * area_img:
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) != 4:
                # fall back to the min-area rectangle for slightly rounded corners
                approx = cv2.boxPoints(cv2.minAreaRect(c))
            quad = _order_corners(np.asarray(approx))
            quad_area = cv2.contourArea(quad)
            if quad_area <= 0:
                continue
            fill = area / quad_area  # 1.0 for a clean rectangle; low when blobs merged in
            if fill < 0.90 or fill > 1.08:
                continue
            # A quad touching the frame is usually background merged into the card.
            hs, ws = small.shape[:2]
            if (quad[:, 0].min() < 2 or quad[:, 1].min() < 2
                    or quad[:, 0].max() > ws - 3 or quad[:, 1].max() > hs - 3):
                continue
            wq = np.linalg.norm(quad[1] - quad[0])
            hq = np.linalg.norm(quad[3] - quad[0])
            if wq == 0 or hq == 0:
                continue
            aspect = min(wq, hq) / max(wq, hq)
            if not 0.62 <= aspect <= 0.80:  # card is 63/88 = 0.716
                continue
            candidates.append((area * fill ** 4, quad))
    if not candidates:
        return None
    _, best = max(candidates, key=lambda t: t[0])
    return best / s


def warp_card(img: np.ndarray, quad: np.ndarray, scale: float = 1.0) -> np.ndarray:
    wq = np.linalg.norm(quad[1] - quad[0])
    hq = np.linalg.norm(quad[3] - quad[0])
    cw, ch = round(CARD_W * scale), round(CARD_H * scale)
    dst_w, dst_h = (cw, ch) if hq >= wq else (ch, cw)
    dst = np.array([[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    warped = cv2.warpPerspective(img, m, (dst_w, dst_h), flags=cv2.INTER_CUBIC)
    if dst_w > dst_h:  # landscape photo of a portrait card
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)
    return warped


# Where the guided camera puts the card: its crop keeps an 8% margin around the on-screen frame.
GUIDE_BOX = (0.08, 0.08, 0.92, 0.92)


def _color_grad(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel edge strength across x and across y: the max over colour channels, because
    a blue card back on a dark table differs mostly in hue, not brightness."""
    f = cv2.GaussianBlur(img, (0, 0), 1.0).astype(np.int16)
    gx = np.zeros(f.shape[:2], np.int16)
    gy = np.zeros(f.shape[:2], np.int16)
    gx[:, 1:-1] = np.abs(f[:, 2:] - f[:, :-2]).max(axis=2)
    gy[1:-1, :] = np.abs(f[2:] - f[:-2]).max(axis=2)
    return gx, gy


def _line_candidates(g: np.ndarray, lo: int, hi: int, a0: int, a1: int, thr: int = 30,
                     max_tilt: int | None = None) -> list[tuple]:
    """Straight edges crossing the band [lo, hi) of axis 1, measured along axis 0 from a0 to a1.

    Score = fraction of samples along the line with a strong edge within +-1 px ("continuity"):
    a card's cut edge is one long straight line; art, wood grain and glare are not.
    Returns [(continuity, p_at_a0, p_at_a1, rank key)], best first, roughly de-duplicated.
    """
    strong = g > thr
    strong[:, 1:] |= strong[:, :-1].copy()
    strong[:, :-1] |= strong[:, 1:].copy()
    ts = np.arange(a0, a1, 2)
    ps = np.arange(max(1, lo), min(g.shape[1] - 1, hi))
    if len(ts) < 10 or len(ps) < 3:
        return []
    span = max(1, a1 - a0)
    if max_tilt is None:
        max_tilt = int(0.09 * span)  # about +-5 degrees
    found = []
    for tilt in range(-max_tilt, max_tilt + 1, 2):
        offs = np.round(tilt * (ts - a0) / span).astype(int)
        idx = ps[None, :] + offs[:, None]
        ok = (idx >= 0) & (idx < g.shape[1])
        ci = np.clip(idx, 0, g.shape[1] - 1)
        cont = (strong[ts[:, None], ci] & ok).mean(axis=0)
        # Ties on continuity are common (a blurred edge is a few px wide): break them by
        # edge strength along the line, so the line sits on the edge's peak.
        key = cont + 0.004 * (g[ts[:, None], ci] * ok).mean(axis=0)
        for i in np.argsort(key)[-4:]:
            found.append((float(cont[i]), int(ps[i]), int(ps[i] + tilt), float(key[i])))
    found.sort(key=lambda c: c[3], reverse=True)
    out = []
    for c in found:
        mid = (c[1] + c[2]) / 2
        if all(abs(mid - (o[1] + o[2]) / 2) > 4 for o in out):
            out.append(c)
        if len(out) >= 8:
            break
    return out


def find_card_quad_lines(img: np.ndarray, prior: tuple | None = None) -> tuple[np.ndarray, float] | None:
    """Find the card from its four straight edges. With `prior` (x0, y0, x1, y1, fractions of the
    image), each edge is searched only near where the guided camera put it; without, in the
    outer 35% of the image. Returns (quad tl/tr/br/bl, quality 0-1) or None."""
    h0, w0 = img.shape[:2]
    s = 800.0 / max(h0, w0)
    small = cv2.resize(img, (round(w0 * s), round(h0 * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    s = small.shape[1] / w0
    h, w = small.shape[:2]
    gx, gy = _color_grad(small)
    if prior:
        bx, by = 0.07 * w, 0.07 * h
        bands = {"l": (prior[0] * w - bx, prior[0] * w + bx), "r": (prior[2] * w - bx, prior[2] * w + bx),
                 "t": (prior[1] * h - by, prior[1] * h + by), "b": (prior[3] * h - by, prior[3] * h + by)}
    else:
        bands = {"l": (0, 0.35 * w), "r": (0.65 * w, w), "t": (0, 0.35 * h), "b": (0.65 * h, h)}
    ya0, ya1 = int(0.18 * h), int(0.82 * h)  # middle of each side: skip rounded corners and fingers
    xa0, xa1 = int(0.18 * w), int(0.82 * w)
    cands = {
        "l": _line_candidates(gx, int(bands["l"][0]), int(bands["l"][1]), ya0, ya1),
        "r": _line_candidates(gx, int(bands["r"][0]), int(bands["r"][1]), ya0, ya1),
        "t": _line_candidates(gy.T, int(bands["t"][0]), int(bands["t"][1]), xa0, xa1),
        "b": _line_candidates(gy.T, int(bands["b"][0]), int(bands["b"][1]), xa0, xa1),
    }
    if any(not c or c[0][0] < 0.35 for c in cands.values()):
        return None
    # Per side, keep strong candidates; prefer the outermost (the cut edge, not the inner
    # border/art edge just inside it), then require a card-shaped result.
    def keep(side):
        # Strongest first (continuity + contrast): the card's cut edge beats a sleeve's faint
        # edge outside it. But a border's inner edge can be a touch stronger than the cut edge
        # (yellow -> silver), so if a nearly-as-strong line lies just outside, that's the cut.
        c = [x for x in cands[side] if x[0] >= max(0.35, 0.5 * cands[side][0][0])][:5]
        top = c[0]
        strength = lambda x: (x[3] - x[0]) / 0.004
        outward = (lambda x: -(x[1] + x[2])) if side in "lt" else (lambda x: x[1] + x[2])
        # A dark-blue back on a dark mat makes the true cut edge only ~half as contrasty as
        # the border's inner edge, but it's just as continuous; the shape check settles it.
        outer = [x for x in c if outward(x) > outward(top) and
                 ((x[0] >= 0.85 * top[0] and strength(x) >= 0.75 * strength(top))
                  or (x[0] >= 0.95 * top[0] and strength(x) >= 0.45 * strength(top)))]
        first = max(outer, key=outward) if outer else top
        return [first] + [x for x in c if x is not first][:3]
    L, R, T, B = keep("l"), keep("r"), keep("t"), keep("b")
    best_combo = None
    for i, l in enumerate(L):
        for j, r in enumerate(R):
            for k, t in enumerate(T):
                for m, b in enumerate(B):
                    width = (r[1] + r[2] - l[1] - l[2]) / 2
                    height = (b[1] + b[2] - t[1] - t[2]) / 2
                    if width <= 0.3 * w or height <= 0.3 * h:
                        continue
                    aspect = width / height
                    # Card is 63/88 = 0.716. Guided shots are roughly square-on, so hold them tight:
                    # a box that includes a sleeve's lip is ~0.68 and gets rejected.
                    if abs(aspect - CARD_W / CARD_H) > (0.03 if prior else 0.06):
                        continue
                    # Strongest first, but a better card shape outweighs a rank step.
                    rank = i + j + k + m + abs(aspect - CARD_W / CARD_H) / 0.02
                    q = min(l[0], r[0], t[0], b[0])
                    key = (rank, -q)
                    if best_combo is None or key < best_combo[0]:
                        best_combo = (key, (l, r, t, b), q)
    if best_combo is None:
        return None
    (l, r, t, b), quality = best_combo[1], best_combo[2]

    # Lines: vertical x = p0 + (p1 - p0) * (y - ya0) / span; horizontal likewise in y.
    def vx(line):
        k = (line[2] - line[1]) / (ya1 - ya0)
        return line[1] - k * ya0, k  # x = a + k*y
    def hy(line):
        k = (line[2] - line[1]) / (xa1 - xa0)
        return line[1] - k * xa0, k  # y = a + k*x
    def meet(v, hz):
        (ax, kx), (ay, ky) = vx(v), hy(hz)
        x = (ax + kx * ay) / (1 - kx * ky)
        return [x, ay + ky * x]
    quad = np.array([meet(l, t), meet(r, t), meet(r, b), meet(l, b)], dtype=np.float32) / s
    return quad, quality


def corners_intact(card: np.ndarray, min_dist: float = 45.0) -> bool:
    """Hypothesis check for an outline: a card's corners are rounded, so the extreme corner of a
    correct crop shows background, not card. A corner patch that matches the border colour
    means the outline cut into the card. Real scans scored 70+; a clipped corner 25."""
    h, w = card.shape[:2]
    p = max(6, round(0.016 * w))
    patches = [card[:p, :p], card[:p, -p:], card[-p:, :p], card[-p:, -p:]]
    edge = [card[p:2 * p, w // 2 - 15:w // 2 + 15], card[-2 * p:-p, w // 2 - 15:w // 2 + 15],
            card[h // 2 - 15:h // 2 + 15, p:2 * p], card[h // 2 - 15:h // 2 + 15, -2 * p:-p]]
    ref = np.median(np.concatenate([e.reshape(-1, 3) for e in edge]), axis=0)
    return all(np.linalg.norm(np.median(c.reshape(-1, 3), axis=0) - ref) >= min_dist for c in patches)


def normalize_card(img: np.ndarray, guided: bool = False) -> tuple[np.ndarray, str, np.ndarray | None]:
    """Return (warped card, detection method, quad in source coords).

    Guided-camera photos: straight-edge search near the frame lines first.
    Other photos: contour detection first, then a straight-edge search over the outer image.
    There's deliberately no "assume the photo is the card" fallback: it produced confident
    nonsense on real phone photos.
    """
    attempts = [("lines-guided", lambda: find_card_quad_lines(img, GUIDE_BOX))] if guided else []
    attempts += [("contour", lambda: find_card_quad(img)), ("lines", lambda: find_card_quad_lines(img))]
    # Hypothesise and verify: take the first outline whose corners check out, else the first found.
    fallback = None
    for name, fn in attempts:
        found = fn()
        if found is None:
            continue
        quad = found[0] if isinstance(found, tuple) else found
        card = warp_card(img, quad)
        if corners_intact(card):
            return card, name, quad
        fallback = fallback or (card, name, quad)
    if fallback:
        return fallback
    raise CardNotFound("Could not find the card's edges. Shoot straight-on against a plain, contrasting background, "
                       "with the whole card in view.")


# ---------------------------------------------------------------- borders

def _profile(lab: np.ndarray, ref: np.ndarray, axis_len: int, getter) -> np.ndarray:
    """Fraction of pixels in each line (row/col) that differ from the border colour."""
    out = np.empty(axis_len, dtype=np.float32)
    for i in range(axis_len):
        line = getter(i)
        dist = np.linalg.norm(line - ref, axis=-1)
        out[i] = np.mean(dist > 18.0)
    return out


def _scan_inward(profile: np.ndarray, start: int, limit: int, thresh: float = 0.5, run: int = 3) -> int | None:
    """Index of the first position (from `start`) where `run` consecutive lines exceed `thresh`."""
    count = 0
    for i in range(start, limit):
        if profile[i] > thresh:
            count += 1
            if count >= run:
                return i - run + 1
        else:
            count = 0
    return None


BORDER_THR = 14


def border_lines(card: np.ndarray) -> dict[str, tuple[float, float] | None]:
    """Inner edge of the printed border on each side of a warped card, found as the outermost
    long straight line parallel to that side: {side: (border width px, continuity) or None}.
    Works where the colour method fails: full-art cards with a thin border that blends into
    the art, and backs where the blue border meets a blue swirl."""
    h, w = card.shape[:2]
    gx, gy = _color_grad(card)
    ya0, ya1, xa0, xa1 = int(0.2 * h), int(0.8 * h), int(0.2 * w), int(0.8 * w)
    tilt = 4  # the warp leaves the frame nearly axis-aligned
    edge_zone = 12  # the cut edge sits within a few px of the warped image's edge

    def side(g, n, a0, a1, flip):
        """Border width on one side: inner edge minus outer (cut) edge, both as straight lines.
        `flip` mirrors the axis so the right/bottom sides are searched from their own edge."""
        gg = g[:, ::-1] if flip else g
        outer = _line_candidates(gg, 1, edge_zone, a0, a1, 25, tilt)
        cut = (outer[0][1] + outer[0][2]) / 2 if outer and outer[0][0] >= 0.4 else 0.0
        cands = _line_candidates(gg, int(cut) + 5, int(0.10 * n), a0, a1, BORDER_THR, tilt)  # borders are < ~9%
        # A tilted line that dips into the cut zone is the cut edge itself, not the border.
        cands = [c for c in cands if min(c[1], c[2]) >= cut + 4 and (c[1] + c[2]) / 2 >= cut + 6]
        if not cands or cands[0][0] < 0.4:
            return None
        best = cands[0][0]
        strong = [c for c in cands if c[0] >= max(0.4, 0.65 * best)]
        cont, a, b, _ = min(strong, key=lambda c: c[1] + c[2])  # outermost strong line
        return float((a + b) / 2 - cut), float(cont)

    return {
        "left": side(gx, w, ya0, ya1, False), "right": side(gx, w, ya0, ya1, True),
        "top": side(gy.T, h, xa0, xa1, False), "bottom": side(gy.T, h, xa0, xa1, True),
    }


BALANCE_MAX = 0.20  # |(L+R) - (T+B)| / mean: the printed frame's one fixed size


def _balance(lr: float, tb: float) -> float:
    return abs(lr - tb) / max(1.0, (lr + tb) / 2)


def _edge_peaks(p: np.ndarray, limit: int) -> list[tuple[float, float]]:
    """Border edges along one side, from `p`, the mean edge strength per line inward from the
    card's edge: [(width from the cut edge, strength)]. Mean, not a line fit, so an edge that's
    faint across much of the side still shows up."""
    cut = int(np.argmax(p[:4])) if p[:4].max() >= 15 else 0  # the cut edge left inside the warp
    band = p[cut + 5:limit]
    if len(band) < 3:
        return []
    floor = max(6.0, 1.4 * float(np.median(band)))
    start = cut + 5
    return [(float(i - cut), float(p[i])) for i in range(start, limit - 1)
            if p[i] >= floor and p[i] >= p[i - 1] and p[i] > p[i + 1]]


def _fit_frame(vals: dict, card: np.ndarray) -> tuple[dict, bool]:
    """Fit the printed frame as one object, the way document scanners test whole-rectangle
    hypotheses instead of trusting single lines. Any one side can latch onto a strong inner line
    (the name bar on a Pokemon front is the classic), but a frame has one fixed size, so of all
    rectangles built from each side's edge peaks only those with left+right = top+bottom are
    real. A rectangle is as good as its weakest edge; among close ones the outermost wins,
    because inner art lines only make a frame smaller. Returns (values, was refitted)."""
    if _balance(vals["left"] + vals["right"], vals["top"] + vals["bottom"]) <= BALANCE_MAX:
        return vals, False
    h, w = card.shape[:2]
    gx, gy = _color_grad(card)
    px = gx[int(0.2 * h):int(0.8 * h)].mean(axis=0)
    py = gy[:, int(0.2 * w):int(0.8 * w)].mean(axis=1)
    lim_w, lim_h = int(0.10 * w), int(0.10 * h)
    sides = [_edge_peaks(px, lim_w), _edge_peaks(px[::-1], lim_w), _edge_peaks(py, lim_h), _edge_peaks(py[::-1], lim_h)]
    if not all(sides):
        return vals, False
    fits = []
    for l, ls in sides[0]:
        for r, rs in sides[1]:
            for t, ts in sides[2]:
                for b, bs in sides[3]:
                    if _balance(l + r, t + b) <= 0.12:
                        fits.append((min(ls, rs, ts, bs), l + r + t + b, l, r, t, b))
    if not fits:
        return vals, False
    strongest = max(f[0] for f in fits)
    _, _, l, r, t, b = min((f for f in fits if f[0] >= 0.7 * strongest), key=lambda f: f[1])
    return {"left": l, "right": r, "top": t, "bottom": b}, True


def measure_borders(card: np.ndarray) -> tuple[Borders, float, list[str]]:
    """Measure printed border widths (px) on a warped, upright card image.

    Primary: straight-line search for the border's inner edge (border_lines). Fallback per
    side: where the border colour stops (the original method, fine on plain borders)."""
    lines = border_lines(card)
    color, conf, warnings = measure_borders_color(card)
    vals = {}
    used_fallback = []
    for side in ("left", "right", "top", "bottom"):
        if lines[side] is not None:
            vals[side] = lines[side][0]
        else:
            vals[side] = getattr(color, side)
            used_fallback.append(side)
    vals, refit = _fit_frame(vals, card)
    if refit:  # every side now has a checked value, whichever method first supplied it
        used_fallback, lines = [], {k: (v, 1.0) for k, v in vals.items()}
    confidence = (0.8 if refit else 1.0) - 0.2 * len(used_fallback)
    weak = [s for s in vals if lines[s] is not None and lines[s][1] < 0.55]
    confidence -= 0.1 * len(weak)
    if used_fallback:
        confidence = min(confidence, conf)
        warnings = [w for w in warnings] + [f"No clear border edge on the {', '.join(used_fallback)}; "
                                            "used border colour there instead."]
    else:
        warnings = [w for w in warnings if "uneven" not in w and "almost absent" not in w]
    for name, v, lim in (("left", vals["left"], card.shape[1]), ("right", vals["right"], card.shape[1]),
                         ("top", vals["top"], card.shape[0]), ("bottom", vals["bottom"], card.shape[0])):
        if v < 0.012 * lim:
            # Real miscuts this bad are rare; a detection miss is likelier, so don't trust it.
            confidence -= 0.3
            warnings.append(f"The {name} border looks almost absent (a rare miscut, or the edge was misread).")
    b = Borders(vals["left"], vals["right"], vals["top"], vals["bottom"])
    return b, max(0.0, round(confidence, 2)), warnings


def verify_borders(card: np.ndarray, b: Borders) -> list[str]:
    """Physical checks a real measurement must pass; returns what failed.

    1. A card's printed frame has one fixed size, so centering only moves it: left+right and
       top+bottom stay about equal however off-centre the card is. On 12 real scans that
       balance was within 14%; misreads were 42-62%.
    2. A border is a uniform band: colour barely changes along it. On those scans the
       along-side variation stayed under 5.5; lines that landed inside artwork scored 9-21.
    3. A band too thin to sample can't be told from the art, so it isn't measured."""
    h, w = card.shape[:2]
    lr, tb = b.left + b.right, b.top + b.bottom
    problems = []
    if _balance(lr, tb) > BALANCE_MAX:
        problems.append("balance")
    lab = cv2.cvtColor(card, cv2.COLOR_BGR2LAB).astype(np.float32)
    y0, y1, x0, x1 = int(0.15 * h), int(0.85 * h), int(0.15 * w), int(0.85 * w)
    m = 4  # stay clear of the blurred edge lines on either side of the band
    for name, v in (("left", b.left), ("right", b.right), ("top", b.top), ("bottom", b.bottom)):
        v = int(v)
        if v < 2 * m + 3:
            problems.append(f"{name} too thin")
            continue
        strip = {"left": lab[y0:y1, m + 2:v - m], "right": lab[y0:y1, w - v + m:w - m - 2],
                 "top": lab[m + 2:v - m, x0:x1], "bottom": lab[h - v + m:h - m - 2, x0:x1]}[name]
        along = strip.mean(axis=1 if name in ("left", "right") else 0)
        if np.median(np.abs(along - np.median(along, axis=0)), axis=0)[1:].max() > 8:
            problems.append(f"{name} not uniform")
    return problems


def measure_borders_color(card: np.ndarray) -> tuple[Borders, float, list[str]]:
    """Measure printed border widths (px) by where the border colour stops."""
    h, w = card.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(card, (3, 3), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    warnings: list[str] = []

    # Skip the very edge (cut edge, shadow, warp error) when sampling border colour.
    edge = max(3, int(0.008 * w))
    strip = int(0.02 * w)
    samples = np.concatenate([
        lab[h // 4: 3 * h // 4, edge:edge + strip].reshape(-1, 3),
        lab[h // 4: 3 * h // 4, w - edge - strip: w - edge].reshape(-1, 3),
        lab[edge:edge + strip, w // 4: 3 * w // 4].reshape(-1, 3),
        lab[h - edge - strip: h - edge, w // 4: 3 * w // 4].reshape(-1, 3),
    ])
    ref = np.median(samples, axis=0)
    spread = float(np.median(np.linalg.norm(samples - ref, axis=1)))

    # Use the middle 60% of each side so rounded corners and corner wear don't skew it.
    r0, r1 = int(0.2 * h), int(0.8 * h)
    c0, c1 = int(0.2 * w), int(0.8 * w)
    col_prof = _profile(lab, ref, w, lambda i: lab[r0:r1, i])
    row_prof = _profile(lab, ref, h, lambda i: lab[i, c0:c1])

    max_w, max_h = int(0.25 * w), int(0.25 * h)

    def side(profile: np.ndarray, limit: int) -> tuple[float | None, int]:
        # Outer edge: first line that is mostly border colour (skips any background
        # sliver left by an imperfect outline). Inner edge: first run of non-border lines.
        outer = next((i for i in range(limit) if profile[i] < 0.4), 0)
        inner = _scan_inward(profile, max(outer + 2, edge), limit)
        if inner is None:
            return None, outer
        # Sub-pixel: interpolate where the profile crosses 0.5 on both transitions.
        def cross(i):
            a, b = profile[i - 1], profile[i]
            return i - 1 + (0.5 - a) / (b - a) if i > 0 and b != a and min(a, b) <= 0.5 <= max(a, b) else float(i)
        o = cross(outer) if outer > 0 else 0.0
        return max(0.0, cross(inner) - o), outer

    left, _ = side(col_prof, max_w)
    right_rev, _ = side(col_prof[::-1].copy(), max_w)
    top, _ = side(row_prof, max_h)
    bottom_rev, _ = side(row_prof[::-1].copy(), max_h)

    found = [v is not None for v in (left, right_rev, top, bottom_rev)]
    if not all(found):
        warnings.append("Could not find the inner frame on every side; the result may be unreliable.")
    left = left if left is not None else max_w
    right = right_rev if right_rev is not None else max_w
    top = top if top is not None else max_h
    bottom = bottom_rev if bottom_rev is not None else max_h

    confidence = 1.0
    confidence -= 0.25 * (4 - sum(found))
    if spread > 10:
        confidence -= 0.2
        warnings.append("The border colour is uneven (glare, shadow or a full-art card); measurement is less reliable.")
    for name, v, lim in (("left", left, w), ("right", right, w), ("top", top, h), ("bottom", bottom, h)):
        if v < 0.012 * lim:
            confidence -= 0.15
            warnings.append(f"The {name} border looks almost absent (full-art card, or a miscut or poor crop).")
    return Borders(float(left), float(right), float(top), float(bottom)), max(0.0, round(confidence, 2)), warnings


def measure_centering(img: np.ndarray, guided: bool = False) -> tuple[CenteringResult, np.ndarray, np.ndarray]:
    """Measure one side. Returns (result, warped card, overlay image)."""
    card, method, quad = normalize_card(img, guided)
    borders, conf, warnings = measure_borders(card)
    crop_ok = corners_intact(card)
    problems = verify_borders(card, borders)
    if problems or not crop_ok:
        conf = min(conf, 0.5)  # below the trust line: it won't cap the grade
        warnings = list(warnings)
        if not crop_ok:
            warnings.append("The card outline cut off a corner, so this measurement isn't reliable.")
        if problems:
            warnings.append("This card's borders can't be measured reliably: the art runs close to the edge "
                            "or the lines found don't fit a printed frame.")
    lr, tb = borders.lr, borders.tb
    worst = max(lr[0], lr[1], tb[0], tb[1])
    res = CenteringResult(
        borders_px=asdict(borders), lr=lr, tb=tb, worst=worst,
        detection=method, confidence=conf, warnings=warnings,
        quad=None if quad is None else np.round(np.asarray(quad, dtype=float), 1).tolist(),
        crop_ok=crop_ok, verified=not problems,
    )
    return res, card, draw_overlay(card, borders)


def draw_overlay(card: np.ndarray, b: Borders) -> np.ndarray:
    out = card.copy()
    h, w = out.shape[:2]
    green, magenta = (60, 220, 60), (220, 60, 220)
    cv2.rectangle(out, (0, 0), (w - 1, h - 1), green, 2)
    x0, y0, x1, y1 = int(b.left), int(b.top), int(w - 1 - b.right), int(h - 1 - b.bottom)
    cv2.rectangle(out, (x0, y0), (x1, y1), magenta, 2)
    lr, tb = b.lr, b.tb
    font = cv2.FONT_HERSHEY_SIMPLEX
    def label(text, org):
        cv2.putText(out, text, org, font, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(out, text, org, font, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    label(f"L {lr[0]:.0f}", (x0 + 6, h // 2))
    label(f"R {lr[1]:.0f}", (x1 - 70, h // 2))
    label(f"T {tb[0]:.0f}", (w // 2 - 30, y0 + 26))
    label(f"B {tb[1]:.0f}", (w // 2 - 30, y1 - 10))
    return out
