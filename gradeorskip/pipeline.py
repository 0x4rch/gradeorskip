"""One "check": front + back photos -> centering, condition, grade range."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from .centering import CardNotFound, measure_centering, warp_card
from .config import DATA_DIR
from .grading import CENTERING_MIN_CONFIDENCE, Finding, centering_cap, estimate_grade, unmeasured_cap
from .providers.condition import MAX_SIDE, ConditionError, get_provider

CHECKS_DIR = DATA_DIR / "checks"
MAX_INPUT = 2000


def decode(data: bytes) -> np.ndarray:
    buf = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)  # applies EXIF orientation
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        rgba = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if rgba is not None and rgba.ndim == 3 and rgba.shape[2] == 4:
            # Transparent corners would read as black, which looks like corner damage.
            # Composite onto a plain dark grey, like a card photographed on a mat.
            alpha = rgba[..., 3:4].astype(np.float32) / 255.0
            img = (rgba[..., :3] * alpha + np.full_like(rgba[..., :3], 45) * (1 - alpha)).astype(np.uint8)
    if img is None:
        raise ValueError("Could not read the image. Use JPEG or PNG; iPhone HEIC photos may need converting first.")
    h, w = img.shape[:2]
    s = MAX_INPUT / max(h, w)
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img


def to_jpeg(img: np.ndarray, max_side: int = MAX_SIDE, quality: int = 85) -> bytes:
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def draw_findings(card: np.ndarray, areas: list[dict]) -> np.ndarray:
    out = card.copy()
    h, w = out.shape[:2]
    for a in areas:
        for loc in a.get("locations", []):
            x0, y0 = int(loc["x"] * w), int(loc["y"] * h)
            x1, y1 = int((loc["x"] + loc["w"]) * w), int((loc["y"] + loc["h"]) * h)
            cv2.rectangle(out, (x0, y0), (max(x1, x0 + 8), max(y1, y0 + 8)), (40, 40, 230), 3)
            cv2.putText(out, loc.get("label", "")[:24], (x0 + 2, max(14, y0 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 230), 2, cv2.LINE_AA)
    return out


def _number_closeup(card: np.ndarray) -> bytes:
    """Bottom 13% of a straightened card at full resolution: collector numbers and promo
    codes are a few px tall in the whole-card view the model sees."""
    h = card.shape[0]
    return to_jpeg(card[int(0.87 * h):], max_side=1600, quality=90)


def combine_centering(cens: list, frames_sent: int = 1) -> dict:
    """One side's centering from several burst frames, by consensus: the median of the frames
    that measured reliably and agree within 4 points (a misread border gets outvoted). A burst
    with no two agreeing frames isn't trusted at all: a lone good-looking frame was how a
    wrong Mew VMAX reading capped a grade. cens[0] is the best frame; its overlay is shown."""
    best = cens[0]
    out = best.to_dict()
    ok = [c for c in cens if c.confidence >= CENTERING_MIN_CONFIDENCE]
    unconfirmed = "The burst frames didn't agree on this measurement, so it isn't being used."
    if len(ok) < 2:
        if frames_sent > 1 and ok:
            out["confidence"] = min(out["confidence"], 0.5)
            out["warnings"] = list(out.get("warnings") or []) + [unconfirmed]
        return out
    med = lambda xs: float(np.median(xs))  # noqa: E731
    lr0, tb0 = med([c.lr[0] for c in ok]), med([c.tb[0] for c in ok])
    keep = [c for c in ok if abs(c.lr[0] - lr0) <= 4 and abs(c.tb[0] - tb0) <= 4]
    if len(keep) < 2:
        out["confidence"] = min(out["confidence"], 0.5)
        out["warnings"] = list(out.get("warnings") or []) + [unconfirmed]
        return out
    lr, tb = round(med([c.lr[0] for c in keep]), 1), round(med([c.tb[0] for c in keep]), 1)
    spread_lr = max(c.lr[0] for c in keep) - min(c.lr[0] for c in keep)
    spread_tb = max(c.tb[0] for c in keep) - min(c.tb[0] for c in keep)
    out.update(lr=[lr, round(100 - lr, 1)], tb=[tb, round(100 - tb, 1)],
               worst=max(lr, 100 - lr, tb, 100 - tb),
               lr_label=f"{lr:.0f}/{100 - lr:.0f}", tb_label=f"{tb:.0f}/{100 - tb:.0f}")
    out["frames"] = {"measured": len(ok), "agree": len(keep),
                     "spread": round(max(spread_lr, spread_tb), 1)}
    out["warnings"] = list(out.get("warnings") or [])
    if len(keep) < len(ok):
        out["warnings"].append(f"{len(ok) - len(keep)} of {len(ok)} frames measured differently and were left out.")
    if max(spread_lr, spread_tb) > 4:
        out["confidence"] = max(0.0, round(out["confidence"] - 0.2, 2))
        out["warnings"].append(f"The frames disagree by up to {max(spread_lr, spread_tb):.0f} points; "
                               "a steadier, straight-on shot will measure better.")
    return out


def _pick_frame(datas: list[bytes], guided: bool):
    """Of several burst frames of one side, the one whose centering measures most reliably
    (then the sharpest card). Returns (img, centering, card, overlay, error, index, all centerings)."""
    best, best_key = None, None
    measured = []
    for i, data in enumerate(datas):
        try:
            img = decode(data)
        except ValueError as e:
            if best is None:
                best, best_key = (None, None, None, None, e, i), (-1,)
            continue
        try:
            cen, card, overlay = measure_centering(img, guided=guided)
            sharp = float(cv2.Laplacian(cv2.cvtColor(card, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            key = (1, cen.crop_ok, cen.confidence, sharp)
            cand = (img, cen, card, overlay, None, i)
            measured.append((key, cen))
        except CardNotFound as e:
            key = (0, False, 0.0, 0.0)
            cand = (img, None, None, None, e, i)
        if best_key is None or key > best_key:
            best, best_key = cand, key
    measured.sort(key=lambda kc: kc[0], reverse=True)
    return (*best, [c for _, c in measured])


def _centering_inputs(result: dict, grader: str = "psa") -> tuple[dict, object, float]:
    """Only trust a side's centering if its measurement is confident enough."""
    sides = result["sides"]
    trusted = {k: c for k in ("front", "back") if (c := sides.get(k, {}).get("centering"))
               and c["confidence"] >= CENTERING_MIN_CONFIDENCE}
    if "front" in trusted:
        cap = centering_cap(trusted["front"]["worst"], trusted["back"]["worst"] if "back" in trusted else None, grader)
    else:
        cap = unmeasured_cap()
    return trusted, cap, min((c["confidence"] for c in trusted.values()), default=1.0)


