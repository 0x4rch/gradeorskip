"""Drive the live-camera scan flow in Chromium with a fake camera (a .y4m video).

    python scripts/make_fake_camera.py /tmp/cam.y4m front
    python scripts/e2e_camera.py /tmp/cam.y4m capture /tmp/shot      # expects auto-capture + a result
    python scripts/make_fake_camera.py /tmp/off.y4m front 0.12
    python scripts/e2e_camera.py /tmp/off.y4m nocapture /tmp/shot    # misaligned: must NOT auto-capture

Needs the server on :8790, /usr/bin/chromium, and `pip install -r requirements-dev.txt`.
"""
import json, sys, time, urllib.request
from playwright.sync_api import sync_playwright
video, expect_capture, shots_prefix = sys.argv[1], sys.argv[2] == "capture", sys.argv[3]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium", args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-video-capture={video}"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                        permissions=["camera"])
    page = ctx.new_page()
    logs = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: logs.append(f"PAGEERROR: {e}"))
    page.goto("http://localhost:8790/")
    page.click("#start-scan")
    page.wait_for_selector("#camera:not([hidden])", timeout=5000)
    hints, t0 = [], time.time()
    got_front = False
    while time.time() - t0 < 9:
        h = page.inner_text("#cam-hint")
        if not hints or hints[-1] != h: hints.append(h)
        if "done" in (page.get_attribute('[data-shot="front"]', "class") or ""):
            got_front = True; break
        time.sleep(0.15)
    page.screenshot(path=f"{shots_prefix}_camera.png")
    print("hints seen:", hints)
    print("front auto-captured:", got_front, f"after {time.time() - t0:.1f}s")
    if not expect_capture:
        print("console:", logs[:5]); b.close(); sys.exit(0)
    # flip screen: must wait for a tap before scanning the back
    page.wait_for_selector("#flip:not([hidden])", timeout=5000)
    page.screenshot(path=f"{shots_prefix}_flip.png")
    time.sleep(1.5)
    assert "done" not in (page.get_attribute('[data-shot="back"]', "class") or ""), "back scanned before the tap"
    page.click("#flip-go")
    # back: same fake video, should auto-capture again and close the camera
    page.wait_for_selector('[data-shot="back"].done', timeout=10000)
    page.wait_for_selector("#camera", state="hidden", timeout=5000)
    print("back auto-captured, camera closed")
    # optional tilt shot: manual shutter, no auto-capture
    page.click("#tilt-btn")
    page.wait_for_selector("#camera:not([hidden])", timeout=5000)
    time.sleep(1.2)
    assert not page.is_hidden("#camera"), "tilt shot must not auto-capture"
    page.click("#shutter")
    page.wait_for_selector("#camera", state="hidden", timeout=5000)
    print("tilt shot:", page.inner_text("#tilt-btn strong"))
    page.screenshot(path=f"{shots_prefix}_ready.png")
    page.click("#analyze")
    page.wait_for_url("**/check/*", timeout=60000)
    cid = page.url.rsplit("/", 1)[1]
    page.screenshot(path=f"{shots_prefix}_result.png", full_page=False)
    d = json.load(urllib.request.urlopen(f"http://localhost:8790/api/check/{cid}"))
    for s, v in d["result"]["sides"].items():
        c = v.get("centering") or {}
        print(f"{s}: centering {c.get('lr_label')} LR, {c.get('tb_label')} TB, conf {c.get('confidence')}, detection {c.get('detection')}")
    print("tilt used:", d["result"]["sides"]["front"].get("tilt"))
    print("grade:", d["result"]["grade"]["label"], "| identified:", (d["result"].get("identification") or {}).get("status"))
    print("console errors:", [l for l in logs if "error" in l.lower()][:5])
    b.close()
