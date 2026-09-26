"""方針役。全体方針を決める LLM を `claude -p` で呼び、Laya の選択肢を絞る (任意機能)。

役割分担:
  - 一手一手を選ぶのは Laya (13ms)。方針役は 1 回 数秒かかるので、節目と危ない場面と定期の見回りでだけ呼ぶ
  - 方針役は Laya に話しかけない。決めた方針に合わない行動を選択肢から外すだけ (例: 「この階は探索し切る」の間は「階段へ」を外す)。
    なので Laya を方針つきで学習し直す必要がない
  - 将来はこの役をプレイヤーに渡すか、プレイヤーの補助として残す

呼ぶ場面:
  floor    新しい階に着いた (深さに対して経験が behind / even のときだけ。ahead なら呼ばない。2026-09-26)
  hp       体力が low / critical に落ちた
  hunger   空腹になった
  danger   手強い敵 (互角以上、または特殊攻撃持ち) が起きて視界に入った
  stuck    同じ場所を行き来して進んでいない
  periodic 最後に呼んでから PERIOD ターン経った (制約が効いているか、体力・空腹に懸念があるときだけ)

方針役の手段は plan (探索し切る / 階段を見つけ次第降りる / 任せる)、rest (9 割まで休む)、tactic (戦え / 逃げろ / 階段で離脱 / 任せる)、
heal_now (次の手で回復薬)。指示文には実測の事実 (死因の内訳、各手段の効き) を引き継ぎ資料として書いてあるが、
台本の推奨は渡さない。判断は毎回 LLM が自分でする。API の失敗が 3 回続いたら COOLDOWN ターンは呼ばずに Laya だけで進み、そのあと再開する。

各自の Claude Code ログイン (サブスクリプション) で動く。利用枠は対話利用と共有なので、1 ゲームと 1 プロセスの呼び出し回数に
上限を置き、超えたら自動で止まる (止まったあとは Laya が制限なしで動く)。ANTHROPIC_API_KEY が環境にあると従量課金に
なるので、その場合は呼ばない。
"""
import json
import math
import os
import random
import subprocess
import time

import rogue_data as D
from brain import SPECIAL, dist_word, hp_word, pace_word, redraw, threat_word
from game import active

DEFAULT_MODEL = "claude-sonnet-5"   # 方針役の既定 (2026-09-24 に Sonnet 5 へ。Opus は過剰。claude -p の --model に渡す)
PERIOD = 300          # 定期の見回り (ターン)。制約が効いているか、体力や空腹に懸念があるときだけ呼ぶ
MIN_GAP = 6           # 連続で呼ばない最短間隔 (ターン)
STUCK_SPAN = 40       # このターン数のあいだ、踏んだマスが STUCK_TILES 種類以下なら足踏みとみなす
STUCK_TILES = 4
MAX_CALLS_PER_GAME = 150
MAX_CALLS_TOTAL = 600
TIMEOUT = 60
COOLDOWN = 300        # 失敗が 3 回続いたら、このターン数は呼ばずに Laya だけで進む (API 側の一時障害の間、毎回 60 秒待たないため)

PLANS = ["explore_fully", "descend_asap", "free"]
TACTICS = ["fight", "flee", "escape", "free"]
READS = ["none", "map", "teleport", "remove_curse", "confuse", "hold", "scare", "food", "enchant_armor", "enchant_weapon", "protect"]
RINGS = ["none", "put_on", "remove"]
TRIES = ["none", "potion", "scroll", "identify"]
ZAPS = ["none", "bolt", "missile", "slow", "away", "polymorph", "drain", "cancel", "light", "unknown"]
WIELDS = ["none", "bow", "melee"]
QUAFFS = ["none", "haste", "raise", "see_invisible", "detect_monsters", "detect_magic"]
FETCHES = ["none", "armor", "weapon", "food", "potion", "scroll", "wand", "ring", "gold"]
ORDERS = ["save_healing", "avoid_rusters", "rest_before_stairs"]  # standing orders: the pilot knows how, the strategist decides when (NOTES 15)
FETCH_TURNS = 30      # fetch (その種類の品を拾いに行く) の有効期間
SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "enum": PLANS},
        "rest": {"type": "boolean"},
        "tactic": {"type": "string", "enum": TACTICS},
        "heal_now": {"type": "boolean"},
        "read_now": {"type": "string", "enum": READS},
        "try_now": {"type": "string", "enum": TRIES},
        "zap_now": {"type": "string", "enum": ZAPS},
        "ring_now": {"type": "string", "enum": RINGS},
        "quaff_now": {"type": "string", "enum": QUAFFS},
        "wield_now": {"type": "string", "enum": WIELDS},
        "fetch": {"type": "string", "enum": FETCHES},
        "orders": {"type": "array", "items": {"type": "string", "enum": ORDERS}},
        "reason": {"type": "string"},
    },
    "required": ["plan", "rest", "tactic", "heal_now", "read_now", "try_now", "zap_now", "ring_now", "quaff_now", "wield_now", "fetch", "orders", "reason"],
    "additionalProperties": False,
}

