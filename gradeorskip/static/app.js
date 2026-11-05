// GradeOrSkip front end: guided capture, photo quality checks, card search. No framework.

// ---------------------------------------------------------------- helpers
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const $ = (sel, root = document) => root.querySelector(sel);

// Full-screen layers: everything beside them leaves the tab order and the screen reader's view.
function setModal(el, on) {
  for (let node = el; node !== document.body; node = node.parentElement) {
    for (const other of node.parentElement.children) if (other !== node) other.inert = on;
  }
}

// Photo quality, measured on a ~400px-wide copy. Thresholds were calibrated on real
// card photos: sharp phone shots score 300-1000+, blurry ones under ~50; usable
// brightness was 90-175 (darkened copies were under 55).
const Q = { blurry: 80, dark: 70, glare: 0.01 };

function measure(source, crop) {
  const w = 400;
  const sw = crop ? crop.w : source.videoWidth || source.width;
  const sh = crop ? crop.h : source.videoHeight || source.height;
  const h = Math.round((w * sh) / sw);
  const cv = measure.canvas || (measure.canvas = document.createElement("canvas"));
  cv.width = w; cv.height = h;
  const ctx = cv.getContext("2d", { willReadFrequently: true });
  if (crop) ctx.drawImage(source, crop.x, crop.y, crop.w, crop.h, 0, 0, w, h);
  else ctx.drawImage(source, 0, 0, w, h);
  const d = ctx.getImageData(0, 0, w, h).data;
  const y = new Float32Array(w * h);
  let sum = 0, bright = 0;
  for (let i = 0, j = 0; i < d.length; i += 4, j++) {
    const v = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    y[j] = v; sum += v; if (v > 250) bright++;
  }
  let n = 0, m = 0, m2 = 0;
  for (let r = 1; r < h - 1; r++) {
    for (let c = 1; c < w - 1; c++) {
      const k = r * w + c;
      const lap = 4 * y[k] - y[k - 1] - y[k + 1] - y[k - w] - y[k + w];
      n++; const delta = lap - m; m += delta / n; m2 += delta * (lap - m);
    }
  }
  return { brightness: sum / (w * h), glare: bright / (w * h), sharpness: m2 / n, y, w, h, rgba: d };
}

// How well the card's edges line up with the guide. A real card edge is a long, straight
// line: in a thin band around each guide line, count the rows (or columns) whose strongest
// colour edge sits on one fitted line. Colour, not brightness, because a blue card back on
// a dark table differs mostly in hue. On simulated phone views this scored 1.0 when aligned
// and 0.0 when shifted 8% or zoomed out (see docs/capture.md). The crop has an 8% margin,
// so the guide lines sit at 8% and 92%.
function alignment(q) {
  const { rgba: d, w, h } = q;
  const at = (r, c, k) => d[(r * w + c) * 4 + k];
  const edge = (r, c, vertical) => {
    let best = 0;
    for (let k = 0; k < 3; k++) {
      const g = vertical ? Math.abs(at(r, c + 1, k) - at(r, c - 1, k)) : Math.abs(at(r + 1, c, k) - at(r - 1, c, k));
      if (g > best) best = g;
    }
    return best;
  };
  const side = (vertical, lo, hi, a0, a1) => {
    const pos = [], strong = [];
    for (let t = a0; t < a1; t++) {
      let peak = 0, at_ = 0;
      for (let p = lo; p < hi; p++) {
        const g = vertical ? edge(t, p, true) : edge(p, t, false);
        if (g > peak) { peak = g; at_ = p; }
      }
      pos.push(at_); strong.push(peak > 20);
    }
    const fit = (mask) => {
      let n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
      pos.forEach((p, i) => { if (mask[i]) { n++; sx += i; sy += p; sxx += i * i; sxy += i * p; } });
      if (n < 5) return null;
      const k = (n * sxy - sx * sy) / (n * sxx - sx * sx || 1);
      return [k, (sy - k * sx) / n];
    };
    let line = fit(strong);
    if (!line) return 0;
    const near = (tol) => pos.map((p, i) => strong[i] && Math.abs(p - (line[0] * i + line[1])) <= tol);
    line = fit(near(2.5)) || line;
    const ok = near(2);
    return ok.filter(Boolean).length / pos.length;
  };
  const mx = Math.round(0.08 * w), my = Math.round(0.08 * h);
  // Bands of +-6% around each guide line: on a real iPhone the old +-3.5% flipped between
  // 0 and 0.9 from one frame to the next as the hand drifted.
  const bw = Math.max(4, Math.round(0.06 * w)), bh = Math.max(4, Math.round(0.06 * h));
  const y0 = Math.round(my + 0.15 * (h - 2 * my)), y1 = Math.round(h - my - 0.15 * (h - 2 * my));
  const x0 = Math.round(mx + 0.15 * (w - 2 * mx)), x1 = Math.round(w - mx - 0.15 * (w - 2 * mx));
  const s = [
    side(true, Math.max(1, mx - bw), mx + bw, y0, y1), side(true, w - mx - bw, Math.min(w - 1, w - mx + bw), y0, y1),
    side(false, Math.max(1, my - bh), my + bh, x0, x1), side(false, h - my - bh, Math.min(h - 1, h - my + bh), x0, x1),
  ];
  // One faint edge (silver border on a light mat) shouldn't zero the score: average the
  // sides, as long as every side shows at least some of its edge.
  const mean = (s[0] + s[1] + s[2] + s[3]) / 4;
  return Math.min(...s) >= 0.25 ? mean : Math.min(...s);
}

