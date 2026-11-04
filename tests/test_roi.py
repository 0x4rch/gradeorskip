import pytest

from gradeorskip.roi import RoiInput, compute_roi, load_fees, yes_path


def test_expected_value_math():
    r = compute_roi(RoiInput(probs={10: 0.4, 9: 0.4, 8: 0.2}, raw_price=50,
                             graded_prices={10: 300, 9: 100, 8: 60}, grading_fee=20, shipping=5))
    assert r.ok
    assert r.ev_graded_gross == pytest.approx(0.4 * 300 + 0.4 * 100 + 0.2 * 60)  # 172
    assert r.ev_graded_net == pytest.approx(172 - 25)
    assert r.gain == pytest.approx(147 - 50)
    assert r.verdict == "Grade it"
    # PSA 8 at $60 doesn't cover raw $50 + $25 cost, so that 20% is a loss.
    assert r.p_loss == pytest.approx(0.2)


def test_dont_grade_when_negative():
    r = compute_roi(RoiInput(probs={9: 0.6, 8: 0.4}, raw_price=40, graded_prices={9: 55, 8: 40},
                             grading_fee=25, shipping=5))
    assert r.verdict == "Don't grade" and r.gain < 0


def test_marginal_band():
    # EV gross 100, cost 30, raw 60 -> gain 10, below the max(15, 7.5) threshold.
    r = compute_roi(RoiInput(probs={9: 1.0}, raw_price=60, graded_prices={9: 100}, grading_fee=25, shipping=5))
    assert r.gain == pytest.approx(10) and r.verdict == "Marginal"


def test_selling_fees_apply_to_both_sides():
    r = compute_roi(RoiInput(probs={10: 1.0}, raw_price=100, graded_prices={10: 200}, grading_fee=0,
                             shipping=0, selling_fee_pct=10))
    assert r.ev_graded_net == pytest.approx(180) and r.raw_net == pytest.approx(90)


def test_missing_raw_price():
    r = compute_roi(RoiInput(probs={10: 1.0}, raw_price=None, graded_prices={10: 200}, grading_fee=20, shipping=0))
    assert not r.ok and r.verdict == "Need prices"


def test_missing_likely_grade_price_is_not_invented():
    r = compute_roi(RoiInput(probs={10: 0.5, 9: 0.5}, raw_price=50, graded_prices={10: 300},
                             grading_fee=20, shipping=0))
    assert not r.ok and "psa9" in r.missing


def test_unlikely_grades_fall_back_to_raw_with_assumption():
    r = compute_roi(RoiInput(probs={10: 0.5, 9: 0.4, 8: 0.1}, raw_price=50, graded_prices={10: 300, 9: 120},
                             grading_fee=20, shipping=0))
    assert r.ok
    row8 = next(x for x in r.rows if x["grade"] == 8)
    assert row8["price"] == 50 and row8["source"] == "assumed = raw"
    assert r.assumptions


def test_declared_value_warning():
    r = compute_roi(RoiInput(probs={10: 1.0}, raw_price=500, graded_prices={10: 3000}, grading_fee=80,
                             shipping=0, max_declared_value=1500))
    assert any("declared value" in w for w in r.warnings)


def test_fee_table_is_labelled():
    fees = load_fees()
    assert fees["as_of"] and fees["official_source"].startswith("https://")
    assert fees["verified"] is True and fees["verified_how"]  # re-check when PSA announces a change
    assert all(t["fee"] > 0 for t in fees["tiers"])


def test_long_shot_is_marginal_not_grade_it():
    # Big PSA 10 premium, but 80% of outcomes lose money.
    r = compute_roi(RoiInput(probs={10: 0.2, 9: 0.5, 8: 0.3}, raw_price=300, graded_prices={10: 1500, 9: 330, 8: 280},
                             grading_fee=80, shipping=8))
    assert r.gain > 15 and r.p_loss == pytest.approx(0.8)
    assert r.verdict == "Marginal" and "of the time" in r.reason


