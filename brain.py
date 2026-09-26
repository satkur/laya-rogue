"""勇者の頭脳。

プログラムがやるのは 2 つだけ:
  - 見えているものを言葉にする (describe)
  - いま実行できる行動を列挙し、それぞれが何をするかを中立に説明する (ACTION_DESC)
どの行動が良いかは一切教えない。安全装置もない。選ぶのは Laya。

人間が命令 (慎重に / 攻めろ / 漁れ / 降りろ) を出す仕組みは、効きが弱かったのでいったん外してある (NOTES.md 4〜6 章)。
腕前が上がってから戻す。

素の Laya はこの形式だとランダム以下なので (NOTES.md)、learn.py の自己対戦で得た経験を
判断ヘッドに学習させた重み (weights/*.pt) を載せて使う。
"""
import math
import random
import time
from pathlib import Path

from game import active

WEIGHTS = Path(__file__).parent / "weights"
TAU = 0.3  # 経験表の評価を確率に直すときの温度。行動 1 つぶんの評価差は 0.3〜1 点と小さいので、1.0 だとほぼ一様になってしまう
INSTRUCTIONS = "You are the hero of a dungeon crawl. Choose the next action."
ACTION_DESC = {
    "attack": "Hit the adjacent enemy.",
    "throw": "Throw a missile at the enemy in line.",
    "approach": "Move toward the nearest enemy.",
    "flee": "Move away from the enemies.",
    "quaff_heal": "Drink a healing potion.",
    "quaff_str": "Drink a strength potion.",
    "read_map": "Read the scroll of magic mapping.",
    "read_teleport": "Read the scroll of teleportation.",
    "quaff_unknown": "Drink an unidentified potion; you learn what it was.",
    "read_unknown": "Read an unidentified scroll; you learn what it was.",
    "read_identify": "Read the scroll of identify to learn what one unidentified potion, scroll, wand or ring you carry is.",
    "read_remove_curse": "Read the scroll of remove curse: the cursed ring, armor or weapon you wear can be taken off again.",
    "put_on_ring": "Put on a ring (a known good one first, otherwise an unidentified one); it works while worn, costs food, and may be cursed.",
    "read_confuse": "Read the scroll of monster confusion: the next enemy you hit in melee becomes confused and wanders.",
    "read_hold": "Read the scroll of hold monster: the awake enemies within two steps freeze until you hit them.",
    "drop_scare": "Drop the scroll of scare monster at your feet: while you stand on it, no monster can reach you in melee.",
    "read_food": "Read the scroll of food detection: you learn where the food on this level lies.",
    "quaff_haste": "Drink the potion of haste self: for a few turns you act twice per turn.",
    "quaff_raise": "Drink the potion of raise level: you gain one experience level.",
    "quaff_see_invisible": "Drink the potion of see invisible: the unseen attacker becomes visible for a long time.",
    "quaff_detect_monsters": "Drink the potion of monster detection: for a while you sense every monster on this level.",
    "quaff_detect_magic": "Drink the potion of magic detection: you learn where the magic items on this level lie.",
    "remove_ring": "Take off a ring you wear (a useless one first, otherwise an unidentified one); fails if it is cursed.",
    "read_enchant_armor": "Read the scroll of enchant armor: your armor gets one point better (and any curse on it is lifted).",
    "read_enchant_weapon": "Read the scroll of enchant weapon: your weapon gets one point better (and any curse on it is lifted).",
    "read_protect": "Read the scroll of protect armor: your armor can no longer rust.",
    "zap_bolt": "Zap a wand of lightning, fire or cold at the enemy in line: a bolt that hurts it badly unless it resists; it can bounce back at you.",
    "zap_missile": "Zap the wand of magic missile at the enemy in line: a small but almost certain hit.",
    "zap_slow": "Zap the wand of slow monster at the enemy in line: it then moves only every other turn.",
    "zap_away": "Zap the wand of teleport away at the enemy in line: it is sent somewhere else on this level.",
    "zap_polymorph": "Zap the wand of polymorph at the enemy in line: it turns into a random other monster, weaker or stronger.",
    "zap_drain": "Zap the wand of drain life: you lose half your HP and the monsters in this room share that damage.",
    "zap_cancel": "Zap the wand of cancellation at the enemy in line: it loses its special power (rust, freeze, poison, drain, flame...).",
    "zap_light": "Zap the wand of light: this dark room becomes lit.",
    "zap_unknown": "Zap an unidentified wand at the enemy in line; you learn what it was.",
    "wield_bow": "Put away your weapon and wield the bow: arrows then hit hard, but you are nearly helpless in melee until you wield the weapon again.",
    "wield_melee": "Put away the bow and wield your melee weapon again.",
    "eat": "Eat food.",
    "pick_up": "Walk to the item (a better weapon or armor first, otherwise the nearest one).",
    "equip": "Put on the better weapon or armor you carry.",
    "explore": "Walk toward unexplored area.",
    "search": "Search the dead ends and walls for a hidden door.",
    "descend": "Walk to the stairs and go down.",
    "ascend": "Walk to the stairs and go up (you carry the Amulet; reaching the surface wins).",
    "drop": "Drop a useless item to make room in your pack.",
    "rest": "Wait a turn.",
}


