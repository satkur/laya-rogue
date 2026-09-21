"""勇者の頭脳。laya-mlx の Snake デモと同じ「ラベル読み」方式。

正直な役割分担:
  - コード (plan + appraise) が盤面を読み、各行動に "Best." / "Fine." / "Bad." の評価文を付ける
  - Laya は評価文を読んで 1 つ選ぶ (1 回の順伝播、生成トークン 0)
  - Laya が "Bad." の行動を選んだときだけ安全装置が介入し、"Bad." 以外で確率最大の行動に差し替える

Laya に盤面を直接読ませる方式 (状況文 + 行動指針、順位を書かない評価文) も試したが、
どちらもランダム行動と同等以下だった。Laya は初見のゲームの戦術を推論できるモデルではない。
"""
import math
import time

INSTRUCTIONS = "Choose the best action for the hero right now."
DANGER_Q = {"type": "noul", "instructions": "Is the hero in immediate danger of dying?"}


def hp_word(g):
    f = g.hp / g.max_hp
    return "critical" if f <= 0.25 else "low" if f <= 0.5 else "wounded" if f < 0.8 else "healthy" if f < 1 else "full"


def threat_word(g, m):
    turns_to_kill = math.ceil(m["hp"] / g.hero_avg())
    turns_to_die = math.ceil(g.hp / g.monster_avg(m))
    r = turns_to_die / turns_to_kill
    return "weak" if r >= 3 else "a fair fight" if r >= 1.5 else "deadly"


def dist_word(d):
    return "adjacent" if d == 1 else "near" if d <= 3 else "far"


def describe(g):
    mons = g.visible_monsters()[:3]
    parts = [f"Hero HP is {hp_word(g)} ({g.hp}/{g.max_hp}).", f"Potions: {g.potions}."]
    parts.append("Enemies: " + "; ".join(f"{m['kind']} {dist_word(g.dist(m['x'], m['y']))}, {threat_word(g, m)}" for m in mons) + "." if mons else "No enemies in view.")
    return " ".join(parts)


def appraise(g, valid):
    """各行動について (評価文, 安全か) を返す。"""
    mons = g.visible_monsters()
    hp = hp_word(g)
    hurt = hp in ("low", "critical")
    deadly = any(threat_word(g, m) == "deadly" for m in mons)
    out = {}
    for a in valid:
        if a == "attack":
            out[a] = (("HP is critical. Fighting on is very dangerous.", False) if hp == "critical"
                      else ("The adjacent enemy is deadly. A losing fight.", False) if deadly and hurt
                      else ("Enemy adjacent and beatable. Strike now.", True))
        elif a == "approach":
            out[a] = (("HP is low. Charging in is reckless.", False) if hurt
                      else ("The enemy is deadly. Charging in is reckless.", False) if deadly
                      else ("Enemy in view and beatable. Engage it.", True))
        elif a == "flee":
            out[a] = ("Danger is real. Escaping is wise.", True) if hurt or deadly else ("No real danger. Running away is pointless.", True)
        elif a == "drink_potion":
            out[a] = (f"HP is {hp}. Healing is urgently needed.", True) if hurt else ("HP is only slightly down. Would waste the potion.", True)
        elif a == "pick_up":
            out[a] = ("Enemies are around. A risky distraction.", False) if mons else ("Item in view and no enemies. An easy reward.", True)
        elif a == "explore":
            out[a] = (("Enemies are around. Exploring now is careless.", False) if mons
                      else ("HP is low. Exploring further is risky.", False) if hurt
                      else ("All clear. A good time to explore.", True))
        elif a == "descend":
            out[a] = (("Enemies are around. A bad time for the stairs.", False) if mons
                      else ("HP is low. Going deeper is risky.", False) if hurt
                      else ("Floor not fully explored yet. Loot may remain.", True) if "explore" in valid
                      else ("Floor cleared and HP is fine. Go deeper.", True))
        elif a == "rest":
            out[a] = (f"No enemies and HP is {hp}. Resting helps.", True) if hp in ("wounded", "low", "critical") else ("HP is nearly full. Resting wastes time.", True)
    return out


def plan(g, valid=None):
    """盤面から最善手を決める (if 文の塊)。これがこのデモの本当のプレイヤー。"""
    valid = valid or g.valid_actions()
    mons = g.visible_monsters()
    hp = hp_word(g)
    hurt = hp in ("low", "critical")
    deadly = any(threat_word(g, m) == "deadly" for m in mons)
    if hurt and "drink_potion" in valid:
        return "drink_potion"
    if mons and (hurt or deadly) and hp != "full" and "attack" not in valid:
        return "flee"
    if "attack" in valid:
        return "flee" if hp == "critical" else "attack"
    if "approach" in valid:
        return "approach"
    if "rest" in valid and hp in ("wounded", "low", "critical"):
        return "rest"
    for a in ("pick_up", "explore", "descend"):
        if a in valid:
            return a
    return valid[0]


class LayaBrain:
    def __init__(self, model="multilingual", guarded=True):
        from laya import Router

        self.model = model
        self.guarded = guarded
        self.name = f"laya/{model}" + ("" if guarded else "/unguarded")
        self.router = Router(preload=[model])

    def decide(self, g):
        valid = g.valid_actions()
        best = plan(g, valid)
        notes = appraise(g, valid)
        criteria = {a: ("Best. " if a == best else "Fine. " if ok else "Bad. ") + text for a, (text, ok) in notes.items()}
        state = describe(g)
        questions = {"danger": DANGER_Q}
        if len(valid) > 1:  # 選択肢が 1 つでは softmax にならないので危険度だけ聞く
            questions["action"] = {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria}
        t0 = time.perf_counter()
        ans = self.router.predict(state, questions, model=self.model)["answers"]
        ms = (time.perf_counter() - t0) * 1000
        probs = ans["action"]["probabilities"] if "action" in ans else {valid[0]: 1.0}
        proposed = max(probs, key=probs.get)
        executed = proposed
        if self.guarded and not notes[proposed][1]:
            safe = [a for a in valid if notes[a][1]]
            executed = max(safe, key=probs.get) if safe else best
        return {"action": executed, "proposed": proposed, "best": best, "intervened": executed != proposed,
                "probs": probs, "criteria": criteria, "danger": ans["danger"]["noul"], "state": state, "ms": ms}


class RandomBrain:
    name = "random"

    def __init__(self, rng):
        self.rng = rng

    def decide(self, g):
        valid = g.valid_actions()
        a = self.rng.choice(valid)
        return {"action": a, "proposed": a, "best": plan(g, valid), "intervened": False, "ms": 0.0}


class RuleBrain:
    name = "rules"

    def decide(self, g):
        a = plan(g)
        return {"action": a, "proposed": a, "best": a, "intervened": False, "ms": 0.0}
