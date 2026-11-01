"""Calibration harness.

Two modes:

  1. Grade calibration (needs labelled real cards):
       python scripts/calibrate.py DIR [--no-llm] [--max-cost 2.00] [--out results.csv]
     DIR/labels.csv has columns: front,back,grade   (back may be empty; grade = actual PSA grade 1-10)
     Image paths are relative to DIR. Reports how often the true grade lands inside
     the predicted range, the exact-match rate of the most likely grade, the mean
     probability assigned to the true grade, Brier score, and a breakdown by true grade.

  2. Centering accuracy (synthetic or hand-measured cards):
       python scripts/calibrate.py --centering tests/fixtures/synthetic
     DIR/labels.json as written by scripts/make_fixtures.py (true border ratios).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def centering_mode(folder: Path) -> None:
    import cv2
    from gradeorskip.centering import CardNotFound, measure_centering

    labels = json.loads((folder / "labels.json").read_text())
    errs, rows, failures = [], [], 0
    for name, sides in sorted(labels.items()):
        for side, lab in sides.items():
            img = cv2.imread(str(folder / lab["file"]))
            try:
                res, _, _ = measure_centering(img)
            except CardNotFound:
                failures += 1
                rows.append((name, side, "FAILED", "", ""))
                continue
            e_lr = abs(res.lr[0] - lab["lr"][0])
            e_tb = abs(res.tb[0] - lab["tb"][0])
            errs += [e_lr, e_tb]
            rows.append((name, side, f"{lab['lr'][0]:.1f}/{lab['tb'][0]:.1f}",
                         f"{res.lr[0]:.1f}/{res.tb[0]:.1f}", f"{max(e_lr, e_tb):.1f}"))
    print(f"{'card':<14}{'side':<7}{'true L%/T%':<14}{'measured':<14}max err (pts)")
    for r in rows:
        print(f"{r[0]:<14}{r[1]:<7}{r[2]:<14}{r[3]:<14}{r[4]}")
    n = len(errs)
    if n:
        print(f"\nsides measured: {n // 2}, detection failures: {failures}")
        print(f"mean abs error: {statistics.mean(errs):.2f} pts   median: {statistics.median(errs):.2f}   "
              f"max: {max(errs):.2f}   within 1pt: {sum(e <= 1 for e in errs) / n:.0%}   "
              f"within 2pt: {sum(e <= 2 for e in errs) / n:.0%}")


def grade_mode(folder: Path, use_llm: bool, max_cost: float, out: Path | None) -> None:
    if not use_llm:
        os.environ["GRADEORSKIP_LLM_PROVIDER"] = "none"
    from gradeorskip.pipeline import run_check

    with open(folder / "labels.csv", newline="") as f:
        items = list(csv.DictReader(f))
    results, spent = [], 0.0
    for i, it in enumerate(items, 1):
        if spent >= max_cost:
            print(f"Stopping: spent ${spent:.2f} >= --max-cost {max_cost}")
            break
        front = (folder / it["front"]).read_bytes()
        back = (folder / it["back"]).read_bytes() if it.get("back") else None
        r = run_check(front, back, use_llm=use_llm)
        spent += r.get("cost_usd") or 0
        true = int(it["grade"])
        g = r.get("grade")
        if not g:
            results.append({"front": it["front"], "true": true, "error": "; ".join(r["errors"])})
            continue
        probs = {int(k): v for k, v in g["probs"].items()}
        mode = max(probs, key=probs.get)
        results.append({
            "front": it["front"], "true": true, "low": g["low"], "high": g["high"], "mode": mode,
            "p_true": probs.get(true, 0.0), "in_range": g["low"] <= true <= g["high"],
            "brier": sum((probs.get(k, 0.0) - (1.0 if k == true else 0.0)) ** 2 for k in range(1, 11)),
            "assessed": g["assessed"], "centering_cap": g["centering_cap"]["grade"],
        })
        print(f"[{i}/{len(items)}] {it['front']}: true {true}, predicted {g['label']} (mode {mode}), "
              f"p(true)={probs.get(true, 0):.2f}  spent ${spent:.3f}")

    ok = [r for r in results if "error" not in r]
    if not ok:
        print("No usable results.")
        return
    n = len(ok)
    print(f"\n=== {n} cards ({len(results) - n} failed to process), LLM {'on' if use_llm else 'off'}, "
          f"spent ${spent:.2f} ===")
    print(f"true grade inside predicted range: {sum(r['in_range'] for r in ok) / n:.0%}")
    print(f"most-likely grade exactly right:   {sum(r['mode'] == r['true'] for r in ok) / n:.0%}")
    print(f"most-likely grade within 1:        {sum(abs(r['mode'] - r['true']) <= 1 for r in ok) / n:.0%}")
    print(f"mean p(true grade):                {statistics.mean(r['p_true'] for r in ok):.2f}")
    print(f"mean Brier score (lower = better): {statistics.mean(r['brier'] for r in ok):.3f}")
    print(f"centering cap below true grade:    {sum(r['centering_cap'] < r['true'] for r in ok)} cards "
          "(the cap was too strict: check the measurement or PSA's tolerance)")
    by_true = defaultdict(list)
    for r in ok:
        by_true[r["true"]].append(r)
    print("\ntrue  n   in-range  mean mode  mean p(true)")
    for t in sorted(by_true, reverse=True):
        rs = by_true[t]
        print(f"{t:>4} {len(rs):>3}   {sum(r['in_range'] for r in rs) / len(rs):>6.0%}  "
              f"{statistics.mean(r['mode'] for r in rs):>9.1f}  {statistics.mean(r['p_true'] for r in rs):>11.2f}")
    if out:
        keys = sorted({k for r in results for k in r})
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results)
        print(f"\nwrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path)
    ap.add_argument("--centering", action="store_true", help="score centering against labels.json")
    ap.add_argument("--no-llm", action="store_true", help="centering-only predictions (free)")
    ap.add_argument("--max-cost", type=float, default=2.0, help="stop once LLM spend reaches this many USD")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.centering:
        centering_mode(a.dir)
    else:
        grade_mode(a.dir, not a.no_llm, a.max_cost, a.out)


if __name__ == "__main__":
    main()