def test_missing_low_grade_never_worth_more_than_higher_grade():
    r = compute_roi(RoiInput(probs={9: 0.5, 7: 0.3, 6: 0.2}, raw_price=300, graded_prices={9: 350, 7: 228},
                             grading_fee=80, shipping=8))
    row6 = next(x for x in r.rows if x["grade"] == 6)
    assert row6["price"] == 228 and row6["source"].startswith("assumed")


def test_breakeven_answer_when_graded_prices_missing():
    r = compute_roi(RoiInput(probs={9: 0.5, 8: 0.5}, raw_price=44, graded_prices={}, grading_fee=79.99, shipping=8))
    assert not r.ok and r.breakeven == pytest.approx(131.99)
    assert "$131.99" in r.reason


def test_breakeven_accounts_for_selling_fees():
    r = compute_roi(RoiInput(probs={9: 1.0}, raw_price=100, graded_prices={}, grading_fee=50, shipping=0,
                             selling_fee_pct=20))
    # Sell graded at B: keep 0.8B - 50 must equal 0.8*100 -> B = 162.5
    assert r.breakeven == pytest.approx(162.5)


def test_likely_grade_without_comp_uses_next_grade_as_ceiling():
    # Oricorio ex promo: likely PSA 6 (no comp), PSA 7 sells for $11, raw $9.99. Even valued at
    # the PSA 7 price, grading loses: a firm "don't grade".
    r = compute_roi(RoiInput(probs={7: 0.4, 6: 0.6}, raw_price=9.99, graded_prices={7: 11, 8: 14.32},
                             grading_fee=79.99, shipping=8))
    assert r.ok and r.verdict == "Don't grade"
    assert any(x["source"] == "at most PSA 7" for x in r.rows)


def test_ceiling_cannot_justify_grading():
    r = compute_roi(RoiInput(probs={10: 0.3, 9: 0.7}, raw_price=50, graded_prices={10: 600},
                             grading_fee=20, shipping=0))
    assert not r.ok and r.verdict == "Need prices" and "psa9" in r.missing


def _yes(**over):
    base = dict(probs={10: 0.2, 9: 0.42, 8: 0.25, 7: 0.13}, raw_price=20, graded_prices={10: 400, 9: 120, 8: 40, 7: 25},
                grading_fee=39.99, shipping=8)
    inp = RoiInput(**{**base, **over})
    return inp, compute_roi(inp)


def test_yes_path_max_cost_is_the_real_verdict_boundary():
    for over in ({}, {"selling_fee_pct": 13}, {"graded_prices": {10: 3000, 9: 900, 8: 300, 7: 100}, "raw_price": 200}):
        inp, r = _yes(**over)
        limit = yes_path(inp, r.to_dict())["max_cost"]
        assert limit > 0
        at = compute_roi(RoiInput(**{**inp.__dict__, "grading_fee": limit - inp.shipping}))
        over_it = compute_roi(RoiInput(**{**inp.__dict__, "grading_fee": limit - inp.shipping + 0.02}))
        assert at.verdict == "Grade it" and over_it.verdict != "Grade it"


def test_yes_path_long_shot_is_limited_by_the_chance_of_a_loss():
    inp, r = _yes(graded_prices={10: 1500, 9: 60, 8: 25, 7: 20})
    assert r.verdict == "Marginal"
    path = yes_path(inp, r.to_dict())
    assert path["max_cost"] == pytest.approx(40) and path["pays_at"] == 10 and path["top_grade"] == 10


def test_yes_path_names_the_lowest_grade_that_pays():
    inp, r = _yes(graded_prices={10: 400, 9: 120, 8: 90, 7: 25})
    assert yes_path(inp, r.to_dict())["pays_at"] == 8


def test_yes_path_can_be_impossible():
    inp, r = _yes(raw_price=12, graded_prices={10: 53.78, 9: 25.78, 8: 14.25, 7: 11})
    path = yes_path(inp, r.to_dict())
    assert path["max_cost"] < 0 and path["pays_at"] is None and path["top_adds"] == pytest.approx(41.78)


def test_yes_path_needs_an_answer():
    inp, r = _yes(raw_price=None)
    assert yes_path(inp, r.to_dict()) is None
