"""行き詰まりの再現: uv run python tools/replay.py <頭脳> <種> <ターン> [難易度]。そのターンの前 300 手の行動と位置を出す"""
import sys
from collections import Counter
sys.path.insert(0, r"C:\Peculium\src\laya-rogue")
from sim import make_cpu_brain
from game import Game
from brain import describe
brain, seed, t_end = make_cpu_brain(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
g = Game(seed, None, sys.argv[4] if len(sys.argv) > 4 else "normal")
acts, pos, logs = [], [], []
while not g.over and g.turn < t_end:
    d = brain.decide(g)
    acts.append(d["action"]); pos.append((g.hx, g.hy))
    g.step(d["action"])
    logs.append(list(g.log)); g.log.clear()
print("行動", Counter(acts[-300:]).most_common(8))
print("位置", Counter(pos[-300:]).most_common(6))
print("valid", g.valid_actions())
print(describe(g, g.valid_actions()))
print("hunger", g.food_left, "food", g.food, "worn", g.worn, "rings", g.rings, "scrolls", g.scrolls, "known", sorted(g.known))
print("items near", [(i["kind"], i.get("name"), i["x"], i["y"], i.get("sensed"), i.get("found")) for i in g.visible_items()][:8])
print("最後のログ", [l for l in logs[-12:] if l])