def hp_word(g):
    f = g.hp / g.max_hp
    return "critical" if f <= 0.25 else "low" if f <= 0.5 else "wounded" if f < 0.8 else "healthy" if f < 1 else "full"


def threat_word(g, m):
    """殴り合ったらどちらが先に倒れるかの見積もり (本家の命中判定とダメージダイスから計算)。
    行動の推奨ではなく、敵の見た目の強さ。特殊攻撃 (錆び・凍結・盗みなど) は含まないので、Laya は名前で覚えるしかない。
    幻覚中は相手が何か分からないので unknown (嘘は書かない)。"""
    if g.hallucinating:
        return "unknown"
    turns_to_kill = math.ceil(m["hp"] / max(0.05, g.hero_damage_per_turn(m)))
    turns_to_die = math.ceil(g.hp / max(0.05, g.monster_damage_per_turn(m)))
    r = turns_to_die / turns_to_kill
    return "weak" if r >= 3 else "even" if r >= 1.5 else "deadly"


def dist_word(d):
    return "adjacent" if d == 1 else "near" if d <= 3 else "far"


def count_word(n):
    return "none" if n == 0 else "one" if n == 1 else "several"


def potion_words(g):
    """回復・力以外の、正体の分かった薬。"""
    kinds = [w for w, name in (("haste", "haste self"), ("raise-level", "raise level"), ("see-invisible", "see invisible"),
                               ("detect-monsters", "monster detection"), ("detect-magic", "magic detection")) if g.has_potion(name)]
    return ", ".join(kinds) if kinds else "none"


def monster_name(g, m):
    if g.hallucinating:
        return "something"
    return m["kind"] if g.can_see(m) and not (g.blind or ("I" in m["flags"] and not g.can_see_invisible())) else "something unseen"


def scroll_words(g):
    kinds = [w for w, name in (("map", "magic mapping"), ("teleport", "teleportation"), ("identify", "identify"), ("remove-curse", "remove curse"),
                               ("confuse", "monster confusion"), ("hold", "hold monster"), ("scare", "scare monster"), ("food-detection", "food detection"),
                               ("enchant-armor", "enchant armor"), ("enchant-weapon", "enchant weapon"), ("protect-armor", "protect armor"))
             if g.scrolls.get(name) and name in g.known]
    return ", ".join(kinds) if kinds else "none"


def ring_words(g):
    """着けている指輪。正体が分かっていれば種類、まだなら unknown (識別の巻物でしか分からない)。"""
    if not g.worn:
        return "none"
    names = [r["name"].replace(" ", "-") if r["name"] in g.known else "unknown" for r in g.worn]
    return ", ".join(w + (" (cursed)" if g.ring_known_cursed(r) else "") for w, r in zip(names, g.worn))


