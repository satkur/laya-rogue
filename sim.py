"""画面なしで何回も潜らせて、頭脳ごとの腕前を比べる。  uv run sim.py [回数] [最大ターン]"""
import random
import statistics
import sys
import time

from brain import LayaBrain, RandomBrain, RuleBrain
from game import Game

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
max_turns = int(sys.argv[2]) if len(sys.argv) > 2 else 600

brains = [RandomBrain(random.Random(0)), RuleBrain(), LayaBrain("multilingual"), LayaBrain("english"), LayaBrain("multilingual", guarded=False)]
print(f"{runs} 回 × 最大 {max_turns} ターン (同じシード列で比較)\n")
for brain in brains:
    depth, kills, deaths, ms, n, agree, intervened = [], [], 0, [], 0, 0, 0
    t0 = time.perf_counter()
    for seed in range(runs):
        g = Game(seed)
        while not g.dead and g.turn < max_turns:
            d = brain.decide(g)
            n += 1
            agree += d["proposed"] == d["best"]
            intervened += d["intervened"]
            ms.append(d["ms"])
            g.step(d["action"])
        depth.append(g.depth)
        kills.append(g.kills)
        deaths += g.dead
    print(f"[{brain.name:28s}] 到達階 平均 {statistics.mean(depth):.1f} (最高 {max(depth)}) | 撃破 {statistics.mean(kills):4.1f} | 死亡 {deaths}/{runs} | "
          f"Best と一致 {agree / n:.0%} | 介入 {intervened / n:.1%} | 推論 {statistics.median(ms):.1f} ms | {time.perf_counter() - t0:.0f}s")
