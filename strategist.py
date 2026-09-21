"""方針役。全体方針を決める LLM を `claude -p` で呼び、Laya の選択肢を絞る (任意機能)。

役割分担:
  - 一手一手を選ぶのは Laya (13ms)。方針役は 1 回 数秒かかるので、節目と危ない場面と定期の見回りでだけ呼ぶ
  - 方針役は Laya に話しかけない。決めた方針に合わない行動を選択肢から外すだけ (例: 「この階は探索し切る」の間は「階段へ」を外す)。
    なので Laya を方針つきで学習し直す必要がない
  - 将来はこの役をプレイヤーに渡すか、プレイヤーの補助として残す

呼ぶ場面:
  floor    新しい階に着いた
  hp       体力が low / critical に落ちた
  hunger   空腹になった
  danger   手強い敵 (互角以上、または特殊攻撃持ち) が起きて視界に入った
  stuck    同じ場所を行き来して進んでいない
  periodic 最後に呼んでから PERIOD ターン経った

各自の Claude Code ログイン (サブスクリプション) で動く。利用枠は対話利用と共有なので、1 ゲームと 1 プロセスの呼び出し回数に
上限を置き、超えたら自動で止まる (止まったあとは Laya が制限なしで動く)。ANTHROPIC_API_KEY が環境にあると従量課金に
なるので、その場合は呼ばない。
"""
import json
import math
import os
import subprocess
import time

from brain import SPECIAL, dist_word, hp_word, threat_word

PERIOD = 200          # 定期の見回り (ターン)
MIN_GAP = 6           # 連続で呼ばない最短間隔 (ターン)
STUCK_SPAN = 40       # このターン数のあいだ、踏んだマスが STUCK_TILES 種類以下なら足踏みとみなす
STUCK_TILES = 4
MAX_CALLS_PER_GAME = 150
MAX_CALLS_TOTAL = 600
TIMEOUT = 60

PLANS = ["explore_fully", "descend_asap", "free"]
TACTICS = ["fight", "flee", "free"]
SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "enum": PLANS},
        "rest": {"type": "boolean"},
        "tactic": {"type": "string", "enum": TACTICS},
        "heal_now": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["plan", "rest", "tactic", "heal_now", "reason"],
    "additionalProperties": False,
}

SYSTEM = """You are the strategist for a hero in a Rogue 5.4-style dungeon crawl. Goal: reach dungeon level 20 alive.
A small, fast model picks the hero's action every turn. You do not pick actions. You set constraints that remove options from it.
You are consulted only at milestones, in danger, when the hero seems stuck, and periodically. Your decision stays in force until the next consultation.

Rules of this game (subset of Rogue 5.4.4):
- Monsters get stronger with depth. The hero gets stronger only by gaining experience levels (killing monsters) and by finding better weapons/armor and strength potions. There are no scrolls, wands, rings or missiles in this version.
- HP regenerates slowly over time; resting with no enemy around is the main way to heal. Healing potions are scarce.
- Hunger: one food ration lasts about 1300 turns. "hungry" -> "weak" -> "fainting" -> death by starvation. Food is found only on the floor of new areas, so lingering costs food; each new dungeon level is a new chance to find food.
- Descending too fast with a low experience level gets the hero killed by mid-level monsters (centaur, troll, etc). Descending too slowly starves the hero. Wandering monsters keep appearing on a level over time.
- The stairs can be used to escape a fight: monsters do not follow.
- Special attacks: aquator rusts armor; rattlesnake poison lowers strength; wraith drains level; vampire drains max HP; ice monster freezes; venus flytrap holds (cannot move away); leprechaun steals gold; nymph steals a potion; medusa confuses; sleeping monsters are hit more easily (+4) and most stay asleep unless approached.

Your outputs:
- plan: "explore_fully" = do not take the stairs while unexplored area remains on this level (gain exp and items first). "descend_asap" = once the stairs are known, stop exploring and picking up, go down. "free" = no constraint.
- rest: true = when no awake enemy is in view, only rest (or eat / drink / equip) until HP is at least 90%. Ignored while hungry-weak or worse.
- tactic (only matters while an awake enemy is in view): "fight" = fleeing and waiting are removed. "flee" = attacking and approaching are removed (running, stairs and potions remain). "free" = no constraint.
- heal_now: true = drink a healing potion on the next turn if one is available.
- reason: one short sentence in Japanese (shown to the player).

Measured facts about this version (hundreds of runs), which override general roguelike intuition:
- The action model was trained by self-play and already plays about as well as hand-written rules (mean depth ~9-10). Its moment-to-moment combat choices are good. Every constraint you set replaced its judgement in past runs and made results WORSE (mean depth 4.5 with a strategist that liked "explore_fully" + "fight", vs 10 without).
- Lingering on shallow levels is bad: wandering hobgoblins kill a level-1 hero on levels 1-2. A policy that descends as soon as the stairs are found reaches depth ~9-10; one that explores every level fully reaches ~7. Experience comes fast enough from monsters met on the way down.
- The real wall is dungeon levels 8-13 (centaur, troll, quagga, yeti) with a hero of experience level 4-6. Starting armor two points better adds about 2.4 levels of depth, so picking up and wearing better armor matters more than anything else you can influence.
So: default to plan="free", rest=false, tactic="free", heal_now=false, and deviate only when the situation clearly calls for it (for example: "descend_asap" when hungry with no food or when a deadly monster is near and the stairs are known; "rest"=true before descending deeper with low HP and no enemy around; "flee" with known stairs against a monster that will clearly win)."""


