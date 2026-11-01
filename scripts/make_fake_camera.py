# Usage: python scripts/make_fake_camera.py out.y4m front [x-offset as fraction, e.g. 0.12 to misalign]
"""A fake 'camera' video: a synthetic card held where the on-screen guide sits (390x844 viewport)."""
import sys, cv2, numpy as np
sys.path.insert(0, ".")
from gradeorskip.synth import make_card
out, side, misalign = sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
W, H = 1080, 1920
rng = np.random.default_rng(1)
bg = cv2.resize(rng.integers(20, 55, (H // 16, W // 16, 3), dtype=np.uint8), (W, H), interpolation=cv2.INTER_CUBIC)
borders = (36, 24, 30, 30) if side == "front" else (33, 27, 30, 30)   # 60/40 front, 55/45 back
card = make_card(*borders, side=side, seed=3)
# Guide width (app.css): min(52vw, 44vh*63/88) on a 390x844 viewport; video is scaled by 844/1280 (cover).
cw = round(min(0.52 * 390, 0.44 * 844 * 63 / 88) / (844 / H)); ch = round(cw * 88 / 63)
card = cv2.resize(card, (cw, ch), interpolation=cv2.INTER_AREA)
cx, cy = W / 2 + misalign * W, 0.46 * H
x0, y0 = int(cx - cw / 2), int(cy - ch / 2)
frame = bg.copy(); frame[y0:y0 + ch, x0:x0 + cw] = card
frame = cv2.GaussianBlur(frame, (3, 3), 0.7)
yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
with open(out, "wb") as f:
    f.write(f"YUV4MPEG2 W{W} H{H} F30:1 Ip A1:1 C420jpeg\n".encode())
    # Like a hand-held phone: 1-2 px tremor and sensor noise on every frame.
    for _ in range(30):
        dx, dy = rng.integers(-2, 3, 2)
        fr = np.roll(frame, (int(dy), int(dx)), axis=(0, 1)).astype(np.int16)
        fr = np.clip(fr + rng.normal(0, 3, fr.shape), 0, 255).astype(np.uint8)
        f.write(b"FRAME\n"); f.write(cv2.cvtColor(fr, cv2.COLOR_BGR2YUV_I420).tobytes())