WAND_WORDS = (("bolt", "lightning"), ("bolt", "fire"), ("bolt", "cold"), ("missile", "magic missile"), ("slow", "slow monster"),
              ("teleport-away", "teleport away"), ("polymorph", "polymorph"), ("drain-life", "drain life"), ("cancellation", "cancellation"), ("light", "light"))


def wand_words(g):
    """正体が分かっていて使い道のある杖。"""
    kinds = []
    for w, name in WAND_WORDS:
        if g.known_sticks().get(name) and w not in kinds:
            kinds.append(w)
    return ", ".join(kinds) if kinds else "none"


def depth_word(d):
    return "shallow" if d <= 4 else "middle" if d <= 9 else "deep" if d <= 14 else "abyss"


def pace_word(g):
    """この深さに対して勇者のレベルが足りているか。「降りる前に鍛えるか」を状況文と経験表のキーに載せる (アドバイザーの提案)。
    目安は本家の経験則 (10 階で Lv8 なら余裕、Lv5 でぎりぎり、Lv4 以下は無理)。"""
    r = g.level / max(1, g.depth)
    return "ahead" if r >= 0.8 else "even" if r >= 0.5 else "behind"


# 殴り合いの見積もり (threat_word) に出てこない厄介さ。経験表のキーに入れて、種別ごとに対処を学べるようにする
SPECIAL = {"L": "steal", "N": "steal",                                # 金貨・持ち物を盗んで消える
           "A": "weaken", "R": "weaken", "W": "weaken", "V": "weaken",  # 鎧の錆び・毒・レベル吸収・最大 HP 吸収
           "F": "disable", "I": "disable", "M": "disable"}            # 拘束・凍結・混乱


def special_word(m):
    return SPECIAL.get(m["ch"], "plain")


ITEM_WORD = {"stick": "wand", "missile": "missiles"}


def item_word(g, it):
    """状況文の品物: 種類と距離。武器・防具は着ている物より良いか悪いかを添える (拾う価値を状況で区別できるように)。"""
    if g.hallucinating:
        return f"something {dist_word(g.dist(it['x'], it['y']))}"
    kind = ITEM_WORD.get(it["kind"], it["kind"])
    if it["kind"] in ("weapon", "armor"):
        kind = ("better " if g.gear_gain(it) > 0 else "worse ") + kind
    return f"{kind} {dist_word(g.dist(it['x'], it['y']))}"