def situation(g, trigger, st):
    """方針役に見せる状況。Laya の状況文と違い、数値をそのまま渡す。"""
    mons = g.visible_monsters()
    items = g.visible_items()
    valid = g.valid_actions()
    lines = [
        f"Consulted because: {trigger}.",
        f"Dungeon level {g.depth} (goal 20). Turn {g.turn}, {g.turn - st.floor_turn} turns on this level.",
        f"Hero: HP {g.hp}/{g.max_hp}, experience level {g.level}, strength {g.str}/{g.max_str}, armor class {g.armor['ac']} ({g.armor['name']}), "
        f"weapon {g.weapon['name']}.",
        f"Hunger: {g.hunger_word()} (about {max(0, g.food_left)} turns of food in stomach). Food rations carried: {g.food}. "
        f"Healing potions: {g.has_heal()}. Gold {g.gold}. Kills {g.kills}.",
        "Status: " + (", ".join(w for w, on in (("confused", g.confused), ("held", g.held_by is not None), ("cannot act", g.no_command > 0)) if on) or "normal") + ".",
    ]
    if mons:
        lines.append("Monsters in view: " + "; ".join(
            f"{m['kind']} ({'awake' if m['awake'] else 'asleep'}, {dist_word(g.dist(m['x'], m['y']))}, distance {g.dist(m['x'], m['y'])}, "
            f"melee estimate: {threat_word(g, m)} - hero kills it in ~{math.ceil(m['hp'] / max(0.05, g.hero_damage_per_turn(m)))} turns, "
            f"it kills hero in ~{math.ceil(g.hp / max(0.05, g.monster_damage_per_turn(m)))} turns, special: {SPECIAL.get(m['ch'], 'none')})" for m in mons[:5]) + ".")
    else:
        lines.append("Monsters in view: none.")
    lines.append("Items in view: " + (", ".join(i["kind"] for i in items[:5]) if items else "none") + ".")
    lines.append(f"Stairs: {'known' if g.seen[g.stairs[1]][g.stairs[0]] else 'not found yet'}. Unexplored area on this level: {'yes' if 'explore' in valid else 'no'}.")
    lines.append(f"Decision in force: plan={st.plan}, rest={st.rest}, tactic={st.tactic}.")
    if st.recent:
        lines.append("Recent actions (oldest first): " + " ".join(st.recent[-20:]))
    if g.log:
        lines.append("Recent events: " + " / ".join(g.log[-6:]))
    return "\n".join(lines)


