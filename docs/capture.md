# Phone capture

The home page is a two-step scan (front, then back) built for phones.

## Two capture modes

| Mode | When | What the user gets |
|---|---|---|
| **Live guided camera** | The page is a secure context (HTTPS, or `localhost`) and the browser allows camera access | Full-screen rear camera, a card-shaped frame, live hints, a ring on the shutter that fills as **auto-capture** arms (0.6 s steady, or 1.5 s framed and in focus), the sharpest of 3 frames is kept, then it flips straight to the back |
| **Native camera** | Plain `http://` on a phone (e.g. `http://your-server.local`); browsers only allow live camera access on secure origins | Tapping "Scan" opens the phone's own camera app. After each shot, the same quality checks run on the still and the tile shows "✓ Looks good" or a warning with "retake" |

Both modes shrink the photo on the phone to ~2000 px JPEG before uploading (fast on cellular; also converts HEIC where the browser can decode it). The live camera crops to the frame plus an 8% margin, so the server still sees background around the card edge, which card detection needs.

## Quality checks (`static/app.js`)

Measured on a 400 px-wide copy:

| Check | Rule | Calibrated on |
|---|---|---|
| Blur | variance of the Laplacian < 80 | Real card photos scored 320–1045; blurred copies < 30; the two blurry forum close-ups 43–50 |
| Too dark | mean brightness < 70 | Usable photos 92–173; darkened copies 27–51 |
| Glare | > 1% of pixels at ≥ 250 | Glary holo photo 1.4%; others ≤ 0.45% |
| Steady | mean change of 16 px block brightness (exposure drift removed) < 4 | Fake camera with 1-2 px tremor + sensor noise captures in ~1.7 s. The old per-pixel rule (> 6) never passed on a real iPhone |
| Aligned | mean share of each side's rows/columns on one fitted edge line, within 6% of the guide; every side at least 0.25; burst starts at 0.35 | Real iPhone scans; the first rule (3.5% band, worst side) flipped between 0 and 0.9 while the phone was held still |

Once the card is roughly framed and in focus, the phone collects frames for about 0.8 s and uploads the best 3. The shutter button always works.

## iPhone Pro focus

The plain "Back Camera" on iPhone 13 Pro and later can't focus closer than about 20 cm, and in a browser it never switches to the macro lens. The first guide filled 82% of the screen width, which put the phone about 10 cm from the card: the camera hunted for focus forever and auto-capture never fired. Now:
- the guide is 52% of the screen width (card about 20 cm away; still ~900 px wide in a 4K frame);
- the app prefers the "Back Triple Camera" / "Back Dual Wide Camera" device, which switches lenses for close focus;
- each check stores `capture_debug` (camera label, track settings, the last 25 readings) so real-device failures can be diagnosed.

The hint only changes after the same message shows twice in a row, and the green state needs 3 bad readings to drop, so it no longer flickers.

## Not yet verified

The live camera mode was not run on a real phone during development (headless browsers have no camera). The steadiness threshold and auto-capture timing are first guesses.