def describe(g, valid):
    """状況文。数値を避けて語彙を絞ってある。品物は近い順に 3 つまで。"""
    mons = g.visible_monsters()
    items = g.visible_loot()
    parts = [f"Depth: {depth_word(g.depth)}.", f"Experience for this depth: {pace_word(g)}.", f"HP {hp_word(g)}.", f"Hunger: {g.hunger_word()}.",
             f"Food: {count_word(g.food)}.", f"Healing potions: {count_word(g.has_heal())}.",
             f"Missiles: {count_word(sum(g.missiles.values()))}.", f"Scrolls: {scroll_words(g)}.", f"Potions: {potion_words(g)}.",
             f"Unidentified potions: {count_word(sum(g.unknown_potions().values()))}.",
             f"Unidentified scrolls: {count_word(sum(g.unknown_scrolls().values()))}.",
             f"Wands: {wand_words(g)}.", f"Unidentified wands: {count_word(len(g.unknown_sticks()))}.",
             f"Rings worn: {ring_words(g)}.", f"Unidentified rings carried: {count_word(sum(1 for r in g.rings if r['name'] not in g.known))}."]
    status = [w for w, on in (("confused", g.confused), ("held", g.held_by is not None), ("weakened", g.base_str() < g.max_str),
                              ("cursed", g.cursed_worn()), ("hands glowing", g.glowing), ("hasted", g.hasted), ("levitating", g.levitating),
                              ("blind", g.blind), ("hallucinating", g.hallucinating), ("wielding the bow", g.wielding_bow())) if on]
    if status:
        parts.append("Status: " + ", ".join(status) + ".")
    if g.on_scare():
        parts.append("Standing on: scare monster scroll (monsters cannot reach you here).")
    if mons:
        seen = ", ".join(f"{monster_name(g, m)} {dist_word(g.dist(m['x'], m['y']))} ({threat_word(g, m)}{'' if m['awake'] else ', asleep'}"
                         f"{', held' if m.get('held') else ''}{', confused' if m.get('confused') else ''})"
                         for m in mons[:3])
        parts.append(f"Enemies: {seen}" + (f" and {len(mons) - 3} more." if len(mons) > 3 else "."))
    else:
        parts.append("Enemies: none.")
    if g.detecting():
        sensed = g.sensed_monsters()
        parts.append(f"Monsters sensed elsewhere on this level: {count_word(len(sensed))}"
                     + (f" (nearest: {monster_name(g, sensed[0])}, {dist_word(g.dist(sensed[0]['x'], sensed[0]['y']))})." if sensed else "."))
    parts.append(f"Items: {', '.join(item_word(g, i) for i in items[:3])}." if items else "Items: none.")
    parts.append("Stairs: " + ("known." if g.stairs_known() else "not found."))  # 浮遊中や道が繋がっていないときも「分かっている」は本当
    if g.rules.amulet:
        parts.append("Amulet: " + ("carried (climb back to the surface)." if g.amulet else "not found (it lies on level 26 or deeper)."))
    if g.pack_full():
        parts.append("Pack: full.")
    parts.append("Unexplored area: " + ("yes." if "explore" in valid else "no."))
    return " ".join(parts)


def coarse_key(g, valid):
    """経験表のキー。状況文 (describe) よりずっと粗い。

    状況文をそのままキーにすると 2 万種類以上に割れ、肝心の戦闘の状況でも経験が 3〜5 件しか溜まらず、
    死亡の減点の振れ幅に埋もれて評価がでたらめになった。表は「勝てそうな敵が隣にいる、体力は低い」くらいの
    粗さで経験を集め、Laya には詳しい状況文を読ませて同じ評価を教える。敵は名前ではなく
    「殴り合いの強さ × 特殊攻撃の種別 (盗む / 弱らせる / 動きを封じる / なし)」まで。深さで判断を変えるところは、この表からは学べない。
    """
    awake = [m for m in g.visible_monsters() if active(m)]
    held = [m for m in g.visible_monsters() if m["awake"] and m.get("held")]
    asleep = [m for m in g.visible_monsters() if not m["awake"]]
    if awake:
        rank = {"weak": 0, "even": 1, "deadly": 2, "unknown": 1}
        worst = max(awake, key=lambda m: (rank[threat_word(g, m)], -g.dist(m["x"], m["y"])))
        enemy = f"{threat_word(g, worst)}-{special_word(worst)}-{dist_word(g.dist(worst['x'], worst['y']))}" + ("+" if len(awake) > 1 else "")
    elif held:  # 拘束した敵は殴るまで動かない
        nearest = held[0]
        enemy = f"held-{threat_word(g, nearest)}-{special_word(nearest)}-{dist_word(g.dist(nearest['x'], nearest['y']))}"
    elif asleep:
        nearest = asleep[0]
        enemy = f"asleep-{threat_word(g, nearest)}-{special_word(nearest)}-{dist_word(g.dist(nearest['x'], nearest['y']))}"
    else:
        enemy = "none"
    hunger = g.hunger_word()
    flags = "".join(c for c, on in (("H", g.held_by is not None), ("C", g.confused), ("G", bool(g.visible_upgrades())),
                                     ("K", bool(g.cursed_worn())), ("S", g.on_scare()), ("B", g.blind), ("X", g.hallucinating),
                                     ("F", g.hasted), ("L", g.levitating), ("W", g.wielding_bow()), ("A", g.amulet)) if on)  # G: 良い装備が見えている、K: 呪われた物、S: 恐怖の巻物の上、B: 盲目、X: 幻覚、F: 加速、L: 浮遊、W: 弓、A: 魔除け
    return "|".join([hp_word(g), "starving" if hunger in ("weak", "fainting") else hunger, enemy, flags, pace_word(g), ",".join(valid)])


