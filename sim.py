"""画面なしで何回も潜らせて、頭脳ごとの腕前を比べる。目標は地下 26 階 (rogue_data.GOAL_DEPTH)。

    uv run sim.py [回数] [最大ターン] [頭脳...] [--seeds 5003,5007] [--start 10] [--difficulty normal|hard|original]

--seeds を付けると回数の代わりにそのシードだけを回す。頭脳ごとの 1 回ずつの結果は data/results_<頭脳>.json に残る
各ゲームの行動列と方針役の相談は data/replays/<頭脳>_<種>[_B<開始階>][_<難易度>].json に残る (50 手ごとに書き出す)。
`uv run server.py --replay data/replays/<ファイル>` で画面に再生できる (回している最中のゲームも追いかけられる)。ゲームは種と行動列から再現するので、記録したときのコードで再生すること
(normal 以外は results_<頭脳>_<難易度>.json)。--difficulty の既定は normal (NOTES.md 15 章)。

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

import rogue_data as D
from brain import DiverBrain, RandomBrain, RuleBrain, TableBrain
from game import Game

DATA = Path(__file__).parent / "data"


LLM_GAMES = 4  # 方針役つきのゲームを同時に進める数 (待ち時間のほとんどは claude -p の応答)


def split_spec(spec):
    spec, _, sh = spec.partition("@")
    return spec, (2.5 if not sh else None if sh == "max" else float(sh))


def split_llm(spec):
    from strategist import DEFAULT_MODEL

    spec, plus, llm = spec.partition("+llm")
    return spec, (llm[1:] or DEFAULT_MODEL) if plus else None


class Locked:
    """1 つの頭脳 (GPU 上の Laya) を複数のゲームのスレッドから順番に使う。"""

    def __init__(self, brain, lock):
        self.brain, self.lock, self.name = brain, lock, brain.name

    def decide(self, g):
        with self.lock:
            return self.brain.decide(g)


def play_llm(make_brain, seeds, max_turns, model, start=None, difficulty="normal"):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import strategist
    from strategist import Guided, Strategist

    strategist.MAX_CALLS_TOTAL = strategist.MAX_CALLS_PER_GAME * len(seeds)  # 明示的に頼まれた比較なので、1 ゲームあたりの上限だけを効かせる

    lock = threading.Lock()
    stats = []

    def one(seed):
        st = Strategist(model=model)
        r = play(Guided(Locked(make_brain(), lock), st), seed, max_turns, start, difficulty)
        stats.append(st)
        (DATA / f"llm_{seed}.json").write_text(json.dumps({"result": {k: v for k, v in r.items() if k != "ms"}, "history": st.history},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"    seed {seed}: {r['depth']} 階 {r['end']} / 方針役 {st.calls} 回 {st.seconds:.0f}s {st.tokens} tokens"
              + (f" / 停止: {st.stopped}" if st.stopped else "") + (f" / 失敗で休止 {st.pauses} 回" if st.pauses else ""), flush=True)
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


def standard_hero(depth):
    """中盤開始の物差し用の標準の勇者 (1〜2 階のホブゴブリンくじを排除して、深い階での判断だけを測る。アドバイザーの提案)。
    6 階なら Lv5、10 階以降なら Lv7。防御は初期装備のまま、正体の分かった回復薬 1 つ、矢 30 本。"""
    lvl = 5 if depth < 10 else 7
    st = Game(0).hero_state()
    st.update(depth=depth, level=lvl, exp=D.EXP_LEVELS[lvl - 2], max_hp=12 + (lvl - 1) * 5, hp=12 + (lvl - 1) * 5,
              potions={"healing": 1}, known={"healing"}, missiles={"arrow": 30})
    return st


STALL_TURNS, STALL_TILES = 300, 4  # この連続ターン数のあいだ踏んだマスが STALL_TILES 種類以下で、休憩も戦闘もしておらず、起きた敵も見えていなければ「行き詰まり」


REPLAYS = DATA / "replays"
REPLAY_FLUSH = 50  # この手数ごとに記録を書き出す (再生側が追いかけられるように)


def replay_path(name, seed, start=None, difficulty="normal"):
    safe = name.replace("/", "-").replace(":", "-")
    return REPLAYS / f"{safe}_{seed}{f'_B{start}' if start else ''}{'' if difficulty == 'normal' else '_' + difficulty}.json"


def save_replay(path, rec):
    """記録を書き出す (仮ファイルに書いてから置き換えるので、再生側が途中の状態を読むことはない)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def play(brain, seed, max_turns, start=None, difficulty="normal"):
    g = Game(seed, standard_hero(start) if start else None, difficulty)
    rec = {"brain": getattr(brain, "name", "?"), "seed": seed, "start": start, "difficulty": difficulty, "actions": [], "advice": [], "done": False}
    rpath = replay_path(rec["brain"], seed, start, difficulty)
    if hasattr(brain, "rng"):  # 行動を引く乱数もゲームごとにシードで決める。共有したままだと同じ重みでも並べる順で結果が変わる
        brain.rng = random.Random(seed)
    g.action_rng = random.Random(seed)  # Laya はこちらを使う。方針役つき (Guided / Locked 越しで brain.rng が付かず、4 ゲームが 1 つの Laya を共有) でも単独と同じゲームになる (NOTES 15 章)
    ms, miss, n = [], 0, 0
    stalls, trail, acts, stall_depth = [], [], [], 0  # 行き詰まり: 敵もいないのに数マスを往復し続ける (NOTES 13 章)
    while not g.over and g.turn < max_turns:
        d = brain.decide(g)
        if d.get("advice"):  # 方針役の相談 (Guided)。i はこの相談の直後に取った行動の番号
            rec["advice"].append({"i": len(rec["actions"]), "turn": g.turn, **{k: v for k, v in d["advice"].items() if k != "valid"}})
        n += 1
        miss += d.get("miss", False)
        if d["ms"]:
            ms.append(d["ms"])
        trail.append((g.depth, g.hx, g.hy))
        acts.append(d["action"])
        g.step(d["action"])
        rec["actions"].append(d["action"])
        if len(rec["actions"]) % REPLAY_FLUSH == 0:
            save_replay(rpath, rec)
        g.log.clear()
        if (len(trail) >= STALL_TURNS and stall_depth != g.depth and len(set(trail[-STALL_TURNS:])) <= STALL_TILES
                and not ({"rest", "attack", "throw"} & set(acts[-STALL_TURNS:])) and not any(m["awake"] for m in g.visible_monsters())):
            stalls.append([g.depth, g.turn])
            stall_depth = g.depth  # 同じ階では 1 回だけ記録
    end = ("帰還" if g.rules.amulet else "到達") if g.won else g.cause if g.dead else "時間切れ"
    rec["done"], rec["result"] = True, dict(depth=g.max_depth, level=g.level, kills=g.kills, gold=g.gold, turn=g.turn, end=end, final_depth=g.depth)
    save_replay(rpath, rec)
    return dict(depth=g.max_depth, level=g.level, kills=g.kills, gold=g.gold, turn=g.turn, end=end, ms=ms, miss=miss / max(1, n), stalls=stalls,
                amulet=g.amulet, final_depth=g.depth)