SYSTEM = f"""You are the strategist for a hero in a Rogue 5.4-style dungeon crawl. Goal: reach dungeon level {D.GOAL_DEPTH} alive.
A small, fast model ("the pilot", trained by 20,000 games of self-play) picks the hero's action every turn. You do not pick actions. You set constraints that remove options from it, and they stay in force until the next consultation.
You are consulted on arrival at a new level when the hero's experience level is behind or even for the depth (not when it is ahead), when HP or hunger worsens, when a non-trivial monster wakes up in view, when a better weapon or armor comes into view, when the hero seems stuck, and periodically while a constraint is in force. Each consultation costs several seconds of real time, nothing in game time.

Rules of this game (subset of Rogue 5.4.4):
- Monsters get stronger with depth. The hero gets stronger by experience levels (killing monsters), better weapons/armor found on the floor, strength potions, and scrolls of enchant armor / enchant weapon / protect armor (the pilot reads them with "read_enchant_armor" / "read_enchant_weapon" / "read_protect"). One weapon in ten and one armor in five is cursed (a bad bonus, and it cannot be taken off once worn; enchant or remove curse lifts it). Dragons breathe fire (6d6 unless the hero resists) at a hero in a straight line within 6 squares.
- The bow must be wielded ("wield_bow") for arrows to hit hard; while it is wielded the hero is nearly helpless in melee (1d1) until "wield_melee". Other missiles (darts, shuriken, daggers, spears) need no bow.
- Rings (14 kinds as in Rogue 5.4.4, one on each hand): protection +n, add strength +n, dexterity (to-hit) +n, increase damage +n, sustain strength, searching, see invisible, regeneration (+1 HP per turn), slow digestion, stealth (sleeping monsters do not notice the hero), maintain armor (no rust), adornment (nothing), aggravate monster (every monster on every level hunts the hero) and teleportation (random teleport now and then). The last two are always cursed, and the four +n kinds are cursed with -1 one time in three. A cursed ring cannot be taken off until a scroll of remove curse is read. A ring's kind is learned only from a scroll of identify: putting it on tells nothing (the pilot sees "unknown"). Most rings make hunger advance faster (regeneration the most). The pilot has "put_on_ring" (a known good ring first, otherwise an unidentified one), "remove_ring" and "read_remove_curse".
- Wands (all 14 kinds of Rogue 5.4.4, unidentified until zapped, 3-7 charges): lightning / fire / cold ("zap_bolt": a bolt for 6d6 unless the monster resists, bounces off walls and can hit the hero), magic missile (1d4+1, nearly always hits), slow monster (it moves every other turn), teleport away (it is sent elsewhere on the level), polymorph (it becomes a random other monster), drain life (the hero loses half of the HP and the monsters in the room share that damage), cancellation (removes a monster's special power: rust, freeze, poison, drain, hold, steal, gaze, flame), light (lights a dark room), and the useless or harmful invisibility, haste monster, teleport to and nothing (never zapped once known). Directional wands need an awake monster in a straight line. These are the answer to a monster the hero cannot beat in melee; charges do not come back.
- Potions and scrolls are unidentified when found (only a color or a title is visible). Using one reveals its kind for the rest of the game; once a kind is known to be useless it is never used again (the pilot keeps it but never walks to another). Kinds in play - potions (all 14 of Rogue 5.4.4; at NORMAL difficulty blindness and hallucination do not appear): healing (also cures blindness), extra healing (also cures confusion and hallucination), gain strength, restore strength, poison (strength -1 to -3, restored by restore strength), confusion (20-27 turns of stumbling), haste self (4-7 turns of two actions per turn; a second one while hasted makes the hero faint), raise level, see invisible (phantoms are invisible without it), monster detection (senses every monster on the level for about 20 turns), magic detection (shows where the magic items on the level are), levitation (about 30 turns: cannot take stairs or pick up, does not spring traps), blindness (about 850 turns of seeing nothing), hallucination (about 850 turns: the pilot cannot tell what monsters and items are). Scrolls: enchant armor, enchant weapon, protect armor (read automatically once known), magic mapping, teleportation, identify (reveals one unidentified kind the hero carries), remove curse, monster confusion, hold monster, scare monster, food detection, sleep (4-7 turns helpless), create monster (a monster appears next to the hero), aggravate monsters (every monster on the level wakes up and comes). Roughly 3 in 10 unknown potions and 2 in 10 unknown scrolls are harmful. The pilot has "quaff_unknown" / "read_unknown" (tries the unknown kind it carries most of) and "read_identify".
- Scrolls the strategist can trigger: magic mapping (reveals the whole level including the stairs; useful when the stairs are unknown and the hero needs an exit or is hungry), teleportation (moves the hero to a random spot on the level, breaking contact with every monster; the one reliable escape when the stairs are unknown), monster confusion (the next monster the hero hits in melee wanders randomly most of the time), hold monster (every awake monster within two steps freezes until the hero hits it - a way to walk away or to shoot it), scare monster (dropped at the hero's feet, "drop_scare": while the hero stands on it no monster can attack in melee, so the hero can rest or shoot; once picked up again it turns to dust) and food detection (shows where the food on this level is).
- Traps (7 kinds, hidden until stepped on: trap door to the next level, bear trap, sleeping gas, arrow, teleport, poison dart, rust). A trap the hero has stepped on is avoided afterwards.
- Missiles: the hero starts with a bow and about 30 arrows and may find darts, shuriken, daggers and spears. "throw" is available when an awake monster is in view on a straight line at distance 2 or more; the missile lands next to the target and can be picked up again. Shooting an approaching monster gets 1-3 hits in before melee.
- HP regenerates slowly (about 1 HP per 10-20 turns at low level, faster later). Resting with no enemy around is the main way to heal. Healing potions are scarce.
- Hunger: one ration lasts about 1300 turns; "hungry" -> "weak" -> "fainting" -> starvation. Food lies on the floor of unexplored areas; each new level is a fresh chance to find some.
- Monsters and the hero move at the same speed. On its turn a monster either attacks (if it is already adjacent) or moves one step toward the hero, never both. So stepping away avoids that turn's attack, but the monster follows and the hero gains nothing unless there is somewhere to go (the stairs) or time to regenerate (about 1 HP per 10-20 turns at low level). Fleeing into a dead end or a corner means being hit again. Taking the stairs always works: monsters never follow.
- Special attacks: aquator rusts armor (permanent, armor is what keeps the hero alive deeper down); rattlesnake poison lowers strength; wraith drains a level; vampire drains max HP; ice monster freezes; venus flytrap holds (cannot move away, must kill it); leprechaun steals gold; nymph steals a potion; medusa confuses. Sleeping monsters are hit more easily; "mean" ones (hobgoblin, troll, quagga, rattlesnake, orc...) usually wake up when they notice the hero.

Your outputs:
- plan: "explore_fully" = do not take the stairs while unexplored area remains (more items and experience, but more wandering monsters). "descend_asap" = once the stairs are known, stop exploring and picking up, take the stairs (down, or up when the hero carries the Amulet). "free" = no constraint. The plan stays in force across levels until you change it.
  What this pilot does when the plan is "free": once the stairs are known and no awake enemy is in view, it takes them in about 9 cases of 10, whatever its experience level is for the depth (in its experience table, "descend" is the top choice 87% of the time even when it is behind in experience). Its training rewards depth, and its 40-turn lookahead cannot see the fights that a low-level hero loses deeper down. So on this pilot "free" means "descend as soon as the stairs are found", not "the pilot weighs exploring against descending".
- rest: true = when no awake enemy is in view, only rest (or eat / drink / equip) until HP is at least 90%. Ignored while hungry-weak or worse.
- tactic (only matters while an awake enemy is in view, and resets to "free" when no awake enemy is in view): "fight" = running away on foot is removed; attacking, the stairs and potions stay. "flee" = attacking and approaching are removed (only useful to reach the stairs or to stall a slow/held situation). "escape" = if the stairs are known, walk to them and go down now, ignoring the monster; if they are not known it behaves like "flee". "free" = no constraint. The stairs are never removed by a tactic.
- heal_now: true = drink a healing potion on the next turn if the hero carries one.
- read_now: "map" = read magic mapping on the next turn (if carried and the stairs are unknown); "teleport" = read teleportation on the next turn (if carried and an enemy is awake in view); "remove_curse" = read remove curse (if carried and something worn is known to be cursed); "confuse" / "hold" = read that scroll (if carried and an enemy is awake in view / within two steps); "scare" = drop the scare monster scroll here; "food" = read food detection; "none" = nothing. One-shot.
- ring_now: "put_on" = put on a ring on the next turn (if one is carried and a hand is free); "remove" = take one off; "none" = nothing. One-shot.
- quaff_now: "haste" (if carried and an enemy is awake in view) / "raise" / "see_invisible" (if something unseen is attacking) / "detect_monsters" / "detect_magic" = drink that known potion on the next turn; "none" = nothing. One-shot.
- zap_now: "bolt" / "missile" / "slow" / "away" / "polymorph" / "cancel" = zap that known wand on the next turn if carried and an awake monster is in line; "drain" (monsters in the room) / "light" (dark room); "unknown" = zap an unidentified wand; "none" = nothing. One-shot.
- wield_now: "bow" = wield the bow on the next turn (if carried, with arrows, and a monster is in line at distance 2+); "melee" = wield the melee weapon again; "none" = nothing. One-shot.
- try_now: "potion" = drink an unidentified potion on the next turn; "scroll" = read an unidentified scroll; "identify" = read a known scroll of identify; "none" = nothing. One-shot. Testing unknown items is safest with no monster in view, full HP and the stairs known.
- fetch: "armor" / "weapon" / "food" / "potion" / "scroll" / "wand" / "ring" / "gold" = walk to the nearest item of that kind in view and pick it up, ignoring everything else while no monster is awake in view. Cleared when it is picked up, when a monster wakes up, or after 30 turns. "none" = nothing. The pilot on its own rarely detours for items (it learned that most floor items are not worth the walk), so this is how you make it collect a specific thing, e.g. better armor. A better weapon or armor is worn automatically once carried.
- orders: standing orders, a list that stays in force across levels until you send a different list (send the full list each time; [] cancels all). The pilot knows how to carry each one out; you decide when it applies.
  "save_healing" = keep healing potions for emergencies: drink one only below half HP. "avoid_rusters" = never rest or walk up to an aquator, and when one is adjacent either hit it or step away (no resting, exploring or picking up next to it) - rust is permanent and armor is what keeps the hero alive deeper down. "rest_before_stairs" = do not take the stairs below 90% HP while no enemy is in view (rest first; ignored when hungry-weak or worse).
- reason: one short sentence in Japanese, shown to the player.

Briefing: what was measured on this pilot (128 games from level 1, no strategist) and on earlier pilots. Use it to calibrate, then decide for yourself.
- This pilot alone averages dungeon level 6.2 (best 16) and ends at experience level 3.7 with 20 kills. Two hand-written rule sets in the same games: one that explores each level before descending averages 7.8 (experience level 5.3, 61 kills), one that dives 9.2 (4.7, 30 kills). Nobody has reached 20.
- Where this pilot dies: 41% of its games end on levels 1-4, mostly to a hobgoblin (26 of those 53 deaths) or a rattlesnake, at experience level 1-2. From level 6 on the killers are centaurs, quaggas, trolls and zombies against a hero of experience level 4-5. At a given depth this pilot is one to two experience levels below the exploring rule set (on level 8: 4.8 vs 5.8; on level 10: 5.3 vs 7.3).
- An earlier pilot, which explored on its own before descending, was studied over 64 deaths: in 52% the killer was already rated "deadly" the first time it was seen, in 91% the stairs were not known yet, the median time from first sight to death was 7 turns, and in 56% the hero had 80%+ HP when the killer appeared. So most deaths are a fight that could not be won or escaped once it started. What a strategist can influence is the state the hero is in when the next monster appears, and whether the stairs are used as an exit.
- Also on earlier pilots: forcing rest below 60-75% HP when no enemy is in view gave a small gain (about +0.2 levels; the pilot already rests on its own most of the time). Over the same 16 seeds: pilot alone 9.4; "rest"=true whenever HP < 60% and no enemy in view 10.0; an older, stronger form of "fight" (which also removed resting and the stairs) forced whenever HP <= 50% with an enemy in view 8.8; both together 7.0. Forcing "flee" at critical HP scored worse than doing nothing. Forcing "escape" against every deadly monster changed nothing on average, because in 9 of 10 such fights the stairs were not known yet. A previous strategist that answered "fight" at nearly every low-HP consultation scored 6.7 against 10.3 for the pilot alone. The pilot's own choice inside a fight is as good as any rule; use "fight"/"flee"/"escape" only when the situation has a feature the pilot cannot see (a known exit, a special attack worth avoiding, a fight that is hopeless by the numbers).
- Every constraint replaces the pilot's judgement, so set one only when you can say why the pilot's default would be wrong here.
- Starting armor two points better adds about 2.4 levels of depth. Better armor found on the floor is the most valuable thing in the game; aquators (rust) are its main enemy.
Note: since the earlier measurements, rings (14 kinds), wands (14 kinds), cursed gear, treasure rooms, dragon fire, bow wielding, scrolls and missiles were added, the dungeon got harder (the exploring rule set fell from 8.5 to 7.8), and the pilot was retrained on all of it. The effect of the strategist's levers on this pilot is not measured yet.
Default to plan="free", rest=false, tactic="free", heal_now=false, read_now="none", try_now="none", zap_now="none", ring_now="none", quaff_now="none", wield_now="none", fetch="none", orders=[] and deviate with a concrete reason: for example "escape" when a deadly monster is awake, the stairs are known and the hero is not fresh; "rest"=true after a hard fight before pushing deeper; "heal_now" when critical in a fight that is otherwise winnable; "descend_asap" when hungry with no food; "flee"/"escape" from an aquator to protect good armor."""


