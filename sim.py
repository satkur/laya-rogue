"""画面なしで何回も潜らせて、頭脳ごとの腕前を比べる。

    uv run sim.py [回数] [最大ターン] [頭脳...]

頭脳: random / rules / table:<ラウンド> / laya (未学習) / laya:<世代名>
      +<命令> で命令を固定 (cautious / aggressive / loot / descend、既定は aggressive)
      @<鋭さ> で行動の引き方を変える (@max で常に最有力、既定は 2.5)
例:   uv run sim.py 40 800 random rules table:8+descend laya laya:gen8+cautious laya:gen8+loot@max
"""
import json
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from brain import DEFAULT_ORDER, RandomBrain, RuleBrain, TableBrain
from game import Game

DATA = Path(__file__).parent / "data"


def split_spec(spec):
    spec, _, sh = spec.partition("@")
    spec, _, order = spec.partition("+")
    return spec, order or DEFAULT_ORDER, (2.5 if not sh else None if sh == "max" else float(sh))


def make_cpu_brain(spec):
    spec, order, sharpness = split_spec(spec)
    if spec == "random":
        return RandomBrain(random.Random(0))
    if spec == "rules":
        return RuleBrain()
    if spec.startswith("table:"):
        brain = TableBrain(json.loads((DATA / f"table_r{spec[6:]}.json").read_text(encoding="utf-8")), sharpness)
        brain.order = order
        return brain
    raise ValueError(spec)


def play(brain, seed, max_turns):
    g = Game(seed)
    ms, miss, n = [], 0, 0
    while not g.dead and g.turn < max_turns:
        d = brain.decide(g)
        n += 1
        miss += d.get("miss", False)
        if d["ms"]:
            ms.append(d["ms"])
        g.step(d["action"])
        g.log.clear()
    return g.depth, g.kills, g.dead, g.turn, ms, miss / max(1, n), g.gold


def _cpu_job(args):
    spec, seed, max_turns = args
    return play(make_cpu_brain(spec), seed, max_turns)


def report(name, results, dt):
    depth = [r[0] for r in results]
    ms = [m for r in results for m in r[4]]
    miss = statistics.mean(r[5] for r in results)
    print(f"[{name:24s}] 到達階 平均 {statistics.mean(depth):.2f} (最高 {max(depth)}) | 撃破 {statistics.mean(r[1] for r in results):5.1f} | "
          f"金貨 {statistics.mean(r[6] for r in results):4.0f} | 死亡 {sum(r[2] for r in results)}/{len(results)} | 生存ターン {statistics.mean(r[3] for r in results):4.0f}"
          + (f" | 推論 {statistics.median(ms):.1f} ms" if ms else "") + (f" | 表にない状況 {miss:.0%}" if miss else "") + f" | {dt:.0f}s", flush=True)


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    max_turns = int(sys.argv[2]) if len(sys.argv) > 2 else 800
    specs = sys.argv[3:] or ["random", "rules"]
    seeds = [5000 + i for i in range(runs)]
    print(f"{runs} 回 × 最大 {max_turns} ターン (全頭脳で同じシード)\n")
    laya = None
    for spec in specs:
        t0 = time.perf_counter()
        if spec.startswith("laya"):
            from brain import LayaBrain

            name, order, sharpness = split_spec(spec)
            gen = name[5:] or None
            if laya is None:
                laya = LayaBrain(gen)
            else:
                laya.load_generation(gen)
            laya.order, laya.sharpness = order, sharpness
            results = [play(laya, s, max_turns) for s in seeds]
        else:
            with ProcessPoolExecutor() as pool:
                results = list(pool.map(_cpu_job, [(spec, s, max_turns) for s in seeds], chunksize=2))
        report(spec, results, time.perf_counter() - t0)


if __name__ == "__main__":
    main()
