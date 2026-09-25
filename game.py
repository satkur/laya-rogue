"""本家 Rogue 5.4.4 のルールに基づくターン制ローグライク。描画も AI も持たない。

数値と規則は Rogue 5.4.4 を参照した独自実装 (rogue_data.py と THIRD_PARTY_NOTICES.md)。
勇者の 1 ターンは「高レベル行動」(ACTIONS) を 1 つ選ぶこと。どのマスへ動くかといった幾何の計算は
ここで行い、「今なにをすべきか」の判断だけを外 (Laya) に委ねる。

本家の要素は全部入っている (NOTES.md 15 章、2026-09-24): 薬 14 種・巻物 18 種・杖 14 種・指輪 14 種、呪いと解呪、罠 7 種、隠し扉と隠し通路、
迷路部屋、宝物部屋、ドラゴンの炎、ファントムの透明化、ゼロックの擬態、26 階の魔除けと 1 階への帰還、持ち物 23 枠。
難易度 (NORMAL / HARD / ORIGINAL) は rogue_data.DIFFICULTIES の切替表で、Game は self.rules に持つ。ORIGINAL は本家そのもの。
NORMAL は判断が生まれない運の要素 (隠し扉・隠し通路・迷路・擬態・盲目と幻覚の薬) を出さず、識別を簡略 (識別の巻物 1 種、使えば判明、
装備の ± が見える) にし、26 階到達で勝ち。HARD は内容は ORIGINAL で識別だけ簡略。
操作の簡略化はしない (判断ボードの回答): 有害と分かった物も持つ (使う行動に出ないだけ)、強化の巻物も Laya が読む、弓は構えてから射る。
どの未識別の物を試すか、どの指輪を着けるかといった「どれを」はプログラムが決め、「するかしないか」が判断。
"""
import copy
import os
import random
from collections import deque

import rogue_data as D

W, H = 80, 24
ROCK, FLOOR, STAIRS, PASSAGE, DOOR, RWALL, SDOOR, SPASS = 0, 1, 2, 3, 4, 5, 6, 7   # SDOOR = 隠し扉 (見つかるまで壁と同じ)、SPASS = 隠し通路 (岩と同じ)
MAZE_W, MAZE_H = 23, 5   # 迷路部屋の大きさ (区画 26 × 8 のうち。奇数にして 2 マス刻みの格子を掘る)
PASSABLE = (FLOOR, STAIRS, PASSAGE, DOOR)
DIRS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
DIRS4 = [(0, -1), (-1, 0), (1, 0), (0, 1)]
LOOSE = os.environ.get("LAYA_LOOSE", "1") == "1"  # 加速・転移・杖 (眠った敵) を持っていれば常時選べる (2026-09-25 のユーザー回答。0 は学習の変種比較用)
VS_POISON, VS_MAGIC = 0, 3
LAMP_DIST = 3

ACTIONS = ["attack", "throw", "approach", "flee", "quaff_heal", "quaff_str", "read_map", "read_teleport",
           "quaff_unknown", "read_unknown", "read_identify", "read_remove_curse", "put_on_ring", "remove_ring",
           "read_confuse", "read_hold", "drop_scare", "read_food",
           "quaff_haste", "quaff_raise", "quaff_see_invisible", "quaff_detect_monsters", "quaff_detect_magic",
           "read_enchant_armor", "read_enchant_weapon", "read_protect",
           "zap_bolt", "zap_missile", "zap_slow", "zap_away", "zap_polymorph", "zap_drain", "zap_cancel", "zap_light", "zap_unknown",
           "wield_bow", "wield_melee", "drop",
           "eat", "pick_up", "equip", "explore", "search", "descend", "ascend", "rest"]
ZAP_KIND = {"zap_missile": "magic missile", "zap_slow": "slow monster", "zap_away": "teleport away", "zap_polymorph": "polymorph",
            "zap_drain": "drain life", "zap_cancel": "cancellation", "zap_light": "light"}   # zap_bolt は稲妻・炎・冷気のどれか、zap_unknown は未識別


def parse_dice(s):
    return [tuple(int(v) for v in part.split("x")) for part in s.split("/")] if s and "%" not in s else [(0, 0)]


def avg_dice(dice):
    return sum(n * (s + 1) / 2 for n, s in dice)


def active(m):
    """起きていて動ける敵 (拘束の巻物で止まった敵は脅威ではない。殴ると解ける)。"""
    return m["awake"] and not m.get("held")


# 一定ターンで切れる勇者の状態 (daemons.c の fuse)。名前 -> 切れたときの言葉
FUSES = {"hasted": "動きが元に戻った", "levitating": "床に降りた", "blind": "目が見えるようになった", "hallucinating": "頭がはっきりした",
         "see_invisible": "", "detecting": ""}


HERO_FIELDS = ("kills", "gold", "level", "exp", "str", "max_str", "hp", "max_hp", "food_left", "food", "potions",
               "weapon", "armor", "gear", "no_food", "missiles", "scrolls", "bow", "known", "sticks", "rings", "worn", "glowing", "melee",
               "tried", "amulet", "max_depth")


