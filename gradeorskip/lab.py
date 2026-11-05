"""Capture lab: records what the live camera saw, for tuning capture on real phones.

On only with GRADEORSKIP_LAB=1, meant for a private LAN build. Each camera session (one side of
one card) arrives as JSON plus about one snapshot a second and a full-resolution still at the
shutter. Abandoned sessions are kept too: they're where capture fails.
"""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .config import DATA_DIR

LAB_DIR = DATA_DIR / "lab"
SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")
router = APIRouter(prefix="/lab", include_in_schema=False)


@router.post("/session")
async def session(request: Request):
    form = await request.form(max_files=200, max_part_size=4 * 1024 * 1024)
    try:
        meta = json.loads(str(form.get("meta") or ""))
        sid = str(meta.get("id", ""))
    except ValueError:
        raise HTTPException(400, "bad meta")
    if not SAFE.fullmatch(sid):
        raise HTTPException(400, "bad id")
    folder = LAB_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{meta.get('side', 'x')}_{sid}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(json.dumps(meta, indent=1))
    for i, snap in enumerate(form.getlist("snap")[:150]):
        (folder / f"snap_{i:03d}.jpg").write_bytes(await snap.read())
    for i, raw in enumerate(form.getlist("raw")[:40]):  # clean frames where a card was seen but rejected
        (folder / f"raw_{i:03d}.jpg").write_bytes(await raw.read())
    if (still := form.get("still")) is not None and not isinstance(still, str):
        (folder / "still.jpg").write_bytes(await still.read())
    await form.close()
    return {"ok": True, "folder": folder.name}


@router.post("/link")
async def link(request: Request):
    body = await request.json()
    with (LAB_DIR / "links.jsonl").open("a") as f:
        f.write(json.dumps({"t": time.time(), "check_id": str(body.get("check_id", ""))[:20],
                            "sessions": [str(s)[:64] for s in body.get("sessions", [])][:20]}) + "\n")
    return {"ok": True}


@router.get("")
def index():
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(LAB_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "session.json").exists():
            meta = json.loads((d / "session.json").read_text())
            out.append({"folder": d.name, "side": meta.get("side"), "end": meta.get("end"), "ticks": len(meta.get("ticks", [])),
                        "files": sorted(f.name for f in d.iterdir() if f.suffix == ".jpg")})
    return out


@router.get("/{folder}/{name}")
def file(folder: str, name: str):
    if not re.fullmatch(r"[\w-]{1,120}", folder) or not re.fullmatch(r"[\w-]{1,40}\.(jpg|json)", name):
        raise HTTPException(404)
    path = LAB_DIR / folder / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)