def _cpu_job(args):
    spec, seed, max_turns, start, difficulty = args
    return play(make_cpu_brain(spec), seed, max_turns, start, difficulty)


def band_deaths(rs):
    """階の帯ごとの死亡率 = その帯で死んだ数 / その帯の階に着いた延べ数。平均到達階より分散が小さく、どの帯で良くなったかが分かる。"""
    out = []
    for lo, hi in ((1, 4), (5, 8), (9, 12), (13, 99)):
        arrivals = sum(max(0, min(r["depth"], hi) - lo + 1) for r in rs)
        deaths = sum(1 for r in rs if r["end"] not in ("到達", "帰還", "時間切れ") and lo <= r.get("final_depth", r["depth"]) <= hi)
        out.append(f"{lo}-{hi if hi < 99 else ''}: {deaths / arrivals:.0%}" if arrivals else f"{lo}-: -")
    return " ".join(out)


def report(name, rs, dt):
    depth = [r["depth"] for r in rs]
    ms = [m for r in rs for m in r["ms"]]
    miss = statistics.mean(r["miss"] for r in rs)
    ends = Counter(r["end"] for r in rs)
    se = statistics.stdev(depth) / len(depth) ** 0.5 if len(depth) > 1 else 0.0
    print(f"[{name:24s}] 到達階 平均 {statistics.mean(depth):5.2f} ±{se:.2f} 中央 {statistics.median(depth):4.1f} 最高 {max(depth):2d} | "
          f"10階+ {sum(d >= 10 for d in depth):2d} 到達 {ends['到達'] + ends['帰還']:2d} /{len(rs)}" + (f" 魔除け {sum(r.get('amulet', False) for r in rs)}" if any(r.get('amulet') for r in rs) else "")
          + f" | Lv {statistics.mean(r['level'] for r in rs):4.1f} | "
          f"撃破 {statistics.mean(r['kills'] for r in rs):5.1f} | 金貨 {statistics.mean(r['gold'] for r in rs):5.0f} | ターン {statistics.mean(r['turn'] for r in rs):5.0f}"
          + (f" | 推論 {statistics.median(ms):.1f} ms" if ms else "") + (f" | 表にない状況 {miss:.0%}" if miss else "") + f" | {dt:.0f}s", flush=True)
    print("    終わり方: " + " ".join(f"{k}×{v}" for k, v in ends.most_common(7)) + " | 死亡率 " + band_deaths(rs), flush=True)


