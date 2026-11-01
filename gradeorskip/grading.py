"""Turn centering measurements + condition findings into a grade *range*.

The numbers in this file are hand-set priors, NOT fitted to data. They are
deliberately pessimistic (photos hide flaws). Use scripts/calibrate.py with
labelled cards to tune them, and don't present the probabilities as calibrated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# PSA centering tolerances: (grade, max larger-side % on the front, on the back).
# Source: https://www.psacard.com/gradingstandards (PSA describes 10 as
# "approximately 55/45 to 60/40" on the front and 75/25 on the back). Transcribed
# from the published standards; the page could not be fetched automatically on
# 2026-10-28, so re-check if PSA revises them.
PSA_CENTERING = [
    (10, 60.0, 75.0),
    (9, 65.0, 90.0),
    (8, 70.0, 90.0),
    (7, 75.0, 90.0),
    (6, 80.0, 90.0),
    (5, 85.0, 90.0),
    (4, 85.0, 90.0),
    (3, 90.0, 90.0),
    (2, 90.0, 90.0),
    (1, 100.0, 100.0),
]
CENTERING_SOURCE = "https://www.psacard.com/gradingstandards"

# TAG centering for TCG cards, from TAG's rubric (read 2026-10-31). 10 here is Gem Mint (950+); Pristine
# needs about 51/49 front and 52/48 back and isn't modelled separately. The rubric excerpt gave no back
# figure for 7 and nothing below it, so those rows reuse the 8 back limit and PSA's lower rows.
TAG_CENTERING = [(10, 55.0, 65.0), (9, 60.0, 75.0), (8, 65.0, 95.0), (7, 70.0, 95.0), *PSA_CENTERING[4:]]

GRADERS = {
    "psa": {"id": "psa", "name": "PSA", "centering": PSA_CENTERING, "centering_source": CENTERING_SOURCE},
    "tag": {"id": "tag", "name": "TAG", "centering": TAG_CENTERING, "centering_source": "https://taggrading.com/pages/rubric"},
}

# Highest grade each severity usually still allows (prior, uncalibrated).
SEVERITY_CEILING = {"none": 10, "minor": 9, "moderate": 7, "major": 5}
SEVERITIES = list(SEVERITY_CEILING)

# Spread of probability below the "top plausible grade". Index 0 = top grade.
SPREAD_ASSESSED = [0.50, 0.30, 0.14, 0.06]
# Nothing found doesn't mean nothing there: on real photos the model caught 3 of 9 known defects
# (docs/condition_eval.md), so a clean read is weak evidence for a 10, weaker in a poor view.
SPREAD_TOP_IS_10 = [0.20, 0.42, 0.25, 0.13]  # clean read, photo good enough to see fine flaws
SPREAD_TOP_IS_10_LIMITED = [0.10, 0.40, 0.30, 0.20]  # clean read, but the photo could hide them
SPREAD_UNASSESSED = [0.30, 0.28, 0.20, 0.12, 0.10]  # centering only, no condition data
SPREAD_UNASSESSED_TOP_10 = [0.18, 0.32, 0.24, 0.14, 0.12]  # unseen corners/surface make a 10 a long shot


@dataclass
class CenteringCap:
    grade: int
    limiting: str  # e.g. "front left/right 63/37"
    margin: float  # points of slack before dropping to the next grade (negative never happens)
    near_boundary: bool


def centering_cap(front_worst: float, back_worst: float | None, grader: str = "psa") -> CenteringCap:
    """Highest grade both sides' centering allows, per the grader's table."""
    table = GRADERS[grader]["centering"]
    for grade, f_max, b_max in table:
        if front_worst <= f_max and (back_worst is None or back_worst <= b_max):
            margins = [f_max - front_worst]
            if back_worst is not None:
                margins.append(b_max - back_worst)
            margin = min(margins)
            if back_worst is not None and (b_max - back_worst) < (f_max - front_worst):
                limiting = f"back {back_worst:.0f}/{100 - back_worst:.0f} (limit {b_max:.0f}/{100 - b_max:.0f})"
            else:
                limiting = f"front {front_worst:.0f}/{100 - front_worst:.0f} (limit {f_max:.0f}/{100 - f_max:.0f})"
            # Within 2 points of the next-lower threshold, or just over the next-higher one.
            above = next((g for g in table if g[0] == grade + 1), None)
            over_by = None
            if above:
                over_by = max(front_worst - above[1], (back_worst or 0) - above[2])
            near = margin < 2.0 or (over_by is not None and over_by < 2.0)
            return CenteringCap(grade, limiting, round(margin, 1), near)
    return CenteringCap(1, "off-centre", 0.0, False)


CENTERING_MIN_CONFIDENCE = 0.6  # e.g. uneven border + two missing borders = 0.5: not trusted


def unmeasured_cap() -> CenteringCap:
    """Used when the photo's centering measurement can't be trusted: no cap is applied."""
    return CenteringCap(10, "not measured reliably from this photo (retake it flat, straight-on, on a plain background)",
                        0.0, False)


@dataclass
class Finding:
    area: str  # corners | edges | surface
    side: str  # front | back
    severity: str
    confidence: float
    notes: str = ""
    locations: list = field(default_factory=list)


LOW_CONFIDENCE = 0.5


def effective_severity(f: Finding) -> str:
    """Below LOW_CONFIDENCE a call counts one level milder: the vision model's
    unsure calls were mostly false positives in testing (docs/condition_eval.md)."""
    if f.confidence < LOW_CONFIDENCE and f.severity != "none":
        return SEVERITIES[SEVERITIES.index(f.severity) - 1]
    return f.severity


def condition_ceiling(findings: list[Finding]) -> tuple[int, str, float]:
    """(ceiling grade, reason, mean confidence) from all condition findings."""
    if not findings:
        return 10, "no findings", 0.0
    ceiling, reason = 10, "no visible defects reported"
    effective = [effective_severity(f) for f in findings]
    for f, sev in zip(findings, effective):
        c = SEVERITY_CEILING.get(sev, 10)
        if c < ceiling:
            hedge = f" (model unsure: called {f.severity})" if sev != f.severity else ""
            ceiling, reason = c, f"{f.side} {f.area}: {sev}{hedge}"
    minors = [sev for sev in effective if sev == "minor"]
    if ceiling == 9 and len(minors) >= 2:
        ceiling, reason = 8, f"{len(minors)} areas with minor issues"
    conf = sum(f.confidence for f in findings) / len(findings)
    return ceiling, reason, conf


@dataclass
class GradeEstimate:
    probs: dict  # grade -> probability (sums to 1)
    low: int
    high: int
    label: str
    ten_label: str
    top: int
    centering: CenteringCap
    condition_ceiling: int | None
    condition_reason: str
    assessed: bool
    explanation: list

    def to_dict(self) -> dict:
        return {
            "probs": {str(k): round(v, 4) for k, v in sorted(self.probs.items(), reverse=True)},
            "low": self.low, "high": self.high, "label": self.label, "ten_label": self.ten_label,
            "top": self.top, "assessed": self.assessed,
            "centering_cap": self.centering.__dict__,
            "condition_ceiling": self.condition_ceiling, "condition_reason": self.condition_reason,
            "explanation": self.explanation,
        }


def _spread(top: int, weights: list[float]) -> dict[int, float]:
    probs: dict[int, float] = {}
    for i, w in enumerate(weights):
        g = max(1, top - i)
        probs[g] = probs.get(g, 0.0) + w
    return probs


def _normalize(p: dict[int, float]) -> dict[int, float]:
    s = sum(p.values())
    return {k: v / s for k, v in p.items() if v > 0}


def estimate_grade(cap: CenteringCap, findings: list[Finding] | None,
                   centering_confidence: float = 1.0, inspection: str = "good",
                   grader: str = "psa") -> GradeEstimate:
    name = GRADERS[grader]["name"]
    expl: list[str] = []
    assessed = findings is not None and len(findings) > 0
    if cap.limiting.startswith("not measured"):
        expl.append("Centering couldn't be measured reliably from the photos, so it isn't limiting this estimate.")
    else:
        expl.append(f"Centering allows at most a {cap.grade} ({cap.limiting}).")
    if assessed:
        ceil, reason, conf = condition_ceiling(findings)
        top = min(cap.grade, ceil)
        expl.append(f"Condition findings suggest at most about a {ceil} ({reason}).")
        weights = SPREAD_ASSESSED
        if top == 10:
            weights = SPREAD_TOP_IS_10 if inspection == "good" else SPREAD_TOP_IS_10_LIMITED
            if inspection != "good":
                expl.append("The photos could hide fine flaws, so finding none counts for less.")
        probs = _spread(top, weights)
        # Low-confidence condition calls: the model may have over-called a defect.
        if ceil < cap.grade and conf < 0.8:
            up = round(0.15 * (1 - conf) / 0.8, 3)
            probs[top + 1] = probs.get(top + 1, 0) + up
            expl.append(f"Condition confidence is modest ({conf:.0%}), so one grade higher remains possible.")
    else:
        ceil, reason = None, "condition not assessed"
        top = cap.grade
        probs = _spread(top, SPREAD_UNASSESSED_TOP_10 if top == 10 else SPREAD_UNASSESSED)
        expl.append("Corners, edges and surface were NOT assessed, so the range is wide and only reflects centering.")

    # Centering near a threshold, or a shaky measurement: allow one step above the cap.
    if (cap.near_boundary or centering_confidence < 0.7) and top == cap.grade and cap.grade < 10:
        probs[cap.grade + 1] = probs.get(cap.grade + 1, 0) + 0.12
        expl.append(f"Centering sits close to a {name} threshold, so the next grade up is possible if the measurement is slightly off.")

    probs = _normalize(probs)
    low, high = _range(probs, 0.75)
    p10 = probs.get(10, 0.0)
    if p10 < 0.10:
        ten = "10 unlikely"
    elif p10 < 0.30:
        ten = "10 possible"
    else:
        ten = "10 plausible"
    label = f"{name} {low}" if low == high else f"{name} {low}–{high}"
    expl.append("This is an estimate from photos. Photos hide surface flaws, and real grades often come in lower.")
    return GradeEstimate(probs, low, high, label, ten, top, cap, ceil, reason, assessed, expl)


def _range(probs: dict[int, float], mass: float) -> tuple[int, int]:
    """Smallest contiguous grade span around the mode holding >= `mass` probability."""
    mode = max(probs, key=probs.get)
    lo = hi = mode
    total = probs[mode]
    while total < mass - 1e-9:
        below, above = probs.get(lo - 1, 0.0), probs.get(hi + 1, 0.0)
        if below == 0 and above == 0:
            break
        if above > below:
            hi += 1
            total += above
        else:
            lo -= 1
            total += below
    return lo, hi
