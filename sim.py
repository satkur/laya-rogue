"""画面なしで何回も潜らせて、頭脳ごとの腕前を比べる。目標は地下 20 階。

    uv run sim.py [回数] [最大ターン] [頭脳...]

頭脳: random / rules / diver / table:<ラウンド> / laya (未学習) / laya:<世代名>
      @<鋭さ> で行動の引き方を変える (@max で常に最有力、既定は 2.5)
      +llm / +llm:sonnet で方針役の LLM を付ける (strategist.py。claude -p を呼ぶので 1 ゲーム数分かかり、利用枠を使う)
例:   uv run sim.py 40 8000 random rules table:12 laya laya:gen12@max
"""
import json
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from brain import DiverBrain, RandomBrain, RuleBrain, TableBrain
from game import Game

DATA = Path(__file__).parent / "data"


LLM_GAMES = 4  # 方針役つきのゲームを同時に進める数 (待ち時間のほとんどは claude -p の応答)


def split_spec(spec):
    spec, _, sh = spec.partition("@")
    return spec, (2.5 if not sh else None if sh == "max" else float(sh))


def split_llm(spec):
    spec, plus, llm = spec.partition("+llm")
    return spec, (llm[1:] or "opus") if plus else None


class Locked:
    """1 つの頭脳 (GPU 上の Laya) を複数のゲームのスレッドから順番に使う。"""

    def __init__(self, brain, lock):
        self.brain, self.lock, self.name = brain, lock, brain.name

    def decide(self, g):
        with self.lock:
            return self.brain.decide(g)


def play_llm(make_brain, seeds, max_turns, model):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import strategist
    from strategist import Guided, Strategist

    strategist.MAX_CALLS_TOTAL = strategist.MAX_CALLS_PER_GAME * len(seeds)  # 明示的に頼まれた比較なので、1 ゲームあたりの上限だけを効かせる

    lock = threading.Lock()
    stats = []

    def one(seed):
        st = Strategist(model=model)
        r = play(Guided(Locked(make_brain(), lock), st), seed, max_turns)
        stats.append(st)
        print(f"    seed {seed}: {r['depth']} 階 {r['end']} / 方針役 {st.calls} 回 {st.seconds:.0f}s {st.tokens} tokens" + (f" / 停止: {st.stopped}" if st.stopped else ""), flush=True)
        return r

    with ThreadPoolExecutor(LLM_GAMES) as pool:
        results = list(pool.map(one, seeds))
    print(f"    方針役の合計: {sum(s.calls for s in stats)} 回, {sum(s.tokens for s in stats)} tokens", flush=True)
    return results


def make_cpu_brain(spec):
    spec, sharpness = split_spec(spec)
    if spec == "random":
        return RandomBrain(random.Random(0))
    if spec == "rules":
        return RuleBrain()
    if spec == "diver":
        return DiverBrain()
    if spec.startswith("table:"):
        return TableBrain(json.loads((DATA / f"table_r{spec[6:]}.json").read_text(encoding="utf-8")), sharpness)
    raise ValueError(spec)


def play(brain, seed, max_turns):
    g = Game(seed)
    ms, miss, n = [], 0, 0
    while not g.over and g.turn < max_turns:
        d = brain.decide(g)
        n += 1
        miss += d.get("miss", False)
        if d["ms"]:
            ms.append(d["ms"])
        g.step(d["action"])
        g.log.clear()
    end = "到達" if g.won else g.cause if g.dead else "時間切れ"
    return dict(depth=g.depth, level=g.level, kills=g.kills, gold=g.gold, turn=g.turn, end=end, ms=ms, miss=miss / max(1, n))


def _cpu_job(args):
    spec, seed, max_turns = args
    return play(make_cpu_brain(spec), seed, max_turns)


def report(name, rs, dt):
    depth = [r["depth"] for r in rs]
    ms = [m for r in rs for m in r["ms"]]
    miss = statistics.mean(r["miss"] for r in rs)
    ends = Counter(r["end"] for r in rs)
    print(f"[{name:24s}] 到達階 平均 {statistics.mean(depth):5.2f} 中央 {statistics.median(depth):4.1f} 最高 {max(depth):2d} | "
          f"10階+ {sum(d >= 10 for d in depth):2d} 20階 {ends['到達']:2d} /{len(rs)} | Lv {statistics.mean(r['level'] for r in rs):4.1f} | "
          f"撃破 {statistics.mean(r['kills'] for r in rs):5.1f} | 金貨 {statistics.mean(r['gold'] for r in rs):5.0f} | ターン {statistics.mean(r['turn'] for r in rs):5.0f}"
          + (f" | 推論 {statistics.median(ms):.1f} ms" if ms else "") + (f" | 表にない状況 {miss:.0%}" if miss else "") + f" | {dt:.0f}s", flush=True)
    print("    終わり方: " + " ".join(f"{k}×{v}" for k, v in ends.most_common(7)), flush=True)


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    max_turns = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    specs = sys.argv[3:] or ["random", "rules"]
    seeds = [5000 + i for i in range(runs)]
    print(f"{runs} 回 × 最大 {max_turns} ターン (全頭脳で同じシード)\n")
    laya = None
    for full in specs:
        t0 = time.perf_counter()
        spec, llm = split_llm(full)
        if spec.startswith("laya"):
            from brain import LayaBrain

            name, sharpness = split_spec(spec)
            gen = name[5:] or None
            if laya is None:
                laya = LayaBrain(gen)
            else:
                laya.load_generation(gen)
            laya.sharpness = sharpness
            results = play_llm(lambda: laya, seeds, max_turns, llm) if llm else [play(laya, s, max_turns) for s in seeds]
        elif llm:
            results = play_llm(lambda: make_cpu_brain(spec), seeds, max_turns, llm)
        else:
            with ProcessPoolExecutor() as pool:
                results = list(pool.map(_cpu_job, [(spec, s, max_turns) for s in seeds], chunksize=2))
        report(full, results, time.perf_counter() - t0)


if __name__ == "__main__":
    main()
