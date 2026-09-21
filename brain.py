"""勇者の頭脳。

プログラムがやるのは 2 つだけ:
  - 見えているものを言葉にする (describe)
  - いま実行できる行動を列挙し、それぞれが何をするかを中立に説明する (ACTION_DESC)
どの行動が良いかは一切教えない。安全装置もない。選ぶのは Laya。

人間は方針役。状況文の先頭に命令 (ORDERS) が入り、Laya は同じ状況でも命令によって行動を変える。
命令ごとに「何が嬉しいか」を変えて自己対戦させてあるので (learn.py)、命令の意味は Laya が経験から覚えたもの。

素の Laya はこの形式だとランダム以下なので (NOTES.md)、learn.py の自己対戦で得た経験を
判断ヘッドに学習させた重み (weights/*.pt) を載せて使う。
"""
import math
import random
import time
from pathlib import Path

WEIGHTS = Path(__file__).parent / "weights"
INSTRUCTIONS = "You are the hero of a dungeon crawl. Follow the order and choose the next action."
ORDERS = ["cautious", "aggressive", "loot", "descend"]
DEFAULT_ORDER = "aggressive"
ACTION_DESC = {
    "attack": "Hit the adjacent enemy.",
    "approach": "Move toward the nearest enemy.",
    "flee": "Move away from the enemies.",
    "drink_potion": "Drink a potion to restore HP.",
    "pick_up": "Walk to the nearest item.",
    "explore": "Walk toward unexplored area.",
    "descend": "Walk to the stairs and go down.",
    "rest": "Wait a turn to recover a little HP.",
}


def hp_word(g):
    f = g.hp / g.max_hp
    return "critical" if f <= 0.25 else "low" if f <= 0.5 else "wounded" if f < 0.8 else "healthy" if f < 1 else "full"


def threat_word(g, m):
    """殴り合ったらどちらが先に倒れるかの見積もり。行動の推奨ではなく、敵の見た目の強さ。"""
    turns_to_kill = math.ceil(m["hp"] / g.hero_avg())
    turns_to_die = math.ceil(g.hp / g.monster_avg(m))
    r = turns_to_die / turns_to_kill
    return "weak" if r >= 3 else "even" if r >= 1.5 else "deadly"


def dist_word(d):
    return "adjacent" if d == 1 else "near" if d <= 3 else "far"


def describe(g, valid, order):
    """状況文。数値を避けて語彙を絞ってあるので、同じ状況は同じ文になる (= 経験表のキーになる)。"""
    mons = g.visible_monsters()
    items = g.visible_items()
    parts = [f"Order: {order}.", f"HP {hp_word(g)}.", "Potions: " + ("none" if g.potions == 0 else "one" if g.potions == 1 else "several") + "."]
    if mons:
        seen = ", ".join(f"{m['kind']} {dist_word(g.dist(m['x'], m['y']))} ({threat_word(g, m)})" for m in mons[:3])
        parts.append(f"Enemies: {seen}" + (f" and {len(mons) - 3} more." if len(mons) > 3 else "."))
    else:
        parts.append("Enemies: none.")
    parts.append(f"Items: {items[0]['kind']} {dist_word(g.dist(items[0]['x'], items[0]['y']))}." if items else "Items: none.")
    parts.append("Stairs: " + ("known." if "descend" in valid else "not found."))
    parts.append("Unexplored area: " + ("yes." if "explore" in valid else "no."))
    return " ".join(parts)


def state_key(text, valid):
    return text + " | " + ",".join(valid)


def choose(probs, sharpness, rng):
    """確率に従って行動を引く。sharpness が大きいほど最有力の行動に寄り、None なら常に最有力。

    常に最有力を選ぶと、評価が僅差の 2 状況を行き来する足踏みループから抜けられない
    (「遠くの金貨へ向かう」↔「近くの金貨から離れて探索」など)。少しだけ揺らぐと抜けられる。
    """
    if sharpness is None:
        return max(probs, key=probs.get)
    acts = list(probs)
    return rng.choices(acts, [probs[a] ** sharpness for a in acts])[0]


