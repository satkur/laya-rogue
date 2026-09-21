"""自己対戦で「この状況でこの行動を取ると、その先どうなったか」の経験表を作る。GPU 不要。

    uv run learn.py [ラウンド数] [1 ラウンドのエピソード数]

1 ラウンドの流れ:
  1. いまの経験表に従って (ときどき気まぐれに) ダンジョンを潜る
  2. 途中の局面で、取れる行動を 1 つずつ実際に試し、その先 HORIZON ターンを何通りか先読みする
  3. 先読みの結果を得点 (score) の増減で測り、(状況文, 行動) ごとに平均して表に足す
次のラウンドは賢くなった表で潜るので、より先の局面の経験が溜まっていく。

人間が与えるのは WANTS の「命令ごとに何が嬉しいか」だけ。どの行動が良いかは一切与えない。
潜っている途中で命令をランダムに切り替えるので、どの深さの局面もすべての命令のもとで経験される。

深い階の経験を増やす工夫: 新しい階に着いたときの勇者の状態 (レベル・装備・持ち物) を控えておき、
次のラウンドでは半分強のエピソードをその続きから始める。浅い階で死に続けても、到達済みの深さの練習ができる。
控えるのは自己対戦で実際に到達した状態だけ。

ラウンドごとの表は data/table_r<N>.json に保存し、train.py がそれを Laya に学習させる。
"""
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from brain import ORDERS, describe, state_key
from game import Game, avg_dice

DATA = Path(__file__).parent / "data"
HORIZON = 40        # 先読みするターン数
ROLLOUTS = 3        # 1 行動あたりの先読み回数
P_EVAL = 0.05       # 通過した局面のうち、先読みで調べる割合
MAX_TURNS = 3000
EPSILON = 0.15      # 表を無視して気まぐれに動く確率 (知らない局面に出会うため)
DECAY = 0.5         # ラウンドをまたぐとき、古い経験の重みをこれだけ残す
ORDER_SPAN = 80     # 学習中、このターン数ごとに命令を引き直す
P_CONTINUE = 0.6    # 控えておいた「階に着いた時点の状態」から始めるエピソードの割合
POOL_PER_DEPTH = 300

# 命令ごとの「何が嬉しいか」
WANTS = {
    "cautious":   dict(depth=5,  level=3, kills=0.3, gold=0.01, hp=10, heal=3.0, food=3, fed=6, gear=1, explored=0.004, death=80),
    "aggressive": dict(depth=8,  level=6, kills=1.0, gold=0.01, hp=4,  heal=1.5, food=2, fed=5, gear=1, explored=0.008, death=30),
    "loot":       dict(depth=4,  level=3, kills=0.3, gold=0.06, hp=5,  heal=4.0, food=4, fed=6, gear=2, explored=0.020, death=40),
    "descend":    dict(depth=25, level=2, kills=0.2, gold=0.01, hp=4,  heal=1.5, food=2, fed=5, gear=1, explored=0.006, death=40),
}


def score(g, order):
    w = WANTS[order]
    gear = (10 - g.armor["ac"]) + avg_dice(g.weapon["dice"]) + g.weapon["dplus"] + 0.5 * g.weapon["hplus"]
    return (w["depth"] * g.depth + w["level"] * g.level + w["kills"] * g.kills + w["gold"] * g.gold
            + w["hp"] * g.hp / g.max_hp + w["heal"] * g.has_heal() + w["food"] * min(g.food, 3)
            + w["fed"] * max(0, min(g.food_left, 1300)) / 1300 + w["gear"] * gear + 1.5 * g.str
            + w["explored"] * g.explored + (100 if g.won else 0) - (w["death"] if g.dead else 0))


def pick(table, key, valid, rng, temp, eps):
    q = table.get(key)
    if q is None or rng.random() < eps:
        return rng.choice(valid)
    vals = [q[a][0] for a in valid]
    top = max(vals)
    weights = [math.exp((v - top) / temp) for v in vals]
    return rng.choices(valid, weights)[0]


