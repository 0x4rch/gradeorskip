from gradeorskip.centering import CenteringResult
from gradeorskip.pipeline import combine_centering


def cen(lr, tb, conf=1.0):
    return CenteringResult(borders_px={}, lr=(lr, 100 - lr), tb=(tb, 100 - tb), worst=max(lr, 100 - lr, tb, 100 - tb),
                           detection="lines-guided", confidence=conf)


def test_median_of_frames_and_misread_frame_is_outvoted():
    out = combine_centering([cen(55, 50), cen(57, 51), cen(70, 50)])  # third frame misread a border
    assert out["lr"][0] == 56 and out["tb"][0] == 50.5
    assert out["frames"] == {"measured": 3, "agree": 2, "spread": 2}
    assert any("left out" in w for w in out["warnings"])
    assert out["worst"] == 56 and out["confidence"] == 1.0


def test_disagreeing_frames_lower_confidence():
    out = combine_centering([cen(52, 50), cen(56, 50), cen(60, 50)])
    assert out["frames"]["agree"] == 3 and out["frames"]["spread"] == 8
    assert out["confidence"] == 0.8


def test_a_lone_good_frame_in_a_burst_is_not_trusted():
    """How a wrong Mew VMAX reading capped a grade: one frame passed, nothing confirmed it."""
    out = combine_centering([cen(62, 46), cen(80, 50, conf=0.3)], frames_sent=3)
    assert out["confidence"] < 0.6 and any("agree" in w for w in out["warnings"])


def test_frames_that_all_disagree_are_not_averaged():
    out = combine_centering([cen(50, 50), cen(60, 50), cen(70, 50)], frames_sent=3)
    assert out["confidence"] < 0.6


def test_a_single_upload_needs_no_consensus():
    assert combine_centering([cen(55, 50)], frames_sent=1)["confidence"] == 1.0