def main():
    argv = list(sys.argv[1:])
    seeds = None
    if "--seeds" in argv:
        i = argv.index("--seeds")
        seeds = [int(x) for x in argv[i + 1].split(",")]
        del argv[i:i + 2]
    start = None
    if "--start" in argv:  # 中盤開始の物差し: この階から標準の勇者で始める
        i = argv.index("--start")
        start = int(argv[i + 1])
        del argv[i:i + 2]
    difficulty = "normal"
    if "--difficulty" in argv:
        i = argv.index("--difficulty")
        difficulty = argv[i + 1]
        del argv[i:i + 2]
    D.rules(difficulty)  # 名前の検査
    runs = int(argv[0]) if argv else 20
    max_turns = int(argv[1]) if len(argv) > 1 else 8000
    specs = argv[2:] or ["random", "rules"]
    seeds = seeds or [5000 + i for i in range(runs)]
    runs = len(seeds)
    print(f"{runs} 回 × 最大 {max_turns} ターン (全頭脳で同じシード、難易度 {difficulty.upper()})" + (f"、B{start}F から標準の勇者で開始" if start else "") + "\n")
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
            results = play_llm(lambda: laya, seeds, max_turns, llm, start, difficulty) if llm else [play(laya, s, max_turns, start, difficulty) for s in seeds]
        elif llm:
            results = play_llm(lambda: make_cpu_brain(spec), seeds, max_turns, llm, start, difficulty)
        else:
            with ProcessPoolExecutor() as pool:
                results = list(pool.map(_cpu_job, [(spec, s, max_turns, start, difficulty) for s in seeds], chunksize=2))
        if start:
            full += f"@B{start}"
        if difficulty != "normal":
            full += f"_{difficulty}"
        report(full, results, time.perf_counter() - t0)
        stalled = [(s, r["stalls"]) for s, r in zip(seeds, results) if r.get("stalls")]
        if stalled:
            print(f"    行き詰まり {len(stalled)} 回: " + " ".join(f"種{s}@B{st[0][0]}F(t{st[0][1]})" for s, st in stalled[:12]) + (" ..." if len(stalled) > 12 else ""), flush=True)
        (DATA / f"results_{full.replace(':', '-').replace('@', '_')}.json").write_text(
            json.dumps({str(s): {k: v for k, v in r.items() if k != "ms"} for s, r in zip(seeds, results)}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
