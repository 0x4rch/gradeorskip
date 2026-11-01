# How centering is verified

Measuring borders is the easy part; knowing when the measurement is wrong is what matters. So every
read is treated as a hypothesis to check, the same approach document scanners use. All thresholds
below were set on a small set of real scans (`tests/fixtures/real`, `data/webtest/clean`), so they
are starting points, not validated limits.

| Check | The fact it relies on | Rule |
|---|---|---|
| Whole-frame fit | The printed frame has one fixed size, so left+right = top+bottom however off-centre the card is | If a read doesn't balance (over 20% apart), test every rectangle built from each side's edge peaks; the outermost balanced one with strong edges wins |
| Band check | A border is a uniform band | Colour must barely vary along it (median deviation 8 or less) |
| Corner check | Corners are rounded, so a correct crop's extreme corners show background | A corner matching the border colour means the outline cut into the card |
| Burst consensus | Independent frames of the same card must agree | Median of frames within 4 points; no two agreeing means no result |

A read that fails is reported as **not measured**: it shows no numbers and doesn't cap the grade.

## Evidence

- Real scans: 14 good reads pass; both Mew VMAX fronts and the sleeved Gengar front are rejected.
- 10 clean renders on a mat: 7 trusted (worst 6 points from 50/50), 3 not measured. Before the checks
  these misreads, usually the name bar taken for the top border, came through at full confidence.
- Synthetic set: unchanged, 0.49 points mean error.

Expected values in the real set are repeat-scan consensus and overlays checked by eye, not graded
ground truth. Near-borderless cards (Mew VMAX) can't be measured from a photo this way.