// Find the card anywhere in view, so the person never has to fit it to a frame. The surface is
// whatever colour the view's outer ring is; the card is the biggest card-shaped region that
// differs from it. Returns its rotated rectangle and outline in source pixels, or null.
// Built from a real iPhone recording (2026-11-04): a full-art card on dark fabric, where the old
// fixed guide pulled the phone in until the lens switched to macro and the photos went grainy.
function findCard(source, vis) {
  const W = 160, H = Math.max(40, Math.round((W * vis.h) / vis.w));
  const cv = findCard.canvas || (findCard.canvas = document.createElement("canvas"));
  cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(source, vis.x, vis.y, vis.w, vis.h, 0, 0, W, H);
  const d = ctx.getImageData(0, 0, W, H).data;
  const N = W * H;
  const inRing = (r, c) => r < 4 || r >= H - 4 || c < 4 || c >= W - 4;

  // The surface: median colour of a 4 px ring around the view.
  const ring = [[], [], []];
  for (let r = 0; r < H; r++) for (let c = 0; c < W; c++) {
    if (!inRing(r, c)) continue;
    const i = (r * W + c) * 4;
    ring[0].push(d[i]); ring[1].push(d[i + 1]); ring[2].push(d[i + 2]);
  }
  const med = ring.map((a) => a.sort((x, y) => x - y)[a.length >> 1]);
  const dist = new Float32Array(N);
  for (let j = 0, i = 0; j < N; j++, i += 4) {
    dist[j] = Math.max(Math.abs(d[i] - med[0]), Math.abs(d[i + 1] - med[1]), Math.abs(d[i + 2] - med[2]));
  }
  // Threshold above the surface's own texture (fabric, wood grain): its 90th percentile, padded.
  const ringD = [];
  for (let r = 0; r < H; r++) for (let c = 0; c < W; c++) if (inRing(r, c)) ringD.push(dist[r * W + c]);
  ringD.sort((x, y) => x - y);
  const T = Math.max(28, 1.5 * ringD[Math.floor(ringD.length * 0.9)] + 8);
  let mask = new Uint8Array(N);
  for (let j = 0; j < N; j++) mask[j] = dist[j] > T ? 1 : 0;
  // Close small gaps (dark art meeting a dark surface): dilate, then erode, 3x3.
  const morph = (src, keep) => {
    const out = new Uint8Array(N);
    for (let r = 1; r < H - 1; r++) for (let c = 1; c < W - 1; c++) {
      let n = 0;
      for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) n += src[(r + dr) * W + c + dc];
      out[r * W + c] = keep(n) ? 1 : 0;
    }
    return out;
  };
  mask = morph(morph(mask, (n) => n > 0), (n) => n === 9);

  // Largest connected region.
  const label = new Int32Array(N), stack = new Int32Array(N);
  let best = 0, bestArea = 0, next = 0;
  for (let s = 0; s < N; s++) {
    if (!mask[s] || label[s]) continue;
    next++; let top = 0, area = 0;
    stack[top++] = s; label[s] = next;
    while (top) {
      const p = stack[--top]; area++;
      const r = (p / W) | 0, c = p - r * W;
      if (c > 0 && mask[p - 1] && !label[p - 1]) { label[p - 1] = next; stack[top++] = p - 1; }
      if (c < W - 1 && mask[p + 1] && !label[p + 1]) { label[p + 1] = next; stack[top++] = p + 1; }
      if (r > 0 && mask[p - W] && !label[p - W]) { label[p - W] = next; stack[top++] = p - W; }
      if (r < H - 1 && mask[p + W] && !label[p + W]) { label[p + W] = next; stack[top++] = p + W; }
    }
    if (area > bestArea) { bestArea = area; best = next; }
  }
  if (bestArea < 0.02 * N) return null;

  // Convex hull of the region's row extents (monotone chain), then the smallest rotated rectangle.
  const pts = [];
  for (let r = 0; r < H; r++) {
    let lo = -1, hi = -1;
    for (let c = 0; c < W; c++) if (label[r * W + c] === best) { if (lo < 0) lo = c; hi = c; }
    if (lo >= 0) pts.push([lo, r], [hi + 1, r], [lo, r + 1], [hi + 1, r + 1]);
  }
  pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const p of pts) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
  for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
  const hull = lower.slice(0, -1).concat(upper.slice(0, -1));
  let rect = null;
  for (let i = 0; i < hull.length; i++) {
    const a = hull[i], b = hull[(i + 1) % hull.length];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (!len) continue;
    const ux = (b[0] - a[0]) / len, uy = (b[1] - a[1]) / len;
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (const p of hull) {
      const u = p[0] * ux + p[1] * uy, v = -p[0] * uy + p[1] * ux;
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const area = (u1 - u0) * (v1 - v0);
    if (!rect || area < rect.area) rect = { area, ux, uy, u0, u1, v0, v1 };
  }
  if (!rect) return null;
  const corner = (u, v) => [u * rect.ux - v * rect.uy, u * rect.uy + v * rect.ux];
  const rc = [corner(rect.u0, rect.v0), corner(rect.u1, rect.v0), corner(rect.u1, rect.v1), corner(rect.u0, rect.v1)];
  const sideA = rect.u1 - rect.u0, sideB = rect.v1 - rect.v0;
  const ratio = Math.min(sideA, sideB) / Math.max(sideA, sideB);
  // The outline: the hull point nearest each rectangle corner. A phone held at an angle makes
  // this a trapezoid, and opposite sides of different length mean "hold the phone flat".
  const near = (k) => hull.reduce((m, p) => (Math.hypot(p[0] - k[0], p[1] - k[1]) < Math.hypot(m[0] - k[0], m[1] - k[1]) ? p : m), hull[0]);
  const quad = rc.map(near);
  const L = (a, b) => Math.hypot(quad[a][0] - quad[b][0], quad[a][1] - quad[b][1]);
  const skew = (a, b) => Math.abs(a - b) / Math.max(a, b, 1);
  const tilt = Math.max(skew(L(0, 1), L(3, 2)), skew(L(1, 2), L(0, 3)));
  // Rotation from upright, folded into -45..45 degrees.
  const deg = (Math.atan2(rect.uy, rect.ux) * 180) / Math.PI;
  const angle = (((deg % 90) + 135) % 90) - 45;
  const xs = rc.map((p) => p[0]), ys = rc.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const edge = 3;  // this close to the view's edge = cut off there (closing the mask blanks the outer 2 px)
  const sx = vis.w / W, sy = vis.h / H;
  return {
    ok: ratio > 0.62 && ratio < 0.8 && bestArea / rect.area > 0.8,
    tilt, angle,
    size: Math.min(sideA, sideB) / W,  // the card's short side as a share of the view's width
    cut: { left: x0 < edge, right: x1 > W - edge, top: y0 < edge, bottom: y1 > H - edge },
    quad: quad.map(([x, y]) => [vis.x + x * sx, vis.y + y * sy]),
    box: { x: vis.x + x0 * sx, y: vis.y + y0 * sy, w: (x1 - x0) * sx, h: (y1 - y0) * sy },
  };
}