def dice_text(w):
    return "+".join(f"{n}d{s}" for n, s in w["dice"]) + (f" +{w['hplus']} to hit" if w.get("hplus") else "") + (f" +{w['dplus']} dmg" if w.get("dplus") else "")


def ring_detail(g, r):
    if r["name"] not in g.known:
        return "unidentified ring" + (" (cursed, cannot remove)" if r.get("cursed_known") else "")
    return f"{r['name']}" + (f" {r['value']:+d}" if r["name"] in D.RING_VALUED else "") + (" (cursed)" if g.ring_known_cursed(r) else "")


DIFFICULTY_NOTES = {
    "normal": "Difficulty NORMAL: the goal is to reach dungeon level {goal} alive. Compared with Rogue 5.4.4 there are no hidden doors or passages, no maze rooms, no xeroc mimics, and the potions of blindness and hallucination do not exist. Scrolls of identify are one kind that identifies anything, using an item always reveals its kind, and the bonus of weapons and armor on the floor is visible.",
    "hard": "Difficulty HARD: the goal is to find the Amulet of Yendor (it lies on level 26 or deeper), then climb back to the surface (\"ascend\" from level 1 wins). Hidden doors, maze rooms and xeroc mimics exist as in Rogue 5.4.4. Scrolls of identify are one kind that identifies anything, using an item always reveals its kind, and the bonus of weapons and armor on the floor is visible.",
    "original": "Difficulty ORIGINAL: Rogue 5.4.4 as it is. The goal is to find the Amulet of Yendor (it lies on level 26 or deeper), then climb back to the surface (\"ascend\" from level 1 wins). Hidden doors and passages, maze rooms and xeroc mimics exist. The five scrolls of identify each identify one category. Using an item reveals its kind only when the effect could be observed (otherwise it is marked as tried). The bonus of armor is learned by wearing it, that of a weapon only by a scroll of identify weapon. The pack holds 23 slots (\"drop\" discards a useless item).",
}


