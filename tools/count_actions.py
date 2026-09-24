"""行動の使用回数を数える: uv run python tools/count_actions.py <頭脳> <シード数> [難易度]"""
import sys
from collections import Counter
sys.path.insert(0, r"C:\Peculium\src\laya-rogue")
from sim import make_cpu_brain
from game import Game
brain, n = make_cpu_brain(sys.argv[1]), int(sys.argv[2])
diff = sys.argv[3] if len(sys.argv) > 3 else "normal"
c, ends, worn_max = Counter(), Counter(), 0
for s in range(5000, 5000 + n):
    g = Game(s, None, diff)
    while not g.over and g.turn < 8000:
        a = brain.decide(g)["action"]
        c[a] += 1
        g.step(a)
        worn_max = max(worn_max, len(g.worn))
    ends[g.cause if g.dead else "alive"] += 1
print({k: v for k, v in c.items() if k not in ("explore", "rest", "attack", "descend", "pick_up", "approach", "throw", "flee")})
print("最大着用", worn_max, ends.most_common(6))
