"""自己対戦で「この状況でこの行動を取ると、その先どうなったか」の経験表を作る。GPU 不要。

    uv run learn.py [ラウンド数] [1 ラウンドのエピソード数] [--difficulty normal]

学習は NORMAL で行う (NOTES.md 15 章)。--difficulty は物差し用に他の難易度でも回せるようにしてあるだけ。

1 ラウンドの流れ:
  1. いまの経験表に従って (ときどき気まぐれに) ダンジョンを潜る
  2. 途中の局面で、取れる行動を 1 つずつ実際に試し、その先 HORIZON ターンを何通りか先読みする
  3. 先読みの結果を得点 (score) の増減で測り、(状況, 行動) ごとに平均して表に足す
  4. 先読みの終点では「その状況から先の見込み」(前のラウンドまでの表の評価) を割り引いて足す。
     40 ターンでは見えない価値 (空腹に備える、休んで回復する、死ぬとその先を全部失う) が、ラウンドを重ねるごとに表に染みていく
次のラウンドは賢くなった表で潜るので、より先の局面の経験が溜まっていく。

人間が与えるのは WANTS の「何が嬉しいか」だけ。どの行動が良いかは一切与えない。

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

import rogue_data as D
from pathlib import Path

from brain import TAU, coarse_key, describe, hp_word
from game import Game, avg_dice

DATA = Path(__file__).parent / "data"
HORIZON = 40        # 先読みするターン数
ROLLOUTS = 3        # 1 行動あたりの先読み回数
P_EVAL = 0.05       # 通過した局面のうち、先読みで調べる割合
P_EVAL_RARE = 0.5   # 稀な判断 (薬・巻物を使う、危険な場面で未識別を試す) が選べる局面は、この割合で調べる。
                    # 5% のままだと経験表の重みが 4 に届かず、回復薬や転移の使い方を判断ヘッドに教えていなかった (NOTES.md 11 章)
RARE_ACTIONS = {"quaff_heal", "quaff_str", "read_map", "read_teleport", "read_identify", "zap_bolt", "zap_missile", "zap_slow", "zap_away",
                "zap_polymorph", "zap_drain", "zap_cancel", "zap_light", "zap_unknown", "wield_bow", "wield_melee",
                "read_enchant_armor", "read_enchant_weapon", "read_protect",
                "read_remove_curse", "put_on_ring", "remove_ring", "read_confuse", "read_hold", "drop_scare", "read_food",
                "quaff_haste", "quaff_raise", "quaff_see_invisible", "quaff_detect_monsters", "quaff_detect_magic"}
MAX_TURNS = 3000
EPSILON = 0.15      # 表を無視して気まぐれに動く確率 (知らない局面に出会うため)
DECAY = 0.5         # ラウンドをまたぐとき、古い経験の重みをこれだけ残す
GAMMA = 0.85        # 先読みの終点から先の見込みを、どれだけ割り引いて足すか (0 で足さない)
ROLL_TEMP = 0.2     # 先読みの中での行動の選び方 (softmax の温度)
COMMIT = 10         # 調べる行動は、状況 (粗いキー) が変わるまで最大このターン数だけ続ける。「休む」のように 1 回では差が出ない行動の価値を測るため
P_CONTINUE = 0.6    # 控えておいた「階に着いた時点の状態」から始めるエピソードの割合
POOL_PER_DEPTH = 300

# 何が嬉しいか。命令ごとに分けていたが、効きが弱かったのでいったん 1 本にしてある (NOTES.md)
# 巻物の価値は「転移を持っている」ことにだけ付ける。強化の巻物は拾った時点で読まれて装備 (gear) の得点になる。地図は持っていても
# 点にしない (持つことに点を付けると読むと損になり、1 回目の学習で地図を読む率が 1〜2% になった)
# 未識別の薬・巻物は 1 つ 0.5 (拾う価値はあるが、正体の分かった回復薬 2.5 より低い。試して当たれば得、外れれば損は効果そのものから)
# 死の減点は残りの階数に比例して増やす (浅い階で死ぬほど失うものが大きい)。50 固定だと「1 階のために 15% の死亡リスクを取る」のが最適になっていた
WANTS = dict(depth=10, level=5, kills=0.5, gold=0.01, hp=5, heal=2.5, food=3, fed=6, gear=1.5, explored=0.02, death=50, death_per_floor=5,
             missile=0.1, scroll=0.5, unknown=0.5, wand=0.5,   # wand: 正体の分かった杖の残り 1 回ぶん。未識別の杖は 1 本 unknown
             ring=1.0, cursed=1.5)                              # ring: 着けている正体の分かった有益な指輪 1 つ (防御・力は数値に出るので別途)。cursed: 外せない物 1 つの減点
TACTICAL_SCROLLS = ("teleportation", "remove curse", "monster confusion", "hold monster", "scare monster", "food detection")
ENCHANT_VALUE = 1.0   # 正体の分かった強化・保護の巻物 1 枚 (読めば装備の点になる。持っているだけより読む方が得になる値)
USEFUL_STICKS = {"lightning", "fire", "cold", "magic missile", "slow monster", "teleport away", "polymorph", "drain life", "cancellation"}
TACTICAL_POTIONS = {"haste self": 1.5, "raise level": 2.0, "see invisible": 0.5, "monster detection": 0.5, "magic detection": 0.5}   # 1 本の価値


def hp_value(f):
    """HP の割合 f (0〜1) の価値。凹型: 低いほど 1 点の HP が重く、満タン近くではほぼ 0。
    割合そのままだと「80% から満タンまで休む」だけで +1 点になり、探索 (40 マスで +0.24) より得に見えて、
    Laya が HP 80〜99% で待機ばかりする癖になった (NOTES 13 章)。"""
    return 1 - (1 - f) ** 2


def score(g):
    w = WANTS
    mw = g.melee_weapon()
    gear = ((10 - g.ac()) + (1 if g.armor.get("protected") else 0) + avg_dice(mw["dice"]) + mw["dplus"] + 0.5 * mw["hplus"])
    rings = sum(1 for r in g.worn if r["name"] in g.known and not g.ring_useless(r) and r["name"] not in ("protection", "add strength"))
    return (w["depth"] * g.depth + w["level"] * g.level + w["kills"] * g.kills + w["gold"] * g.gold
            + w["hp"] * hp_value(g.hp / max(1, g.max_hp)) + w["heal"] * g.has_heal() + w["food"] * min(g.food, 3)
            + w["fed"] * max(0, min(g.food_left, 1300)) / 1300 + w["gear"] * gear + 1.5 * g.str
            + w["explored"] * g.explored + w["missile"] * min(30, sum(g.missiles.values()))
            + w["scroll"] * sum(g.scrolls.get(n, 0) for n in TACTICAL_SCROLLS if n in g.known)
            + sum(v * g.has_potion(n) for n, v in TACTICAL_POTIONS.items())
            + w["unknown"] * (sum(g.unknown_potions().values()) + sum(g.unknown_scrolls().values()) + len(g.unknown_sticks())
                              + sum(g.unknown_rings().values()))
            + w["wand"] * sum(c for k, c in g.known_sticks().items() if k in USEFUL_STICKS) + w["ring"] * rings - w["cursed"] * len(g.cursed_worn())
            + ENCHANT_VALUE * sum(g.scrolls.get(n, 0) for n in D.ENCHANT_SCROLLS if n in g.known)
            + (100 if g.won else 0) - ((w["death"] + w["death_per_floor"] * max(0, D.GOAL_DEPTH - g.depth)) if g.dead else 0))


TEXTS_PER_KEY = 24  # 粗いキー 1 つにつき、Laya の教材として控えておく状況文の数


def pick(table, key, valid, rng, temp, eps):
    q = table.get(key, {}).get("q")
    if q is None or rng.random() < eps:
        return rng.choice(valid)
    vals = [q[a][0] for a in valid]
    top = max(vals)
    weights = [math.exp((v - top) / temp) for v in vals]
    return rng.choices(valid, weights)[0]


def value(table, key, valid):
    """その状況から先の見込み。表の評価を、先読みの中と同じ選び方で平均する。経験がほとんどない状況は None。"""
    q = table.get(key, {}).get("q")
    if q is None or min(v[1] for v in q.values()) < 1:
        return None
    vals = [q[a][0] for a in valid]
    top = max(vals)
    weights = [math.exp((v - top) / ROLL_TEMP) for v in vals]
    return sum(v * w for v, w in zip(vals, weights)) / sum(weights)


def rollout(g, action, table, v_default, seed):
    rng = random.Random(seed)
    sim = g.clone(seed)
    before = score(sim)
    key0 = coarse_key(sim, sim.valid_actions())
    sim.step(action)
    committed = 1
    for _ in range(HORIZON - 1):
        if sim.over:
            break
        valid = sim.valid_actions()
        key = coarse_key(sim, valid)
        if committed and committed < COMMIT and key == key0:
            committed += 1
            sim.step(action)
            continue
        committed = 0
        sim.step(pick(table, key, valid, rng, ROLL_TEMP, 0.05))
    ret = score(sim) - before
    if GAMMA and not sim.over:  # 死んだらその先は 0。生きていれば、終点の状況の見込みを足す
        valid = sim.valid_actions()
        v = value(table, coarse_key(sim, valid), valid)
        ret += GAMMA * (v_default if v is None else v)
    return ret


_table, _v_default = {}, 0.0


def _init(table, v_default):
    global _table, _v_default
    _table, _v_default = table, v_default


def episode(args):
    """1 回潜り、調べた局面ごとの (粗いキー, 状況文, {行動: 平均リターン}) と、階に着いた時点の勇者の状態を返す。"""
    seed, start, difficulty = args
    rng = random.Random(seed)
    g = Game(rng.randrange(1 << 30), start, difficulty)
    out, arrivals, depth = [], [], g.depth
    turns = 0
    while not g.over and turns < MAX_TURNS:
        turns += 1
        valid = g.valid_actions()
        key = coarse_key(g, valid)
        gamble = ("quaff_unknown" in valid or "read_unknown" in valid) and (
            hp_word(g) in ("low", "critical") or any(m["awake"] for m in g.visible_monsters()))
        p_eval = P_EVAL_RARE if (RARE_ACTIONS & set(valid) or gamble) else P_EVAL
        if len(valid) > 1 and rng.random() < p_eval:
            # 行動どうしの比較では同じ乱数列を使う。「運の差」が消えて「行動の差」だけが残る
            seeds = [rng.random() for _ in range(ROLLOUTS)]
            out.append((key, describe(g, valid), {a: sum(rollout(g, a, _table, _v_default, s) for s in seeds) / ROLLOUTS for a in valid}))
        g.step(pick(_table, key, valid, rng, TAU, EPSILON))
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
    argv = list(sys.argv[1:])
    difficulty = "normal"
    if "--difficulty" in argv:
        i = argv.index("--difficulty")
        difficulty = argv[i + 1]
        del argv[i:i + 2]
    D.rules(difficulty)  # 名前の検査
    rounds = int(argv[0]) if argv else 12
    episodes = int(argv[1]) if len(argv) > 1 else 640
    print(f"難易度 {difficulty.upper()}", flush=True)
    be_nice()
    DATA.mkdir(exist_ok=True)
    table = {}   # 粗いキー -> {"q": {行動: [平均リターン, 重み]}, "texts": [そのキーで実際に出会った状況文]}
    pool = {}    # 深さ -> 階に着いた時点の勇者の状態
    rng = random.Random(0)
    for r in range(1, rounds + 1):
        t0 = time.perf_counter()
        for entry in table.values():
            for v in entry["q"].values():
                v[1] *= DECAY
        # 表にない (経験がほとんどない) 状況の見込みは、表全体の平均で代用する。0 にすると未知の状況を不当に避けてしまう
        known = [(value(table, k, list(e["q"])), min(v[1] for v in e["q"].values())) for k, e in table.items()]
        known = [(v, w) for v, w in known if v is not None]
        v_default = sum(v * w for v, w in known) / sum(w for _, w in known) if known else 0.0
        jobs = []
        for i in range(episodes):
            start = rng.choice(pool[rng.choice(list(pool))]) if pool and rng.random() < P_CONTINUE else None
            jobs.append((r * 1_000_003 + i, start, difficulty))
        with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 4) - 2), initializer=_init, initargs=(table, v_default)) as pool_exec:
            results = list(pool_exec.map(episode, jobs, chunksize=2))
        n_eval, fresh_depths, deepest = 0, [], 0
        for samples, arrivals, depth, dead, fresh in results:
            deepest = max(deepest, depth)
            if fresh:
                fresh_depths.append(depth)
            for key, text, rets in samples:
                n_eval += 1
                entry = table.setdefault(key, {"q": {a: [0.0, 0.0] for a in rets}, "texts": []})
                for a, ret in rets.items():
                    mean, w = entry["q"][a]
                    entry["q"][a] = [(mean * w + ret) / (w + 1), w + 1]
                if text not in entry["texts"]:
                    if len(entry["texts"]) < TEXTS_PER_KEY:
                        entry["texts"].append(text)
                    else:
                        entry["texts"][rng.randrange(TEXTS_PER_KEY)] = text
            for h in arrivals:
                bucket = pool.setdefault(h["depth"], [])
                if len(bucket) < POOL_PER_DEPTH:
                    bucket.append(h)
                else:
                    bucket[rng.randrange(POOL_PER_DEPTH)] = h
        (DATA / f"table_r{r}.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        print(f"round {r}: 1 階から始めた回の平均到達階 {sum(fresh_depths) / max(1, len(fresh_depths)):.2f} | 全体の最深 {deepest} | "
              f"控えのある深さ {max(pool) if pool else 1} | 調べた局面 {n_eval} | 表の状況数 {len(table)} | 見込みの平均 {v_default:.1f} | {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