def system_for(rules):
    """難易度ごとの説明を付けた SYSTEM (NOTES.md 15 章: Rules と Kinds in play を難易度に合わせる)。"""
    return SYSTEM + "\n" + DIFFICULTY_NOTES[rules.name].format(goal=D.GOAL_DEPTH)


def item_detail(g, it):
    """方針役に見せる品物: 名前と数値、着ている物との比較、距離 (床の上の品のみ)。"""
    k = it["kind"]
    if k == "armor":
        gain = g.gear_gain(it)
        s = f"{it['name']} ({'AC ' + str(it['ac']) if it.get('known', True) else 'bonus unknown until worn'}, {'better' if gain > 0 else 'worse'} than worn by {abs(gain)})"
    elif k == "weapon":
        gain = g.gear_gain(it)
        s = f"{it['name']} ({dice_text(it) if it.get('known', True) else 'bonus unknown'}, {'better' if gain > 0 else 'worse'} than worn by {abs(gain):.1f})"
    elif k in ("potion", "scroll", "stick", "ring"):
        kind = "wand" if k == "stick" else k
        s = f"{kind} of {it['name']}" if it["name"] in g.known else f"unidentified {kind}" + (" (tried before)" if it["name"] in g.tried else "")
    elif k == "amulet":
        s = "the Amulet of Yendor"
    elif k == "gold":
        s = f"gold ({it['value']})"
    elif k == "missile":
        s = f"{it.get('count', 1)} {it['name']}"
    else:
        s = it["name"] if "name" in it else k
    return s + (f" {g.dist(it['x'], it['y'])} steps away" if "x" in it else "")


