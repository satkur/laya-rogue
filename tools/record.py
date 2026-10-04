"""Record one game of the running viewer (server.py --no-open) to webm, with a 0.5 s timeline for cutting.

    uv run --no-project --with playwright python tools/record.py <out dir> <theme> <limit sec>
    theme: laya / dos / cga / ansi (xterm). Uses the installed Edge, so no browser download is needed.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
THEME, LIMIT = sys.argv[2], int(sys.argv[3])
SIZE = {"width": 1920, "height": 1080}
PROBE = """() => ({turn: $('turn').textContent, depth: $('depth').textContent, hp: $('hpv').textContent, foe: $('foeName').textContent,
                  foeState: $('foeArt').className, thinking: $('adviser').classList.contains('thinking'), dead: $('died').classList.contains('show')})"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    ctx = b.new_context(viewport=SIZE, record_video_dir=str(OUT), record_video_size=SIZE)
    ctx.add_init_script(f"try {{ localStorage.setItem('laya-rogue-theme', '{THEME}') }} catch {{}}")
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8766/")
    page.click("#restart")  # a fresh game from turn 0
    t0, log = time.time(), []
    while time.time() - t0 < LIMIT:
        time.sleep(0.5)
        row = page.evaluate(PROBE); row["t"] = round(time.time() - t0, 2); log.append(row)
        if row["dead"]:
            time.sleep(5.0); break
    (OUT / "timeline.json").write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    video = page.video.path()
    ctx.close(); b.close()
    print("done", video, log[-1])
