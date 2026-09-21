"""自己対戦で「この状況でこの行動を取ると、その先どうなったか」の経験表を作る。GPU 不要。

    uv run learn.py [ラウンド数] [1 ラウンドのエピソード数]

1 ラウンドの流れ:
  1. いまの経験表に従って (ときどき気まぐれに) ダンジョンを潜る
  2. 途中の局面で、取れる行動を 1 つずつ実際に試し、その先 HORIZON ターンを何通りか先読みする
  3. 先読みの結果を得点 (score) の増減で測り、(状況文, 行動) ごとに平均して表に足す
次のラウンドは賢くなった表で潜るので、より先の局面の経験が溜まっていく。

人間が与えるのは score() の「何が嬉しいか」だけ。どの行動が良いかは一切与えない。
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

from brain import describe, state_key
from game import Game

DATA = Path(__file__).parent / "data"
HORIZON = 40       # 先読みするターン数
ROLLOUTS = 3       # 1 行動あたりの先読み回数
P_EVAL = 0.12      # 通過した局面のうち、先読みで調べる割合
MAX_TURNS = 500
EPSILON = 0.15     # 表を無視して気まぐれに動く確率 (知らない局面に出会うため)
DECAY = 0.5        # ラウンドをまたぐとき、古い経験の重みをこれだけ残す


def score(g):
    """何が嬉しいか。深く潜る・倒す・拾う・体力と薬を保つ・未知を減らす。死は大損。"""
    return (10 * g.depth + 1.5 * g.kills + 0.04 * g.gold + 5 * g.hp / g.max_hp + 2 * g.potions
            + 0.01 * g.explored - (30 if g.dead else 0))


def pick(table, key, valid, rng, temp, eps):
    q = table.get(key)
    if q is None or rng.random() < eps:
        return rng.choice(valid)
    vals = [q[a][0] for a in valid]
    top = max(vals)
    weights = [math.exp((v - top) / temp) for v in vals]
    return rng.choices(valid, weights)[0]


def rollout(g, action, table, seed):
    rng = random.Random(seed)
    sim = g.clone(seed)
    before = score(sim)
    sim.step(action)
    for _ in range(HORIZON - 1):
        if sim.dead:
            break
        valid = sim.valid_actions()
        sim.step(pick(table, state_key(describe(sim, valid), valid), valid, rng, 0.5, 0.05))
    return score(sim) - before


_table = {}


def _init(table):
    global _table
    _table = table


def episode(seed):
    """1 回潜り、調べた局面ごとに (キー, {行動: 平均リターン}) を返す。"""
    rng = random.Random(seed)
    g = Game(rng.randrange(1 << 30))
    out = []
    while not g.dead and g.turn < MAX_TURNS:
        valid = g.valid_actions()
        key = state_key(describe(g, valid), valid)
        if len(valid) > 1 and rng.random() < P_EVAL:
            # 行動どうしの比較では同じ乱数列を使う。「運の差」が消えて「行動の差」だけが残る
            seeds = [rng.random() for _ in range(ROLLOUTS)]
            out.append((key, {a: sum(rollout(g, a, _table, s) for s in seeds) / ROLLOUTS for a in valid}))
        g.step(pick(_table, key, valid, rng, 1.0, EPSILON))
        g.log.clear()
    return out, g.depth, g.dead


def be_nice():
    """全コアを何分も使うので、低優先度にして他の作業を邪魔しない (子プロセスにも引き継がれる)。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    else:
        os.nice(10)


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    episodes = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    be_nice()
    DATA.mkdir(exist_ok=True)
    table = {}  # key -> {action: [平均リターン, 重み]}
    for r in range(1, rounds + 1):
        t0 = time.perf_counter()
        for q in table.values():
            for v in q.values():
                v[1] *= DECAY
        with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2), initializer=_init, initargs=(table,)) as pool:
            results = list(pool.map(episode, [r * 1_000_003 + i for i in range(episodes)], chunksize=4))
        n_eval, depths, deaths = 0, [], 0
        for samples, depth, dead in results:
            depths.append(depth)
            deaths += dead
            for key, rets in samples:
                n_eval += 1
                q = table.setdefault(key, {a: [0.0, 0.0] for a in rets})
                for a, ret in rets.items():
                    mean, w = q[a]
                    q[a] = [(mean * w + ret) / (w + 1), w + 1]
        (DATA / f"table_r{r}.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        print(f"round {r}: 平均到達階 {sum(depths) / len(depths):.2f} (最高 {max(depths)}) | 死亡 {deaths}/{episodes} | "
              f"調べた局面 {n_eval} | 表の状況数 {len(table)} | {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
