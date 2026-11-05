// Capture lab (GRADEORSKIP_LAB=1 builds only): a live readout on the camera, and a recording of
// every camera session sent to /lab/session. Each session is one side of one card: every check the
// loop made, about one snapshot a second with the card outline and crop drawn on, and a
// full-resolution still at the shutter. Sessions that end without a photo are sent too.
(() => {
  const cam = document.querySelector("#camera");
  linkCheck();
  if (!cam) return;

  const hud = document.createElement("pre");
  hud.className = "lab-hud";
  hud.setAttribute("aria-hidden", "true");
  hud.addEventListener("click", () => hud.classList.toggle("min"));
  cam.appendChild(hud);

  let S = null, stream = {}, lastTick = 0, fps = 0;
  const now = () => Math.round(performance.now());
  const rid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

  function start(side, video) {
    if (S) send("reopened");
    S = { id: rid(), side, video, t0: now(), started: new Date().toISOString(), ua: navigator.userAgent,
          screen: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio }, stream: { ...stream },
          ticks: [], snaps: [], raws: [], still: null, lastSnap: -1e9, lastRaw: -1e9, lastMsg: "" };
  }

  function snap(x) {
    const v = x.v, w = 360, h = Math.round((w * v.h) / v.w), s = w / v.w;
    const cv = document.createElement("canvas");
    cv.width = w; cv.height = h;
    const g = cv.getContext("2d");
    g.drawImage(S.video, v.x, v.y, v.w, v.h, 0, 0, w, h);
    const P = ([px, py]) => [(px - v.x) * s, (py - v.y) * s];
    if (x.card) {
      g.strokeStyle = x.found ? (x.close ? "#4ade80" : "#fff") : "#f87171"; g.lineWidth = 2; g.beginPath();
      x.card.quad.forEach((p, i) => { const [a, b] = P(p); i ? g.lineTo(a, b) : g.moveTo(a, b); });
      g.closePath(); g.stroke();
    }
    if (x.crop) {
      const [a, b] = P([x.crop.x, x.crop.y]);
      g.strokeStyle = "#60a5fa"; g.setLineDash([4, 3]); g.strokeRect(a, b, x.crop.w * s, x.crop.h * s); g.setLineDash([]);
    }
    g.fillStyle = "#000a"; g.fillRect(0, h - 18, w, 18);
    g.fillStyle = "#fff"; g.font = "11px monospace";
    g.fillText(`${((now() - S.t0) / 1000).toFixed(1)}s ${x.msg}`.slice(0, 58), 4, h - 5);
    const at = now() - S.t0;
    cv.toBlob((b) => { if (S && b && S.snaps.length < 150) S.snaps.push({ t: at, blob: b }); }, "image/jpeg", 0.7);
  }

  function raw(x) {
    const v = x.v, w = 480, h = Math.round((w * v.h) / v.w);
    const cv = document.createElement("canvas");
    cv.width = w; cv.height = h;
    const g = cv.getContext("2d");
    g.imageSmoothingQuality = "high";
    g.drawImage(S.video, v.x, v.y, v.w, v.h, 0, 0, w, h);
    const at = now() - S.t0;
    cv.toBlob((b) => { if (S && b) S.raws.push({ t: at, blob: b }); }, "image/jpeg", 0.9);
  }

  function still(video) {
    const cv = document.createElement("canvas");
    cv.width = video.videoWidth; cv.height = video.videoHeight;
    cv.getContext("2d").drawImage(video, 0, 0);
    return new Promise((res) => cv.toBlob(res, "image/jpeg", 0.92));
  }

  function hudText(x) {
    const c = x.card, f = (n, d = 2) => (n == null ? "—" : Number(n).toFixed(d));
    const cut = c ? Object.entries(c.cut).filter(([, v]) => v).map(([k]) => k[0]).join("") || "—" : "—";
    return [
      `${stream.label || "?"} · zoom ${f(x.zoom, 1)} · ${stream.w}×${stream.h} · ${fps.toFixed(0)}/s`,
      `card ${x.found ? "found" : x.partial ? "partial" : c ? "not card-shaped" : "none"}  size ${f(c && c.size)} [${f(x.band[0])}–${f(x.band[1])}]`,
      `tilt ${f(c && c.tilt)} (≤.07)  turn ${f(c && c.angle, 0)}° (≤8)  cut ${cut}`,
      `sharp ${f(x.q.s, 0)} / peak ${f(x.peak, 0)} ${x.focused ? "focused" : "soft"}  moved ${f(x.shift * 100, 1)}% ${x.steady ? "steady" : "moving"}`,
      `light ${f(x.q.b, 0)}  glare ${f(x.q.g * 100, 1)}%`,
      `ready ${x.n}/${x.need}  ${x.fix || (x.close ? "ok" : "")}`,
    ].join("\n");
  }

  async function send(end, s = S) {
    if (s === S) S = null;
    if (!s) return;
    const fd = new FormData();
    const { video, snaps, raws, still: st, lastSnap, lastRaw, lastMsg, ...meta } = s;
    meta.end = end; meta.duration = now() - s.t0;
    meta.snapTimes = snaps.map((x) => x.t);
    meta.rawTimes = raws.map((x) => x.t);
    fd.append("meta", JSON.stringify(meta));
    snaps.forEach((x, i) => fd.append("snap", x.blob, `snap_${i}.jpg`));
    raws.forEach((x, i) => fd.append("raw", x.blob, `raw_${i}.jpg`));
    if (st) fd.append("still", st, "still.jpg");
    const ids = JSON.parse(sessionStorage.getItem("labSessions") || "[]");
    sessionStorage.setItem("labSessions", JSON.stringify([...ids, s.id].slice(-10)));
    try { await fetch("/lab/session", { method: "POST", body: fd }); } catch (e) { /* lab only */ }
  }

  // On the answer page, tie the camera sessions to the check they produced.
  function linkCheck() {
    const m = location.pathname.match(/^\/check\/([0-9a-f]{12})$/);
    const ids = JSON.parse(sessionStorage.getItem("labSessions") || "[]");
    if (!m || !ids.length) return;
    sessionStorage.removeItem("labSessions");
    fetch("/lab/link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ check_id: m[1], sessions: ids }) });
  }

  window.captureLab = {
    stream({ track, zoom }) {
      const st = track.getSettings ? track.getSettings() : {};
      stream = { label: track.label, zoom, w: st.width, h: st.height, settings: st,
                 caps: track.getCapabilities ? track.getCapabilities() : null };
    },
    open({ side, video }) { start(side, video); },
    tick(x) {
      if (!S) return;
      const t = now();
      if (lastTick) fps = 0.8 * fps + 0.2 * (1000 / Math.max(1, t - lastTick));
      lastTick = t;
      const c = x.card;
      S.ticks.push({ ...x.sample, t: t - S.t0, msg: x.msg, shown: x.shown, found: x.found, partial: !!x.partial, fix: x.fix, close: x.close, n: x.n,
                     lost: x.lost, zoom: x.zoom, peak: Math.round(x.peak || 0), focused: x.focused, steady: x.steady, shift: +(x.shift || 0).toFixed(4),
                     card: c ? { ok: c.ok, size: +c.size.toFixed(3), tilt: +c.tilt.toFixed(3), angle: +c.angle.toFixed(1), cut: c.cut,
                                 ratio: c.ratio && +c.ratio.toFixed(3), fill: c.fill && +c.fill.toFixed(3), threshold: c.threshold,
                                 quad: c.quad.map((p) => p.map(Math.round)) } : null,
                     crop: x.crop && { x: Math.round(x.crop.x), y: Math.round(x.crop.y), w: Math.round(x.crop.w), h: Math.round(x.crop.h) } });
      if (S.ticks.length > 4000) S.ticks.shift();
      if (t - S.lastSnap > 900 || x.msg !== S.lastMsg) { S.lastSnap = t; S.lastMsg = x.msg; snap(x); }
      // A card seen but rejected: keep a clean frame (no drawings) to replay the detector on later.
      if (c && !c.ok && t - S.lastRaw > 1500 && S.raws.length < 40) { S.lastRaw = t; raw(x); }
      hud.textContent = hudText(x);
    },
    async shot({ auto, debug, video }) {
      const s = S;
      if (!s) return;
      S = null;  // the back's session can open while this one's still is still encoding
      s.shot = { auto, debug, at: now() - s.t0 };
      s.still = await still(video);
      send("shot", s);
    },
    close() { send("closed"); },
  };
})();