function problems(q) {
  const out = [];
  if (q.brightness < Q.dark) out.push("Too dark: add light");
  if (q.glare > Q.glare) out.push("Glare: tilt the card or move away from the light");
  if (q.sharpness < Q.blurry) out.push("Blurry: hold steady and tap to focus");
  return out;
}

async function loadBitmap(file) {
  try { return await createImageBitmap(file, { imageOrientation: "from-image" }); }
  catch (e) {
    const img = new Image();
    img.src = URL.createObjectURL(file);
    await img.decode();
    return img;
  }
}

// Downscale on the phone before upload: a 12 MP photo becomes ~2000 px (well over what
// the server uses) and uploads in a fraction of the time. Re-encoding also turns
// HEIC into JPEG where the browser can decode it.
function toJpeg(source, crop, maxSide = 2000) {
  const sw = crop ? crop.w : source.videoWidth || source.width;
  const sh = crop ? crop.h : source.videoHeight || source.height;
  const s = Math.min(1, maxSide / Math.max(sw, sh));
  const cv = document.createElement("canvas");
  cv.width = Math.round(sw * s); cv.height = Math.round(sh * s);
  const ctx = cv.getContext("2d");
  if (crop) ctx.drawImage(source, crop.x, crop.y, crop.w, crop.h, 0, 0, cv.width, cv.height);
  else ctx.drawImage(source, 0, 0, cv.width, cv.height);
  return new Promise((res) => cv.toBlob(res, "image/jpeg", 0.9));
}

