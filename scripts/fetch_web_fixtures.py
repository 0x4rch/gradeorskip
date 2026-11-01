"""Re-download the real-photo condition test set (tests/fixtures/web_labels.json) into data/webtest/.

Images are third-party photos used only for local testing; they are not committed.
Elite Fourum images are fetched from each thread's opening posts (direct imgur links and albums).
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "webtest"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) GradeOrSkip-test-fixtures"}


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read()


def thread_images(tid: str) -> list[str]:
    d = json.loads(get(f"https://www.elitefourum.com/t/{tid}.json"))
    posts = d["post_stream"]["posts"]
    op = posts[0]["username"]
    urls = []
    for p in posts[:20]:
        if p["username"] != op:
            continue
        c = p["cooked"]
        urls += re.findall(r'<img[^>]+src="([^"]+imgur[^"]+)"', c)
        urls += re.findall(r'href="(https?://i\.imgur\.com/[^"]+\.(?:jpg|jpeg|png))"', c)
        for alb in re.findall(r'href="https?://(?:www\.)?imgur\.com/(?:a|gallery)/([A-Za-z0-9]+)', c):
            page = get(f"https://imgur.com/a/{alb}/embed?pub=true").decode("utf-8", "replace")
            urls += [f"https://i.imgur.com/{h}.jpg" for h in re.findall(r'"hash":"([A-Za-z0-9]{5,8})"', page)]
    return [u.replace("http://", "https://") for u in dict.fromkeys(urls)]


def main():
    labels = json.loads((ROOT / "tests" / "fixtures" / "web_labels.json").read_text())
    threads: dict[str, list[str]] = {}
    for case in labels["cases"]:
        for rel in case["files"].values():
            dest = OUT / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel.startswith("clean/"):
                url = case["source"]
            else:
                tid, idx = Path(rel).stem.split("_")
                if tid not in threads:
                    threads[tid] = [u for u in thread_images(tid)]
                big = [u for u in threads[tid]]
                url = big[int(idx) - 1] if int(idx) <= len(big) else None
            if not url:
                print("missing", rel, file=sys.stderr)
                continue
            dest.write_bytes(get(url))
            print("fetched", rel)


if __name__ == "__main__":
    main()
