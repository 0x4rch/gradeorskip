# Condition read: real-photo evaluation (2026-10-28)

**Bottom line:** the vision model (OpenAI `gpt-5.5`, prompt v2) now rarely invents defects: 1 false call in 30 areas on clean card images, down from 15/30 for the old default. It catches **obvious** damage (edge tears, a scratched holo) but **misses** the bent corner, a dent, a bend across the back, faint corner whitening and print lines (3 of 9 labelled defects found). The product copy now says this plainly, and the grade model discounts low-confidence calls.

Reproduce: `python scripts/fetch_web_fixtures.py && python scripts/eval_condition.py`. Responses are cached by image, model and prompt version, so a re-run is free.

## Test set (`tests/fixtures/web_labels.json`)

Images aren't committed (third-party copyright); the fetch script re-downloads them.

| Group | n | Source | Label quality |
|---|---|---|---|
| Clean renders | 10 | Official pokemontcg.io images of modern cards (sm/swsh/sv), padded onto a dark background like a photo | Strong: digital renders have no physical wear, so any defect call is a false positive |
| Damaged | 5 | Elite Fourum threads: bent corner, dent, bend, two edge tears | Medium: forum consensus, not PSA results |
| Light flaws | 4 | Elite Fourum: back-corner whitening, two print lines, a scratched reverse holo | Medium-weak |

Excluded: WotC-era pokemontcg.io images (they're cropped inside the card, so there are no real corners to judge) and the "Who is best PSA estimator" thread (20 cards with real PSA grades and forum predictions, a perfect set, but its postimg.org images are dead and not archived).

Caveats: small n (19 cases); two tear close-ups carry the poster's red circle; one card is in a toploader; several are hand-held.

## Results

| Model / prompt | False calls on clean (of 30 areas) | Labelled defects found (of 9) | $ per check (2 sides) | s per side |
|---|---|---|---|---|
| gpt-4.1-mini / v1 (old default) | 13 of 18* | 7* (flags nearly everything as "minor") | $0.003 | 3.5 |
| gpt-4.1-mini / v2 | 15 | 6 | $0.003 | 3.5 |
| gpt-5.4-mini / v2 | 8 | 4 | $0.005 | 2.3 |
| **gpt-5.5 / v2 (new default)** | **1** | **3** (4 before the untrusted-warp fix; see below) | **$0.032** | 5.2 |
| gpt-5.5 / v3 (extra "how to look" cues) | 0 | 3 | ~$0.04 | |

\*v1 ran on an earlier version of the clean set (6 images, including the invalid WotC scans).

Costs use OpenAI prices from third-party aggregators ($5/$30 per M tokens for gpt-5.5, $0.75/$4.50 for gpt-5.4-mini), not openai.com. Measured tokens per side: ~1,450 input, ~300 output (including reasoning at `reasoning_effort=low`).

### gpt-5.5 / v2 per case

| Case | Expected | Model said | Grade range shown |
|---|---|---|---|
| JP Charizard, bent corner | PSA ≤ 6 | corners/edges minor; "no definite crease": bend **missed** | PSA 7–8 ✗ |
| Base Charizard, dent on back | PSA 5–7 | dent **missed** (back edge minor) | PSA 7–9 ✗ |
| Card back with a bend | PSA ≤ 6 | bend **missed** (corner/edge minor) | (no front photo) |
| Snorlax edge tear (close-up) | PSA 2–4 | edges **moderate** | – |
| Charizard promo edge tear (close-up, blurry) | PSA 2–4 | edges minor (0.5) | – |
| Rhydon, back-corner whitening, toploader | PSA 8–9 | whitening **missed** | PSA 6–8 |
| Raichu GX print line (scan) | PSA 9 | **missed** | PSA 8–9 |
| Charizard GX print line (photo) | PSA 9 | **missed** | PSA 9–10 (optimistic) |
| Riolu reverse holo, scratched | scratched | surface **moderate** | PSA 6–7 |

## What changed in the code

1. **Prompt v2** (`providers/condition.py`): "none" is the default; an explicit list of non-defects (rounded corners, noise/JPEG, holo texture, glare, dust, sleeves and slab plastic); every non-"none" call needs a location box.
2. **Post-processing:** an unlocated "minor" call is dropped, unlocated moderate/major calls are capped at 0.5 confidence, and duplicate area entries are merged (worst wins).
3. **Default model** `gpt-4.1-mini` → `gpt-5.5` (`reasoning_effort=low`). gpt-5.4-mini stays available via `OPENAI_MODEL` for about a sixth of the cost and ~8× the false calls.
4. **Grade model:** calls below 0.5 confidence count one severity level milder.
5. **Centering trust:** a side whose centering confidence is under 0.6 no longer caps the grade. The page tells the user to retake the photo. (The bad Japanese Charizard warp had produced a bogus 90/10 reading that capped it at PSA 3.)
6. **PNG alpha:** transparent corners are composited onto a dark background instead of turning black (which looked like corner damage).
7. **UI copy:** the condition section now names what the model misses and says "none" doesn't mean flawless.
8. **Untrusted warps aren't sent to the model.** When centering confidence is low, the model sees the original photo. The Japanese Charizard's earlier "moderate corners" hit came from a bad crop that cut off the card's right side; on the real photo the model calls the corners minor and finds no crease. So the honest result is 3/9, not 4/9.

## Real-photo centering (no ground truth, visual check only)

Of 10 real photos: 3 produced a confident measurement that looks right by eye; 2 found no card (a hand-held back at an angle, a dark back on a dark surface); 5 were low-confidence (0.05–0.5) and are now ignored for the grade cap. One of them, the Japanese Charizard (a wrong warp, confidence 0.5), had slipped through the old 0.5 threshold. Synthetic accuracy (0.5-point mean error) **does not carry over** to casual phone photos. Detection on hand-held, angled, sleeved and full-art cards is the next centering work item.

## Real phone photos of clean cards (2026-11-04)

The renders above have no sparkle, light-catching edges or camera softness, so they flattered the model. The capture lab added four pack-fresh, sleeved cards the owner checked in hand (Mega Charizard X ex promo, Mega Gengar ex JP, Heliolisk, gold Pikachu ex), photographed with the live camera on an iPhone. They are local only (`data/webtest/lab/`, cases `lab_*` in `web_labels.json`). The eval now mirrors the pipeline: a 2x straightened card, its corner close-up, and the number strip on fronts.

| Prompt | False calls, real clean (of 24) | False calls, renders (of 30) | Labelled defects found (of 8) | Lab cards' grades |
|---|---|---|---|---|
| v6 (corner close-up) | 5, all corners/edges, up to moderate | 0 | 3 | 5–7, 5–7, 8–9, 8–10 |
| v7 (phone-frame context) | 1 | 0 | 2 (lost the blurry promo tear) | all 8–10 |
| **v7b** (+ tears, chips, creases always count) | 2, both minor corners | 0 | 3 | **all 8–10** |

Decision (owner, 2026-11-04): a person checks whitening and nicks in hand far better than a camera, so the read should lean clean. A minor corner or edge call no longer lowers the grade; the page says to check corners and edges in hand. Cost: the bent-corner JP Charizard now reads 8–10. It reached 6–7 before only through back corner/edge calls; neither prompt ever saw the bend.