def situation(g, trigger, st):
    """方針役に見せる状況。Laya の状況文と違い、数値をそのまま渡す。"""
    mons = g.visible_monsters()
    items = g.visible_loot()
    valid = g.valid_actions()
    lines = [
        f"Consulted because: {trigger}.",
        f"Dungeon level {g.depth} (goal {D.GOAL_DEPTH}). Turn {g.turn}, {g.turn - st.floor_turn} turns on this level.",
        f"Hero: HP {g.hp}/{g.max_hp}, experience level {g.level}, strength {g.str}/{g.max_str}, armor class {g.armor['ac']} ({g.armor['name']}), "
        f"weapon {g.weapon['name']}. Experience for this depth: {pace_word(g)} "
        f"(the pilot's own scale, experience level / dungeon level = {g.level / max(1, g.depth):.2f}: behind < 0.5 <= even < 0.8 <= ahead).",
        f"Hunger: {g.hunger_word()} (about {max(0, g.food_left)} turns of food in stomach). Food rations carried: {g.food}. "
        f"Healing potions: {g.has_heal()}. Other known potions: {', '.join(f'{n} x{c}' for n, c in g.potions.items() if n in g.known and c > 0 and n not in ('healing', 'extra healing')) or 'none'}. "
        f"Unidentified potions: {sum(g.unknown_potions().values())} ({len(g.unknown_potions())} kinds). "
        f"Gold {g.gold}. Kills {g.kills}.",
        f"Missiles: {sum(g.missiles.values())} ({', '.join(f'{n} x{c}' for n, c in g.missiles.items()) or 'none'}), bow: {'wielded' if g.wielding_bow() else 'yes' if g.bow else 'no'}. "
        f"Known scrolls: {', '.join(f'{n} x{c}' for n, c in g.scrolls.items() if n in g.known and c > 0) or 'none'}. "
        f"Unidentified scrolls: {sum(g.unknown_scrolls().values())} ({len(g.unknown_scrolls())} kinds). "
        f"Wands: {', '.join(f'{n} x{c}' for n, c in g.known_sticks().items()) or 'none'}; unidentified wands: {len(g.unknown_sticks())}. "
        f"Rings worn: {', '.join(ring_detail(g, r) for r in g.worn) or 'none'}. Rings carried: {', '.join(ring_detail(g, r) for r in g.rings) or 'none'}. "
        f"Identified kinds so far: {', '.join(sorted(g.known)) or 'none'}.",
        "Status: " + (", ".join(w for w, on in (("confused", g.confused), ("held", g.held_by is not None), ("cannot act", g.no_command > 0),
                                                ("hands glowing (next hit confuses)", g.glowing), ("standing on a scare monster scroll", g.on_scare()),
                                                (f"hasted ({g.fuses['hasted']} turns)", g.hasted), (f"levitating ({g.fuses['levitating']} turns)", g.levitating),
                                                (f"blind ({g.fuses['blind']} turns)", g.blind), (f"hallucinating ({g.fuses['hallucinating']} turns)", g.hallucinating),
                                                ("seeing invisible", g.can_see_invisible()), ("sensing monsters", g.detecting())) if on) or "normal") + ".",
    ]
    if mons:
        lines.append("Monsters in view: " + "; ".join(
            f"{m['kind']} ({'awake' if m['awake'] else 'asleep'}{', held' if m.get('held') else ''}{', confused' if m.get('confused') else ''}, "
            f"{dist_word(g.dist(m['x'], m['y']))}, distance {g.dist(m['x'], m['y'])}, "
            f"melee estimate: {threat_word(g, m)} - hero kills it in ~{math.ceil(m['hp'] / max(0.05, g.hero_damage_per_turn(m)))} turns, "
            f"it kills hero in ~{math.ceil(g.hp / max(0.05, g.monster_damage_per_turn(m)))} turns, special: {SPECIAL.get(m['ch'], 'none')})" for m in mons[:5]) + ".")
    else:
        lines.append("Monsters in view: none.")
    if g.detecting():
        sensed = g.sensed_monsters()
        lines.append("Monsters sensed elsewhere on this level: " + (", ".join(f"{m['kind']} ({'awake' if m['awake'] else 'asleep'}, distance {g.dist(m['x'], m['y'])})"
                                                                            for m in sensed[:8]) if sensed else "none") + ".")
    lines.append("Items in view: " + (", ".join(item_detail(g, i) for i in items[:5]) if items else "none") + ".")
    lines.append(f"Worn: {g.armor['name']} (AC {g.armor['ac']}{', cursed' if g.armor.get('cursed_known') else ''}), {g.weapon['name']} ({dice_text(g.weapon)}"
                 f"{', cursed' if g.weapon.get('cursed_known') else ''}). This level: {g.explored_ratio():.0%} explored.")
    stairs = f"known, {max(abs(g.hx - g.stairs[0]), abs(g.hy - g.stairs[1]))} steps away" if g.seen[g.stairs[1]][g.stairs[0]] else "not found yet"
    lines.append(f"Stairs: {stairs}. Unexplored area on this level: {'yes' if 'explore' in valid else 'no'}.")
    if g.rules.amulet:
        lines.append("Amulet of Yendor: " + ("carried - climb back to the surface." if g.amulet else f"not yet found (deepest level so far {g.max_depth}).")
                     + (f" Pack: {g.pack_count()}/{g.rules.pack_limit} slots." if g.rules.pack_limit else ""))
    if g.tried:
        lines.append("Tried but not identified: " + ", ".join(sorted(g.names[k] for k in g.tried)) + ".")
    if g.gear:
        lines.append("Carried but not worn: " + ", ".join(item_detail(g, x) for x in g.gear) + ".")
    lines.append(f"Decision in force: plan={st.plan}, rest={st.rest}, tactic={st.tactic}, fetch={st.fetch}, orders={sorted(st.orders)}.")
    if st.recent:
        lines.append("Recent actions (oldest first): " + " ".join(st.recent[-20:]))
    if g.log:
        lines.append("Recent events: " + " / ".join(g.log[-6:]))
    return "\n".join(lines)