class LayaBrain:
    """generation=None で素の Laya、名前を渡すと weights/<名前>.pt の学習済みヘッドを載せる。"""

    def __init__(self, generation=None, model="multilingual", sharpness=2.5):
        from laya import Router

        self.order = DEFAULT_ORDER
        self.sharpness = sharpness
        self.rng = random.Random(0)
        self.agent = Router().load(model)
        self.pristine = {k: v.clone() for k, v in self.agent.model.state_dict().items() if not k.startswith("encoder.")}
        self.generation = None
        self.name = "laya/untrained"
        self.load_generation(generation)

    def load_generation(self, generation):
        import torch

        state = self.pristine if generation is None else torch.load(WEIGHTS / f"{generation}.pt", map_location="cpu")
        self.agent.model.load_state_dict(state, strict=False)
        self.agent.temperature_by_options = {} if generation else self.agent.cfg.get("temperature_by_options", {})
        self.agent.temperature = [1.0, 1.0, 1.0] if generation else self.agent.cfg.get("temperature", [1.0, 1.0, 1.0])
        self.generation = generation
        self.name = f"laya/{generation or 'untrained'}"

    def decide(self, g):
        valid = g.valid_actions()
        state = describe(g, valid, self.order)
        if len(valid) == 1:  # 選びようがないときは推論しない
            return {"action": valid[0], "probs": {valid[0]: 1.0}, "state": state, "ms": 0.0}
        q = {"action": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {a: ACTION_DESC[a] for a in valid}}}
        t0 = time.perf_counter()
        probs = self.agent.predict(state, q)["answers"]["action"]["probabilities"]
        ms = (time.perf_counter() - t0) * 1000
        return {"action": choose(probs, self.sharpness, self.rng), "probs": probs, "state": state, "ms": ms}


class TableBrain:
    """自己対戦で作った経験表をそのまま引く頭脳。Laya がこの表をどこまで写し取れたかの物差し。"""

    name = "table"

    def __init__(self, table, sharpness=2.5, rng=None):
        self.table = table
        self.order = DEFAULT_ORDER
        self.sharpness = sharpness
        self.rng = rng or random.Random(0)

    def decide(self, g):
        valid = g.valid_actions()
        state = describe(g, valid, self.order)
        q = self.table.get(state_key(state, valid))
        if q is None:
            a, probs = self.rng.choice(valid), {x: 1 / len(valid) for x in valid}
        else:  # train.py が Laya に教えるのと同じ softmax(平均リターン) を確率として使う
            top = max(q[x][0] for x in valid)
            z = {x: math.exp(q[x][0] - top) for x in valid}
            probs = {x: v / sum(z.values()) for x, v in z.items()}
            a = choose(probs, self.sharpness, self.rng)
        return {"action": a, "probs": probs, "state": state, "ms": 0.0, "miss": q is None}


class RandomBrain:
    name = "random"

    def __init__(self, rng):
        self.rng = rng

    def decide(self, g):
        valid = g.valid_actions()
        a = self.rng.choice(valid)
        return {"action": a, "probs": {a: 1.0}, "state": "", "ms": 0.0}


class RuleBrain:
    """人間が書いた if 文。学習には一切使わない、成績の物差し。"""

    name = "rules"

    def decide(self, g):
        valid = g.valid_actions()
        mons = g.visible_monsters()
        hp = hp_word(g)
        hurt = hp in ("low", "critical")
        deadly = any(threat_word(g, m) == "deadly" for m in mons)
        if hurt and "drink_potion" in valid:
            a = "drink_potion"
        elif mons and (hurt or deadly) and hp != "full" and "attack" not in valid:
            a = "flee"
        elif "attack" in valid:
            a = "flee" if hp == "critical" else "attack"
        elif "approach" in valid:
            a = "approach"
        elif "rest" in valid and hp in ("wounded", "low", "critical"):
            a = "rest"
        else:
            a = next((x for x in ("pick_up", "explore", "descend") if x in valid), valid[0])
        return {"action": a, "probs": {a: 1.0}, "state": "", "ms": 0.0}