class Game:
    def __init__(self, seed=None, start=None, difficulty=None):
        """start に hero_state() の結果を渡すと、その勇者でその階から始める (学習で深い階を練習するため)。
        seed を省略すると乱数で決めて self.seed に残す (デモの地図をあとから sim.py --seeds で再現するため)。
        difficulty は "normal" / "hard" / "original" (rogue_data.DIFFICULTIES)。省略すると start の難易度、それも無ければ normal。"""
        if seed is None:
            seed = random.randrange(1_000_000)
        self.seed = seed
        self.rules = D.rules(difficulty or (start["difficulty"] if start else None) or "normal")
        self.rng = random.Random(seed)
        self.depth = 0
        self.turn = 0
        self.kills = 0
        self.gold = 0
        self.level, self.exp = 1, 0
        self.str = self.max_str = D.INIT_STR
        self.hp = self.max_hp = D.INIT_HP
        self.food_left = D.HUNGER_TIME
        self.food = 1
        self.potions = {}                       # 種類 -> 個数
        name, dmg, hplus, dplus = D.INIT_WEAPON
        self.weapon = dict(name=name, dice=parse_dice(dmg), hplus=hplus, dplus=dplus, known=True)
        self.armor = dict(name=D.INIT_ARMOR[0], ac=D.INIT_ARMOR[1], known=True)
        self.gear = []                          # 拾ったが装備していない武器・防具
        self.pick_kind = None                   # 方針役の fetch: この種類の品を優先して拾いに行く (None なら良い装備 → 最寄りの順)
        self.bow = True                         # 弓を持っているか (初期装備)。構える (wield_bow) と矢に弓の威力が乗り、殴りは 1d1 になる
        self.melee = None                       # 弓を構えているあいだ、しまってある近接武器
        self.missiles = {"arrow": D.INIT_ARROWS[0] + self.rnd(D.INIT_ARROWS[1])}  # 投げる物: 名前 -> 本数
        self.scrolls = {}                       # 巻物: 名前 -> 枚数
        self.sticks = {}                        # 杖: 種類 -> 残り回数 (同じ種類は合算)
        self.rings = []                         # 持っている指輪 (着けていない): {name, value, cursed}
        self.worn = []                          # 着けている指輪 (最大 2)。呪われていると外せない
        self.known = set()                      # 正体の分かった薬・巻物・杖・指輪の種類 (未識別の仕組み)
        self.tried = set()                      # 使ったが正体の分からなかった種類 (ORIGINAL: 効果を観測できなかったとき)
        self.amulet = False                     # 魔除けを持っているか (HARD / ORIGINAL: 26 階以降で拾い、1 階へ帰還すると勝ち)
        self.max_depth = 0                      # 到達した最深の階
        self.names = self._item_names(seed)     # 未識別のあいだの見た目 (薬の色、巻物の題名)。表示にだけ使う
        self.no_command = 0                     # 凍結・気絶で動けない残りターン
        self.no_move = 0                        # 熊の罠で歩けない残りターン (攻撃や薬は使える)
        self._fell = False                      # 落とし穴で階を降りた直後 (その手はモンスターが動かない)
        self.confused = 0
        self.glowing = False                    # 怪物混乱の巻物を読んだ (次に当てた相手が混乱する)
        self.fuses = {k: 0 for k in FUSES}      # 加速・浮遊・盲目・幻覚・透明視・怪物探知の残りターン
        self._extra_move = False                # 加速中: このターンは勇者の 2 手目が残っている
        self.floor_turn = 0                     # この階に着いたターン
        self.held_by = None                     # ハエトリグサに捕まっているとき、その id
        self.vf_hit = 0
        self.quiet = 0                          # 自然回復のカウンタ
        self.no_food = 0
        self.dead = self.won = False
        self.cause = ""
        self.log = []
        self._next_id = 0
        self.explored = 0
        self.wander_fuse = self.spread(D.WANDER_TIME)
        self.between = 0
        if start:
            for k in HERO_FIELDS:
                setattr(self, k, copy.deepcopy(start[k]))
            self.depth = start["depth"] - 1
        self.new_floor()

    def hero_state(self):
        return {"depth": self.depth, "difficulty": self.rules.name, **{k: copy.deepcopy(getattr(self, k)) for k in HERO_FIELDS}}

    @property
    def hasted(self):
        return self.fuses["hasted"] > 0

    @property
    def levitating(self):
        return self.fuses["levitating"] > 0

    @property
    def blind(self):
        return self.fuses["blind"] > 0

    @property
    def hallucinating(self):
        return self.fuses["hallucinating"] > 0

    def can_see_invisible(self):
        return self.fuses["see_invisible"] > 0 or self.wearing("see invisible") > 0

    def detecting(self):
        return self.fuses["detecting"] > 0

    @staticmethod
    def _item_names(seed):
        """薬の色と巻物の題名をゲームごとに割り当てる (本家の rainbow と sylls)。本編の乱数列は消費しない (同じシードなら同じ地図のまま)。"""
        r = random.Random(seed)
        names = dict(zip(sorted(D.POTIONS_IN_PLAY), r.sample(D.POTION_COLORS, len(D.POTIONS_IN_PLAY))))
        for kind in sorted(D.SCROLLS_IN_PLAY):
            names[kind] = " ".join(r.choice(D.SCROLL_SYLLABLES) for _ in range(r.randrange(2, 4)))
        names.update(zip(sorted(D.STICKS_IN_PLAY), r.sample(D.STICK_MATERIALS, len(D.STICKS_IN_PLAY))))
        names.update(zip(sorted(D.RING_KINDS), r.sample(D.RING_STONES, len(D.RING_KINDS))))
        return names

    def label(self, kind):
        """薬・巻物の呼び名。正体が分かっていれば種類名、まだなら色か題名。"""
        if kind in D.POTIONS_IN_PLAY:
            return f"{D.POTION_JP[kind]}の薬" if kind in self.known else f"{self.names[kind]}色の薬"
        if kind in D.STICKS_IN_PLAY:
            return f"{D.STICK_JP[kind]}の杖" if kind in self.known else f"{self.names[kind]}の杖"
        if kind in D.RING_KINDS:
            return f"{D.RING_JP[kind]}の指輪" if kind in self.known else f"{self.names[kind]}の指輪"
        return f"{D.SCROLL_JP[kind]}の巻物" if kind in self.known else f"「{self.names[kind]}」の巻物"

    def ring_label(self, r):
        s = self.label(r["name"])
        if r["name"] in self.known and r["name"] in D.RING_VALUED:
            s += f" {r['value']:+d}"
        return s

    # ------------------------------------------------------------------ 乱数 (本家と同じ語彙)
    def rnd(self, n):
        return self.rng.randrange(n) if n > 0 else 0

    def roll(self, n, sides):
        return sum(self.rnd(sides) + 1 for _ in range(n))

    def spread(self, n):
        return n - n // 20 + self.rnd(n // 10)  # misc.c: nm - nm / 20 + rnd(nm / 10)

    def save(self, which):
        return self.roll(1, 20) >= 14 + which - self.level // 2

    @property
    def over(self):
        return self.dead or self.won

    def say(self, msg):
        self.log.append(msg)

    def clone(self, seed):
        """先読みシミュレーション用の複製。乱数だけ別系列にして「ありえた未来」を分岐させる。"""
        c = Game.__new__(Game)
        c.__dict__.update(self.__dict__)
        c.rng = random.Random(seed)
        c.seen = [row[:] for row in self.seen]  # tiles と _nbr は隠し扉を見つけたときだけ書き換える (そのとき複製する) ので共有でよい
        c.searched = dict(self.searched)
        c.monsters = [dict(m) for m in self.monsters]
        c.traps = [dict(t) for t in self.traps]
        c.items = [dict(i) for i in self.items]
        c.potions = dict(self.potions)
        c.known, c.tried = set(self.known), set(self.tried)
        c.sticks = dict(self.sticks)
        c.rings, c.worn = [dict(r) for r in self.rings], [dict(r) for r in self.worn]
        c.fuses = dict(self.fuses)
        c.missiles, c.scrolls = dict(self.missiles), dict(self.scrolls)
        c.weapon, c.armor = dict(self.weapon), dict(self.armor)
        c.melee = dict(self.melee) if self.melee else None
        c.gear = [dict(x) for x in self.gear]
        c.visible = set(self.visible)
        c.newly_seen = []
        c.log = []
        c._dist = None
        c._explore = list(self._explore)
        c._goal = (list(self._goal[0]), self._goal[1])
        c._search_path = list(self._search_path)
        return c

    # ------------------------------------------------------------------ 階の生成 (3×3 の区画に部屋、全域木 + 余分な通路)
    def new_floor(self, up=False):
        rng = self.rng
        self.depth += -1 if up else 1
        self.max_depth = max(self.max_depth, self.depth)
        if self.rules.amulet:
            if up and self.depth == 0:  # 魔除けを持って地上へ (command.c の u_level → total_winner)
                self.won = True
                self.say("魔除けを持って地上に出た！")
                return
        elif self.depth >= D.GOAL_DEPTH:
            self.won = True
            self.say(f"地下 {self.depth} 階に到達した！")
        self.no_food += 1
        self.held_by, self.vf_hit, self.no_move = None, 0, 0
        self.floor_turn = self.turn
        self.fuses["detecting"] = 0  # 怪物探知はその階だけ
        while True:  # どの部屋にも歩いて行ける地図ができるまで作り直す (保険。通常は 1 回で通る)
            self._build_map()
            if self._all_joined():
                break
        for y in range(H):  # 隠し扉 (rooms.c の door) と隠し通路 (passages.c の putpass)。つながっていることを確かめたあとで隠す
            for x in range(W):
                if self.rules.hidden_doors and self.tiles[y][x] == DOOR and self.rnd(10) + 1 < self.depth and self.rnd(5) == 0:
                    self.tiles[y][x] = SDOOR
                elif (self.rules.hidden_passages and self.tiles[y][x] == PASSAGE and self.rnd(10) + 1 < self.depth and self.rnd(40) == 0
                      and not self.room_at(x, y) and not any((rm := self.room_at(x + dx, y + dy)) and rm.get("maze") for dx, dy in DIRS4)):
                    # 迷路と欠けた部屋の中と、迷路に接するマスは隠さない (捜索の巡回が迷路の袋小路を探さないため。ORIGINAL 種 5080 の 16 階で行き詰まった)
                    self.tiles[y][x] = SPASS
        self._populate()

    def _build_map(self):
        rng = self.rng
        self.tiles = [[ROCK] * W for _ in range(H)]
        self.rooms = []
        gone = set(rng.sample(range(9), self.rnd(4)))
        for i in range(9):
            left, top = (i % 3) * 26 + 1, (i // 3) * 8 or 1
            if i in gone:
                x, y = left + self.rnd(24) + 1, top + 1 + self.rnd((i // 3) * 8 + 5 - top)
                self.rooms.append(dict(x=x, y=y, w=1, h=1, gone=True, dark=True, gold=False))
                self.tiles[y][x] = PASSAGE
                continue
            w, h = self.rnd(22) + 4, self.rnd(4) + 4
            # 区画の右端の列と下端の行は岩のまま残す。隣の区画の部屋と壁が接すると、扉どうしをつなぐ通路を掘れない
            h = min(h, (i // 3) * 8 + 7 - top)
            x, y = left + self.rnd(26 - w), top + self.rnd((i // 3) * 8 + 8 - top - h)
            dark = self.rnd(10) < self.depth - 1
            if dark and self.rules.mazes and self.rnd(15) == 0:  # 迷路部屋 (rooms.c): 区画いっぱいの通路の迷路。扉はない
                room = dict(x=left, y=top, w=MAZE_W, h=MAZE_H, gone=False, dark=True, maze=True, gold=False)
                self.rooms.append(room)
                self._dig_maze(room)
                continue
            room = dict(x=x, y=y, w=w, h=h, gone=False, dark=dark, gold=False)
            self.rooms.append(room)
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    edge = yy in (y, y + h - 1) or xx in (x, x + w - 1)
                    self.tiles[yy][xx] = RWALL if edge else FLOOR
        self._passages()

    def _dig_maze(self, r):
        """迷路 (rooms.c の do_maze): 2 マス刻みの格子点を深さ優先で掘り、隣の点との間も通路にする。全部の点がつながる。"""
        x0, y0 = r["x"], r["y"]
        cols, rows = (r["w"] + 1) // 2, (r["h"] + 1) // 2
        start = (self.rnd(cols), self.rnd(rows))
        visited, stack = {start}, [start]
        self.tiles[y0 + 2 * start[1]][x0 + 2 * start[0]] = PASSAGE
        while stack:
            cx, cy = stack[-1]
            nbrs = [(cx + dx, cy + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    if 0 <= cx + dx < cols and 0 <= cy + dy < rows and (cx + dx, cy + dy) not in visited]
            if not nbrs:
                stack.pop()
                continue
            nx, ny = nbrs[self.rnd(len(nbrs))]
            visited.add((nx, ny))
            self.tiles[y0 + cy + ny][x0 + cx + nx] = PASSAGE
            self.tiles[y0 + 2 * ny][x0 + 2 * nx] = PASSAGE
            stack.append((nx, ny))

    def _all_joined(self):
        starts = [(r["x"] + (0 if r["gone"] or r.get("maze") else 1), r["y"] + (0 if r["gone"] or r.get("maze") else 1)) for r in self.rooms]
        seen, q = {starts[0]}, deque([starts[0]])
        while q:
            x, y = q.popleft()
            for dx, dy in DIRS:
                n = (x + dx, y + dy)
                if n not in seen and self._step_ok(x, y, *n):
                    seen.add(n)
                    q.append(n)
        return all(p in seen for p in starts)

    def _populate(self):
        rng = self.rng
        self.monsters, self.items = [], []
        real = [r for r in self.rooms if not r["gone"]]
        taken = set()
        start = rng.choice(real)
        self.hx, self.hy = self._floor_spot(start, taken)
        self.stairs = self._floor_spot(rng.choice(real), taken)
        self.tiles[self.stairs[1]][self.stairs[0]] = STAIRS
        for r in real:
            if self.rnd(2) == 0:
                r["gold"] = True
                x, y = self._floor_spot(r, taken)
                self.items.append(dict(kind="gold", x=x, y=y, value=self.rnd(50 + 10 * self.depth) + 2))
            if self.rnd(100) < (80 if r["gold"] else 25):
                x, y = self._floor_spot(r, taken)
                self._spawn(self._rand_monster(False), x, y)
        if not (self.amulet and self.depth < self.max_depth):  # 魔除けを持って戻る階には新しい品物は出ない (new_level.c の put_things)
            if self.rules.treasure_rooms and self.rnd(D.TREAS_ROOM) == 0:
                self._treasure_room(real, taken)
            for _ in range(D.MAX_OBJ):
                if self.rnd(100) < 36:
                    thing = self._new_thing()
                    if thing:
                        thing["x"], thing["y"] = self._floor_spot(rng.choice(real), taken)
                        self.items.append(thing)
            if self.rules.amulet and self.depth >= D.AMULET_LEVEL and not self.amulet:  # 魔除けは 26 階以降に必ず落ちている
                x, y = self._floor_spot(rng.choice(real), taken)
                self.items.append(dict(kind="amulet", x=x, y=y))
        self.searched = {}                      # 捜索した回数 (マスごと)
        self.traps = []
        if self.rnd(10) < self.depth:
            for _ in range(min(D.MAXTRAPS, self.rnd(self.depth // 4) + 1)):
                kind, jp = D.TRAPS[self.rnd(len(D.TRAPS))]
                x, y = self._floor_spot(rng.choice(real), taken)
                self.traps.append(dict(kind=kind, jp=jp, x=x, y=y, found=False))
        self.seen = [[False] * W for _ in range(H)]
        self.mapped = set()                     # 魔法の地図で知ったマス。歩く経路と階段の位置には使えるが、探索の「見た」には数えない
        self.visible = set()
        self.newly_seen = []
        self._dist = None
        self._explore, self._goal = [], ([], None)
        # 歩けるマスごとの「1 歩で行けるマス」と「周囲 8 マス」。階の途中で変わらないので複製とも共有する
        cells = [(x, y) for y in range(H) for x in range(W) if self.tiles[y][x] in PASSABLE]
        self._nbr = {c: tuple((c[0] + dx, c[1] + dy) for dx, dy in DIRS if self._step_ok(c[0], c[1], c[0] + dx, c[1] + dy))
                     for c in cells}
        self._nb8 = {c: tuple((c[0] + dx, c[1] + dy) for dx, dy in DIRS if 0 <= c[0] + dx < W and 0 <= c[1] + dy < H)
                     for c in cells}
        self._wall_spots = self._find_wall_spots()
        self._search_path = []
        self._look()
        if not self.won:
            self.say(f"地下 {self.depth} 階")

    def _treasure_room(self, real, taken):
        """宝物部屋 (new_level.c の treas_room): 部屋を 1 つ選び、品物を rnd(spots) + 2 個 (最大 10)、モンスターをそれより 2 体以上多く
        1 階深い出現表から置く。全員 mean (見かけると襲ってくる) で持ち物つき。扉は普通 (本家も特別な扉はない)。"""
        r = self.rng.choice(real)
        floor = (r["w"] - 2) * (r["h"] - 2)
        spots = min(floor - D.MINTREAS, D.MAXTREAS - D.MINTREAS)
        n_items = self.rnd(spots) + D.MINTREAS
        for _ in range(n_items):
            thing = self._new_thing()
            if thing:
                thing["x"], thing["y"] = self._floor_spot(r, taken)
                self.items.append(thing)
        nm = min(max(self.rnd(spots) + D.MINTREAS, n_items + 2), floor)
        self.depth += 1
        for _ in range(nm):
            x, y = self._floor_spot(r, taken)
            self._spawn(self._rand_monster(False), x, y)
            m = self.monsters[-1]
            if "M" not in m["flags"]:
                m["flags"] += "M"
        self.depth -= 1
        r["treasure"] = True

    def _floor_spot(self, room, taken):
        for _ in range(60):
            if room.get("maze"):
                p = (room["x"] + self.rnd(room["w"]), room["y"] + self.rnd(room["h"]))
            else:
                p = (room["x"] + 1 + self.rnd(room["w"] - 2), room["y"] + 1 + self.rnd(room["h"] - 2))
            if p not in taken and self.tiles[p[1]][p[0]] in (FLOOR, PASSAGE):
                break
        taken.add(p)  # 2×2 の部屋が埋まっているときは重ねて置く
        return p

    def _passages(self):
        adj = {i: [j for j in (i - 3, i + 3, i - 1 if i % 3 else -1, i + 1 if i % 3 != 2 else -1) if 0 <= j < 9] for i in range(9)}
        joined, linked = {self.rnd(9)}, set()
        while len(joined) < 9:
            a = self.rng.choice([i for i in joined if any(j not in joined for j in adj[i])])
            b = self.rng.choice([j for j in adj[a] if j not in joined])
            self._connect(a, b)
            joined.add(b)
            linked.add(frozenset((a, b)))
        for _ in range(self.rnd(5)):
            a = self.rnd(9)
            free = [j for j in adj[a] if frozenset((a, j)) not in linked]
            if free:
                b = self.rng.choice(free)
                self._connect(a, b)
                linked.add(frozenset((a, b)))

    def _connect(self, a, b):
        if a > b:
            a, b = b, a
        ra, rb = self.rooms[a], self.rooms[b]
        down = b - a == 3

        def door(r, side):
            if r["gone"]:
                return r["x"], r["y"]
            if r.get("maze"):  # 迷路に扉はない: その辺にある迷路の通路に直接つなぐ (passages.c の door)
                if side in ("bottom", "top"):
                    yy = r["y"] + r["h"] - 1 if side == "bottom" else r["y"]
                    cands = [(xx, yy) for xx in range(r["x"], r["x"] + r["w"]) if self.tiles[yy][xx] == PASSAGE]
                else:
                    xx = r["x"] + r["w"] - 1 if side == "right" else r["x"]
                    cands = [(xx, yy) for yy in range(r["y"], r["y"] + r["h"]) if self.tiles[yy][xx] == PASSAGE]
                return cands[self.rnd(len(cands))]
            if side == "bottom":
                p = (r["x"] + 1 + self.rnd(r["w"] - 2), r["y"] + r["h"] - 1)
            elif side == "top":
                p = (r["x"] + 1 + self.rnd(r["w"] - 2), r["y"])
            elif side == "right":
                p = (r["x"] + r["w"] - 1, r["y"] + 1 + self.rnd(r["h"] - 2))
            else:
                p = (r["x"], r["y"] + 1 + self.rnd(r["h"] - 2))
            self.tiles[p[1]][p[0]] = DOOR
            return p

        (x0, y0), (x1, y1) = (door(ra, "bottom"), door(rb, "top")) if down else (door(ra, "right"), door(rb, "left"))
        if down:
            turn = y0 + 1 + self.rnd(max(1, y1 - y0 - 1))
            path = [(x0, y) for y in range(y0 + 1, turn + 1)] + [(x, turn) for x in range(min(x0, x1), max(x0, x1) + 1)] \
                + [(x1, y) for y in range(turn, y1)]
        else:
            turn = x0 + 1 + self.rnd(max(1, x1 - x0 - 1))
            path = [(x, y0) for x in range(x0 + 1, turn + 1)] + [(turn, y) for y in range(min(y0, y1), max(y0, y1) + 1)] \
                + [(x, y1) for x in range(turn, x1)]
        for x, y in path:
            if self.tiles[y][x] == ROCK:
                self.tiles[y][x] = PASSAGE

    # ------------------------------------------------------------------ モンスターとアイテムの生成
    def _rand_monster(self, wander):
        table = D.WANDER_MONSTERS if wander else D.LEVEL_MONSTERS
        while True:
            d = self.depth + self.rnd(10) - 6
            if d < 0:
                d = self.rnd(5)
            if d > 25:
                d = self.rnd(5) + 21
            if table[d] != " ":
                return table[d]

    def _spawn(self, ch, x, y, awake=False):
        name, jp, carry, flags, exp, lvl, arm, dmg = D.MONSTERS[ch]
        add = max(0, self.depth - D.AMULET_LEVEL)
        lvl, arm = lvl + add, arm - add
        hp = self.roll(lvl, 8)
        mod = hp // 8 if lvl == 1 else hp // 6
        mod *= 20 if lvl > 9 else 4 if lvl > 6 else 1
        self._next_id += 1
        if self.wearing("aggravate monster"):  # 怪物寄せの指輪: 新しいモンスターも最初から追ってくる (monsters.c の new_monster)
            awake = True
        m = dict(id=self._next_id, ch=ch, kind=name, jp=jp, x=x, y=y, hp=hp, max_hp=hp, lvl=lvl, arm=arm,
                 dice=parse_dice(dmg), exp=exp + add * 10 + mod, flags=flags, carry=carry, awake=awake, gazed=False)
        if self.depth > 29:  # 30 階より深いとモンスターは加速 (monsters.c)
            m["haste"] = True
        if ch == "X" and self.rules.xeroc_disguise and not awake:  # ゼロックは品物に化けて動かない。踏み込むと正体を現す
            m["disguise"] = self._fake_item()
        self.monsters.append(m)

    def _fake_item(self):
        for _ in range(10):
            it = self._new_thing()
            if it:
                return it
        return dict(kind="food")

    def _pick(self, table):
        r = self.rnd(sum(p for _, p, *_ in table))
        for entry in table:
            r -= entry[1]
            if r < 0:
                return entry

    def _new_thing(self):
        """本家の出現比率で 1 つ引く。未実装の種類を引いたら何も出ない (= 本家より物資が少ない)。"""
        kind = "food" if self.no_food > 3 else self._pick(D.THING_PROBS)[0]
        if kind == "food":
            self.no_food = 0
            return dict(kind="food")
        if kind == "potion":
            name = self._pick(D.POTION_PROBS)[0]
            return dict(kind="potion", name=name) if name not in self.rules.potions_out else None
        if kind == "scroll":
            name = self._pick(D.SCROLL_PROBS)[0]
            if name.startswith("identify") and self.rules.identify == "single":  # 本家の識別 5 種を 1 種にまとめる (NORMAL / HARD)
                name = "identify"
            return dict(kind="scroll", name=name)
        if kind == "weapon":
            name, _, dmg, hurl, launcher = self._pick(D.WEAPONS)
            if name == "short bow":
                return dict(kind="bow", name=name)
            if name in D.MISSILES:
                return dict(kind="missile", name=name, count=self.rnd(8) + 8 if name in D.STACKED else 1)
            r = self.rnd(100)  # 10% は呪い (命中 −1〜−3、装備すると外せない)、5% は +1〜+3 (things.c)
            hplus = -(self.rnd(3) + 1) if r < 10 else self.rnd(3) + 1 if r < 15 else 0
            return dict(kind="weapon", name=name, dice=parse_dice(dmg), hplus=hplus, dplus=0, cursed=r < 10, known=self.rules.gear_known)
        if kind == "armor":
            name, _, ac = self._pick(D.ARMORS)
            r = self.rnd(100)  # 20% は呪い (防御が 1〜3 悪い、着ると脱げない)、8% は 1〜3 良い
            ac += self.rnd(3) + 1 if r < 20 else -(self.rnd(3) + 1) if r < 28 else 0
            return dict(kind="armor", name=name, ac=ac, cursed=r < 20, known=self.rules.gear_known)  # ORIGINAL: 着るまで ± は分からない
        if kind == "stick":
            name = self._pick(D.STICK_PROBS)[0]
            charges = self.rnd(10) + 10 if name == "light" else self.rnd(D.STICK_CHARGES[0]) + D.STICK_CHARGES[1]
            return dict(kind="stick", name=name, charges=charges)
        if kind == "ring":
            name = self._pick(D.RING_PROBS)[0]
            value, cursed = 0, name in D.RING_CURSED
            if name in D.RING_VALUED:
                value = self.rnd(3)
                if value == 0:
                    value, cursed = -1, True
            return dict(kind="ring", name=name, value=value, cursed=cursed)
        return None

    # ------------------------------------------------------------------ 視界: 明るい部屋は全体、それ以外は隣のマスだけ
    def room_at(self, x, y):
        for r in self.rooms:
            if not r["gone"] and r["x"] <= x < r["x"] + r["w"] and r["y"] <= y < r["y"] + r["h"]:
                return r
        return None

    def _look(self):
        if self.blind:  # 盲目: 見えるのは自分のマスだけ (敵も品物も見えない)。周囲 8 マスの地形は手探りで既知になる (そうしないと壁の縁が残って探索が往復する)
            self.visible = {(self.hx, self.hy)}
            for x, y in self._nb8.get((self.hx, self.hy), ()) + ((self.hx, self.hy),):
                if not self.seen[y][x]:
                    self.seen[y][x] = True
                    self.newly_seen.append((x, y, RWALL if self.tiles[y][x] == SDOOR else ROCK if self.tiles[y][x] == SPASS else self.tiles[y][x]))
                    if self.tiles[y][x] != ROCK:
                        self.explored += 1
            return
        vis = {(self.hx + dx, self.hy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               if 0 <= self.hx + dx < W and 0 <= self.hy + dy < H}
        r = self.room_at(self.hx, self.hy)
        if r and not r["dark"]:
            vis.update((x, y) for y in range(r["y"], r["y"] + r["h"]) for x in range(r["x"], r["x"] + r["w"]))
        self.visible = vis
        for x, y in vis:
            if not self.seen[y][x]:
                self.seen[y][x] = True
                self.newly_seen.append((x, y, RWALL if self.tiles[y][x] == SDOOR else ROCK if self.tiles[y][x] == SPASS else self.tiles[y][x]))
                if self.tiles[y][x] != ROCK:
                    self.explored += 1

    def dist(self, x, y):
        return max(abs(x - self.hx), abs(y - self.hy))

    def can_see(self, m):
        """そのモンスターが見えているか。透明 (ファントム) は透明視がないと見えない。盲目のときは何も見えない。
        見えない相手でも、いま殴られた (felt) なら隣にいることは分かる (chase.c の see_monst)。"""
        if m.get("disguise"):  # 品物に見える
            return False
        if m.get("felt") == self.turn and self._adjacent(m):
            return True
        if (m["x"], m["y"]) not in self.visible or self.blind:
            return False
        return "I" not in m["flags"] or self.can_see_invisible()

    def visible_monsters(self):
        return sorted((m for m in self.monsters if self.can_see(m)), key=lambda m: self.dist(m["x"], m["y"]))

    def sensed_monsters(self):
        """怪物探知の薬で分かっている、見えていないモンスター (階全体)。"""
        if not self.detecting():
            return []
        return sorted((m for m in self.monsters if not self.can_see(m)), key=lambda m: self.dist(m["x"], m["y"]))

    def visible_items(self):
        items = [i for i in self.items if (i["x"], i["y"]) in self.visible or i.get("sensed")]
        if not self.blind:  # 化けたゼロックは品物に見える
            items += [dict(m["disguise"], x=m["x"], y=m["y"]) for m in self.monsters if m.get("disguise") and (m["x"], m["y"]) in self.visible]
        return sorted(items, key=lambda i: self.dist(i["x"], i["y"]))

    def wanted(self, it):
        """拾いに行く価値のある品か。一度持った恐怖の巻物 (拾うと塵になる) と、正体の分かった使い道のない物は違う。"""
        return not (it["kind"] == "scroll" and it["name"] == "scare monster" and it.get("found")) and not self.useless(it)

    def visible_loot(self):
        """状況文と回収の標的に使う品物 (足元に置いた恐怖の巻物は除く)。"""
        return [i for i in self.visible_items() if self.wanted(i)]

    # ------------------------------------------------------------------ 移動と経路
    def _step_ok(self, x0, y0, x1, y1):
        if not (0 <= x1 < W and 0 <= y1 < H) or self.tiles[y1][x1] not in PASSABLE:
            return False
        if x0 != x1 and y0 != y1:  # 斜め移動は、扉の出入りと角抜けができない
            if DOOR in (self.tiles[y0][x0], self.tiles[y1][x1]):
                return False
            return self.tiles[y0][x1] in PASSABLE and self.tiles[y1][x0] in PASSABLE
        return True

    def _bfs_path(self, goal_fn):
        """勇者から goal_fn を満たす最寄りマスへの経路。既知のマスだけを通り、見えているモンスターは避ける。

        見えていないモンスターまで避けると、扉の前で眠っている 1 体のせいで「探索先なし」になり、待機しかできなくなる。
        見えている眠ったモンスターも、それを避けると道がないときだけは通る (踏み込む 1 歩は攻撃になる)。
        避け続けると、唯一の通路で眠る 1 体のせいで探索先も階段もなくなり、餓死するまで足踏みする。"""
        seen = self.visible_monsters() + self.sensed_monsters()
        awake = {(m["x"], m["y"]) for m in seen if active(m)}
        asleep = {(m["x"], m["y"]) for m in seen if not active(m)}
        traps = {(t["x"], t["y"]) for t in self.traps if t["found"]}  # 踏んで分かった罠はよける (他に道がなければ踏んで通る)
        path = self._bfs(goal_fn, awake | asleep | traps)
        if path is None and asleep:
            path = self._bfs(goal_fn, awake | traps)
        if path is None and traps:
            path = self._bfs(goal_fn, awake)
        return path

    def _bfs(self, goal_fn, blocked):
        seen, nbr, mapped = self.seen, self._nbr, self.mapped
        start = (self.hx, self.hy)
        prev = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            if cur != start and goal_fn(cur):
                path = []
                while cur != start:
                    path.append(cur)
                    cur = prev[cur]
                return path[::-1]
            for n in nbr[cur]:
                if n not in prev and (seen[n[1]][n[0]] or n in mapped) and (n not in blocked or goal_fn(n)):
                    prev[n] = cur
                    q.append(n)
        return None

    def _step_toward(self, target):
        """target への 1 歩。経路は使い回し、無効になったときだけ探し直す (探索と同じ)。

        毎ターン引き直すと、眠った敵が見える位置では避ける遠回りの一歩、見えない位置では最短の一歩と入れ替わり、
        2 マスを永遠に往復して餓死した (rules で 500 回中 16 回、NOTES 13 章)。"""
        path, tgt = self._goal
        if path and path[0] == (self.hx, self.hy):  # 前のターンの一歩を踏んだ
            path = path[1:]
        if not (tgt == target and path and path[0] in self._nbr[(self.hx, self.hy)] and self._monster_at(*path[0]) is None):
            path = self._bfs_path(lambda c: c == target) or []
        self._goal = (path, target)
        return path[0] if path else None

    def _frontier(self, c):
        seen = self.seen
        return any(not seen[y][x] for x, y in self._nb8[c])

    def _explore_step(self):
        """未探索の縁への 1 歩。経路は使い回し、無効になったときだけ探し直す (毎ターン探すと全体の 4 割を食う)。"""
        path = self._explore
        if not (path and path[0] in self._nbr[(self.hx, self.hy)] and self._frontier(path[-1])
                and self._monster_at(*path[0]) is None):
            path = self._explore = self._bfs_path(self._frontier) or []
        return path[0] if path else None

    # ------------------------------------------------------------------ 隠し扉と捜索 (command.c の search)
    def _reveal(self, x, y):
        """隠し扉を扉に、隠し通路を通路にする。tiles と経路の隣接表は複製と共有しているので、書き換える前に自分の分を作る。"""
        found = DOOR if self.tiles[y][x] == SDOOR else PASSAGE
        self.tiles = [row[:] for row in self.tiles]
        self.tiles[y][x] = found
        self._nbr, self._nb8 = dict(self._nbr), dict(self._nb8)
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                if 0 <= cx < W and 0 <= cy < H and self.tiles[cy][cx] in PASSABLE:
                    self._nbr[(cx, cy)] = tuple((cx + dx, cy + dy) for dx, dy in DIRS if self._step_ok(cx, cy, cx + dx, cy + dy))
                    self._nb8[(cx, cy)] = tuple((cx + dx, cy + dy) for dx, dy in DIRS if 0 <= cx + dx < W and 0 <= cy + dy < H)
        if self.seen[y][x]:
            self.newly_seen.append((x, y, found))
        self._wall_spots = self._find_wall_spots()
        self._explore, self._search_path, self._goal = [], [], ([], None)
        self.say("隠し扉を見つけた" if found == DOOR else "隠れた通路を見つけた")

    def _find_wall_spots(self):
        """部屋の壁ぎわで捜索に立つマス。1 回の捜索が壁 3 マスぶんを調べるので、部屋の端から 3 マスおき (と端) に立つ。
        壁のすぐ外を既知の通路が通っていても扉があるとは限らない (通過しているだけのことが多い) ので、特別扱いしない。"""
        spots = set()
        for r in self.rooms:
            if r["gone"] or r.get("maze"):
                continue
            a_x, b_x, a_y, b_y = r["x"] + 1, r["x"] + r["w"] - 2, r["y"] + 1, r["y"] + r["h"] - 2
            for y in range(a_y, b_y + 1):
                for x in range(a_x, b_x + 1):
                    if self.tiles[y][x] != FLOOR:
                        continue
                    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                        if self.tiles[y + dy][x + dx] not in (RWALL, SDOOR):  # 隠し扉は壁に見えている
                            continue
                        along, a, b = (x, a_x, b_x) if dy else (y, a_y, b_y)
                        if (along - a) % 3 == 1 or along == b:
                            spots.add((x, y))
                            break
        return spots

    def _search_class(self, c):
        """捜索先の種類。0 = 通路の行き止まり (この地図では隠し扉か袋小路の節しかない)、1 = 部屋の壁ぎわ、None = 探す価値なし。"""
        if self.tiles[c[1]][c[0]] == PASSAGE:
            r = self.room_at(*c)
            if r and r.get("maze"):  # 迷路の袋小路は探さない (隠し扉・隠し通路は迷路の中には置かない)
                return None
            return 0 if sum(1 for n in self._nbr[c] if self.seen[n[1]][n[0]] or n in self.mapped) <= 1 else None
        return 1 if c in self._wall_spots else None

    def _search_ok(self, c, want):
        return self._search_class(c) == want and self.searched.get(c, 0) < D.SEARCH_CAPS[want]

    def _search_target(self):
        """いちばん近い捜索先への経路 (自分のマスならその場)。行き止まりを先に、次に壁ぎわ、それぞれ上限まで。
        全部使い切ったら、捜索した回数がいちばん少ない所から順に探し続ける (待って餓死するよりはよい)。
        経路は使い回し、無効になったときだけ探し直す (毎ターン探すと全体の 3 割を食う)。"""
        here = (self.hx, self.hy)
        path = self._search_path
        if path and path[0] in self._nbr[here] and self._monster_at(*path[0]) is None:
            goal = path[-1]
            want = self._search_class(goal)
            if want is not None and self.searched.get(goal, 0) < D.SEARCH_CAPS[want]:
                return path
        for want in (0, 1):
            if self._search_ok(here, want):
                self._search_path = []
                return []
            path = self._bfs_path(lambda c: self._search_ok(c, want))
            if path:
                self._search_path = path
                return path
        spots = [c for c in self._nbr if (self.seen[c[1]][c[0]] or c in self.mapped) and self._search_class(c) is not None]
        self._search_path = []
        if not spots:
            return None
        fewest = min(self.searched.get(c, 0) for c in spots)
        if self.searched.get(here, 0) == fewest and here in spots:
            return []
        return self._bfs_path(lambda c: c in spots and self.searched.get(c, 0) == fewest)

    def _search(self):
        here = (self.hx, self.hy)
        path = self._search_target()
        if path is None:
            return
        if path:
            self._move_to(path[0])
            if self._search_path and (self.hx, self.hy) == self._search_path[0]:
                self._search_path.pop(0)
            return
        self.searched[here] = self.searched.get(here, 0) + 1
        self._search_here()

    def _search_here(self):
        """周囲 8 マスの隠し扉と罠を、それぞれ 1/5 で見つける (command.c の search)。"""
        for nx, ny in self._nb8[(self.hx, self.hy)]:
            if self.tiles[ny][nx] in (SDOOR, SPASS) and self.rnd(5) == 0:
                self._reveal(nx, ny)
            t = self._trap_at(nx, ny)
            if t and not t["found"] and self.rnd(5) == 0:
                t["found"] = True
                self.say(f"{t['jp']}を見つけた")

    def _monster_at(self, x, y):
        return next((m for m in self.monsters if (m["x"], m["y"]) == (x, y)), None)

    def _hero_dist_map(self):
        """勇者からの歩数。モンスターの追跡用で、1 ターンに 1 回だけ計算する。"""
        if self._dist is None:
            nbr = self._nbr
            dist = {(self.hx, self.hy): 0}
            q = deque(dist)
            while q:
                cur = q.popleft()
                d = dist[cur] + 1
                if d > 40:
                    break
                for n in nbr[cur]:
                    if n not in dist:
                        dist[n] = d
                        q.append(n)
            self._dist = dist
        return self._dist

    # ------------------------------------------------------------------ 戦闘 (fight.c の swing / roll_em)
    @staticmethod
    def hit_chance(att_lvl, def_arm, hplus):
        need = (20 - att_lvl) - def_arm - hplus
        return min(1.0, max(0.0, (20 - need) / 20))

    def _swing(self, att_lvl, def_arm, hplus):
        return self.rnd(20) + hplus >= (20 - att_lvl) - def_arm

    def hero_damage_per_turn(self, m):
        hplus = self.weapon["hplus"] + D.STR_PLUS[self.str] + self.ring_bonus("dexterity") + (0 if m["awake"] else 4)
        per_hit = avg_dice(self.weapon["dice"]) + self.weapon["dplus"] + D.ADD_DAM[self.str] + self.ring_bonus("increase damage")
        return self.hit_chance(self.level, m["arm"], hplus) * max(0.0, per_hit)

    def monster_damage_per_turn(self, m):
        dice = [(self.vf_hit or 1, 1)] if m["ch"] == "F" else m["dice"]
        return sum(self.hit_chance(m["lvl"], self.ac(), 0) * n * (s + 1) / 2 for n, s in dice)

    def _hero_attacks(self, m):
        if m.pop("disguise", None):
            self.say("待て、それはゼロックだ！")
        hplus = self.weapon["hplus"] + D.STR_PLUS[self.str] + self.ring_bonus("dexterity") + (0 if m["awake"] else 4)
        m["awake"] = True
        m.pop("held", None)  # 拘束は殴ると解ける (chase.c の runto)
        hit = False
        for n, s in self.weapon["dice"]:
            if self._swing(self.level, m["arm"], hplus):
                m["hp"] -= max(0, self.roll(n, s) + self.weapon["dplus"] + D.ADD_DAM[self.str] + self.ring_bonus("increase damage"))
                hit = True
        if hit and self.glowing:  # 怪物混乱の巻物: 当てた相手が混乱する (fight.c)
            self.glowing = False
            m["confused"] = True
            self.say(f"手の光が消え、{m['jp']}は混乱した")
        if m["hp"] > 0:
            self.say(f"{m['jp']}に攻撃が{'当たった' if hit else '外れた'}")
            return
        self._kill(m)

    def _kill(self, m):
        self.monsters.remove(m)
        self.kills += 1
        self.say(f"{m['jp']}を倒した")
        if self.held_by == m["id"]:
            self.held_by, self.vf_hit = None, 0
        if m["ch"] == "L":
            value = self.rnd(50 + 10 * self.depth) + 2
            if self.save(VS_MAGIC):
                value += sum(self.rnd(50 + 10 * self.depth) + 2 for _ in range(4))
            self.items.append(dict(kind="gold", x=m["x"], y=m["y"], value=value))
        elif self.rnd(100) < m["carry"]:
            thing = self._new_thing()
            if thing:
                thing["x"], thing["y"] = m["x"], m["y"]
                self.items.append(thing)
        self.exp += m["exp"]
        self._check_level()

    def _check_level(self):
        new = next((i for i, need in enumerate(D.EXP_LEVELS) if need > self.exp), len(D.EXP_LEVELS)) + 1
        if new > self.level:
            add = self.roll(new - self.level, 10)
            self.max_hp += add
            self.hp += add
            self.say(f"レベル {new} に上がった")
        self.level = new

    def _rust(self):
        if self.wearing("maintain armor"):
            return
        if self.armor["name"] != "leather armor" and self.armor["ac"] < 9 and not self.armor.get("protected"):
            self.armor["ac"] += 1
            self.say("鎧が錆びて弱くなった")

    def _teleport(self):
        real = [r for r in self.rooms if not r["gone"]]
        taken = {(m["x"], m["y"]) for m in self.monsters} | {(self.hx, self.hy)}
        self.hx, self.hy = self._floor_spot(self.rng.choice(real), taken)
        self.held_by, self.vf_hit = None, 0
        self._explore, self._goal = [], ([], None)
        self._look()

    # ------------------------------------------------------------------ 罠 (move.c の be_trapped)
    def _trap_at(self, x, y):
        return next((t for t in self.traps if (t["x"], t["y"]) == (x, y)), None)

    def _spring(self, t):
        t["found"] = True
        k = t["kind"]
        if k == "trap door":
            self.say("落とし穴に落ちた！")
            self.new_floor()
            self._fell = True
        elif k == "bear trap":
            self.no_move += self.spread(D.BEARTIME)
            self.say("熊の罠に足を挟まれた")
        elif k == "sleeping gas":
            self.no_command += self.spread(D.SLEEPTIME)
            self.say("白い霧に包まれて眠ってしまった")
        elif k == "arrow trap":
            if self._swing(self.level - 1, self.ac(), 1):  # 本家は勇者のレベル (階ではない)
                self.hp -= self.roll(1, 6)
                self.say("矢が飛んできて刺さった")
            else:
                self.items.append(dict(kind="missile", name="arrow", count=1, x=self.hx, y=self.hy))
                self.say("矢が飛んできたがそれた")
        elif k == "teleport trap":
            self.say("転移の罠を踏んだ")
            self._teleport()
        elif k == "dart trap":
            if self._swing(self.level + 1, self.ac(), 1):
                self.hp -= self.roll(1, 4)
                if not self.save(VS_POISON) and self.str > 3 and not self.wearing("sustain strength"):
                    self.str -= 1
                    self.say("毒ダーツが刺さり、力が抜けた")
                else:
                    self.say("毒ダーツが刺さった")
            else:
                self.say("ダーツが飛んできたがそれた")
        elif k == "rust trap":
            self.say("水が降ってきた")
            self._rust()
        if self.hp <= 0:
            self.hp, self.dead, self.cause = 0, True, t["jp"]
            self.say(f"勇者は{t['jp']}で倒れた…")

    def _monster_attacks(self, m):
        self.quiet = 0
        who = m["jp"] if self.can_see(m) else "何か見えないもの"
        m["felt"] = self.turn  # 見えない相手でも、殴られればそこにいると分かる
        dice = [(self.vf_hit or 0, 1)] if m["ch"] == "F" else m["dice"]
        hit, before = False, self.hp
        for n, s in dice:
            if self._swing(m["lvl"], self.ac(), 0):
                self.hp -= max(0, self.roll(n, s))
                hit = True
        if not hit:
            self.say(f"{who}の攻撃は外れた")
            return
        if before > self.hp:
            self.say(f"{who}の攻撃！ {before - self.hp} ダメージ")
        ch = m["ch"] if not m.get("cancelled") else ""  # 無効化の杖: 特殊攻撃をしない (fight.c の ISCANC)
        if ch == "A":
            self._rust()
        elif ch == "I":
            self.no_command += self.rnd(2) + 2
            self.say("凍りついて動けない")
        elif ch == "R":
            if not self.save(VS_POISON) and self.str > 3 and not self.wearing("sustain strength"):
                self.str -= 1
                self.say("毒で力が抜けた")
        elif ch in "WV":
            if self.rnd(100) < (15 if ch == "W" else 30):
                fatal = ch == "W" and self.exp == 0  # 吸われるレベルがもうない
                if ch == "W":
                    self.level = max(1, self.level - 1)
                    self.exp = 0 if self.level == 1 else D.EXP_LEVELS[self.level - 2] + 1
                    fewer = self.roll(1, 10)
                else:
                    fewer = self.roll(1, 3)
                self.hp -= fewer
                self.max_hp -= fewer
                if fatal or self.max_hp <= 0:
                    self.hp, self.max_hp = 0, max(1, self.max_hp)  # 死ぬ。最大 HP を 0 のままにすると得点計算が 0 除算になる
                elif self.hp <= 0:
                    self.hp = 1
                self.say("急に体が弱くなった")
        elif ch == "F":
            self.held_by = m["id"]
            self.vf_hit += 1
            self.hp -= 1
            self.say("ハエトリグサに捕まった")
        elif ch == "L":
            before_gold = self.gold
            self.gold -= self.rnd(50 + 10 * self.depth) + 2
            if not self.save(VS_MAGIC):
                self.gold -= sum(self.rnd(50 + 10 * self.depth) + 2 for _ in range(4))
            self.gold = max(0, self.gold)
            self.monsters.remove(m)
            if self.gold != before_gold:
                self.say("財布が軽くなった")
        elif ch == "N":
            owned = [k for k, n in self.potions.items() if n > 0]
            if owned:
                k = self.rng.choice(owned)
                self.potions[k] -= 1
                self.monsters.remove(m)
                self.say("ニンフに薬を盗まれた")
        if self.hp <= 0:
            self.hp, self.dead, self.cause = 0, True, m["jp"]
            self.say(f"勇者は{m['jp']}に倒された…")

    # ------------------------------------------------------------------ 行動
    def melee_weapon(self):
        """近接武器 (弓を構えているあいだは、しまってある方)。"""
        return self.melee or self.weapon

    def wielding_bow(self):
        return self.weapon["name"] == "short bow"

    def gear_gain(self, it):
        """武器・防具 it を装備したときの得 (防具は防御の改善、武器は 1 撃の平均ダメージの改善)。それ以外の品は 0。"""
        if it["kind"] == "armor":
            ac = it["ac"] if it.get("known", True) else next(a for n, _, a in D.ARMORS if n == it["name"])  # ± が分からなければ素の値で比べる
            return self.armor["ac"] - ac
        if it["kind"] == "weapon":
            w = self.melee_weapon()
            plus = it["dplus"] + it["hplus"] * 0.5 if it.get("known", True) else 0
            return (avg_dice(it["dice"]) + plus) - (avg_dice(w["dice"]) + w["dplus"] + w["hplus"] * 0.5)
        return 0

    def _better_gear(self):
        """装備すれば得をする持ち物。着ている物が呪われていると分かっている枠は候補にしない (外せない)。弓を構えているときは武器も替えない。"""
        cands = [g for g in self.gear if not (self.armor if g["kind"] == "armor" else self.weapon).get("cursed_known")
                 and not (g["kind"] == "weapon" and self.wielding_bow())]
        best = max(cands, key=self.gear_gain, default=None)
        return best if best is not None and self.gear_gain(best) > 0 else None

    def visible_upgrades(self):
        """見えている品のうち、着ている物より良い武器・防具 (近い順)。"""
        return [i for i in self.visible_items() if self.gear_gain(i) > 0]

    def pick_target(self):
        """「回収」で向かう品。方針役の fetch があればその種類の最寄り、なければ良い装備、それも無ければ最寄りの品。"""
        items = self.visible_loot()
        if not items:
            return None
        if self.pick_kind:
            wanted = [i for i in items if i["kind"] == self.pick_kind]
            if wanted:  # 武器・防具なら、その種類のうち最も得な品 (最寄りが悪い方だと無駄足になる)
                return max(wanted, key=self.gear_gain) if self.pick_kind in ("armor", "weapon") else wanted[0]
        ups = [i for i in items if self.gear_gain(i) > 0]
        return ups[0] if ups else items[0]

    def explored_ratio(self):
        """この階の岩以外のマスのうち、見たことのある割合。"""
        total = seen = 0
        for y in range(H):
            for x in range(W):
                if self.tiles[y][x] != ROCK:
                    total += 1
                    seen += self.seen[y][x]
        return seen / max(1, total)

    def has_heal(self):
        """正体の分かっている回復薬の数。未識別の物は数えない (飲んでみるまで何か分からない)。"""
        return sum(self.potions.get(k, 0) for k in ("extra healing", "healing") if k in self.known)

    def has_potion(self, kind):
        """正体の分かっている薬の数。"""
        return self.potions.get(kind, 0) if kind in self.known else 0

    def has_str_potion(self):
        n = self.potions.get("gain strength", 0) if "gain strength" in self.known else 0
        if "restore strength" in self.known and self.base_str() < self.max_str:
            n += self.potions.get("restore strength", 0)
        return n

    # ------------------------------------------------------------------ 指輪 (rings.c)
    def wearing(self, kind):
        return sum(1 for r in self.worn if r["name"] == kind)

    def ring_bonus(self, kind):
        return sum(r["value"] for r in self.worn if r["name"] == kind)

    def ac(self):
        """実効の防御 (防御の指輪込み。fight.c)。小さいほど硬い。"""
        return self.armor["ac"] - self.ring_bonus("protection")

    def base_str(self):
        """力の指輪を差し引いた腕力。max_str と比べるときはこちら (misc.c の chg_str)。"""
        return self.str - self.ring_bonus("add strength")

    def _chg_str(self, amt):
        self.str = max(3, min(31, self.str + amt))
        self.max_str = max(self.max_str, self.base_str())

    def _ring_eat(self):
        """指輪ぶんの空腹の増加 (rings.c の ring_eat)。"""
        eat = 0
        for r in self.worn:
            u = D.RING_EAT[r["name"]]
            if u < 0:
                u = 1 if self.rnd(-u) == 0 else 0
            if r["name"] == "slow digestion":
                u = -u
            eat += u
        return eat

    def ring_useless(self, r):
        """正体が分かっていて着ける価値がない (飾り、常に呪いの種類、マイナスの値)。"""
        k = r["name"]
        return k in self.known and (k == "adornment" or k in D.RING_CURSED or (k in D.RING_VALUED and r["value"] < 0))

    def ring_known_cursed(self, r):
        return bool(r.get("cursed_known")) or (r["name"] in self.known and (r["name"] in D.RING_CURSED or r["value"] < 0))

    def unknown_rings(self):
        out = {}
        for r in self.worn + self.rings:
            if r["name"] not in self.known:
                out[r["name"]] = out.get(r["name"], 0) + 1
        return out

    def _ring_to_wear(self):
        """着けるならどれか: 正体の分かった有益な指輪 (RING_WORTH の順) → 未識別 (見た目の順)。着けるかどうかは Laya。"""
        cands = [r for r in self.rings if not self.ring_useless(r)]
        known = [r for r in cands if r["name"] in self.known]
        if known:
            return max(known, key=lambda r: (D.RING_WORTH.get(r["name"], 0), r["value"]))
        return min(cands, key=lambda r: self.names[r["name"]]) if cands else None

    def _ring_to_remove(self):
        """外すならどれか: 分かっている不要な物 → 未識別 → 空腹の進む物。呪いが分かっている物は候補にしない。"""
        cands = [r for r in self.worn if not self.ring_known_cursed(r)]
        if not cands:
            return None
        for group in ([r for r in cands if self.ring_useless(r)], [r for r in cands if r["name"] not in self.known]):
            if group:
                return group[0]
        return max(cands, key=lambda r: D.RING_EAT[r["name"]])

    def cursed_worn(self):
        """外せないと分かっている装備 (解呪の巻物の対象): 指輪、鎧、武器。"""
        return [r for r in self.worn if self.ring_known_cursed(r)] + [g for g in (self.armor, self.weapon) if g.get("cursed_known")]

    def _put_on(self, r):
        self.rings.remove(r)
        self.worn.append(r)
        self.say(f"{self.ring_label(r)}を着けた")
        if r["name"] == "add strength":
            self._chg_str(r["value"])
        elif r["name"] == "aggravate monster":
            for m in self.monsters:
                m["awake"] = True
                m.pop("held", None)
            self.say("怪物たちが目を覚ました気配がする")

    def _remove(self, r):
        if r["cursed"]:
            r["cursed_known"] = True
            self.say(f"{self.ring_label(r)}は外せない (呪われている)")
            return
        self.worn.remove(r)
        self.rings.append(r)
        if r["name"] == "add strength":
            self._chg_str(-r["value"])
        self.say(f"{self.ring_label(r)}を外した")

    def _ring_turn(self):
        """毎ターンの指輪の効き (command.c の末尾): 探索の指輪は自動で捜索、瞬間移動の指輪は 1/50 で飛ばされる。"""
        for r in list(self.worn):
            if r["name"] == "searching":
                self._search_here()
            elif r["name"] == "teleportation" and self.rnd(50) == 0:
                self.say("指輪の力でどこかへ飛ばされた")
                self._teleport()

    # ------------------------------------------------------------------ 未識別 (簡略版)
    def unknown_potions(self):
        return {k: n for k, n in self.potions.items() if n > 0 and k not in self.known}

    def unknown_scrolls(self):
        return {k: n for k, n in self.scrolls.items() if n > 0 and k not in self.known}

    def unknown_sticks(self):
        return {k: n for k, n in self.sticks.items() if n > 0 and k not in self.known}

    def known_sticks(self):
        return {k: n for k, n in self.sticks.items() if n > 0 and k in self.known}

    def _unknown_pick(self, bag):
        """未識別の物のうちどれを試すか: まだ試していない種類 → 多く持っている種類。同数なら見た目 (色・題名) の順。
        正体の名前順にすると巻物は aggravate → create → … で必ず有害物から試すことになる (アドバイザーの指摘で修正)。"""
        return max(sorted(bag, key=lambda k: self.names[k]), key=lambda k: (k not in self.tried, bag[k]))

    def _learn(self, kind, observed=True):
        """正体が分かった。有害と分かった物も捨てない (本家仕様。判断ボードの回答)。使う行動には出ないだけ。
        ORIGINAL (rules.learn_on_use が偽) では効果を観測できたときだけ分かり、そうでなければ「試した」印だけ付く (本家の call_it の代わり)。"""
        if kind in self.known:
            return
        before = self.label(kind)
        if not observed and not self.rules.learn_on_use:
            self.tried.add(kind)
            self.say(f"{before}の正体は分からなかった")
            return
        self.known.add(kind)
        self.tried.discard(kind)
        self.say(f"{before}は{self.label(kind)}だった")

    def useless(self, it):
        """正体が分かっていて使い道のない品 (拾いに行く標的にしない。踏めば拾う)。"""
        k, name = it["kind"], it.get("name")
        if k == "potion":
            return name in self.known and name in D.BAD_POTIONS
        if k == "scroll":
            return name in self.known and name in D.BAD_SCROLLS
        if k == "stick":
            return name in self.known and name in D.BAD_STICKS
        if k == "ring":
            return self.ring_useless(it)
        return False

    def _quaff(self, kind):
        """薬を 1 つ飲む (potions.c)。正体が分かっていてもいなくても効果は同じ。observed は効果を観測できたか (ORIGINAL の判明条件)。"""
        self.potions[kind] -= 1
        if self.potions[kind] <= 0:
            del self.potions[kind]
        observed = True
        if kind in ("healing", "extra healing"):
            self.hp += self.roll(self.level, 8 if kind == "extra healing" else 4)
            if self.hp > self.max_hp:
                if kind == "extra healing" and self.hp > self.max_hp + self.level + 1:
                    self.max_hp += 1
                self.max_hp += 1
                self.hp = self.max_hp
            self.fuses["blind"] = 0  # 回復の薬は目を治す (sight)。大回復は幻覚も (come_down)
            if kind == "extra healing":
                self.confused = 0
                self.fuses["hallucinating"] = 0
            self.say("体力が回復した")
        elif kind == "restore strength":
            if self.base_str() < self.max_str:
                self.str = self.max_str + self.ring_bonus("add strength")
            else:
                observed = False
            self.say("力が戻った")
        elif kind == "gain strength":
            self._chg_str(1)
            self.say("力が強くなった")
        elif kind == "poison":
            if self.wearing("sustain strength"):
                self.say("一瞬気分が悪くなった (毒)")
            else:
                self.str = max(3, self.str - (self.rnd(3) + 1))  # chg_str(-(rnd(3) + 1))。最大値は下がらないので力の回復で戻る
                self.say("気分が悪くなった (毒)")
        elif kind == "confusion":
            self.confused += self.rnd(8) + D.HUHDURATION
            observed = not self.hallucinating  # 幻覚中は混乱したことが分からない (potions.c の do_pot)
            self.say("目が回る (混乱)")
        elif kind == "haste self":
            if self.hasted:  # 加速中にもう 1 本: 気絶して加速が切れる (misc.c の add_haste)
                self.fuses["hasted"] = 0
                self.no_command += self.rnd(8)
                self.say("疲れ果てて気を失った")
            else:
                self.fuses["hasted"] = self.rnd(D.HASTE_TIME[0]) + D.HASTE_TIME[1]
                self.say("体がずっと速く動くようになった (加速)")
        elif kind == "raise level":
            if self.level <= len(D.EXP_LEVELS):
                self.exp = D.EXP_LEVELS[self.level - 1] + 1
                self._check_level()
            self.say("急に腕が上がった気がする (レベル上昇)")
        elif kind == "levitation":
            self._fuse("levitating", D.HEALTIME)
            self.say("体が宙に浮いた (浮遊)")
        elif kind == "blindness":
            self._fuse("blind", D.SEEDURATION)
            self.say("目の前が真っ暗になった (盲目)")
            self._look()
        elif kind == "hallucination":
            self._fuse("hallucinating", D.SEEDURATION)
            self.say("何もかもが宇宙的に見える (幻覚)")
        elif kind == "see invisible":
            self._fuse("see_invisible", D.SEEDURATION)
            observed = any("I" in m["flags"] and (m["x"], m["y"]) in self.visible for m in self.monsters)  # 見えなかった相手が見えたときだけ
            self.say("この薬は果汁の味がした (透明視)")
        elif kind == "monster detection":
            self._fuse("detecting", D.HUHDURATION)
            n = len(self.sensed_monsters())
            observed = n > 0
            self.say(f"この階の怪物 {n} 体の気配を感じた (怪物探知)" if n else "一瞬妙な感じがしたが、すぐに消えた")
        elif kind == "magic detection":
            found = 0
            for it in self.items:
                if self.is_magic(it) and (it["x"], it["y"]) not in self.visible and not it.get("sensed"):
                    it["sensed"] = True
                    found += 1
            observed = found > 0
            self.say(f"この階の魔法の品 {found} 個の気配を感じた (魔法探知)" if found else "一瞬妙な感じがしたが、すぐに消えた")
        if kind == "poison":
            self.fuses["hallucinating"] = 0  # 毒は幻覚を覚ます (come_down)
        self._learn(kind, observed)

    def _fuse(self, name, n):
        """一定ターンの状態を始める (すでに続いていれば延ばす)。長さは spread(n) (potions.c の do_pot)。"""
        self.fuses[name] += self.spread(n)

    def _run_fuses(self):
        for name in FUSES:
            if self.fuses[name] > 0:
                self.fuses[name] -= 1
                if self.fuses[name] == 0:
                    if FUSES[name]:
                        self.say(FUSES[name])
                    if name == "blind":
                        self._look()
                    if name == "levitating":  # 降りたら足元の品を拾う
                        self._pick_up_here()

    @staticmethod
    def is_magic(it):
        """魔法探知に映る品 (potions.c の is_magic): 薬・巻物・指輪・杖、± の付いた武器、素の値と違う鎧か保護された鎧。"""
        k = it["kind"]
        if k in ("potion", "scroll", "ring", "stick", "amulet"):
            return True
        if k == "weapon":
            return it["hplus"] != 0 or it["dplus"] != 0
        if k == "armor":
            base = next(ac for name, _, ac in D.ARMORS if name == it["name"])
            return it["ac"] != base or bool(it.get("protected"))
        return False

    def valid_actions(self):
        if self.no_command > 0:
            return ["rest"]
        mons = self.visible_monsters()
        adjacent = [m for m in mons if self._adjacent(m)]
        free = self.held_by is None and self.no_move == 0
        v = []
        awake = any(active(m) for m in mons)
        if adjacent:
            v.append("attack")
        if self.missiles and self._throw_target():
            v.append("throw")
        if free and mons and not adjacent and self._step_toward((mons[0]["x"], mons[0]["y"])):
            v.append("approach")
        if free and awake:
            v.append("flee")
        if self.has_heal() and (self.hp < self.max_hp or self.blind):
            v.append("quaff_heal")
        if self.has_str_potion():
            v.append("quaff_str")
        if self.has_potion("haste self") and (awake or LOOSE) and not self.hasted:  # いつ飲むかは Laya (加速中に飲むと気絶するので、加速中は出さない)
            v.append("quaff_haste")
        if self.has_potion("raise level"):
            v.append("quaff_raise")
        if self.has_potion("see invisible") and not self.can_see_invisible() and any(m.get("felt") == self.turn for m in self.monsters):
            v.append("quaff_see_invisible")
        if self.has_potion("monster detection") and not self.detecting():
            v.append("quaff_detect_monsters")
        if self.has_potion("magic detection"):
            v.append("quaff_detect_magic")
        if self.scrolls.get("magic mapping") and "magic mapping" in self.known and not self.stairs_known():
            v.append("read_map")
        if self.scrolls.get("teleportation") and "teleportation" in self.known and (awake or LOOSE):  # いつ読むかは Laya
            v.append("read_teleport")
        if self.unknown_potions():
            v.append("quaff_unknown")
        if self.unknown_scrolls():
            v.append("read_unknown")
        if self._identify_options():
            v.append("read_identify")
        if self.scrolls.get("remove curse") and "remove curse" in self.known and self.cursed_worn():
            v.append("read_remove_curse")
        if self.scrolls.get("monster confusion") and "monster confusion" in self.known and awake and not self.glowing:
            v.append("read_confuse")
        if self.scrolls.get("hold monster") and "hold monster" in self.known and self._hold_targets():
            v.append("read_hold")
        if self.scrolls.get("scare monster") and "scare monster" in self.known and not self.on_scare():
            v.append("drop_scare")
        if self.scrolls.get("food detection") and "food detection" in self.known and not any(i["kind"] == "food" for i in self.visible_items()):
            v.append("read_food")
        if len(self.worn) < 2 and self._ring_to_wear():
            v.append("put_on_ring")
        if self._ring_to_remove():
            v.append("remove_ring")
        for name, act in (("enchant armor", "read_enchant_armor"), ("enchant weapon", "read_enchant_weapon"), ("protect armor", "read_protect")):
            if self.scrolls.get(name) and name in self.known and not (name == "protect armor" and self.armor.get("protected")):
                v.append(act)
        if self.sticks and self._zap_target():
            if any(self.sticks.get(k) and k in self.known for k in D.BOLT_STICKS):
                v.append("zap_bolt")
            for act in ("zap_missile", "zap_slow", "zap_away", "zap_polymorph", "zap_cancel"):
                kind = ZAP_KIND[act]
                if self.sticks.get(kind) and kind in self.known:
                    v.append(act)
            if self.unknown_sticks():
                v.append("zap_unknown")
        if self.sticks.get("drain life") and "drain life" in self.known and self.hp >= 2 and self._drain_targets():
            v.append("zap_drain")
        if self.sticks.get("light") and "light" in self.known and (r := self.room_at(self.hx, self.hy)) and r["dark"] and not r.get("maze"):
            v.append("zap_light")
        if self.bow and not self.wielding_bow() and self.missiles.get("arrow") and self._throw_target():
            v.append("wield_bow")
        if self.wielding_bow() and self.melee:
            v.append("wield_melee")
        if self.food and self.food_left < 1000:
            v.append("eat")
        if free and not self.levitating and (target := self.pick_target()) and ((target["x"], target["y"]) in self.visible
                                                                                    or self._step_toward((target["x"], target["y"]))):
            v.append("pick_up")  # 探知しただけ (見えていない) の品は、既知の経路で行けるときだけ。浮遊中は拾えない
        if self._better_gear():
            v.append("equip")
        if free and self._explore_step():
            v.append("explore")
        elif self.rules.hidden_doors and free and not self.stairs_known() and self._search_target() is not None:
            v.append("search")
        if free and not self.levitating and self.stairs_known() and ((self.hx, self.hy) == self.stairs or self._step_toward(self.stairs)):
            v.append("descend")  # 見えているだけで既知のマスでは繋がっていない階段は選べない (NOTES 13 章)。浮遊中は降りられない
            if self.amulet:
                v.append("ascend")
        if self.pack_full() and self._junk():
            v.append("drop")
        # 安全弁 (NOTES 13 章): HP 90% 以上で敵が起きておらず空腹でもなければ待つ理由がない。他に取れる行動があるときだけ外す
        if not (self.hp >= 0.9 * self.max_hp and not awake and self.hunger_word() == "fine" and any(a in v for a in ("explore", "pick_up", "descend", "search"))):
            v.append("rest")
        return v

    def stairs_known(self):
        return self.seen[self.stairs[1]][self.stairs[0]] or self.stairs in self.mapped

    # ------------------------------------------------------------------ 飛び道具 (weapons.c の missile)
    def _throw_target(self):
        """8 方向のどれかに、起きている見えている敵が直線上にいれば (間に何もない、距離 2 以上)、いちばん近いものを返す。"""
        best = None
        for dx, dy in DIRS:
            x, y = self.hx, self.hy
            while True:
                nx, ny = x + dx, y + dy
                if not self._step_ok(x, y, nx, ny):
                    break
                m = self._monster_at(nx, ny)
                if m:
                    d = self.dist(nx, ny)
                    if m["awake"] and self.can_see(m) and d >= 2 and (best is None or d < best[1]):
                        best = (m, d, (x, y))
                    break
                x, y = nx, ny
        return best

    def _zap_target(self):
        """杖の標的: 8 方向のどれかに直線上で見えている敵 (隣でもよい)。起きた敵を先に、次に近さ (眠った敵にも撃てる。追放・変身・鈍足は眠った強敵にこそ効く)。"""
        best = None
        for dx, dy in DIRS:
            x, y = self.hx, self.hy
            while True:
                nx, ny = x + dx, y + dy
                if not self._step_ok(x, y, nx, ny):
                    break
                m = self._monster_at(nx, ny)
                if m:
                    d = self.dist(nx, ny)
                    if (m["awake"] or LOOSE) and self.can_see(m) and (best is None or (m["awake"], -d) > (best[0]["awake"], -best[1])):
                        best = (m, d)
                    break
                x, y = nx, ny
        return best

    def _monster_save(self, m):
        """モンスターの魔法への抵抗 (save_throw: 14 + VS_MAGIC − レベル / 2 以上を d20 で出す)。"""
        return self.roll(1, 20) >= 14 + VS_MAGIC - m["lvl"] // 2

    def _drain_targets(self):
        """生命吸収の杖が効く相手: 同じ部屋のモンスター (通路にいれば隣のマスの敵)。"""
        r = self.room_at(self.hx, self.hy)
        if r:
            return [m for m in self.monsters if self.room_at(m["x"], m["y"]) is r]
        return [m for m in self.monsters if self._adjacent(m)]

    def _zap(self, kind, m):
        """杖を 1 回振る (sticks.c の do_zap)。m は直線上の標的 (方向のいらない杖は None)。"""
        if kind == "drain life" and self.hp < 2:
            self.say("弱りすぎていて使えない")
            return
        self.sticks[kind] -= 1
        if self.sticks[kind] <= 0:
            del self.sticks[kind]
        if m is not None:
            m["awake"] = True
            m.pop("held", None)
        name = m["jp"] if m is not None and self.can_see(m) else "何か"
        observed = kind in ("lightning", "fire", "cold", "magic missile", "drain life") or (kind == "polymorph" and self.can_see(m)) \
            or (kind == "light" and (r := self.room_at(self.hx, self.hy)) is not None and r["dark"])  # 鈍足・加速・追放・引き寄せ・透明化・無効化・無は分からない
        if kind in D.BOLT_STICKS:
            dx, dy = (m["x"] > self.hx) - (m["x"] < self.hx), (m["y"] > self.hy) - (m["y"] < self.hy)
            self._fire_bolt(self.hx, self.hy, dx, dy, D.STICK_JP[kind], None)
        elif kind == "magic missile":  # 1d4 + 1、ほぼ必中。抵抗されると消える
            if self._monster_save(m):
                self.say("魔法の矢は煙になって消えた")
            else:
                m["hp"] -= self.roll(1, 4) + 1
                self.say(f"魔法の矢が{name}に当たった")
                if m["hp"] <= 0:
                    self._kill(m)
        elif kind == "slow monster":
            if m.get("haste"):
                del m["haste"]
            else:
                m["slow"] = True
            self.say(f"{name}の動きが鈍くなった")
        elif kind == "haste monster":
            if m.get("slow"):
                del m["slow"]
            else:
                m["haste"] = True
            self.say(f"{name}の動きが速くなった")
        elif kind == "teleport away":
            real = [r for r in self.rooms if not r["gone"]]
            taken = {(o["x"], o["y"]) for o in self.monsters} | {(self.hx, self.hy)}
            m["x"], m["y"] = self._floor_spot(self.rng.choice(real), taken)
            self.say(f"{name}はどこかへ消えた")
        elif kind == "teleport to":
            free = [c for c in self._nbr[(self.hx, self.hy)] if self._monster_at(*c) is None]
            if free:
                m["x"], m["y"] = min(free, key=lambda c: max(abs(c[0] - m["x"]), abs(c[1] - m["y"])))
            self.say(f"{name}が目の前に引き寄せられた")
        elif kind == "polymorph":
            self._polymorph(m)
        elif kind == "cancellation":
            m["cancelled"] = True
            m["gazed"] = True
            m["flags"] = m["flags"].replace("I", "")
            if m["ch"] == "X":
                m.pop("disguise", None)
            self.say(f"{name}の特殊な力が消えた")
        elif kind == "invisibility":
            if "I" not in m["flags"]:
                m["flags"] += "I"
            self.say(f"{name}の姿が消えた")
        elif kind == "light":
            r = self.room_at(self.hx, self.hy)
            if r and r["dark"]:
                r["dark"] = False
                self._look()
                self.say("部屋が明るくなった")
            else:
                self.say("何も起きなかった")
        elif kind == "drain life":
            targets = self._drain_targets()
            if not targets:
                observed = False
                self.say("体がちくちくした")
            else:
                self.hp //= 2
                each = self.hp // len(targets)
                for t in targets:
                    t["hp"] -= each
                    t["awake"] = True
                    if t["hp"] <= 0:
                        self._kill(t)
                self.say(f"自分の生命を絞って周りの怪物 {len(targets)} 体を打った")
        elif kind == "nothing":
            self.say("何も起きなかった")
        self._learn(kind, observed)

    def _polymorph(self, m):
        """変身の杖: ランダムな別の種類になる (位置と起きているかは保つ。sticks.c の WS_POLYMORPH)。"""
        ch = chr(self.rnd(26) + 65)
        was = m["jp"]
        self._spawn(ch, m["x"], m["y"], awake=True)
        new = self.monsters.pop()
        for k in ("kind", "jp", "ch", "hp", "max_hp", "lvl", "arm", "dice", "exp", "flags", "carry"):
            m[k] = new[k]
        for k in ("slow", "haste", "confused", "cancelled", "disguise"):
            m.pop(k, None)
        self.say(f"{was}は{m['jp']}に変身した")

    def _bolt_path(self, x, y, dx, dy):
        """bolt の通り道 (sticks.c の fire_bolt): BOLT_LENGTH マス進み、壁に当たると跳ね返る (跳ね返りも 1 歩ぶん使う)。"""
        path = []
        for _ in range(D.BOLT_LENGTH):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < W and 0 <= ny < H) or self.tiles[ny][nx] not in PASSABLE:
                dx, dy = -dx, -dy
                continue
            x, y = nx, ny
            path.append((x, y))
        return path

    def _fire_bolt(self, x, y, dx, dy, name, shooter):
        """稲妻・炎・冷気の bolt。shooter が None なら勇者が振った杖、そうでなければそのモンスター (ドラゴンの炎)。
        最初に当たった相手で止まる。モンスターは魔法の抵抗に成功すると外れ、ドラゴンは炎が効かない。跳ね返って勇者に当たることもある。"""
        for px, py in self._bolt_path(x, y, dx, dy):
            if (px, py) == (self.hx, self.hy):
                if self.save(VS_MAGIC):
                    self.say(f"{name}は体をかすめて飛んでいった")
                else:
                    self.hp -= self.roll(6, 6)
                    self.say(f"{name}に打たれた！")
                    if self.hp <= 0:
                        self.hp, self.dead, self.cause = 0, True, f"{shooter['jp']}の炎" if shooter else f"跳ね返った{name}"
                        self.say(f"勇者は{self.cause}で倒れた…")
                return
            m = self._monster_at(px, py)
            if m is not None and m is not shooter:
                m["awake"] = True
                who = m["jp"] if self.can_see(m) else "何か"
                if m["ch"] == "D" and name == "炎":
                    self.say(f"{name}は{who}に跳ね返された")
                elif self._monster_save(m):
                    self.say(f"{name}は{who}をかすめた")
                else:
                    m["hp"] -= self.roll(6, 6)
                    self.say(f"{name}が{who}を打った")
                    if m["hp"] <= 0:
                        self._kill(m)
                return

    def _throw(self):
        target = self._throw_target()
        if not target:
            return
        m, _, landing = target
        name = max(self.missiles, key=lambda n: avg_dice(parse_dice(self._hurl_dice(n))))
        self.missiles[name] -= 1
        if self.missiles[name] <= 0:
            del self.missiles[name]
        hplus = D.STR_PLUS[self.str] + (0 if m["awake"] else 4)
        m["awake"] = True
        if self._swing(self.level, m["arm"], hplus):
            dmg = max(0, self.roll(*parse_dice(self._hurl_dice(name))[0]) + D.ADD_DAM[self.str])
            m["hp"] -= dmg
            self.say(f"{name} が{m['jp']}に当たった")
            if m["hp"] <= 0:
                self._kill(m)
        else:
            self.say(f"{name} は{m['jp']}に外れた")
        # 投げた物は敵の手前のマスに落ちる (拾い直せる)
        for it in self.items:
            if it["kind"] == "missile" and it["name"] == name and (it["x"], it["y"]) == landing:
                it["count"] += 1
                break
        else:
            self.items.append(dict(kind="missile", name=name, count=1, x=landing[0], y=landing[1]))

    def _hurl_dice(self, name):
        """投げたときのダメージダイス。矢は弓を構えていてこそ (構えていなければ振り回しの 1x1。weapons.c の missile)。"""
        for n, _, dmg, hurl, launcher in D.WEAPONS:
            if n == name:
                return hurl if launcher is None or self.weapon["name"] == launcher else dmg
        return "1x1"

    def missile_damage_per_turn(self, m):
        if not self.missiles:
            return 0.0
        best = max(avg_dice(parse_dice(self._hurl_dice(n))) for n in self.missiles)
        return self.hit_chance(self.level, m["arm"], D.STR_PLUS[self.str]) * max(0.0, best + D.ADD_DAM[self.str])

    # ------------------------------------------------------------------ 巻物 (scrolls.c)
    def _identify_options(self):
        """読める識別の巻物と、その対象。[(巻物の名前, 対象)]。対象は種類の名前か、± の分からない武器・防具の dict。
        指輪・杖 → 薬 → 巻物 → 鎧 → 武器の順 (着けている未識別の指輪は外せるかどうかに関わるので最初)。"""
        out = []
        for sname, cats in D.IDENTIFY_SCROLLS.items():
            if not (self.scrolls.get(sname) and sname in self.known):
                continue
            for cat in cats:
                target = None
                if cat == "ring":
                    worn = [r for r in self.worn if r["name"] not in self.known]
                    target = worn[0]["name"] if worn else (self._unknown_pick(self.unknown_rings()) if self.unknown_rings() else None)
                elif cat == "stick" and self.unknown_sticks():
                    target = self._unknown_pick(self.unknown_sticks())
                elif cat == "potion" and self.unknown_potions():
                    target = self._unknown_pick(self.unknown_potions())
                elif cat == "scroll" and self.unknown_scrolls():
                    target = self._unknown_pick(self.unknown_scrolls())
                elif cat in ("armor", "weapon"):
                    worn = self.armor if cat == "armor" else self.melee_weapon()
                    cands = ([worn] if not worn.get("known", True) else []) + [g for g in self.gear if g["kind"] == cat and not g.get("known", True)]
                    target = cands[0] if cands else None
                if target is not None:
                    out.append((sname, target))
                    break
        return out

    def _read(self, name):
        self.scrolls[name] -= 1
        if self.scrolls[name] <= 0:
            del self.scrolls[name]
        observed = True
        if name == "enchant armor":
            self.armor["ac"] -= 1
            self.armor.pop("cursed", None)  # 強化は呪いも解く (scrolls.c)
            self.armor.pop("cursed_known", None)
            self.say("鎧が輝いた (強化)")
        elif name == "enchant weapon":
            w = self.melee_weapon()  # 弓を構えていても本家は「構えている物」だが、弓に乗せても意味が薄いので近接武器に
            if self.rnd(2) == 0:
                w["hplus"] += 1
            else:
                w["dplus"] += 1
            w.pop("cursed", None)
            w.pop("cursed_known", None)
            self.say(f"{w['name']} が輝いた (強化)")
        elif name == "protect armor":
            self.armor["protected"] = True
            self.say("鎧が錆びなくなった")
        elif name == "magic mapping":
            for y in range(H):
                for x in range(W):
                    if self.tiles[y][x] in (SDOOR, SPASS):
                        self._reveal(x, y)
                    if self.tiles[y][x] != ROCK and not self.seen[y][x] and (x, y) not in self.mapped:
                        self.mapped.add((x, y))
                        self.newly_seen.append((x, y, self.tiles[y][x]))
            for t in self.traps:  # 本家の地図は罠のマスも映す
                t["found"] = True
            self._explore, self._goal = [], ([], None)
            self.say("この階の地図が頭に浮かんだ")
        elif name == "teleportation":
            before = self.room_at(self.hx, self.hy)
            self._teleport()
            observed = self.room_at(self.hx, self.hy) is not before  # 同じ部屋に落ちると分からない (scrolls.c)
            self.say("別の場所に飛ばされた")
        elif name in D.IDENTIFY_SCROLLS:
            self._learn(name)
            opts = [t for s, t in self._identify_options() if s == name]
            if not opts:
                self.say("識別の巻物だったが、調べる物がなかった")
            elif isinstance(opts[0], str):
                self._learn(opts[0])
            else:
                opts[0]["known"] = True
                self.say(f"{opts[0]['name']} の正体が分かった")
        elif name == "monster confusion":
            self.glowing = True
            self.say("手が赤く光りだした (怪物混乱)")
        elif name == "hold monster":
            targets = self._hold_targets()
            for m in targets:
                m["held"] = True
            observed = bool(targets)
            self.say(f"周りの怪物 {len(targets)} 体が動かなくなった (拘束)" if targets else "何かを失った気がした (拘束)")
        elif name == "scare monster":
            self.say("遠くで狂ったような笑い声がした (恐怖の巻物は読むと消える)")
        elif name == "food detection":
            found = 0
            for it in self.items:
                if it["kind"] == "food" and (it["x"], it["y"]) not in self.visible and not it.get("sensed"):
                    it["sensed"] = True
                    found += 1
            observed = found > 0
            self.say(f"鼻がむずむずして、食料の匂いがした ({found} 個)" if found else "鼻がむずむずした (食料探知)")
        elif name == "remove curse":
            observed = bool(self.cursed_worn())
            for r in self.worn:
                r["cursed"], r["cursed_known"] = False, False
            for g in (self.armor, self.weapon):
                g.pop("cursed", None)
                g.pop("cursed_known", None)
            self.say("誰かに見守られている気がした (解呪)")
        elif name == "sleep":
            self.no_command += self.rnd(self.spread(D.SLEEPTIME)) + 4  # rnd(SLEEPTIME) + 4
            self.say("眠気に襲われた (眠り)")
        elif name == "create monster":
            free = [c for c in self._nb8[(self.hx, self.hy)] if self.tiles[c[1]][c[0]] in PASSABLE and not self._monster_at(*c)]
            if free:
                x, y = self.rng.choice(free)
                self._spawn(self._rand_monster(False), x, y, awake=True)
                self.say(f"{self.monsters[-1]['jp']}が現れた (怪物召喚)")
            else:
                observed = False
                self.say("遠くでかすかな悲鳴が聞こえた (怪物召喚)")
        elif name == "aggravate monsters":
            for m in self.monsters:
                m["awake"] = True
                m.pop("held", None)
            self.say("高い音が響き、怪物たちが目を覚ました (怪物寄せ)")
        self._learn(name, observed)

    def _hold_targets(self):
        """拘束の巻物が効く相手: 周囲 2 マス (5 × 5) にいる起きたモンスター (scrolls.c の S_HOLD)。"""
        return [m for m in self.monsters if m["awake"] and not m.get("held")
                and abs(m["x"] - self.hx) <= D.HOLD_RANGE and abs(m["y"] - self.hy) <= D.HOLD_RANGE]

    def _adjacent(self, m):
        return (m["x"], m["y"]) in self._nbr[(self.hx, self.hy)]

    def scare_at(self, x, y):
        return any(i["kind"] == "scroll" and i["name"] == "scare monster" and (i["x"], i["y"]) == (x, y) for i in self.items)

    def on_scare(self):
        """恐怖の巻物の上に立っている (モンスターはこのマスに入れないので殴ってこない)。"""
        return self.scare_at(self.hx, self.hy)

    def _move_to(self, step):
        if step is None:
            return
        if self.confused and self.rnd(5) != 0:  # 混乱中は 8 割の確率で千鳥足
            options = self._nbr[(self.hx, self.hy)]
            step = self.rng.choice(options) if options else step
        m = self._monster_at(*step)
        if m:
            self._hero_attacks(m)
        else:
            self.hx, self.hy = step
            t = self._trap_at(*step)
            if t and not self.levitating:  # 浮遊中は罠を踏まない (move.c)
                self._spring(t)

    def step(self, action):
        """勇者が action を実行し、続けて全モンスターが動き、空腹と自然回復が進む。"""
        self.turn += 1
        self._dist = None
        self._pos0 = (self.hx, self.hy)  # このターンの始めの位置 (動かずに足元の恐怖の巻物を拾わないため)
        if self.no_move > 0:
            self.no_move -= 1
        if self.no_command > 0:
            self.no_command -= 1
        else:
            if self._act(action) or self._fell:
                self._fell = False
                self._extra_move = False
                return  # 階を降りた (階段か落とし穴)
            if self.hasted and not self._extra_move:  # 加速: 勇者はもう 1 手動ける。モンスターと空腹はそのあと (command.c の ntimes)
                self._extra_move = True
                self._look()
                return
        self._extra_move = False
        if self.confused:
            self.confused -= 1
        self._look()
        self._monsters_act()
        if not self.dead:
            self._doctor()
            self._stomach()
            self._wanderer()
            self._ring_turn()
            self._run_fuses()

    def _act(self, action):
        mons = self.visible_monsters()
        if action == "attack":
            adj = [m for m in mons if self._adjacent(m)]
            if adj:
                self._hero_attacks(min(adj, key=lambda m: m["hp"]))
        elif action == "approach" and mons:
            self._move_to(self._step_toward((mons[0]["x"], mons[0]["y"])))
        elif action == "throw":
            self._throw()
        elif action == "flee":
            self._flee([m for m in mons if m["awake"]])
        elif action == "read_map" and self.scrolls.get("magic mapping"):
            self._read("magic mapping")
        elif action == "read_teleport" and self.scrolls.get("teleportation"):
            self._read("teleportation")
        elif action == "quaff_heal" and self.has_heal():
            # 体力が大きく減っていれば強い薬から、少しなら弱い薬から
            order = ["extra healing", "healing"] if self.hp <= self.max_hp // 2 else ["healing", "extra healing"]
            kind = next(k for k in order if self.potions.get(k, 0) and k in self.known)
            self.say(f"{self.label(kind)}を飲んだ")
            self._quaff(kind)
        elif action == "quaff_str" and self.has_str_potion():
            restore = "restore strength" in self.known and self.potions.get("restore strength", 0) and self.base_str() < self.max_str
            kind = "restore strength" if restore else "gain strength"
            self.say(f"{self.label(kind)}を飲んだ")
            self._quaff(kind)
        elif action in ("quaff_haste", "quaff_raise", "quaff_see_invisible", "quaff_detect_monsters", "quaff_detect_magic"):
            kind = {"quaff_haste": "haste self", "quaff_raise": "raise level", "quaff_see_invisible": "see invisible",
                    "quaff_detect_monsters": "monster detection", "quaff_detect_magic": "magic detection"}[action]
            if self.has_potion(kind):
                self.say(f"{self.label(kind)}を飲んだ")
                self._quaff(kind)
        elif action == "quaff_unknown" and self.unknown_potions():
            kind = self._unknown_pick(self.unknown_potions())
            self.say(f"{self.label(kind)}を飲んでみた")
            self._quaff(kind)
        elif action == "read_unknown" and self.unknown_scrolls():
            kind = self._unknown_pick(self.unknown_scrolls())
            self.say(f"{self.label(kind)}を読んでみた")
            self._read(kind)
        elif action == "read_identify":
            opts = self._identify_options()
            if opts:
                self._read(opts[0][0])
        elif action == "drop":
            junk = self._junk()
            if junk:
                self._drop(junk)
        elif action == "read_remove_curse" and self.scrolls.get("remove curse") and "remove curse" in self.known:
            self._read("remove curse")
        elif action == "read_confuse" and self.scrolls.get("monster confusion") and "monster confusion" in self.known:
            self._read("monster confusion")
        elif action == "read_hold" and self.scrolls.get("hold monster") and "hold monster" in self.known:
            self._read("hold monster")
        elif action == "read_food" and self.scrolls.get("food detection") and "food detection" in self.known:
            self._read("food detection")
        elif action == "drop_scare" and self.scrolls.get("scare monster") and "scare monster" in self.known:
            self.scrolls["scare monster"] -= 1
            if self.scrolls["scare monster"] <= 0:
                del self.scrolls["scare monster"]
            self.items.append(dict(kind="scroll", name="scare monster", x=self.hx, y=self.hy, found=True))
            self.say("恐怖の巻物を足元に置いた")
            return False  # 置いた巻物を同じターンに拾わない
        elif action == "put_on_ring" and len(self.worn) < 2:
            r = self._ring_to_wear()
            if r:
                self._put_on(r)
        elif action == "remove_ring":
            r = self._ring_to_remove()
            if r:
                self._remove(r)
        elif action in ("read_enchant_armor", "read_enchant_weapon", "read_protect"):
            name = {"read_enchant_armor": "enchant armor", "read_enchant_weapon": "enchant weapon", "read_protect": "protect armor"}[action]
            if self.scrolls.get(name) and name in self.known:
                self._read(name)
        elif action == "zap_drain" and self.sticks.get("drain life") and "drain life" in self.known:
            self.say("生命吸収の杖を振った")
            self._zap("drain life", None)
        elif action == "zap_light" and self.sticks.get("light") and "light" in self.known:
            self.say("光の杖を振った")
            self._zap("light", None)
        elif action in ("zap_bolt", "zap_missile", "zap_slow", "zap_away", "zap_polymorph", "zap_cancel", "zap_unknown"):
            target = self._zap_target()
            if target:
                m = target[0]
                if action == "zap_bolt":
                    kind = next(k for k in ("lightning", "fire", "cold") if self.sticks.get(k) and k in self.known)
                elif action == "zap_unknown":
                    kind = self._unknown_pick(self.unknown_sticks())
                else:
                    kind = ZAP_KIND[action]
                if self.sticks.get(kind):
                    self.say(f"{self.label(kind)}を{m['jp'] if self.can_see(m) else '何か'}に向けて振った")
                    self._zap(kind, m)
        elif action == "eat" and self.food:
            self.food -= 1
            self.food_left = min(D.STOMACH_SIZE, max(0, self.food_left) + D.HUNGER_TIME - 200 + self.rnd(400))
            self.say("食事をした")
        elif action == "pick_up":
            it = self.pick_target()
            if it:
                self._move_to(self._step_toward((it["x"], it["y"])))
        elif action == "equip":
            g = self._better_gear()
            if g:
                cur = self.armor if g["kind"] == "armor" else self.weapon
                if cur.get("cursed"):  # 呪われた物は外せない (pack.c の dropcheck)。試して初めて分かる
                    cur["cursed_known"] = True
                    self.say(f"{cur['name']} は外せない (呪われている)")
                else:
                    self.gear.remove(g)
                    if g["kind"] == "armor":  # 着ると ± が分かる (armor.c の wear)
                        self.armor = dict(name=g["name"], ac=g["ac"], cursed=g.get("cursed", False), known=True)
                    else:
                        self.weapon = dict(name=g["name"], dice=g["dice"], hplus=g["hplus"], dplus=g["dplus"], cursed=g.get("cursed", False),
                                           known=g.get("known", True))
                    self.say(f"{g['name']} を装備した")
        elif action == "wield_bow" and self.bow and not self.wielding_bow():
            self.melee = self.weapon
            self.weapon = dict(name="short bow", dice=parse_dice("1x1"), hplus=1, dplus=0)  # 初期装備の弓は +1 (init.c)
            self.say("弓を構えた")
        elif action == "wield_melee" and self.wielding_bow() and self.melee:
            self.weapon, self.melee = self.melee, None
            self.say(f"{self.weapon['name']} を構え直した")
        elif action == "explore":
            self._move_to(self._explore_step())
            if self._explore and (self.hx, self.hy) == self._explore[0]:
                self._explore.pop(0)
        elif action == "search":
            self._search()
        elif action == "descend":
            if (self.hx, self.hy) == self.stairs:
                self.new_floor()
                return True
            self._move_to(self._step_toward(self.stairs))
        elif action == "ascend" and self.amulet:
            if (self.hx, self.hy) == self.stairs:
                self.new_floor(up=True)
                return True
            self._move_to(self._step_toward(self.stairs))

        self._pick_up_here()
        return False

    def pack_count(self):
        """持ち物の枠の数 (pack.c: 同じ種類の薬・巻物・矢はまとめて 1 枠)。"""
        return (len(self.potions) + len(self.scrolls) + len(self.sticks) + len(self.rings) + len(self.worn) + len(self.gear) + len(self.missiles)
                + bool(self.food) + bool(self.bow) + 1 + 1 + bool(self.melee) + bool(self.amulet))

    def pack_full(self):
        return self.rules.pack_limit is not None and self.pack_count() >= self.rules.pack_limit

    def _fits(self, it):
        """拾えるか。枠が空いているか、同じ種類の枠にまとまる物。"""
        if not self.pack_full() or it["kind"] in ("gold", "amulet"):
            return True
        k, name = it["kind"], it.get("name")
        return ((k == "potion" and name in self.potions) or (k == "scroll" and name in self.scrolls) or (k == "stick" and name in self.sticks)
                or (k == "missile" and name in self.missiles) or (k == "food" and self.food > 0))

    def _junk(self):
        """捨ててよい物 (正体が分かっていて使い道がない): 薬・巻物・杖の種類か、持っている指輪。"""
        for bag, kind in ((self.potions, "potion"), (self.scrolls, "scroll"), (self.sticks, "stick")):
            for name in sorted(bag):
                if self.useless(dict(kind=kind, name=name)):
                    return (kind, name)
        for r in self.rings:
            if self.ring_useless(r):
                return ("ring", r)
        return None

    def _drop(self, junk):
        """捨てる (pack.c の drop)。同じ種類はまとめて足元に置く。捨てた物は拾い直さない。"""
        kind, what = junk
        if kind == "ring":
            self.rings.remove(what)
            self.items.append(dict(what, kind="ring", x=self.hx, y=self.hy, junk=True))
            self.say(f"{self.ring_label(what)}を捨てた")
            return
        bag = {"potion": self.potions, "scroll": self.scrolls, "stick": self.sticks}[kind]
        n = bag.pop(what)
        for _ in range(n if kind != "stick" else 1):
            self.items.append(dict(kind=kind, name=what, x=self.hx, y=self.hy, junk=True, **({"charges": n} if kind == "stick" else {})))
        self.say(f"{self.label(what)}を捨てた")

    def _pick_up_here(self):
        if self.levitating:
            return
        for it in [i for i in self.items if (i["x"], i["y"]) == (self.hx, self.hy)]:
            if it.get("junk"):
                continue
            if not self._fits(it):
                self.say("持ち物がいっぱいで拾えない")
                continue
            self.items.remove(it)
            if it["kind"] == "scroll" and it["name"] == "scare monster" and it.get("found"):
                if (self.hx, self.hy) == getattr(self, "_pos0", None):  # 自分で置いた巻物の上に留まっている (動いて踏んだときだけ拾って塵になる)
                    self.items.append(it)
                    continue
                self.say("拾おうとした巻物は塵になった")
                continue
            if it["kind"] == "amulet":
                self.amulet = True
                self.say("イェンダーの魔除けを手に入れた！ 上の階段で地上へ戻れる")
                continue
            if it["kind"] == "gold":
                self.gold += it["value"]
                self.say(f"金貨 {it['value']} 枚を拾った")
            elif it["kind"] == "food":
                self.food += 1
                self.say("食料を拾った")
            elif it["kind"] == "potion":
                k = it["name"]
                self.potions[k] = self.potions.get(k, 0) + 1
                self.say(f"{self.label(k)}を拾った")
            elif it["kind"] == "scroll":
                k = it["name"]
                self.scrolls[k] = self.scrolls.get(k, 0) + 1
                self.say(f"{self.label(k)}を拾った")
            elif it["kind"] == "stick":
                self.sticks[it["name"]] = self.sticks.get(it["name"], 0) + it["charges"]
                self.say(f"{self.label(it['name'])}を拾った")
            elif it["kind"] == "ring":
                self.rings.append(dict(name=it["name"], value=it["value"], cursed=it["cursed"]))
                self.say(f"{self.label(it['name'])}を拾った")
            elif it["kind"] == "missile":
                self.missiles[it["name"]] = self.missiles.get(it["name"], 0) + it["count"]
                self.say(f"{it['name']} を {it['count']} 拾った")
            elif it["kind"] == "bow":
                self.bow = True
                self.say("弓を拾った")
            else:
                self.gear.append(it)
                self.say(f"{it['name']} を拾った")

    def _flee(self, mons):
        if not mons:
            return
        occupied = {(m["x"], m["y"]) for m in self.monsters}

        def danger(p):
            return sum(1.0 / max(1, max(abs(p[0] - m["x"]), abs(p[1] - m["y"]))) ** 2 for m in mons)

        best, best_s = None, danger((self.hx, self.hy))
        for p in self._nbr[(self.hx, self.hy)]:
            if self.seen[p[1]][p[0]] and p not in occupied and danger(p) < best_s:
                best, best_s = p, danger(p)
        self._move_to(best)

    # ------------------------------------------------------------------ モンスターの手番
    def _monsters_act(self):
        for m in list(self.monsters):
            if m not in self.monsters or self.dead:
                continue
            seen = (m["x"], m["y"]) in self.visible
            if not m["awake"]:
                # 意地悪 (mean) な相手は、見かけるたびに 2/3 で襲ってくる。強欲 (greedy) も目を覚ます
                if seen and (("M" in m["flags"] and self.rnd(3) != 0 and not self.wearing("stealth") and not self.levitating) or "G" in m["flags"]):
                    m["awake"] = True
                continue
            if m.get("slow") and self.turn % 2 == 1:  # 鈍足の杖: 1 ターンおきにしか動けない
                continue
            if m.get("held"):  # 拘束の巻物: 殴られるか怪物寄せまで動かない
                continue
            if m["ch"] == "M" and seen and not m["gazed"] and not self.blind and not self.hallucinating:
                r = self.room_at(self.hx, self.hy)
                if (r and not r["dark"]) or self.dist(m["x"], m["y"]) < LAMP_DIST:
                    m["gazed"] = True
                    if not self.save(VS_MAGIC):
                        self.confused += self.spread(20)
                        self.say("メデューサの視線で混乱した")
            self._monster_turn(m)
            if m in self.monsters and not self.dead and m.get("haste"):  # 怪物加速の杖: 2 回動く (chase.c の move_monst)
                self._monster_turn(m)
            if m in self.monsters and not self.dead and "F" in m["flags"] and self.dist(m["x"], m["y"]) >= 3:  # 飛行: 離れていれば 2 歩 (chase.c の runners)
                self._monster_turn(m)

    def _room_gold(self, m):
        """強欲 (オーク) が守る金貨: 自分のいる部屋に落ちている金貨の位置 (monsters.c、chase.c の do_chase)。"""
        r = self.room_at(m["x"], m["y"])
        if not r or not r["gold"]:
            return None
        for it in self.items:
            if it["kind"] == "gold" and r["x"] <= it["x"] < r["x"] + r["w"] and r["y"] <= it["y"] < r["y"] + r["h"]:
                return (it["x"], it["y"])
        return None

    def _monster_turn(self, m):
        """起きているモンスター 1 体の 1 手: 隣なら攻撃、そうでなければ勇者へ 1 歩 (do_chase)。"""
        if m["ch"] == "D" and not m.get("cancelled") and self.rules.dragon_flame:
            dx, dy = self.hx - m["x"], self.hy - m["y"]
            if (dx == 0 or dy == 0 or abs(dx) == abs(dy)) and dx * dx + dy * dy <= D.BOLT_LENGTH ** 2 and self.rnd(D.DRAGONSHOT) == 0:
                self.say("ドラゴンが炎を吐いた！")
                self._fire_bolt(m["x"], m["y"], (dx > 0) - (dx < 0), (dy > 0) - (dy < 0), "炎", m)
                return
        scared = self.on_scare()
        if self._adjacent(m) and not scared and not m.get("confused"):
            self._monster_attacks(m)
            return
        if m["ch"] == "F":
            return
        occupied = {(o["x"], o["y"]) for o in self.monsters if o is not m} | ({(self.hx, self.hy)} if scared else set())
        steps = [p for p in self._nbr[(m["x"], m["y"])] if p not in occupied and not self.scare_at(*p)]
        if not steps:
            return
        if (m.get("confused") and self.rnd(5) != 0) or (m["ch"] == "B" and self.rnd(2) == 0) or (m["ch"] == "P" and self.rnd(5) == 0):
            # 混乱した相手 (4/5) とコウモリ・ファントムはふらふら動く。勇者のマスに踏み込めばそれが攻撃になる (chase.c)
            if m.get("confused") and self.rnd(20) == 0:
                del m["confused"]
            p = self.rng.choice(steps)
            if p == (self.hx, self.hy):
                self._monster_attacks(m)
            else:
                m["x"], m["y"] = p
            return
        if self._adjacent(m):  # 混乱していて 1/5 で正気に動いたぶん
            self._monster_attacks(m)
            return
        steps = [p for p in steps if p != (self.hx, self.hy)]
        if not steps:
            return
        if "G" in m["flags"]:  # 強欲: 部屋の金貨を守りに行き、そこに立つ。金貨が拾われたら勇者を追う
            gold = self._room_gold(m)
            if gold:
                if (m["x"], m["y"]) != gold:
                    best = min(steps, key=lambda p: max(abs(p[0] - gold[0]), abs(p[1] - gold[1])))
                    if max(abs(best[0] - gold[0]), abs(best[1] - gold[1])) < max(abs(m["x"] - gold[0]), abs(m["y"] - gold[1])):
                        m["x"], m["y"] = best
                return
        dist = self._hero_dist_map()
        here = dist.get((m["x"], m["y"]), 99)
        best = min(steps, key=lambda p: dist.get(p, 99))
        if dist.get(best, 99) < here:
            m["x"], m["y"] = best

    # ------------------------------------------------------------------ 毎ターンの処理 (daemons.c)
    def _doctor(self):
        before = self.hp
        self.quiet += 1
        if self.level < 8:
            if self.quiet + (self.level << 1) > 20:
                self.hp += 1
        elif self.quiet >= 3:
            self.hp += self.rnd(self.level - 7) + 1
        self.hp += self.wearing("regeneration")  # 再生の指輪: 1 つにつき毎ターン +1
        if self.hp != before:
            self.hp = min(self.hp, self.max_hp)
            self.quiet = 0

    def hunger_word(self):
        return "fainting" if self.food_left <= 0 else "weak" if self.food_left < D.MORE_TIME else "hungry" if self.food_left < 2 * D.MORE_TIME else "fine"

    def _stomach(self):
        if self.food_left <= 0:
            self.food_left -= 1
            if self.food_left < -D.STARVE_TIME:
                self.hp, self.dead, self.cause = 0, True, "餓死"
                self.say("勇者は餓死した…")
            elif self.no_command == 0 and self.rnd(5) == 0:
                self.no_command += self.rnd(8) + 4
                self.say("空腹で気を失った")
            return
        before = self.food_left
        self.food_left -= 1 + self._ring_eat() - (1 if self.amulet else 0)  # 魔除けを持っていると空腹が進まない (daemons.c の stomach)
        if self.food_left < D.MORE_TIME <= before:
            self.say("空腹で力が入らない")
        elif self.food_left < 2 * D.MORE_TIME <= before:
            self.say("お腹が空いてきた")

    def _wanderer(self):
        if self.wander_fuse > 0:
            self.wander_fuse -= 1
            return
        self.between += 1
        if self.between < 4:
            return
        self.between = 0
        if self.roll(1, 6) != 4:
            return
        here = self.room_at(self.hx, self.hy)
        rooms = [r for r in self.rooms if not r["gone"] and r is not here]
        if rooms:
            taken = {(m["x"], m["y"]) for m in self.monsters} | {(self.hx, self.hy)}
            x, y = self._floor_spot(self.rng.choice(rooms), taken)
            self._spawn(self._rand_monster(True), x, y, awake=True)
        self.wander_fuse = self.spread(D.WANDER_TIME)