// ---------------------------------------------------------------- scan flow
const app = $("#scan-app");
if (app) {
  const shots = { front: null, back: null };
  let current = "front";
  let skipBack = false;
  const hasLiveCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const startBtn = $("#start-scan"), analyzeBtn = $("#analyze"), skipLink = $("#skip-back");

  function render() {
    for (const side of ["front", "back"]) {
      const tile = $(`[data-shot="${side}"]`), img = $("img", tile), sub = $(".shot-label .small", tile);
      if (shots[side]) {
        img.src = URL.createObjectURL(shots[side].blob); img.hidden = false;
        tile.classList.add("done");
        tile.classList.toggle("warn", shots[side].warnings.length > 0);
        sub.textContent = shots[side].warnings.length ? shots[side].warnings[0] : "Looks good · tap to retake";
      } else {
        img.hidden = true; tile.classList.remove("done", "warn");
        sub.textContent = side === "back" && skipBack ? "Skipped" : "Tap to scan";
      }
    }
    const warn = $("#shot-warnings");
    warn.innerHTML = "";
    for (const side of ["front", "back"]) {
      if (shots[side] && shots[side].warnings.length) {
        const div = document.createElement("div");
        div.className = "notice small";
        div.textContent = `${side[0].toUpperCase() + side.slice(1)}: ${shots[side].warnings.join("; ")}. Retaking will give a better read.`;
        warn.appendChild(div);
      }
    }
    const tiltBtn = $("#tilt-btn"), tiltImg = $("img", tiltBtn);
    tiltBtn.hidden = !shots.front;
    if (shots.tilt) {
      tiltImg.src = URL.createObjectURL(shots.tilt.blob); tiltImg.hidden = false;
      $("strong", tiltBtn).textContent = "Tilt shot added";
      $(".small", tiltBtn).textContent = "tap to retake";
    } else {
      tiltImg.hidden = true;
      $("strong", tiltBtn).textContent = "+ Tilt shot";
      $(".small", tiltBtn).textContent = "optional · reveals scratches, print lines and dents";
    }
    const ready = shots.front && (shots.back || skipBack);
    analyzeBtn.hidden = !ready;
    startBtn.hidden = !!ready;
    startBtn.textContent = shots.front ? "Scan back" : "Scan front";
    skipLink.hidden = !(shots.front && !shots.back && !skipBack);
  }

  async function accept(side, blob, warnings, guided = false, debug = null, alts = [], outlines = []) {
    shots[side] = { blob, warnings, guided, debug, alts, outlines };
    if (side === "back") skipBack = false;
    if (navigator.vibrate) navigator.vibrate(40);
    render();
  }

  // Still photo from the native camera or an upload: check it, downscale, keep it.
  async function fromFile(side, file) {
    if (!file) return;
    const bmp = await loadBitmap(file);
    const q = measure(bmp);
    const blob = await toJpeg(bmp);
    await accept(side, blob, problems(q));
    // (No auto-advance to the back here: browsers only open the camera from a tap.)
  }

  const native = $("#native-camera");
  native.addEventListener("change", () => { fromFile(native.dataset.side, native.files[0]); native.value = ""; });
  document.querySelectorAll("[data-upload]").forEach((inp) =>
    inp.addEventListener("change", () => fromFile(inp.dataset.upload, inp.files[0])));

  function capture(side) {
    current = side;
    if (hasLiveCamera) openCamera(side);
    else { native.dataset.side = side; native.click(); }
  }

  startBtn.addEventListener("click", () => capture(shots.front ? "back" : "front"));
  document.querySelectorAll("[data-shot]").forEach((t) => t.addEventListener("click", () => capture(t.dataset.shot)));
  $("#tilt-btn").addEventListener("click", () => capture("tilt"));
  skipLink.addEventListener("click", (e) => { e.preventDefault(); skipBack = true; render(); });

  analyzeBtn.addEventListener("click", async () => {
    const fd = new FormData();
    fd.append("front", shots.front.blob, "front.jpg");
    if (shots.back) fd.append("back", shots.back.blob, "back.jpg");
    if (shots.tilt) fd.append("tilt", shots.tilt.blob, "tilt.jpg");
    // Extra frames from the burst: the server keeps whichever it measures best.
    for (const s of ["front", "back"]) {
      (shots[s] && shots[s].alts || []).forEach((b, i) => fd.append(`${s}_alt`, b, `${s}_alt${i}.jpg`));
    }
    // Tell the server which photos came from the guided camera (card sits on the frame lines),
    // and where the camera saw the card in each one (main photo first, then the alternates).
    fd.append("guided", ["front", "back"].filter((s) => shots[s] && shots[s].guided).join(","));
    const outlines = {};
    for (const s of ["front", "back"]) if (shots[s] && (shots[s].outlines || []).some(Boolean)) outlines[s] = shots[s].outlines;
    if (Object.keys(outlines).length) fd.append("outlines", JSON.stringify(outlines));
    // Camera diagnostics (which lens, focus/steadiness readings before the shot) for tuning.
    const dbg = {};
    for (const s of ["front", "back"]) if (shots[s] && shots[s].debug) dbg[s] = shots[s].debug;
    dbg.ua = navigator.userAgent;
    fd.append("capture_debug", JSON.stringify(dbg));
    const overlay = $("#analyzing"), step = $("#analyzing-step");
    overlay.hidden = false;
    setModal(overlay, true);
    overlay.focus();
    const steps = ["Uploading…", "Measuring centering…", "Checking corners, edges and surface…", "Identifying the card…", "Looking up prices…"];
    let i = 0;
    const tick = setInterval(() => { step.textContent = steps[Math.min(++i, steps.length - 1)]; }, 2200);
    try {
      const res = await fetch("/check", { method: "POST", body: fd });
      if (!res.ok) throw new Error(`Server said ${res.status}`);
      window.location = res.url;  // fetch followed the redirect to /check/{id}
    } catch (e) {
      clearInterval(tick);
      overlay.hidden = true;
      setModal(overlay, false);
      alert("Something went wrong: " + e.message + ". Try again.");
    }
  });

  // ------------------------------------------------------------ live camera
  // Why it's built this way (iPhone Pro testing): the plain "Back Camera" can't focus closer
  // than ~20 cm and the browser never switches it to the macro lens, so a big on-screen guide
  // (which makes you hold the phone ~10 cm away) meant the camera hunted for focus forever.
  // So: prefer the multi-lens virtual camera (it switches lenses for close focus), keep the
  // guide small enough that the card fills it ~22 cm away (36vw, docs/capture.md), judge steadiness on coarse blocks (hand
  // tremor and holo shimmer don't count), and smooth the state so the hint doesn't flicker.
  const cam = $("#camera"), video = $("video", cam), hint = $("#cam-hint"), frameEl = $("#cam-frame");
  const lab = (event, data) => { try { if (window.captureLab) window.captureLab[event](data); } catch (e) { /* never break capture */ } };
  const shutterBtn = $("#shutter"), camStatus = $("#cam-status");
  let returnFocus = null, announceTimer = null, lastAnnounced = 0;
  let stream = null, timer = null, running = false, lastBlocks = null;
  let st = null;  // per-side capture state
  // Burst capture: once the card is roughly framed and in focus, every check grabs a frame;
  // after BURST_N such checks the best few go to the server, which keeps the one whose card
  // outline it measures most reliably. No single frame has to be perfect.
  // 5 good checks in a row (~0.8 s): lab round 3 lost a run of 5 sharp frames to one soft one at 7.
  const BURST_N = 5, KEEP = 3, CLOSE_ALIGN = 0.35;
  // Where the card should sit, as its short side's share of the view's width, at 1x (times the zoom). The guide is 36%:
  // about 22 cm away on a phone's main lens. Much closer and an iPhone Pro switches to its
  // macro lens and the picture turns grainy, so "too close" is as firm as "too far".
  // Lab round 2: people settle at about 0.25 x zoom and those frames were sharp, so that's the floor.
  const SIZE_MIN = 0.25, SIZE_MAX = 0.47, MAX_TILT = 0.07, MAX_TURN = 8;
  const LOST_FALLBACK = 15;  // checks without a card before falling back to the fixed guide
  // From the capture lab (13 sessions on an iPhone, 2026-11-04): sharpness swung 10-40x between
  // consecutive frames as focus hunted and the hand moved, and sharp frames of one card scored
  // 1,000-5,000 (fronts) or ~1,000-2,000 (backs). So a frame counts as sharp against the best this
  // card has shown lately, not a fixed number; and steady holds read motion 4-9, while the one
  // smeared photo was taken at 36.
  const MOTION_MAX = 10, SHARP_SHARE = 0.55, PEAK_WINDOW = 30;
  const resetState = () => {
    st = { close: 0, burst: [], msg: "", pending: "", pendingN: 0, samples: [], lost: 0, crop: null, motion: 0, recent: [], quad: null, shift: 99 };
    lastBlocks = null;
  };

  // The part of the video the screen shows (object-fit: cover), in video pixels.
  function view() {
    const vr = video.getBoundingClientRect(), vw = video.videoWidth, vh = video.videoHeight;
    const scale = Math.max(vr.width / vw, vr.height / vh);
    const w = vr.width / scale, h = vr.height / scale;
    return { x: (vw - w) / 2, y: (vh - h) / 2, w, h, scale, vr };
  }

  // The card's corners as fractions of a crop: sent with each photo so the server searches for the
  // card's edges right where the camera saw them, not near a fixed line.
  const outlineIn = (quad, crop) => quad.map(([x, y]) => [+((x - crop.x) / crop.w).toFixed(4), +((y - crop.y) / crop.h).toFixed(4)]);

  // Crop around the card found, with the same 8% margin the guide crop uses, so the server's
  // guided edge search finds the card where it expects it.
  function cardCrop(card) {
    const vw = video.videoWidth, vh = video.videoHeight, b = card.box;
    const mx = (b.w * 0.08) / 0.84, my = (b.h * 0.08) / 0.84;
    const x = Math.max(0, b.x - mx), y = Math.max(0, b.y - my);
    return { x, y, w: Math.min(b.w + 2 * mx, vw - x), h: Math.min(b.h + 2 * my, vh - y) };
  }

  const outline = $("#cam-outline"), outlinePoly = outline && $("polygon", outline);
  function drawOutline(card, v, good) {
    if (!outline) return;
    if (!card) { outline.classList.remove("on"); return; }
    outline.setAttribute("viewBox", `0 0 ${v.vr.width} ${v.vr.height}`);
    outlinePoly.setAttribute("points", card.quad.map(([x, y]) => `${((x - v.x) * v.scale).toFixed(1)},${((y - v.y) * v.scale).toFixed(1)}`).join(" "));
    outline.classList.add("on");
    outline.classList.toggle("good", good);
  }

  // What to change, most important first, or "" when the card is placed well.
  function placement(card) {
    const c = card.cut, n = c.left + c.right + c.top + c.bottom;
    if (n >= 2 || (c.left && c.right) || (c.top && c.bottom)) return "Move back so the whole card shows";
    if (c.left) return "Move the phone left";
    if (c.right) return "Move the phone right";
    if (c.top) return "Move the phone up";
    if (c.bottom) return "Move the phone down";
    if (card.size > Math.min(0.85, SIZE_MAX * zoom)) return "Move back a little: too close to focus";
    if (card.size < SIZE_MIN * zoom) return "Move a little closer";
    if (card.tilt > MAX_TILT) return "Hold the phone flat, straight above the card";
    if (Math.abs(card.angle) > MAX_TURN) return "Turn the phone to line up with the card";
    return "";
  }

  function guideCrop() {
    // Map the on-screen guide (object-fit: cover) to video pixels, plus an 8% margin.
    const vr = video.getBoundingClientRect(), fr = frameEl.getBoundingClientRect();
    const vw = video.videoWidth, vh = video.videoHeight;
    const scale = Math.max(vr.width / vw, vr.height / vh);
    const ox = (vr.width - vw * scale) / 2, oy = (vr.height - vh * scale) / 2;
    let x = (fr.left - vr.left - ox) / scale, y = (fr.top - vr.top - oy) / scale;
    let w = fr.width / scale, h = fr.height / scale;
    const mx = (w * 0.08) / 0.84, my = (h * 0.08) / 0.84;
    x -= mx; y -= my; w += 2 * mx; h += 2 * my;
    x = Math.max(0, x); y = Math.max(0, y);
    return { x, y, w: Math.min(w, vw - x), h: Math.min(h, vh - y) };
  }

  // Mean luma of 16 px blocks: a 1-2 px hand tremor barely moves these, a real move does.
  function blocks(q) {
    const B = 16, bw = Math.floor(q.w / B), bh = Math.floor(q.h / B), out = new Float32Array(bw * bh);
    for (let r = 0; r < bh * B; r++) {
      const row = r * q.w, br = Math.floor(r / B) * bw;
      for (let c = 0; c < bw * B; c++) out[br + Math.floor(c / B)] += q.y[row + c];
    }
    for (let i = 0; i < out.length; i++) out[i] /= B * B;
    return out;
  }
  function motionOf(b) {
    if (!lastBlocks) { lastBlocks = b; return 99; }
    if (lastBlocks.length !== b.length) { lastBlocks = b; return st.motion; }  // the crop followed the card
    let mean = 0;
    for (let i = 0; i < b.length; i++) mean += b[i] - lastBlocks[i];
    mean /= b.length;  // auto-exposure shifts every block alike: ignore that
    let m = 0;
    for (let i = 0; i < b.length; i++) m += Math.abs(b[i] - lastBlocks[i] - mean);
    lastBlocks = b;
    return m / b.length;
  }

  const VIDEO = { width: { ideal: 3840 }, height: { ideal: 2160 } };
  let cameraLabel = "", zoom = 1;
  async function getStream() {
    let s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, ...VIDEO }, audio: false });
    try {
      // Labels are only readable after permission. iPhone Pro: "Back Triple Camera" / "Back Dual
      // Wide Camera" switch to the ultra-wide macro lens up close; "Back Camera" alone can't.
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      const pick = devs.find((d) => /triple/i.test(d.label)) || devs.find((d) => /dual wide/i.test(d.label));
      const cur = s.getVideoTracks()[0].getSettings().deviceId;
      if (pick && pick.deviceId && pick.deviceId !== cur) {
        s.getTracks().forEach((t) => t.stop());
        try {
          s = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: pick.deviceId }, ...VIDEO }, audio: false });
        } catch (e) {
          s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, ...VIDEO }, audio: false });
        }
      }
    } catch (e) { /* keep the default camera */ }
    const track = s.getVideoTracks()[0];
    cameraLabel = track.label || "";
    try {  // continuous autofocus where the browser exposes it
      const caps = track.getCapabilities ? track.getCapabilities() : {};
      if (caps.focusMode && caps.focusMode.includes("continuous")) await track.applyConstraints({ advanced: [{ focusMode: "continuous" }] });
    } catch (e) { /* not supported */ }
    try {
      // On Apple's multi-lens cameras zoom 1 is the main lens (it hands over to the macro lens up
      // close: seen on a real iPhone), and 2x is a crop of its 48 MP sensor rather than an upscale.
      // So at the same ~20 cm the card fills twice the frame with about twice the real detail.
      const caps = track.getCapabilities ? track.getCapabilities() : {};
      if (/triple|dual wide/i.test(cameraLabel) && caps.zoom && caps.zoom.max >= 2) await track.applyConstraints({ advanced: [{ zoom: 2 }] });
    } catch (e) { /* stay at 1x */ }
    const got = track.getSettings ? track.getSettings().zoom : 1;
    zoom = Math.min(2, Math.max(1, Number(got) || 1));
    frameEl.style.setProperty("--zoom", zoom);  // the guide and the distance band scale with it
    lab("stream", { track, zoom });
    return s;
  }

  async function openCamera(side) {
    $("#cam-title").textContent = { front: "Front of card", back: "Back of card", tilt: "Front, tilted to the light" }[side];
    if (cam.hidden) returnFocus = document.activeElement;
    cam.hidden = false;
    setModal(cam, true);
    cam.focus();
    document.body.classList.add("no-scroll");
    hint.textContent = "Starting camera…";
    announce($("#cam-title").textContent + ". Starting camera.");
    try {
      stream = stream || await getStream();
      video.srcObject = stream;
      await video.play();
    } catch (e) {
      closeCamera();
      native.dataset.side = side; native.click();  // permission denied or no camera: native picker
      return;
    }
    lab("open", { side, video });
    startLoop();
  }

  function startLoop(delay = 0) {
    resetState();
    setProgress(0);
    running = true;
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  }
  function stopLoop() { running = false; clearTimeout(timer); }
  function tick() {
    if (!running) return;
    try { checkFrame(); } catch (e) { /* keep going */ }
    if (running) timer = setTimeout(tick, 110);  // chained, so a slow frame never queues up
  }

  function closeCamera() {
    lab("close", { side: current });
    stopLoop();
    clearTimeout(announceTimer);
    const wasOpen = !cam.hidden;
    cam.hidden = true;
    document.body.classList.remove("no-scroll");
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (!wasOpen) return;
    setModal(cam, false);
    // The button that opened the camera may have been swapped out by the photo it took.
    const back = returnFocus && returnFocus.offsetParent ? returnFocus : analyzeBtn.hidden ? startBtn : analyzeBtn;
    back.focus();
  }

  // Hints change as the card moves, so a screen reader hears the latest one every few seconds at most.
  function announce(msg) {
    clearTimeout(announceTimer);
    announceTimer = setTimeout(() => { camStatus.textContent = msg; lastAnnounced = Date.now(); },
                               Math.max(0, lastAnnounced + 4000 - Date.now()));
  }

  function setProgress(p) {
    shutterBtn.style.setProperty("--p", p.toFixed(3));
    shutterBtn.classList.toggle("arming", p > 0);
  }

  // Only change the hint once the same message has shown on two checks in a row.
  function say(msg) {
    if (msg === st.msg) { st.pending = ""; return; }
    if (msg === st.pending) st.pendingN++; else { st.pending = msg; st.pendingN = 1; }
    if (st.pendingN >= 2 || !st.msg) { st.msg = msg; hint.textContent = msg; announce(msg); st.pending = ""; }
  }

  function grab(crop) {
    const s = Math.min(1, 2000 / Math.max(crop.w, crop.h));
    const cv = document.createElement("canvas");
    cv.width = Math.round(crop.w * s); cv.height = Math.round(crop.h * s);
    cv.getContext("2d").drawImage(video, crop.x, crop.y, crop.w, crop.h, 0, 0, cv.width, cv.height);
    return cv;
  }

  function checkFrame() {
    if (!video.videoWidth) return;
    if (current === "tilt") {
      // Glare is the point here, so no checks and no auto-capture: the user picks the moment.
      frameEl.classList.remove("good");
      setProgress(0);
      say("Tilt the front toward the light until it shines across the card, then tap the button");
      return;
    }
    // Find the card wherever it is. Only if none shows for a while, fall back to the fixed guide.
    const v = view();
    const card = findCard(video, v);
    const found = !!(card && card.ok);
    st.lost = found ? 0 : st.lost + 1;
    const fallback = !found && st.lost >= LOST_FALLBACK;
    const crop = found ? cardCrop(card) : guideCrop();
    if (found) st.crop = crop;
    const q = measure(video, crop);
    const motion = st.motion = motionOf(blocks(q));
    // A card cut off at the view's edge isn't card-shaped yet, but its edge still says which way to move.
    const partial = !found && card && (card.cut.left || card.cut.right || card.cut.top || card.cut.bottom) && card.size > 0.12;
    const fix = found || partial ? placement(card) : "";
    const aligned = found ? (fix ? 0 : 1 - Math.abs(card.angle) / 30 - 2 * card.tilt) : fallback ? alignment(q) : 0;
    const lightOk = q.brightness >= Q.dark && q.glare <= Q.glare;
    const sharp = q.sharpness >= Q.blurry;
    if (found) { st.recent.push(q.sharpness); if (st.recent.length > PEAK_WINDOW) st.recent.shift(); }
    const peak = Math.max(Q.blurry, ...st.recent);
    // Hysteresis: once the countdown runs, a little more drift is allowed before it restarts. In lab
    // round 2 a hand at motion 7-9 against a hard 10 flipped the hint and restarted the count all the time.
    const arming = st.close >= 2;
    const focused = sharp && (!found || q.sharpness >= (arming ? 0.45 : SHARP_SHARE) * peak);
    // Steady = the card's outline barely moved since the last check (share of the view's width).
    // Lab round 3: on holo fronts, brightness change mostly measured foil shimmer, not the hand
    // (its "moving" frames were the sharpest), while outline movement under 1% gave frames at 0.88
    // of the card's best sharpness, 1-2% gave 0.77 and over 2% gave 0.35.
    if (found) {
      st.shift = st.quad ? card.quad.reduce((sum, p) => sum + Math.min(...st.quad.map((o) => Math.hypot(p[0] - o[0], p[1] - o[1]))), 0) / 4 / v.w : 99;
      st.quad = card.quad;
    } else { st.quad = null; st.shift = 99; }
    const steady = found ? st.shift <= (arming ? 0.03 : 0.02) : motion <= (arming ? 14 : MOTION_MAX);
    const close = lightOk && focused && steady && (found ? !fix : aligned >= CLOSE_ALIGN);
    st.samples.push({ t: Date.now(), a: +aligned.toFixed(2), s: Math.round(q.sharpness), m: +motion.toFixed(1),
                      b: Math.round(q.brightness), g: +(q.glare * 100).toFixed(2),
                      ...(card ? { f: card.ok ? 1 : 0, z: +card.size.toFixed(2), k: +card.tilt.toFixed(2), r: Math.round(card.angle) } : {}) });
    if (st.samples.length > 40) st.samples.shift();

    let msg;
    // With no card found, the crop is mostly the (recommended) dark surface: only a near-black view is "too dark".
    if (q.brightness < (found ? Q.dark : 25)) msg = "Too dark: add light";
    else if (fix) msg = fix;
    else if (q.glare > Q.glare) msg = "Glare: tilt the card or move away from the light";
    else if (!found && !fallback) msg = "Looking for the card: plain, dark surface works best";
    else if (!found && aligned < CLOSE_ALIGN) msg = "Fit the card's edges to the frame";
    else if (found && !steady) msg = "Hold still";  // a moving phone blurs every frame, whatever the focus
    else if (!sharp) msg = found ? "Hold still while it focuses" : "Move back a little so it can focus";
    else if (!steady) msg = "Hold still";
    else if (!focused) msg = "Hold still while it focuses";
    else msg = $("#auto-capture").checked ? "Hold still… capturing" : "Looks good: take the photo";
    say(msg);
    lab("tick", { sample: st.samples[st.samples.length - 1], msg, shown: st.msg, card, found, partial, fix, close, n: st.close,
                  need: BURST_N, lost: st.lost, crop, v, q: { s: q.sharpness, b: q.brightness, g: q.glare }, motion, shift: st.shift, zoom, peak, focused, steady,
                  band: [SIZE_MIN * zoom, Math.min(0.85, SIZE_MAX * zoom)] });
    drawOutline(found ? card : null, v, close);
    frameEl.classList.toggle("found", found);
    frameEl.classList.toggle("good", !found && st.close >= 2);

    // A miss only steps the count back, so one jittery frame doesn't restart the burst.
    st.close = close ? st.close + 1 : Math.max(0, st.close - 1);
    if (close) {
      const score = q.sharpness / peak + 0.2 * aligned - motion / 40;  // sharpness first
      const worst = st.burst.length >= KEEP + 1 ? st.burst[st.burst.length - 1].score : -Infinity;
      if (score > worst) {
        st.burst.push({ cv: grab(crop), q, score, outline: found ? outlineIn(card.quad, crop) : null });
        st.burst.sort((a, b) => b.score - a.score);
        st.burst.length = Math.min(st.burst.length, KEEP + 1);
      }
    }
    if (!$("#auto-capture").checked) { setProgress(0); return; }
    setProgress(Math.min(1, st.close / BURST_N));
    if (st.close >= BURST_N) shoot([], true);
  }

  async function shoot(warnings, auto = false) {
    if (!running) return;
    stopLoop();
    setProgress(0);
    const flash = $("#flash");
    flash.classList.remove("go"); void flash.offsetWidth; flash.classList.add("go");
    // Never send a frame much softer than the best: the server keeps whichever measures best, sharp or not.
    let frames = st.burst.slice(0, KEEP);
    frames.sort((a, b) => b.q.sharpness - a.q.sharpness);  // the sharpest leads, whatever its score
    frames = frames.filter((f) => f.q.sharpness >= 0.6 * frames[0].q.sharpness);
    if (!auto || !frames.length) {
      // Manual shutter: this moment matters most; take 3 frames ~60 ms apart.
      frames = [];
      for (let i = 0; i < KEEP; i++) {
        if (i) await new Promise((r) => setTimeout(r, 60));
        const recent = st.crop && st.lost < 5;
        const cv = grab(recent ? st.crop : guideCrop());
        frames.push({ cv, q: measure(cv), score: 0, outline: recent && st.quad ? outlineIn(st.quad, st.crop) : null });
      }
      frames.sort((a, b) => b.q.sharpness - a.q.sharpness);
    }
    const blobs = await Promise.all(frames.map((f) => toJpeg(f.cv)));
    const q = frames[0].q;
    const side = current;
    const track = stream && stream.getVideoTracks()[0];
    const debug = { camera: cameraLabel, settings: track && track.getSettings ? track.getSettings() : null,
                    auto, frames: frames.map((f) => ({ score: +f.score.toFixed(2), s: Math.round(f.q.sharpness) })),
                    samples: st.samples.slice(-25) };
    st.burst = [];
    lab("shot", { side, auto, debug, video });
    const warn = side === "tilt" ? [] : warnings.length ? warnings : problems(q);  // tilt: glare is wanted
    await accept(side, blobs[0], warn, side !== "tilt", debug, side === "tilt" ? [] : blobs.slice(1),
                 side === "tilt" ? [] : frames.map((f) => f.outline || null));
    if (side === "tilt") {
      closeCamera();
    } else if (side === "front" && !shots.back && !skipBack) {
      // Capture is quick enough that a passing hint got missed: pause on a clear "flip it"
      // screen and wait for a tap, so the back scan can't start on the front.
      current = "back";
      $("#cam-title").textContent = "Back of card";
      $("#flip-thumb").src = URL.createObjectURL(blobs[0]);
      $("#flip").hidden = false;
      $("#flip-go").focus();
    } else {
      closeCamera();
    }
  }

  shutterBtn.addEventListener("click", () => {
    const q = video.videoWidth ? measure(video, st.crop && st.lost < 5 ? st.crop : guideCrop()) : null;
    shoot(q ? problems(q) : []);
  });
  const dismiss = () => { $("#flip").hidden = true; closeCamera(); };
  $(".cam-close").addEventListener("click", dismiss);
  cam.addEventListener("keydown", (e) => { if (e.key === "Escape") dismiss(); });
  $("#flip-go").addEventListener("click", () => {
    $("#flip").hidden = true;
    hint.textContent = "Looking for the card…";
    lab("open", { side: current, video });
    cam.focus();
    startLoop(300);
  });
  $("#flip-skip").addEventListener("click", () => {
    $("#flip").hidden = true;
    skipBack = true;
    closeCamera();
    render();
  });
  video.addEventListener("click", () => {  // tap to (re)focus where supported
    const track = stream && stream.getVideoTracks()[0];
    const caps = track && track.getCapabilities ? track.getCapabilities() : {};
    if (caps.focusMode && caps.focusMode.includes("single-shot")) track.applyConstraints({ advanced: [{ focusMode: "single-shot" }] });
  });

  render();
}

