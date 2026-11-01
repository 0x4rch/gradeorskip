// GradeOrSkip front end: guided capture, photo quality checks, card search. No framework.

// ---------------------------------------------------------------- helpers
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const $ = (sel, root = document) => root.querySelector(sel);

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

  async function accept(side, blob, warnings, guided = false, debug = null, alts = []) {
    shots[side] = { blob, warnings, guided, debug, alts };
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
    // Tell the server which photos came from the guided camera (card sits on the frame lines).
    fd.append("guided", ["front", "back"].filter((s) => shots[s] && shots[s].guided).join(","));
    // Camera diagnostics (which lens, focus/steadiness readings before the shot) for tuning.
    const dbg = {};
    for (const s of ["front", "back"]) if (shots[s] && shots[s].debug) dbg[s] = shots[s].debug;
    dbg.ua = navigator.userAgent;
    fd.append("capture_debug", JSON.stringify(dbg));
    const overlay = $("#analyzing"), step = $("#analyzing-step");
    overlay.hidden = false;
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
      alert("Something went wrong: " + e.message + ". Try again.");
    }
  });

  // ------------------------------------------------------------ live camera
  // Why it's built this way (iPhone Pro testing): the plain "Back Camera" can't focus closer
  // than ~20 cm and the browser never switches it to the macro lens, so a big on-screen guide
  // (which makes you hold the phone ~10 cm away) meant the camera hunted for focus forever.
  // So: prefer the multi-lens virtual camera (it switches lenses for close focus), keep the
  // guide small enough that the card sits ~20 cm away, judge steadiness on coarse blocks (hand
  // tremor and holo shimmer don't count), and smooth the state so the hint doesn't flicker.
  const cam = $("#camera"), video = $("video", cam), hint = $("#cam-hint"), frameEl = $("#cam-frame");
  const shutterBtn = $("#shutter");
  let stream = null, timer = null, running = false, lastBlocks = null;
  let st = null;  // per-side capture state
  // Burst capture: once the card is roughly framed and in focus, every check grabs a frame;
  // after BURST_N such checks the best few go to the server, which keeps the one whose card
  // outline it measures most reliably. No single frame has to be perfect.
  const BURST_N = 7, KEEP = 3, CLOSE_ALIGN = 0.35;
  const resetState = () => {
    st = { close: 0, burst: [], msg: "", pending: "", pendingN: 0, samples: [] };
    lastBlocks = null;
  };

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
    if (!lastBlocks || lastBlocks.length !== b.length) { lastBlocks = b; return 99; }
    let mean = 0;
    for (let i = 0; i < b.length; i++) mean += b[i] - lastBlocks[i];
    mean /= b.length;  // auto-exposure shifts every block alike: ignore that
    let m = 0;
    for (let i = 0; i < b.length; i++) m += Math.abs(b[i] - lastBlocks[i] - mean);
    lastBlocks = b;
    return m / b.length;
  }

  const VIDEO = { width: { ideal: 3840 }, height: { ideal: 2160 } };
  let cameraLabel = "";
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
    return s;
  }

  async function openCamera(side) {
    $("#cam-title").textContent = { front: "Front of card", back: "Back of card", tilt: "Front, tilted to the light" }[side];
    cam.hidden = false;
    document.body.classList.add("no-scroll");
    hint.textContent = "Starting camera…";
    try {
      stream = stream || await getStream();
      video.srcObject = stream;
      await video.play();
    } catch (e) {
      closeCamera();
      native.dataset.side = side; native.click();  // permission denied or no camera: native picker
      return;
    }
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
    stopLoop();
    cam.hidden = true;
    document.body.classList.remove("no-scroll");
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  }

  function setProgress(p) {
    shutterBtn.style.setProperty("--p", p.toFixed(3));
    shutterBtn.classList.toggle("arming", p > 0);
  }

  // Only change the hint once the same message has shown on two checks in a row.
  function say(msg) {
    if (msg === st.msg) { st.pending = ""; return; }
    if (msg === st.pending) st.pendingN++; else { st.pending = msg; st.pendingN = 1; }
    if (st.pendingN >= 2 || !st.msg) { st.msg = msg; hint.textContent = msg; st.pending = ""; }
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
    const crop = guideCrop();
    const q = measure(video, crop);
    const motion = motionOf(blocks(q));
    const aligned = alignment(q);
    const lightOk = q.brightness >= Q.dark && q.glare <= Q.glare;
    const sharp = q.sharpness >= Q.blurry;
    const close = lightOk && sharp && aligned >= CLOSE_ALIGN;
    st.samples.push({ t: Date.now(), a: +aligned.toFixed(2), s: Math.round(q.sharpness), m: +motion.toFixed(1),
                      b: Math.round(q.brightness), g: +(q.glare * 100).toFixed(2) });
    if (st.samples.length > 40) st.samples.shift();

    let msg;
    if (q.brightness < Q.dark) msg = "Too dark: add light";
    else if (q.glare > Q.glare) msg = "Glare: tilt the card or move away from the light";
    else if (aligned < CLOSE_ALIGN) msg = "Fit the card's edges to the frame";
    else if (!sharp) msg = "Move back a little so it can focus";
    else msg = $("#auto-capture").checked ? "Hold still… capturing" : "Looks good: take the photo";
    say(msg);
    frameEl.classList.toggle("good", st.close >= 2);

    // A miss only steps the count back, so one jittery frame doesn't restart the burst.
    st.close = close ? st.close + 1 : Math.max(0, st.close - 1);
    if (close) {
      const score = aligned + 0.3 * Math.min(1, q.sharpness / 600) - Math.min(0.5, motion / 30);
      const worst = st.burst.length >= KEEP + 1 ? st.burst[st.burst.length - 1].score : -Infinity;
      if (score > worst) {
        st.burst.push({ cv: grab(crop), q, score });
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
    let frames = st.burst.slice(0, KEEP);
    if (!auto || !frames.length) {
      // Manual shutter: this moment matters most; take 3 frames ~60 ms apart.
      frames = [];
      for (let i = 0; i < KEEP; i++) {
        if (i) await new Promise((r) => setTimeout(r, 60));
        const cv = grab(guideCrop());
        frames.push({ cv, q: measure(cv), score: 0 });
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
    const warn = side === "tilt" ? [] : warnings.length ? warnings : problems(q);  // tilt: glare is wanted
    await accept(side, blobs[0], warn, side !== "tilt", debug, side === "tilt" ? [] : blobs.slice(1));
    if (side === "tilt") {
      closeCamera();
    } else if (side === "front" && !shots.back && !skipBack) {
      // Capture is quick enough that a passing hint got missed: pause on a clear "flip it"
      // screen and wait for a tap, so the back scan can't start on the front.
      current = "back";
      $("#cam-title").textContent = "Back of card";
      $("#flip-thumb").src = URL.createObjectURL(blobs[0]);
      $("#flip").hidden = false;
    } else {
      closeCamera();
    }
  }

  shutterBtn.addEventListener("click", () => {
    const q = video.videoWidth ? measure(video, guideCrop()) : null;
    shoot(q ? problems(q) : []);
  });
  $(".cam-close").addEventListener("click", () => { $("#flip").hidden = true; closeCamera(); });
  $("#flip-go").addEventListener("click", () => {
    $("#flip").hidden = true;
    hint.textContent = "Fit the card's edges to the frame";
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
  const run = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { list.innerHTML = ""; return; }
    list.innerHTML = '<li class="soft">Searching…</li>';
    try {
      const res = await fetch("/api/cards/search?q=" + encodeURIComponent(q));
      const data = await res.json();
      list.innerHTML = "";
      if (!data.cards.length) {
        list.innerHTML = `<li class="soft">No matches${data.errors.length ? " (card database unreachable)" : ""}. You can still enter prices by hand.</li>`;
        return;
      }
      for (const c of data.cards) {
        const li = document.createElement("li");
        const price = c.raw_price ? `$${c.raw_price.toFixed(2)} raw` : "no raw price";
        li.innerHTML = `${c.image ? `<img src="${c.image}" alt="">` : ""}<div><strong></strong><div class="small soft"></div></div>`;
        li.querySelector("strong").textContent = c.name;
        li.querySelector(".small").textContent = `${c.set} · #${c.number}${c.printed_total ? "/" + c.printed_total : ""} · ${price}`;
        li.addEventListener("click", () => {
          list.querySelectorAll("li").forEach((x) => x.classList.remove("sel"));
          li.classList.add("sel");
          target.value = c.id;
          const btn = target.form && target.form.querySelector("[data-needs-card]");
          if (btn) btn.disabled = false;
        });
        list.appendChild(li);
      }
    } catch (e) {
      list.innerHTML = '<li class="soft">Search failed. You can still enter prices by hand.</li>';
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
  if (panel) { panel.open = true; setTimeout(() => panel.scrollIntoView({ behavior: "smooth" }), 50); }
}));

// Busy state for plain forms (batch).
document.querySelectorAll("#check-form").forEach((form) => {
  form.addEventListener("submit", () => {
    form.classList.add("busy");
    const b = form.querySelector("button[type=submit]");
    if (b) { b.disabled = true; b.firstChild.textContent = "Analyzing "; }
  });
});
