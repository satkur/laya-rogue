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
    "quaff_heal": "Drink a healing potion.",
    "quaff_str": "Drink a strength potion.",
    "eat": "Eat food.",
    "pick_up": "Walk to the nearest item.",
    "equip": "Put on the better weapon or armor you carry.",
    "explore": "Walk toward unexplored area.",
    "descend": "Walk to the stairs and go down.",
    "rest": "Wait a turn.",
}


def hp_word(g):
    f = g.hp / g.max_hp
    return "critical" if f <= 0.25 else "low" if f <= 0.5 else "wounded" if f < 0.8 else "healthy" if f < 1 else "full"


def threat_word(g, m):
    """殴り合ったらどちらが先に倒れるかの見積もり (本家の命中判定とダメージダイスから計算)。
    行動の推奨ではなく、敵の見た目の強さ。特殊攻撃 (錆び・凍結・盗みなど) は含まないので、Laya は名前で覚えるしかない。"""
    turns_to_kill = math.ceil(m["hp"] / max(0.05, g.hero_damage_per_turn(m)))
    turns_to_die = math.ceil(g.hp / max(0.05, g.monster_damage_per_turn(m)))
    r = turns_to_die / turns_to_kill
    return "weak" if r >= 3 else "even" if r >= 1.5 else "deadly"


def dist_word(d):
    return "adjacent" if d == 1 else "near" if d <= 3 else "far"


def count_word(n):
    return "none" if n == 0 else "one" if n == 1 else "several"


def depth_word(d):
    return "shallow" if d <= 4 else "middle" if d <= 9 else "deep" if d <= 14 else "abyss"


def describe(g, valid, order):
    """状況文。数値を避けて語彙を絞ってあるので、同じ状況は同じ文になる (= 経験表のキーになる)。"""
    mons = g.visible_monsters()
    items = g.visible_items()
    parts = [f"Order: {order}.", f"Depth: {depth_word(g.depth)}.", f"HP {hp_word(g)}.", f"Hunger: {g.hunger_word()}.",
             f"Food: {count_word(g.food)}.", f"Healing potions: {count_word(g.has_heal())}."]
    status = [w for w, on in (("confused", g.confused), ("held", g.held_by is not None), ("weakened", g.str < g.max_str)) if on]
    if status:
        parts.append("Status: " + ", ".join(status) + ".")
    if mons:
        seen = ", ".join(f"{m['kind']} {dist_word(g.dist(m['x'], m['y']))} ({threat_word(g, m)}{'' if m['awake'] else ', asleep'})"
                         for m in mons[:3])
        parts.append(f"Enemies: {seen}" + (f" and {len(mons) - 3} more." if len(mons) > 3 else "."))
    else:
        parts.append("Enemies: none.")
    parts.append(f"Items: {items[0]['kind']} {dist_word(g.dist(items[0]['x'], items[0]['y']))}." if items else "Items: none.")
    parts.append("Stairs: " + ("known." if "descend" in valid else "not found."))
    parts.append("Unexplored area: " + ("yes." if "explore" in valid else "no."))
    return " ".join(parts)


def coarse_key(g, valid, order):
    """経験表のキー。状況文 (describe) よりずっと粗い。

    状況文をそのままキーにすると 2 万種類以上に割れ、肝心の戦闘の状況でも経験が 3〜5 件しか溜まらず、
    死亡の減点の振れ幅に埋もれて評価がでたらめになった。表は「勝てそうな敵が隣にいる、体力は低い」くらいの
    粗さで経験を集め、Laya には詳しい状況文を読ませて同じ評価を教える。敵の名前や深さで判断を変えるところまでは、
    この表からは学べない (第 1 段階の割り切り)。
    """
    awake = [m for m in g.visible_monsters() if m["awake"]]
    asleep = [m for m in g.visible_monsters() if not m["awake"]]
    if awake:
        rank = {"weak": 0, "even": 1, "deadly": 2}
        worst = max(awake, key=lambda m: (rank[threat_word(g, m)], -g.dist(m["x"], m["y"])))
        enemy = f"{threat_word(g, worst)}-{dist_word(g.dist(worst['x'], worst['y']))}" + ("+" if len(awake) > 1 else "")
    elif asleep:
        nearest = asleep[0]
        enemy = f"asleep-{threat_word(g, nearest)}-{dist_word(g.dist(nearest['x'], nearest['y']))}"
    else:
        enemy = "none"
    hunger = g.hunger_word()
    flags = "".join(c for c, on in (("H", g.held_by is not None), ("C", g.confused)) if on)
    return "|".join([order, hp_word(g), "starving" if hunger in ("weak", "fainting") else hunger, enemy, flags, ",".join(valid)])


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
        q = self.table.get(coarse_key(g, valid, self.order), {}).get("q")
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
        awake = [m for m in mons if m["awake"]]
        hp = hp_word(g)
        hurt = hp in ("low", "critical")
        deadly = any(threat_word(g, m) == "deadly" for m in awake)
        a = None
        if hurt and "quaff_heal" in valid:
            a = "quaff_heal"
        elif "quaff_str" in valid and not awake:
            a = "quaff_str"
        elif "equip" in valid and not awake:
            a = "equip"
        elif "eat" in valid and g.hunger_word() != "fine":
            a = "eat"
        elif "attack" in valid and any((m["awake"] or "M" in m["flags"]) and g._adjacent(m) for m in mons):
            a = "flee" if hp == "critical" and "flee" in valid and "quaff_heal" not in valid and deadly else "attack"
        elif awake and (hurt or deadly) and "flee" in valid:
            a = "flee"
        elif "approach" in valid and awake:
            a = "approach"
        elif not awake and hp in ("wounded", "low", "critical") and g.hunger_word() == "fine":
            a = "rest"
        if a is None:
            a = next((x for x in ("pick_up", "explore", "descend") if x in valid), "rest")
        return {"action": a, "probs": {a: 1.0}, "state": "", "ms": 0.0}