def ask(text, model=DEFAULT_MODEL, effort="high", system=SYSTEM):
    """claude -p を 1 回呼ぶ。戻り値は (決定の dict, 秒数, 使用トークン)。失敗したら例外。"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY があると従量課金になるので呼ばない")
    cmd = ["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json", "--system-prompt", system,
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
           "--json-schema", json.dumps(SCHEMA)]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT)
    sec = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result", ""))[:200])
    d = out.get("structured_output")
    if (not isinstance(d, dict) or d.get("plan") not in PLANS or d.get("tactic") not in TACTICS or d.get("read_now", "none") not in READS
            or d.get("try_now", "none") not in TRIES or d.get("zap_now", "none") not in ZAPS or d.get("ring_now", "none") not in RINGS
            or d.get("quaff_now", "none") not in QUAFFS or d.get("wield_now", "none") not in WIELDS
            or d.get("fetch", "none") not in FETCHES or not set(d.get("orders", [])) <= set(ORDERS)):
        raise RuntimeError("想定外の応答: " + p.stdout[:300])
    u = out.get("usage", {})
    tokens = sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    return d, sec, tokens


total_calls = 0  # プロセス全体の呼び出し回数 (自動停止用)


class Strategist:
    """1 ゲーム分の方針。check() が「いま呼ぶべき理由」を返し、consult() が呼び、allowed() が選択肢を絞る。"""

    def __init__(self, model=DEFAULT_MODEL, effort="high", enabled=True, asker=ask, orders=()):
        """asker=None with orders: a fixed strategist that never consults and only keeps those standing orders (for measuring them)."""
        self.model, self.effort, self.enabled, self.asker = model, effort, enabled, asker
        self.fixed_orders = set(orders)
        self.calls = self.tokens = self.errors = 0
        self.seconds = 0.0
        self.stopped = None  # 自動停止した理由
        self.paused_until = -1  # 失敗が続いたあと、このターンまで呼ばない
        self.history = []    # 相談の記録 (あとで方針役の判断を読み返すため)
        self.reset()

    def reset(self):
        self.plan, self.rest, self.tactic, self.heal_now, self.reason = "free", False, "free", False, ""
        self.orders = set(self.fixed_orders)
        self.read_now = self.try_now = self.zap_now = self.ring_now = self.quaff_now = self.wield_now = "none"
        self.fetch, self.fetch_until = "none", 0
        self.known_gear = set()  # すでに相談した「良い装備」 (名前, 位置)
        self.paused_until, self.pauses = -1, 0
        self.depth = 0
        self.floor_turn = self.last_turn = 0
        self.hp_band = self.hunger = None
        self.known = set()   # すでに相談した敵の id
        self.trail = []      # 最近踏んだマス
        self.recent = []     # 最近の行動
        self.game_calls = 0
        self.kind = None

    # ------------------------------------------------------------------ いつ呼ぶか
    def check(self, g):
        if not self.enabled or self.stopped or self.asker is None:
            return None
        self.trail = (self.trail + [(g.hx, g.hy)])[-STUCK_SPAN:]
        awake = [m for m in g.visible_monsters() if active(m)]
        if not awake:
            self.tactic = "free"
        hp, hunger = hp_word(g), g.hunger_word()
        trigger = None
        if g.depth != self.depth:  # 新しい階: 経験が深さに対して behind / even のときだけ相談する (ahead なら呼ばない。全部の階で呼ぶ形は 2026-09-24 に外した:
            self.depth, self.floor_turn, self.known, self.trail, self.known_gear = g.depth, g.turn, set(), [], set()  # 244 回中 133 回を占めて成績に効かなかった)
            if pace_word(g) != "ahead":
                trigger, self.kind = f"arrived on level {g.depth} with experience {pace_word(g)} for this depth", "floor"
        if trigger:
            pass
        elif hp != self.hp_band and hp in ("low", "critical"):
            trigger, self.kind = f"HP dropped to {hp}", "hp"
        elif hunger != self.hunger and hunger != "fine":
            trigger, self.kind = f"hunger is now {hunger}", "hunger"
        else:
            fresh = [m for m in awake if m["id"] not in self.known and (threat_word(g, m) != "weak" or m["ch"] in SPECIAL)]
            ups = [i for i in g.visible_upgrades() if (i["name"], i["x"], i["y"]) not in self.known_gear] if not awake else []
            if fresh:
                trigger, self.kind = f"dangerous monster in view: {fresh[0]['kind']}", "danger"
            elif ups:
                trigger, self.kind = f"better {ups[0]['kind']} in view: {ups[0]['name']}", "gear"
                self.known_gear.add((ups[0]["name"], ups[0]["x"], ups[0]["y"]))
            elif len(self.trail) == STUCK_SPAN and len(set(self.trail)) <= STUCK_TILES and not awake and not (self.rest and hp_word(g) != "full"):
                trigger, self.kind = "the hero seems stuck (pacing between the same few tiles for 40 turns)", "stuck"
                self.trail = []
            elif g.turn - self.last_turn >= PERIOD and (self.plan != "free" or self.rest or hp not in ("full", "healthy") or hunger != "fine"):
                trigger, self.kind = "periodic check (a constraint is in force or HP/hunger deserves a look)", "periodic"
        self.hp_band, self.hunger = hp, hunger
        self.known.update(m["id"] for m in awake)
        if trigger and g.turn - self.last_turn < MIN_GAP and not trigger.startswith("arrived"):
            return None
        if trigger and g.turn < self.paused_until:
            return None
        return trigger

    def consult(self, g, trigger):
        """方針役を呼んで方針を更新する (数秒ブロックする)。戻り値は画面に出す用の dict。"""
        global total_calls
        if self.game_calls >= MAX_CALLS_PER_GAME or total_calls >= MAX_CALLS_TOTAL:
            self.stopped = "呼び出し回数の上限に達した"
            self.plan, self.rest, self.tactic = "free", False, "free"
            return {"trigger": trigger, "kind": self.kind, "error": self.stopped}
        self.last_turn = g.turn
        self.game_calls += 1
        self.calls += 1
        total_calls += 1
        try:
            d, sec, tokens = self.asker(situation(g, trigger, self), self.model, self.effort, system_for(g.rules))
        except Exception as e:  # noqa: BLE001  失敗しても Laya は動き続ける
            self.errors += 1
            if self.errors >= 3:  # 制約は外して、しばらく Laya だけで進む
                self.errors = 0
                self.paused_until = g.turn + COOLDOWN
                self.plan, self.rest, self.tactic, self.heal_now = "free", False, "free", False
                self.pauses = getattr(self, "pauses", 0) + 1
            return {"trigger": trigger, "kind": self.kind, "error": str(e)[:200]}
        self.errors = 0
        self.seconds += sec
        self.tokens += tokens
        self.plan, self.rest, self.tactic, self.heal_now, self.reason = d["plan"], d["rest"], d["tactic"], d["heal_now"], d["reason"]
        self.read_now = d.get("read_now", "none")
        self.try_now = d.get("try_now", "none")
        self.zap_now = d.get("zap_now", "none")
        self.ring_now = d.get("ring_now", "none")
        self.quaff_now = d.get("quaff_now", "none")
        self.wield_now = d.get("wield_now", "none")
        self.fetch = d.get("fetch", "none")
        self.fetch_until = g.turn + FETCH_TURNS
        self.orders = set(d.get("orders", []))
        self.history.append({"turn": g.turn, "depth": g.depth, "hp": g.hp, "max_hp": g.max_hp, "level": g.level, "kind": self.kind,
                             "trigger": trigger, **d, "sec": round(sec, 1), "valid": g.valid_actions(), "recent": self.recent[-12:]})
        return {"trigger": trigger, "kind": self.kind, **d, "sec": round(sec, 1), "tokens": tokens, "calls": self.calls}

    # ------------------------------------------------------------------ 選択肢を絞る
    def allowed(self, g, valid):
        g.pick_kind = None
        if not self.enabled or self.stopped or len(valid) <= 1:
            return valid
        if self.fetch != "none":  # その種類の品を拾いに行く。敵が起きた・期限切れ・見当たらない (拾った) なら解除
            kind = "stick" if self.fetch == "wand" else self.fetch
            if any(active(m) for m in g.visible_monsters()) or g.turn > self.fetch_until or not any(i["kind"] == kind for i in g.visible_items()):
                self.fetch = "none"
            elif "pick_up" in valid:
                g.pick_kind = kind
                return ["pick_up"]
        if self.heal_now:
            self.heal_now = False
            if "quaff_heal" in valid:
                return ["quaff_heal"]
        if self.read_now != "none":
            action, self.read_now = ("drop_scare" if self.read_now == "scare" else f"read_{self.read_now}"), "none"
            if action in valid:
                return [action]
        if self.try_now != "none":
            action = {"potion": "quaff_unknown", "scroll": "read_unknown", "identify": "read_identify"}[self.try_now]
            self.try_now = "none"
            if action in valid:
                return [action]
        if self.zap_now != "none":
            action, self.zap_now = f"zap_{self.zap_now}", "none"
            if action in valid:
                return [action]
        if self.wield_now != "none":
            action, self.wield_now = f"wield_{self.wield_now}", "none"
            if action in valid:
                return [action]
        if self.ring_now != "none":
            action = {"put_on": "put_on_ring", "remove": "remove_ring"}[self.ring_now]
            self.ring_now = "none"
            if action in valid:
                return [action]
        if self.quaff_now != "none":
            action, self.quaff_now = f"quaff_{self.quaff_now}", "none"
            if action in valid:
                return [action]
        awake = any(active(m) for m in g.visible_monsters())
        drop = set()
        if awake:
            if self.tactic == "fight":  # 徒歩の逃走だけを外す。休憩と階段まで外す形は Laya の成績を下げた (NOTES.md 7 章)
                drop.add("flee")
            elif self.tactic == "flee":
                drop |= {"attack", "approach"}
            elif self.tactic == "escape":
                if "ascend" in valid and g.amulet:
                    return ["ascend"]
                if "descend" in valid:
                    return ["descend"]
                drop |= {"attack", "approach"}
        elif self.rest and g.hp < 0.9 * g.max_hp and g.hunger_word() in ("fine", "hungry"):
            drop |= {"approach", "pick_up", "explore", "descend"}
        if self.plan == "explore_fully" and "explore" in valid:
            drop |= {"descend", "ascend"}
        elif self.plan == "descend_asap" and ("descend" in valid or "ascend" in valid) and not awake:
            drop |= {"explore", "pick_up"}
        drop |= self.order_drops(g, valid, awake)
        kept = [a for a in valid if a not in drop]
        return kept or valid

    def order_drops(self, g, valid, awake):
        """Actions the standing orders remove this turn."""
        drop = set()
        if "save_healing" in self.orders and g.hp >= 0.5 * g.max_hp and not g.blind:
            drop.add("quaff_heal")
        if "avoid_rusters" in self.orders:
            rusters = [m for m in g.visible_monsters() if m["kind"] == "aquator" and m["awake"]]
            if rusters:
                drop |= {"rest", "approach"}
                if any(g.dist(m["x"], m["y"]) <= 1 for m in rusters):
                    drop |= {"pick_up", "explore", "search"}
        if "rest_before_stairs" in self.orders and not awake and "rest" in valid and g.hp < 0.9 * g.max_hp and g.hunger_word() in ("fine", "hungry"):
            drop.add("descend")
        return drop


class Masked:
    """選択肢だけを差し替えたゲームの見せかけ。頭脳 (Laya でも経験表でも if 文でも) はこれを本物と同じように読む。"""

    def __init__(self, g, valid):
        self._g, self._valid = g, valid

    def valid_actions(self):
        return self._valid

    def __getattr__(self, name):
        return getattr(self._g, name)


class Guided:
    """頭脳に方針役を付ける。decide() の中で必要なら方針役を呼ぶので、その 1 手だけ数秒かかる。"""

    def __init__(self, brain, strategist):
        self.brain, self.strategist, self.game = brain, strategist, None
        tag = "llm" if strategist.asker else "+".join(sorted(strategist.fixed_orders)) or "none"
        self.name = f"{getattr(brain, 'name', 'brain')}+{tag}"

    def decide(self, g):
        st = self.strategist
        if g is not self.game:  # 新しいゲームが始まった
            self.game = g
            st.reset()
        trigger = st.check(g)
        advice = st.consult(g, trigger) if trigger else None
        d = decide_within(self.brain, g, st)
        d["advice"] = advice
        return d


def decide_within(brain, g, st):
    """The pilot's decision under the strategist's constraints. The pilot sees all its options and, when its pick is not
    allowed, the action is re-drawn from its own distribution over the allowed ones. Hiding options (Masked) showed the
    pilot option sets it never trained on: with "flee" hidden, Laya rested next to the enemy (NOTES 15)."""
    g.pick_kind = None  # fetch's pick target must not leak into this turn's options (they are recorded and replayed without it)
    valid = g.valid_actions()
    allowed = st.allowed(g, valid)  # may set g.pick_kind for the step
    d = redraw(brain.decide(Masked(g, valid)), allowed, getattr(brain, "sharpness", None), getattr(g, "action_rng", random))
    if d["action"] not in allowed:  # a one-hot brain (rules / diver): ask it again with only the allowed options
        d = brain.decide(Masked(g, allowed))
    st.recent = (st.recent + [d["action"]])[-40:]
    return d
