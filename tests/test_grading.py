import pytest

from gradeorskip.grading import Finding, centering_cap, condition_ceiling, estimate_grade


def F(sev, area="corners", side="front", conf=0.9):
    return Finding(area, side, sev, conf)


@pytest.mark.parametrize("front,back,grade", [
    (50, 50, 10), (55, 60, 10), (60, 75, 10),
    (60.5, 50, 9), (58, 76, 9), (65, 90, 9),
    (68, 50, 8), (72, 50, 7), (78, 50, 6), (84, 50, 5),
    (60, 91, 1), (95, 50, 1),
])
def test_centering_cap_thresholds(front, back, grade):
    assert centering_cap(front, back).grade == grade


def test_centering_cap_without_back():
    assert centering_cap(58, None).grade == 10


def test_near_boundary_flag():
    assert centering_cap(59.5, 50).near_boundary  # 0.5 below the 10 limit
    assert centering_cap(61, 50).near_boundary    # just over 60/40, could be a 10 if mismeasured
    assert not centering_cap(52, 50).near_boundary


def test_condition_ceiling():
    assert condition_ceiling([F("none"), F("none", "edges")])[0] == 10
    assert condition_ceiling([F("minor")])[0] == 9
    assert condition_ceiling([F("minor"), F("minor", "edges")])[0] == 8
    assert condition_ceiling([F("none"), F("moderate", "surface")])[0] == 7
    assert condition_ceiling([F("major")])[0] == 5


def test_probabilities_sum_to_one_and_respect_caps():
    for cap_front in (50, 62, 68, 74):
        cap = centering_cap(cap_front, 50)
        for findings in (None, [F("none")], [F("minor")], [F("major", conf=0.4)]):
            est = estimate_grade(cap, findings)
            assert sum(est.probs.values()) == pytest.approx(1.0)
            assert est.low <= est.high
            # Never more than one grade above the centering cap.
            assert max(est.probs) <= min(10, cap.grade + 1)


def test_clean_card_well_centered_is_a_range_not_a_point():
    est = estimate_grade(centering_cap(52, 55), [F("none"), F("none", "edges"), F("none", "surface")])
    assert est.high == 10 and est.low <= 9
    assert est.label == "PSA 8–10"
    assert est.probs[10] <= 0.25  # the model misses most subtle flaws, so a clean read isn't a 10


def test_clean_read_in_a_limited_photo_counts_for_less():
    clean = [F("none"), F("none", "edges"), F("none", "surface")]
    good = estimate_grade(centering_cap(52, 55), clean, inspection="good")
    limited = estimate_grade(centering_cap(52, 55), clean, inspection="limited")
    assert limited.probs[10] < good.probs[10] and limited.probs[10] <= 0.12
    assert limited.ten_label != "10 plausible"


def test_off_center_caps_grade():
    est = estimate_grade(centering_cap(68, 55), [F("none")])
    assert est.top == 8
    assert est.probs.get(10, 0) == 0
    assert est.ten_label == "10 unlikely"


def test_unassessed_is_wider():
    cap = centering_cap(52, 55)
    a = estimate_grade(cap, [F("none")])
    u = estimate_grade(cap, None)
    assert (u.high - u.low) >= (a.high - a.low)
    assert not u.assessed and any("NOT assessed" in e for e in u.explanation)


def test_low_confidence_defect_leaves_room_above():
    cap = centering_cap(52, 55)
    est = estimate_grade(cap, [F("moderate", conf=0.55)])
    assert est.top == 7 and est.probs.get(8, 0) > 0


def test_unassessed_never_calls_a_10_plausible():
    est = estimate_grade(centering_cap(50, 50), None)
    assert est.probs[10] < 0.3 and est.ten_label != "10 plausible"


def test_low_confidence_calls_count_one_level_milder():
    assert condition_ceiling([F("minor", conf=0.4)])[0] == 10
    assert condition_ceiling([F("major", conf=0.3)])[0] == 7
    assert condition_ceiling([F("major", conf=0.5)])[0] == 5


@pytest.mark.parametrize("front,back,grade", [(55, 65, 10), (56, 50, 9), (55, 70, 9), (60, 75, 9), (64, 80, 8), (70, 50, 7)])
def test_tag_centering_is_tighter_than_psa(front, back, grade):
    assert centering_cap(front, back, "tag").grade == grade


def test_tag_estimate_is_labelled_tag():
    est = estimate_grade(centering_cap(58, 50, "tag"), None, grader="tag")
    assert est.label.startswith("TAG") and est.centering.grade == 9
