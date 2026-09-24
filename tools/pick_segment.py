"""timeline.json から GIF 向きの区間を探す: WINDOW 秒のあいだに撃破が増え、階が変わり (階段を降り)、勇者が動いている区間。
    python tools/pick_segment.py <timeline.json> [WINDOW=12]
"""
import json, sys
L = [r for r in json.load(open(sys.argv[1], encoding="utf-8")) if "turn" in r]
W = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
cands = []
for i, a in enumerate(L):
    win = [r for r in L[i:] if r["t"] - a["t"] <= W]
    if len(win) < 3:
        continue
    b = win[-1]
    kills = int(b["kills"]) - int(a["kills"])
    floors = len({r["depth"] for r in win})
    turns = int(b["turn"]) - int(a["turn"])
    pauses = sum(1 for x, y in zip(win, win[1:]) if x["turn"] == y["turn"])  # 止まっていた秒数
    score = kills * 3 + (floors - 1) * 4 + min(turns, 80) / 20 - pauses * 1.5
    cands.append((score, a["t"], b["t"], kills, floors, turns, pauses, a["depth"], b["depth"]))
cands.sort(reverse=True)
seen = set()
for c in cands:
    key = round(c[1] / 8)
    if key in seen:
        continue
    seen.add(key)
    print(f"score {c[0]:5.1f}  t {c[1]:6.1f}-{c[2]:6.1f}  撃破 +{c[3]}  階 {c[7]}→{c[8]}  ターン +{c[5]}  停止 {c[6]}s")
    if len(seen) >= 8:
        break
