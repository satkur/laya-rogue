"""server.py --llm --no-open を起動した状態で実行。gen を選んで 1 ゲームを録画する (webm)。
    uv run --no-project --with playwright python tools/record.py <出力dir> <gen> <上限秒> <mode>   (Playwright は venv に無いので --with で取る)   mode: shell (旧ヘッドレス) / headed (ウィンドウ) / newheadless (GPU 付き新ヘッドレス)
"""
import sys, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
GEN = sys.argv[2]; LIMIT = int(sys.argv[3]); MODE = sys.argv[4]
ARGS = ["--disable-frame-rate-limit", "--disable-gpu-vsync"] if MODE != "headed" else []
with sync_playwright() as p:
    if MODE == "headed":
        b = p.chromium.launch(headless=False, args=["--window-position=0,0"])
    elif MODE == "newheadless":
        b = p.chromium.launch(channel="chromium", headless=True, args=ARGS + ["--enable-gpu", "--use-angle=d3d11", "--enable-gpu-rasterization"])
    else:
        b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT), record_video_size={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8766/")
    page.wait_for_function("document.getElementById('device').textContent === ''", timeout=60000)
    page.wait_for_selector(f"#gens button[data-gen='{GEN}']", timeout=30000)
    page.click(f"#gens button[data-gen='{GEN}']")
    page.wait_for_function(f"document.getElementById('genNow').textContent === '{GEN}'", timeout=30000)
    t0 = time.time(); log = []; last = None
    while time.time() - t0 < LIMIT:
        time.sleep(1.0)
        depth = page.eval_on_selector("#depth", "e => e.textContent")
        turn = page.eval_on_selector("#turn", "e => e.textContent")
        plan = page.eval_on_selector("#plan", "e => e.textContent")
        dead = page.eval_on_selector("#died", "e => e.classList.contains('show')")
        kills = page.eval_on_selector("#kills", "e => e.textContent")
        hp = page.eval_on_selector("#hpv", "e => e.textContent")
        row = {"t": round(time.time() - t0, 1), "depth": depth, "turn": turn, "plan": plan, "kills": kills, "hp": hp}
        if row != last: log.append(row); last = row
        if dead:
            log.append({"t": round(time.time() - t0, 1), "dead": True, "sub": page.eval_on_selector("#diedSub", "e => e.textContent")})
            time.sleep(4.0); break
    (OUT / "timeline.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.close(); b.close()
    print("done", log[-1])
