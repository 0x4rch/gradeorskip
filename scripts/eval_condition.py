"""Score the vision-LLM condition read on the real-photo test set.

    python scripts/fetch_web_fixtures.py               # once: download images to data/webtest/
    python scripts/eval_condition.py [--max-cost 1.00]

Uses whatever provider/model is configured (GRADEORSKIP_LLM_PROVIDER, OPENAI_MODEL,
ANTHROPIC_MODEL). Responses are cached by (provider, model, prompt version, image),
so re-runs are free unless the prompt or model changes.

Reports:
  - clean false positives: any non-"none" call on an official scan (there's no wear to find)
  - detections: did the model flag the labelled area at >= the labelled severity
  - the resulting grade range for cases with photos
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gradeorskip.centering import CardNotFound, measure_centering  # noqa: E402
from gradeorskip.grading import (CENTERING_MIN_CONFIDENCE, SEVERITIES, Finding, centering_cap,
                              estimate_grade, unmeasured_cap)  # noqa: E402
from gradeorskip.pipeline import decode, to_jpeg  # noqa: E402
from gradeorskip.providers.condition import PROMPT_VERSION, get_provider  # noqa: E402

DATA = ROOT / "data" / "webtest"
RANK = {s: i for i, s in enumerate(SEVERITIES)}


def prepare(path: Path):
    img = decode(path.read_bytes())
    if path.parent.name == "clean":
        # Official scans are tight crops; put them on a mat like a real photo so the
        # card edge (not the inner frame) is what gets detected.
        pad = max(img.shape[:2]) // 8
        img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(45, 45, 45))
    try:
        cen, card, _ = measure_centering(img)
        # Mirror the pipeline: an untrusted warp isn't shown to the model.
        return (card if cen.confidence >= CENTERING_MIN_CONFIDENCE else img), cen
    except CardNotFound:
        return img, None  # close-ups / partial cards: send the photo as-is


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cost", type=float, default=1.0)
    ap.add_argument("--only", help="comma-separated case ids")
    a = ap.parse_args()
    provider = get_provider()
    if provider is None:
        sys.exit("No LLM provider configured")
    labels = json.loads((ROOT / "tests" / "fixtures" / "web_labels.json").read_text())
    cases = labels["cases"]
    if a.only:
        cases = [c for c in cases if c["id"] in a.only.split(",")]
    print(f"provider={provider.name} model={provider.model} prompt={PROMPT_VERSION}\n")

    spent, fp_calls, clean_areas, hits, needed = 0.0, 0, 0, 0, 0
    rows = []
    for case in cases:
        findings, calls, cens = [], {}, {}
        for side, rel in case["files"].items():
            path = DATA / rel
            if not path.exists():
                print(f"skip {case['id']}: missing {rel} (run fetch_web_fixtures.py)")
                break
            if spent >= a.max_cost:
                sys.exit(f"stopped at --max-cost ${a.max_cost}")
            card, cen = prepare(path)
            cens[side] = cen
            resp = provider.assess(to_jpeg(card), side)
            spent += 0 if resp.get("cached") else (resp.get("cost_usd") or 0)
            areas = resp["result"]["areas"]
            calls[side] = areas
            findings += [Finding(x["area"], side, x["severity"], float(x["confidence"]), x.get("notes", ""),
                                 x.get("locations", [])) for x in areas]
        if not calls:
            continue
        summary = "; ".join(f"{s}:{x['area'][0]}={x['severity']}({x['confidence']:.1f})"
                            for s, areas in calls.items() for x in areas if x["severity"] != "none") or "all none"
        verdict = ""
        if case["kind"] == "clean":
            n = sum(1 for areas in calls.values() for x in areas if x["severity"] != "none")
            fp_calls += n
            clean_areas += sum(len(areas) for areas in calls.values())
            verdict = "OK" if n == 0 else f"FP x{n}"
        else:
            for m in case["must_flag"]:
                needed += 1
                got = next((x for x in calls.get(m["side"], []) if x["area"] == m["area"]), None)
                ok = got is not None and RANK[got["severity"]] >= RANK[m["min_severity"]]
                hits += ok
                verdict += ("HIT " if ok else "MISS ") + f"{m['side']} {m['area']}>={m['min_severity']} "
        grade = ""
        ok = {k: c for k, c in cens.items() if c and c.confidence >= CENTERING_MIN_CONFIDENCE}
        if cens.get("front"):
            cap = (centering_cap(ok["front"].worst, ok["back"].worst if "back" in ok else None)
                   if "front" in ok else unmeasured_cap())
            est = estimate_grade(cap, findings, min((c.confidence for c in ok.values()), default=1.0))
            grade = est.label
        rows.append((case["id"], case["kind"], verdict.strip(), grade, summary, case.get("consensus", "")))

    for r in rows:
        print(f"{r[0]:<32} {r[1]:<8} {r[2]:<40} {r[3]:<10} {r[4]}")
        if r[5]:
            print(f"{'':<42}expected: {r[5]}")
    print(f"\nclean scans: {fp_calls} false-positive area calls out of {clean_areas}")
    print(f"labelled defects detected: {hits}/{needed}")
    print(f"new spend this run: ${spent:.4f}")


if __name__ == "__main__":
    main()