// ---------------------------------------------------------------- card search
document.querySelectorAll("input[data-card-search]").forEach((input) => {
  const list = document.getElementById(input.dataset.results || "card-results");
  const target = document.getElementById(input.dataset.target || "card-id");
  const status = document.getElementById(input.dataset.status);
  const tell = (msg) => { if (status) status.textContent = msg; };
  const run = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { list.innerHTML = ""; return; }
    list.innerHTML = '<li class="soft">Searching…</li>';
    tell("Searching");
    try {
      const res = await fetch("/api/cards/search?q=" + encodeURIComponent(q));
      const data = await res.json();
      list.innerHTML = "";
      if (!data.cards.length) {
        list.innerHTML = `<li class="soft">No matches${data.errors.length ? " (card database unreachable)" : ""}. You can still enter prices by hand.</li>`;
        tell(list.textContent);
        return;
      }
      for (const c of data.cards) {
        const li = document.createElement("li");
        const pick = document.createElement("button");
        pick.type = "button";
        pick.setAttribute("aria-pressed", "false");
        const price = c.raw_price ? `$${c.raw_price.toFixed(2)} raw` : "no raw price";
        pick.innerHTML = '<span class="result-text"><strong></strong><span class="small soft"></span></span>';
        if (c.image) { const img = new Image(); img.src = c.image; img.alt = ""; pick.prepend(img); }
        pick.querySelector("strong").textContent = c.name;
        pick.querySelector(".small").textContent = `${c.set} · #${c.number}${c.printed_total ? "/" + c.printed_total : ""} · ${price}`;
        pick.addEventListener("click", () => {
          list.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
          pick.setAttribute("aria-pressed", "true");
          target.value = c.id;
          const btn = target.form && target.form.querySelector("[data-needs-card]");
          if (btn) btn.disabled = false;
          tell(`${c.name} selected.`);
        });
        li.appendChild(pick);
        list.appendChild(li);
      }
      tell(`${data.cards.length} ${data.cards.length === 1 ? "match" : "matches"}.`);
    } catch (e) {
      list.innerHTML = '<li class="soft">Search failed. You can still enter prices by hand.</li>';
      tell(list.textContent);
    }
  }, 350);
  input.addEventListener("input", run);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  if (input.value.trim().length >= 2) {  // prefilled with what the model read
    const det = input.closest("details");
    if (det && det.open) run();
    else if (det) det.addEventListener("toggle", () => det.open && !list.children.length && run());
  }
});

// Printing picker submits on change; every link to a results panel opens that panel.
document.querySelectorAll("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));
document.querySelectorAll("[data-open]").forEach((a) => a.addEventListener("click", () => {
  const panel = document.getElementById(a.dataset.open);
  if (panel) { panel.open = true; setTimeout(() => panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }), 50); }
}));

// Busy state for plain forms (batch).
document.querySelectorAll("#check-form").forEach((form) => {
  form.addEventListener("submit", () => {
    form.classList.add("busy");
    const b = form.querySelector("button[type=submit]");
    if (b) { b.disabled = true; b.firstChild.textContent = "Analyzing "; }
  });
});