def rollout(g, action, order, table, seed):
    rng = random.Random(seed)
    sim = g.clone(seed)
    before = score(sim, order)
    sim.step(action)
    for _ in range(HORIZON - 1):
        if sim.over:
            break
        valid = sim.valid_actions()
        sim.step(pick(table, state_key(describe(sim, valid, order), valid), valid, rng, 0.5, 0.05))
    return score(sim, order) - before


_table = {}


def _init(table):
    global _table
    _table = table


def episode(args):
    """1 回潜り、調べた局面ごとの (キー, {行動: 平均リターン}) と、階に着いた時点の勇者の状態を返す。"""
    seed, start = args
    rng = random.Random(seed)
    g = Game(rng.randrange(1 << 30), start)
    out, arrivals, depth = [], [], g.depth
    order = rng.choice(ORDERS)
    turns = 0
    while not g.over and turns < MAX_TURNS:
        if turns % ORDER_SPAN == 0:
            order = rng.choice(ORDERS)
        turns += 1
        valid = g.valid_actions()
        key = state_key(describe(g, valid, order), valid)
        if len(valid) > 1 and rng.random() < P_EVAL:
            # 行動どうしの比較では同じ乱数列を使う。「運の差」が消えて「行動の差」だけが残る
            seeds = [rng.random() for _ in range(ROLLOUTS)]
            out.append((key, {a: sum(rollout(g, a, order, _table, s) for s in seeds) / ROLLOUTS for a in valid}))
        g.step(pick(_table, key, valid, rng, 1.0, EPSILON))
        g.log.clear()
        if g.depth != depth and not g.over:
            depth = g.depth
            arrivals.append(g.hero_state())
    return out, arrivals, g.depth, g.dead, start is None


def be_nice():
    """全コアを何分も使うので、低優先度にして他の作業を邪魔しない (子プロセスにも引き継がれる)。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    else:
        os.nice(10)


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    episodes = int(sys.argv[2]) if len(sys.argv) > 2 else 640
    be_nice()
    DATA.mkdir(exist_ok=True)
    table = {}   # key -> {action: [平均リターン, 重み]}
    pool = {}    # 深さ -> 階に着いた時点の勇者の状態
    rng = random.Random(0)
    for r in range(1, rounds + 1):
        t0 = time.perf_counter()
        for q in table.values():
            for v in q.values():
                v[1] *= DECAY
        jobs = []
        for i in range(episodes):
            start = rng.choice(pool[rng.choice(list(pool))]) if pool and rng.random() < P_CONTINUE else None
            jobs.append((r * 1_000_003 + i, start))
        with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2), initializer=_init, initargs=(table,)) as pool_exec:
            results = list(pool_exec.map(episode, jobs, chunksize=2))
        n_eval, fresh_depths, deepest = 0, [], 0
        for samples, arrivals, depth, dead, fresh in results:
            deepest = max(deepest, depth)
            if fresh:
                fresh_depths.append(depth)
            for key, rets in samples:
                n_eval += 1
                q = table.setdefault(key, {a: [0.0, 0.0] for a in rets})
                for a, ret in rets.items():
                    mean, w = q[a]
                    q[a] = [(mean * w + ret) / (w + 1), w + 1]
            for h in arrivals:
                bucket = pool.setdefault(h["depth"], [])
                if len(bucket) < POOL_PER_DEPTH:
                    bucket.append(h)
                else:
                    bucket[rng.randrange(POOL_PER_DEPTH)] = h
        (DATA / f"table_r{r}.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        print(f"round {r}: 1 階から始めた回の平均到達階 {sum(fresh_depths) / max(1, len(fresh_depths)):.2f} | 全体の最深 {deepest} | "
              f"控えのある深さ {max(pool) if pool else 1} | 調べた局面 {n_eval} | 表の状況数 {len(table)} | {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