def grade_for(result: dict, grader: str) -> dict | None:
    """The stored estimate is PSA's. Other graders re-run it from the same measurements and findings."""
    if grader == "psa" or not result.get("grade"):
        return result.get("grade")
    failed = any(e.startswith("Condition check failed") for e in result.get("errors", []))
    findings = [] if failed else [
        Finding(a["area"], side, a["severity"], float(a["confidence"]), a.get("notes", ""), a.get("locations", []))
        for side, s in result["sides"].items() for a in (s.get("condition") or {}).get("result", {}).get("areas", [])]
    _, cap, conf = _centering_inputs(result, grader)
    return estimate_grade(cap, findings or None, conf, result.get("inspection", "good"), grader).to_dict()


def run_check(front: bytes, back: bytes | None, use_llm: bool = True, guided: set[str] | None = None,
              alts: dict[str, list[bytes]] | None = None, tilt: bytes | None = None) -> dict:
    """guided: sides captured with the live guided camera (card expected on the frame lines).
    alts: extra burst frames per side; the best-measuring frame is used.
    tilt: optional photo of the front tilted under a light, shown to the model for surface defects."""
    check_id = uuid.uuid4().hex[:12]
    folder = CHECKS_DIR / check_id
    folder.mkdir(parents=True, exist_ok=True)
    result: dict = {"id": check_id, "sides": {}, "errors": [], "llm": None}
    provider = get_provider() if use_llm else None
    findings: list[Finding] = []
    llm_failed = False

    # Pass 1 (fast, local): decode and measure centering for each side.
    views: dict[str, np.ndarray] = {}
    hires: set[str] = set()  # sides whose model view is the straightened card
    for side, data in (("front", front), ("back", back)):
        if data is None:
            continue
        side_out: dict = {}
        result["sides"][side] = side_out
        datas = [data] + list((alts or {}).get(side) or [])
        img, cen, card, overlay, err, idx, all_cens = _pick_frame(datas, side in (guided or set()))
        if img is None:
            result["errors"].append(f"{side}: {err}")
            side_out["error"] = str(err)
            continue
        if len(datas) > 1:
            side_out["frames"] = {"used": idx, "of": len(datas)}
            for i, frame in enumerate(datas):  # every burst frame is kept for diagnosis
                (folder / f"{side}_frame{i}.jpg").write_bytes(frame)
        cv2.imwrite(str(folder / f"{side}_input.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if cen is not None:
            cv2.imwrite(str(folder / f"{side}_overlay.jpg"), overlay, [cv2.IMWRITE_JPEG_QUALITY, 88])
            cv2.imwrite(str(folder / f"{side}_card.jpg"), card, [cv2.IMWRITE_JPEG_QUALITY, 90])
            side_out["centering"] = combine_centering(all_cens, len(datas))
        else:
            # No centering for this side, but the condition read still runs on the photo:
            # skipping it silently dropped back-side damage from the estimate.
            side_out["error"] = str(err)
            result["errors"].append(f"{side}: {err} Centering skipped for this side; condition was still checked.")
        # A low-confidence warp may have cropped the card wrongly; then the model
        # judges invented "corners". Show it the original photo instead.
        # (A sound outline is enough here: Mew VMAX's borders can't be measured, but its crop is fine.)
        if cen is not None and cen.crop_ok and (
                side_out["centering"]["confidence"] >= CENTERING_MIN_CONFIDENCE or not cen.verified):
            hires.add(side)
            # The model gets a 2x straightened card: at 630x880 small print (collector
            # numbers, promo codes) and fine whitening were lost.
            views[side] = warp_card(img, np.asarray(cen.quad, dtype=np.float32), scale=2.0) if cen.quad else card
        else:
            views[side] = img

    # Extra images for the model, sent with that side's call.
    extras: dict[str, list[tuple[str, bytes]]] = {}
    if "front" in hires:
        extras.setdefault("front", []).append(
            ("Close-up of the bottom strip (collector number only)", _number_closeup(views["front"])))
    if tilt and "front" in views:
        try:
            timg = decode(tilt)
            cv2.imwrite(str(folder / "tilt_input.jpg"), timg, [cv2.IMWRITE_JPEG_QUALITY, 85])
            extras.setdefault("front", []).append(("Tilted shot of the front (surface defects only)", to_jpeg(timg)))
            result["sides"]["front"]["tilt"] = True
        except ValueError as e:
            result["errors"].append(f"tilt shot: {e}")

    # Pass 2 (slow, network): both condition calls at once.
    if provider is not None and views:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {side: pool.submit(provider.assess, to_jpeg(view), side, extras.get(side))
                       for side, view in views.items()}
        for side, fut in futures.items():
            try:
                resp = fut.result()
                areas = resp["result"].get("areas", [])
                side_findings = [Finding(a["area"], side, a["severity"], float(a["confidence"]),
                                         a.get("notes", ""), a.get("locations", [])) for a in areas]
            except (ConditionError, KeyError, TypeError, ValueError) as e:
                llm_failed = True
                result["errors"].append(f"Condition check failed ({e}). Showing a centering-only estimate.")
                continue
            result["sides"][side]["condition"] = resp
            if side == "front" and resp["result"].get("card"):
                result["identified"] = resp["result"]["card"]
            findings += side_findings
            cv2.imwrite(str(folder / f"{side}_findings.jpg"), draw_findings(views[side], areas),
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
        if llm_failed:
            findings = []

    result["llm"] = {"provider": provider.name, "model": provider.model} if provider else None
    front_c = result["sides"].get("front", {}).get("centering")
    back_c = result["sides"].get("back", {}).get("centering")
    if not front_c and not findings:
        result["grade"] = None
        return result
    trusted, cap, conf = _centering_inputs(result)
    views_seen = [(s.get("condition") or {}).get("result", {}).get("inspection", "good")
                  for s in result["sides"].values()]
    inspection = next((v for v in ("poor", "limited") if v in views_seen), "good")  # the worst side counts
    result["inspection"] = inspection
    est = estimate_grade(cap, findings if (findings and not llm_failed) else None, conf, inspection)
    if back is None:
        est.explanation.insert(1, "No back photo, so back centering and condition aren't included.")
    for k in ("front", "back"):
        if "error" in result["sides"].get(k, {}) and "condition" in result["sides"][k]:
            est.explanation.insert(1, f"The card outline wasn't found in the {k} photo, so its centering isn't "
                                      "included (its condition was still checked).")
    untrusted = [k for k, c in (("front", front_c), ("back", back_c)) if c and k not in trusted]
    if untrusted:
        est.explanation.insert(1, f"The {' and '.join(untrusted)} centering measurement looked unreliable, so it was "
                                  "ignored. Retake the photo flat and straight-on to include it.")
    result["grade"] = est.to_dict()
    result["cost_usd"] = round(sum((s.get("condition") or {}).get("cost_usd") or 0
                                   for s in result["sides"].values()), 5)
    return result