def ask(text, model="opus", effort="high"):
    """claude -p を 1 回呼ぶ。戻り値は (決定の dict, 秒数, 使用トークン)。失敗したら例外。"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY があると従量課金になるので呼ばない")
    cmd = ["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json", "--system-prompt", SYSTEM,
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
           "--json-schema", json.dumps(SCHEMA)]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT)
    sec = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    out = json.loads(p.stdout)
    d = out.get("structured_output")
    if not isinstance(d, dict) or d.get("plan") not in PLANS or d.get("tactic") not in TACTICS:
        raise RuntimeError("想定外の応答: " + p.stdout[:300])
    u = out.get("usage", {})
    tokens = sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    return d, sec, tokens


total_calls = 0  # プロセス全体の呼び出し回数 (自動停止用)


class Strategist:
    """1 ゲーム分の方針。check() が「いま呼ぶべき理由」を返し、consult() が呼び、allowed() が選択肢を絞る。"""

    def __init__(self, model="opus", effort="high", enabled=True, asker=ask):
        self.model, self.effort, self.enabled, self.asker = model, effort, enabled, asker
        self.calls = self.tokens = self.errors = 0
        self.seconds = 0.0
        self.stopped = None  # 自動停止した理由
        self.history = []    # 相談の記録 (あとで方針役の判断を読み返すため)
        self.reset()

    def reset(self):
        self.plan, self.rest, self.tactic, self.heal_now, self.reason = "free", False, "free", False, ""
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
        if not self.enabled or self.stopped:
            return None
        self.trail = (self.trail + [(g.hx, g.hy)])[-STUCK_SPAN:]
        awake = [m for m in g.visible_monsters() if m["awake"]]
        if not awake:
            self.tactic = "free"
        hp, hunger = hp_word(g), g.hunger_word()
        trigger = None
        if g.depth != self.depth:
            trigger, self.kind = f"arrived at dungeon level {g.depth}", "floor"
            self.depth, self.floor_turn, self.known, self.trail = g.depth, g.turn, set(), []
        elif hp != self.hp_band and hp in ("low", "critical"):
            trigger, self.kind = f"HP dropped to {hp}", "hp"
        elif hunger != self.hunger and hunger != "fine":
            trigger, self.kind = f"hunger is now {hunger}", "hunger"
        else:
            fresh = [m for m in awake if m["id"] not in self.known and (threat_word(g, m) != "weak" or m["ch"] in SPECIAL)]
            if fresh:
                trigger, self.kind = f"dangerous monster in view: {fresh[0]['kind']}", "danger"
            elif len(self.trail) == STUCK_SPAN and len(set(self.trail)) <= STUCK_TILES and not awake and not (self.rest and hp_word(g) != "full"):
                trigger, self.kind = "the hero seems stuck (pacing between the same few tiles for 40 turns)", "stuck"
                self.trail = []
            elif g.turn - self.last_turn >= PERIOD:
                trigger, self.kind = "periodic check", "periodic"
        self.hp_band, self.hunger = hp, hunger
        self.known.update(m["id"] for m in awake)
        if trigger and g.turn - self.last_turn < MIN_GAP and not trigger.startswith("arrived"):
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
            d, sec, tokens = self.asker(situation(g, trigger, self), self.model, self.effort)
        except Exception as e:  # noqa: BLE001  失敗しても Laya は動き続ける
            self.errors += 1
            if self.errors >= 3:
                self.stopped = f"失敗が続いた: {e}"
            return {"trigger": trigger, "kind": self.kind, "error": str(e)[:200]}
        self.errors = 0
        self.seconds += sec
        self.tokens += tokens
        self.plan, self.rest, self.tactic, self.heal_now, self.reason = d["plan"], d["rest"], d["tactic"], d["heal_now"], d["reason"]
        self.history.append({"turn": g.turn, "depth": g.depth, "hp": g.hp, "max_hp": g.max_hp, "level": g.level, "kind": self.kind,
                             "trigger": trigger, **d, "sec": round(sec, 1)})
        return {"trigger": trigger, "kind": self.kind, **d, "sec": round(sec, 1), "tokens": tokens, "calls": self.calls}

    # ------------------------------------------------------------------ 選択肢を絞る
    def allowed(self, g, valid):
        if not self.enabled or self.stopped or len(valid) <= 1:
            return valid
        if self.heal_now:
            self.heal_now = False
            if "quaff_heal" in valid:
                return ["quaff_heal"]
        awake = any(m["awake"] for m in g.visible_monsters())
        drop = set()
        if awake:
            if self.tactic == "fight":
                drop |= {"flee", "rest"}
            elif self.tactic == "flee":
                drop |= {"attack", "approach"}
        elif self.rest and g.hp < 0.9 * g.max_hp and g.hunger_word() in ("fine", "hungry"):
            drop |= {"approach", "pick_up", "explore", "descend"}
        if self.plan == "explore_fully" and "explore" in valid:
            drop.add("descend")
        elif self.plan == "descend_asap" and "descend" in valid and not awake:
            drop |= {"explore", "pick_up"}
        kept = [a for a in valid if a not in drop]
        return kept or valid


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
        self.name = f"{getattr(brain, 'name', 'brain')}+llm"

    def decide(self, g):
        st = self.strategist
        if g is not self.game:  # 新しいゲームが始まった
            self.game = g
            st.reset()
        trigger = st.check(g)
        advice = st.consult(g, trigger) if trigger else None
        valid = g.valid_actions()
        d = self.brain.decide(Masked(g, st.allowed(g, valid)))
        st.recent = (st.recent + [d["action"]])[-40:]
        d["advice"] = advice
        return d