def choose(probs, sharpness, rng):
    """確率に従って行動を引く。sharpness が大きいほど最有力の行動に寄り、None なら常に最有力。

    常に最有力を選ぶと、評価が僅差の 2 状況を行き来する足踏みループから抜けられない
    (「遠くの金貨へ向かう」↔「近くの金貨から離れて探索」など)。少しだけ揺らぐと抜けられる。
    """
    if sharpness is None:
        return max(probs, key=probs.get)
    acts = list(probs)
    return rng.choices(acts, [probs[a] ** sharpness for a in acts])[0]


def redraw(d, allowed, sharpness, rng):
    """Keep the pilot's decision if allowed, else draw again from its own distribution over the allowed actions.
    (Hiding options from the pilot instead shows it option sets it never trained on; NOTES 15.)"""
    if d["action"] in allowed:
        return d
    probs = {a: p for a, p in d["probs"].items() if a in allowed and p > 0}
    if probs:
        d["action"] = choose(probs, sharpness, rng)
    return d


def safety_valves(g, valid):
    """Actions the pilot may take this turn (NOTES 15, 2026-09-26): no resting next to an awake enemy (unless standing on a
    scare monster scroll). Applied by redraw, so the pilot still sees every option. Anything more is a standing order (strategist.ORDERS)."""
    allowed = list(valid)
    if "rest" in allowed and "attack" in valid and not g.on_scare():  # "attack" is offered only when an awake enemy is in reach
        allowed.remove("rest")
    return allowed or list(valid)


class LayaBrain:
    """generation=None で素の Laya、名前を渡すと weights/<名前>.pt の学習済みヘッドを載せる。"""

    def __init__(self, generation=None, model="multilingual", sharpness=2.5):
        from laya import Router

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
        state = describe(g, valid)
        if len(valid) == 1:  # 選びようがないときは推論しない
            return {"action": valid[0], "probs": {valid[0]: 1.0}, "state": state, "ms": 0.0}
        q = {"action": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {a: ACTION_DESC[a] for a in valid}}}
        t0 = time.perf_counter()
        probs = self.agent.predict(state, q)["answers"]["action"]["probabilities"]
        ms = (time.perf_counter() - t0) * 1000
        rng = getattr(g, "action_rng", self.rng)
        d = {"action": choose(probs, self.sharpness, rng), "probs": probs, "state": state, "ms": ms}
        return redraw(d, safety_valves(g, valid), self.sharpness, rng)


class TableBrain:
    """自己対戦で作った経験表をそのまま引く頭脳。Laya がこの表をどこまで写し取れたかの物差し。"""

    name = "table"

    def __init__(self, table, sharpness=2.5, rng=None):
        self.table = table
        self.sharpness = sharpness
        self.rng = rng or random.Random(0)

    def decide(self, g):
        valid = g.valid_actions()
        state = describe(g, valid)
        q = self.table.get(coarse_key(g, valid), {}).get("q")
        if q is None:
            a, probs = self.rng.choice(valid), {x: 1 / len(valid) for x in valid}
        else:  # train.py が Laya に教えるのと同じ softmax(平均リターン) を確率として使う
            top = max(q[x][0] for x in valid)
            z = {x: math.exp((q[x][0] - top) / TAU) for x in valid}
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


CROWD, CROWD_TURNS = 3, 10   # 40 ターンだと逃げすぎて 500 シードで 0.3 階損した


