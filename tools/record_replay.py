"""Record a replay (server.py --replay <file> --no-open must be running) to webm with Playwright, plus a timeline.json for cut_video.py.
    uv run --no-project --with playwright python tools/record_replay.py <out dir> <limit seconds>
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
LIMIT = int(sys.argv[2])
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT), record_video_size={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8766/")
    page.wait_for_function("document.getElementById('device').textContent === ''", timeout=60000)
    t0 = time.time(); log = []; last = None
    while time.time() - t0 < LIMIT:
        time.sleep(1.0)
        row = {"t": round(time.time() - t0, 1), "depth": page.eval_on_selector("#depth", "e => e.textContent"), "turn": page.eval_on_selector("#turn", "e => e.textContent"),
               "kills": page.eval_on_selector("#kills", "e => e.textContent")}
        if row != last:
            log.append(row); last = row
        if page.eval_on_selector("#died", "e => e.classList.contains('show')"):
            log.append({"t": round(time.time() - t0, 1), "dead": True, "sub": page.eval_on_selector("#diedSub", "e => e.textContent")})
            time.sleep(4.0); break
    (OUT / "timeline.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.close(); b.close()
    print("done", log[-1])
