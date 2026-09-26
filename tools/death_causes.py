"""Replay recordings and describe each death: how the final fight started (HP, level, enemies), what the hero did in it,
hits / misses, resting next to the enemy, armor, unused potions, stairs, and the strategist constraint in force.

    uv run tools/death_causes.py "data/replays/laya-gen25b_50*.json" "data/replays/laya-gen25b+llm_50*.json"
"""
import glob
import json
import sys
from collections import Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from game import Game  # noqa: E402

FIGHT_GAP = 12  # turns without an awake monster in view that end a fight


def analyze(path):
    rec = json.loads(open(path, encoding="utf-8").read())
    g = Game(rec["seed"], None, rec["difficulty"])
    advice = {a["i"]: a for a in rec["advice"]}
    plan, tactic, rest = "free", "free", False
    fight = None  # dict for the current fight
    quiet = 0
    hist = []
    for i, a in enumerate(rec["actions"]):
        g.valid_actions()
        if i in advice:
            plan, tactic, rest = advice[i].get("plan", plan), advice[i].get("tactic", tactic), advice[i].get("rest", rest)
        awake = [m for m in g.visible_monsters() if m["awake"]]
        if awake:
            if fight is None:
                fight = dict(start_i=i, start_turn=g.turn, hp0=g.hp, max_hp=g.max_hp, lv=g.level, depth=g.depth, acts=Counter(), hit=0, miss=0,
                             enemies=Counter(), maxn=0, heal0=g.has_heal(), stairs0=g.stairs_known(), plan=plan, tactic=tactic, hp_min=g.hp, taken=0, consults=0)
            quiet = 0
            fight["acts"][a] += 1
            fight["enemies"].update(m["kind"] for m in awake)
            fight["maxn"] = max(fight["maxn"], len(awake))
            fight["consults"] += i in advice
        else:
            quiet += 1
            if fight is not None and quiet > FIGHT_GAP:
                hist.append(fight)
                fight = None
        n0 = len(g.log)
        hp_before, kills0 = g.hp, g.kills
        adj = any(g.dist(m["x"], m["y"]) <= 1 for m in awake)
        if fight is not None and a == "rest" and adj:
            fight["rest_adj"] = fight.get("rest_adj", 0) + 1
        g.pick_kind = rec.get("picks", {}).get(str(i))
        g.step(a)
        if fight is not None:
            for line in g.log[n0:]:
                if "に攻撃が当たった" in line:
                    fight["hit"] += 1
                elif "に攻撃が外れた" in line:
                    fight["miss"] += 1
            fight["taken"] += max(0, hp_before - g.hp)
            fight["hit"] += g.kills - kills0  # a killing blow logs no "hit" line
            fight["hp_min"] = min(fight["hp_min"], g.hp)
            fight["plan"], fight["tactic"] = plan, tactic
    r = rec.get("result", {})
    if not g.dead:
        return f"{rec['seed']}: 生存 B{g.max_depth} t{g.turn} ({r.get('end')})"
    f = fight or (hist[-1] if hist else None)
    if f is None:
        return f"{rec['seed']}: B{g.depth} {g.cause} 戦闘の記録なし"
    dur = g.turn - f["start_turn"]
    acts = ", ".join(f"{k} {v}" for k, v in f["acts"].most_common(5))
    enemies = ", ".join(k for k, _ in f["enemies"].most_common(4))
    return (f"{rec['seed']}: B{g.depth} Lv{f['lv']} 死因 {g.cause} | 最後の戦闘: 開始 HP {f['hp0']}/{f['max_hp']} ({100 * f['hp0'] // f['max_hp']}%), "
            f"{dur} ターン, 敵 {enemies} (同時最大 {f['maxn']}), 命中 {f['hit']} / 外れ {f['miss']}, 被弾 {f['taken']}, 隣接中の休憩 {f.get('rest_adj', 0)} | 行動: {acts} | "
            f"AC {g.ac()} {g.armor['name']}, 回復薬 {f['heal0']}, 未識別の薬 {sum(g.unknown_potions().values())}, 階段 {'既知' if f['stairs0'] else '不明'} | plan={f['plan']} tactic={f['tactic']}")


for pat in sys.argv[1:]:
    for path in sorted(glob.glob(pat)):
        print(analyze(path))