def crowd_escape(brain, g, awake, valid):
    """起きた敵が CROWD 体以上見えたら (宝物部屋の入り口)、その階では CROWD_TURNS ターンのあいだ階段へ向かう (なければ逃げる)。
    見えた瞬間だけ反応すると「1 歩離れて見えなくなる → 探索で戻る」の往復になるので、覚えておく。動かないハエトリグサは数えない。"""
    movers = [m for m in awake if m["ch"] != "F"]
    alert = getattr(brain, "crowd_alert", None)
    if len(movers) >= CROWD and not any(g._adjacent(m) for m in movers):
        brain.crowd_alert = alert = (id(g), g.depth, g.turn + CROWD_TURNS)
    if alert is None or alert[0] != id(g) or alert[1] != g.depth or g.turn >= alert[2]:
        return None
    return "descend" if "descend" in valid else "flee" if "flee" in valid else None


class RuleBrain:
    """人間が書いた if 文。学習には一切使わない、成績の物差し。"""

    name = "rules"

    def decide(self, g):
        valid = g.valid_actions()
        mons = g.visible_monsters()
        awake = [m for m in mons if active(m)]
        hp = hp_word(g)
        hurt = hp in ("low", "critical")
        zt = g._zap_target()
        zap_awake = zt is not None and zt[0]["awake"]  # 杖は起きた標的にだけ (眠った敵にも撃てるが、物差しは使わない)
        deadly = any(threat_word(g, m) == "deadly" for m in awake)
        a = None
        if hurt and "quaff_heal" in valid:
            a = "quaff_heal"
        elif hp == "critical" and deadly and "read_teleport" in valid:
            a = "read_teleport"
        elif "quaff_see_invisible" in valid:
            a = "quaff_see_invisible"
        elif g.blind and "quaff_heal" in valid:
            a = "quaff_heal"
        elif deadly and "quaff_haste" in valid:
            a = "quaff_haste"
        elif deadly and "read_hold" in valid:
            a = "read_hold"
        elif deadly and "read_confuse" in valid:
            a = "read_confuse"
        elif deadly and zap_awake and "zap_away" in valid:
            a = "zap_away"
        elif deadly and zap_awake and "zap_slow" in valid:
            a = "zap_slow"
        elif deadly and zap_awake and "zap_cancel" in valid and any(m["ch"] in SPECIAL or m["ch"] == "D" for m in awake):
            a = "zap_cancel"
        elif (deadly or hurt) and zap_awake and "zap_bolt" in valid:
            a = "zap_bolt"
        elif (deadly or hurt) and zap_awake and "zap_missile" in valid:
            a = "zap_missile"
        elif deadly and zap_awake and "zap_polymorph" in valid:
            a = "zap_polymorph"
        elif deadly and zap_awake and "zap_unknown" in valid:
            a = "zap_unknown"
        elif awake and "wield_melee" in valid and any(g._adjacent(m) for m in awake):
            a = "wield_melee"
        elif "wield_bow" in valid and not any(g._adjacent(m) for m in awake) and g.missiles.get("arrow", 0) >= 3 and g._throw_target()[1] >= 3:
            a = "wield_bow"  # 距離 2 では構えた次のターンに隣に来て 1 本も射れない
        elif "wield_melee" in valid and not awake and "throw" not in valid:  # 起きた敵が消えたら戻す。拘束した敵が直線上にいるあいだは構えたまま射る (戻す↔構えるの往復を防ぐ)
            a = "wield_melee"
        elif "quaff_str" in valid and not awake:
            a = "quaff_str"
        elif "equip" in valid and not awake:
            a = "equip"
        elif not awake and any(x in valid for x in ("read_enchant_armor", "read_enchant_weapon", "read_protect")):
            a = next(x for x in ("read_enchant_armor", "read_enchant_weapon", "read_protect") if x in valid)
        elif "eat" in valid and g.hunger_word() != "fine":
            a = "eat"
        elif g.hunger_word() != "fine" and not g.food and "read_food" in valid:
            a = "read_food"
        elif g.hunger_word() != "fine" and not g.food and "remove_ring" in valid:  # 食料がないのに指輪で空腹が進む
            a = "remove_ring"
        elif g.hunger_word() != "fine" and not g.food and not awake and "descend" in valid:  # 食料がないなら階を探し尽くすより次の階 (餓死 27/500)
            a = "descend"
        elif not mons and "read_remove_curse" in valid:
            a = "read_remove_curse"
        elif not mons and "remove_ring" in valid and g.ring_useless(g._ring_to_remove()):
            a = "remove_ring"
        elif not mons and "put_on_ring" in valid and (g.hunger_word() == "fine" or g.food):
            a = "put_on_ring"
        elif not mons and "quaff_raise" in valid:
            a = "quaff_raise"
        elif not mons and g.turn - g.floor_turn < 3 and "quaff_detect_monsters" in valid:  # 新しい階に着いたら
            a = "quaff_detect_monsters"
        elif not mons and g.turn - g.floor_turn < 3 and "quaff_detect_magic" in valid:
            a = "quaff_detect_magic"
        elif not mons and "read_identify" in valid:
            a = "read_identify"
        elif not mons and hp != "critical" and "quaff_unknown" in valid:
            a = "quaff_unknown"
        elif not mons and "read_unknown" in valid:
            a = "read_unknown"
        elif (crowd := crowd_escape(self, g, awake, valid)):  # 起きた敵が 3 体以上 (宝物部屋) なら、しばらく階段へ・逃げる
            a = crowd
        elif "attack" in valid and any((m["awake"] or "M" in m["flags"]) and g._adjacent(m) for m in mons):
            a = "flee" if hp == "critical" and "flee" in valid and "quaff_heal" not in valid and deadly else "attack"
        elif "throw" in valid:
            a = "throw"
        elif awake and (hurt or deadly) and "flee" in valid:
            a = "flee"
        elif "approach" in valid and awake:
            a = "approach"
        elif "read_map" in valid and "explore" not in valid:
            a = "read_map"
        elif not awake and hp in ("wounded", "low", "critical") and g.hunger_word() == "fine":
            a = "rest"
        if a is None:
            a = next((x for x in (("drop", "ascend") if g.amulet else ("drop",)) + ("pick_up", "explore", "descend", "search") if x in valid), "rest")
        return {"action": a, "probs": {a: 1.0}, "state": "", "ms": 0.0}


