"""Generate synthetic card photos with known border widths into tests/fixtures/synthetic/.

Each card gets a front and back photo plus an entry in labels.json with the
true border widths and ratios, so centering accuracy can be scored.
"""
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gradeorskip.centering import _ratio  # noqa: E402
from gradeorskip.synth import make_card, photograph  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "synthetic"

# (name, front borders L,R,T,B, back borders, angle)
CASES = [
    ("perfect", (30, 30, 30, 30), (30, 30, 30, 30), -5),
    ("lr_55_45", (33, 27, 30, 30), (30, 30, 31, 29), 3),
    ("lr_60_40", (36, 24, 30, 30), (32, 28, 30, 30), 7),
    ("tb_62_38", (30, 30, 37, 23), (30, 30, 30, 30), -3),
    ("lr_65_35", (39, 21, 29, 31), (33, 27, 30, 30), 2),
    ("lr_70_30", (42, 18, 30, 30), (36, 24, 30, 30), -8),
    ("both_off", (24, 36, 36, 24), (40, 20, 25, 35), 5),
    ("back_80_20", (31, 29, 30, 30), (48, 12, 30, 30), 0),
    ("lr_75_25", (45, 15, 30, 30), (30, 30, 30, 30), 10),
    ("thin_border", (18, 14, 17, 15), (30, 30, 30, 30), -2),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    labels = {}
    for i, (name, fb, bb, angle) in enumerate(CASES):
        entry = {}
        for side, b in (("front", fb), ("back", bb)):
            img = photograph(make_card(*b, side=side, seed=i), angle=angle, seed=i)
            path = OUT / f"{name}_{side}.jpg"
            cv2.imwrite(str(path), img)
            entry[side] = {"file": path.name, "borders": dict(zip("lrtb", b)),
                           "lr": _ratio(b[0], b[1]), "tb": _ratio(b[2], b[3])}
        labels[name] = entry
    (OUT / "labels.json").write_text(json.dumps(labels, indent=1))
    print(f"wrote {len(CASES)} cards to {OUT}")


if __name__ == "__main__":
    main()