class DiverBrain:
    """人間が書いた if 文その 2。階段を見つけたらすぐ降り、こまめに休み、眠っている敵も倒す。いまのところ最良の物差し。"""

    name = "diver"

    def decide(self, g):
        valid = g.valid_actions()
        mons = g.visible_monsters()
        awake = [m for m in mons if active(m)]
        hp = hp_word(g)
        hurt = hp in ("low", "critical")
        zt = g._zap_target()
        zap_awake = zt is not None and zt[0]["awake"]  # 杖は起きた標的にだけ (眠った敵にも撃てるが、物差しは使わない)
        if hurt and "quaff_heal" in valid:
            a = "quaff_heal"
        elif hp == "critical" and "read_teleport" in valid and any(threat_word(g, m) == "deadly" for m in awake):
            a = "read_teleport"
        elif "quaff_see_invisible" in valid:
            a = "quaff_see_invisible"
        elif g.blind and "quaff_heal" in valid:
            a = "quaff_heal"
        elif any(threat_word(g, m) == "deadly" for m in awake) and "quaff_haste" in valid:
            a = "quaff_haste"
        elif any(threat_word(g, m) == "deadly" for m in awake) and "read_hold" in valid:
            a = "read_hold"
        elif any(threat_word(g, m) == "deadly" for m in awake) and "read_confuse" in valid:
            a = "read_confuse"
        elif any(threat_word(g, m) == "deadly" for m in awake) and zap_awake and "zap_away" in valid:
            a = "zap_away"
        elif any(threat_word(g, m) == "deadly" for m in awake) and zap_awake and "zap_slow" in valid:
            a = "zap_slow"
        elif any(threat_word(g, m) == "deadly" for m in awake) and zap_awake and "zap_cancel" in valid and any(m["ch"] in SPECIAL or m["ch"] == "D" for m in awake):
            a = "zap_cancel"
        elif (hurt or any(threat_word(g, m) == "deadly" for m in awake)) and zap_awake and "zap_bolt" in valid:
            a = "zap_bolt"
        elif (hurt or any(threat_word(g, m) == "deadly" for m in awake)) and zap_awake and "zap_missile" in valid:
            a = "zap_missile"
        elif any(threat_word(g, m) == "deadly" for m in awake) and zap_awake and "zap_polymorph" in valid:
            a = "zap_polymorph"
        elif any(threat_word(g, m) == "deadly" for m in awake) and zap_awake and "zap_unknown" in valid:
            a = "zap_unknown"
        elif awake and "wield_melee" in valid and any(g._adjacent(m) for m in awake):
            a = "wield_melee"
        elif "wield_bow" in valid and not any(g._adjacent(m) for m in awake) and g.missiles.get("arrow", 0) >= 3 and g._throw_target()[1] >= 3:
            a = "wield_bow"  # 距離 2 では構えた次のターンに隣に来て 1 本も射れない
        elif "wield_melee" in valid and not awake and "throw" not in valid:  # 起きた敵が消えたら戻す。拘束した敵が直線上にいるあいだは構えたまま射る (戻す↔構えるの往復を防ぐ)
            a = "wield_melee"
        elif not awake and any(x in valid for x in ("read_enchant_armor", "read_enchant_weapon", "read_protect")):
            a = next(x for x in ("read_enchant_armor", "read_enchant_weapon", "read_protect") if x in valid)
        elif "eat" in valid and g.hunger_word() != "fine":
            a = "eat"
        elif g.hunger_word() != "fine" and not g.food and "read_food" in valid:
            a = "read_food"
        elif g.hunger_word() != "fine" and not g.food and "remove_ring" in valid:  # 食料がないのに指輪で空腹が進む
            a = "remove_ring"
        elif not awake and "quaff_str" in valid:
            a = "quaff_str"
        elif not awake and "equip" in valid:
            a = "equip"
        elif not awake and "read_map" in valid:
            a = "read_map"
        elif not mons and "read_remove_curse" in valid:
            a = "read_remove_curse"
        elif not mons and "remove_ring" in valid and g.ring_useless(g._ring_to_remove()):
            a = "remove_ring"
        elif not mons and "put_on_ring" in valid and (g.hunger_word() == "fine" or g.food):
            a = "put_on_ring"
        elif not mons and "quaff_raise" in valid:
            a = "quaff_raise"
        elif not mons and g.turn - g.floor_turn < 3 and "quaff_detect_monsters" in valid:  # 新しい階に着いたら
            a = "quaff_detect_monsters"
        elif not mons and g.turn - g.floor_turn < 3 and "quaff_detect_magic" in valid:
            a = "quaff_detect_magic"
        elif not mons and "read_identify" in valid:
            a = "read_identify"
        elif not mons and hp != "critical" and "quaff_unknown" in valid:
            a = "quaff_unknown"
        elif not mons and "read_unknown" in valid:  # 眠った敵が見えているときは読まない (怪物寄せで起こす)
            a = "read_unknown"
        elif (crowd := crowd_escape(self, g, awake, valid)):  # 起きた敵が 3 体以上 (宝物部屋) なら、しばらく階段へ・逃げる
            a = crowd
        elif "attack" in valid:
            a = "attack"
        elif "throw" in valid:
            a = "throw"
        elif awake and "descend" in valid and not hurt:
            a = "descend"
        elif awake and "approach" in valid:
            a = "approach"
        elif not awake and hp in ("wounded", "low", "critical") and g.hunger_word() == "fine":
            a = "rest"
        else:
            a = next((x for x in (("drop", "ascend") if g.amulet else ("drop",)) + ("pick_up", "descend", "explore", "search") if x in valid), "rest")
        return {"action": a, "probs": {a: 1.0}, "state": "", "ms": 0.0}
