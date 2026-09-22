"""本家 Rogue 5.4.4 のルールに基づくターン制ローグライク (第 2 段階: 戦闘と生存 + 巻物と飛び道具)。描画も AI も持たない。

数値と規則は Rogue 5.4.4 を参照した独自実装 (rogue_data.py と THIRD_PARTY_NOTICES.md)。
勇者の 1 ターンは「高レベル行動」(ACTIONS) を 1 つ選ぶこと。どのマスへ動くかといった幾何の計算は
ここで行い、「今なにをすべきか」の判断だけを外 (Laya) に委ねる。

第 2 段階で実装していないもの (NOTES.md に一覧): 指輪・杖・未識別、巻物のうち識別系・解呪・眠り・召喚・恐怖・拘束・混乱・食料探知、
罠、隠し扉、迷路部屋、ドラゴンの炎、ファントムの透明化、ゼロックの擬態、呪い、26 階の魔除け。
飛び道具は本家と違って弓を「構える」必要がなく、持っていれば矢に弓の威力が乗る (装備の持ち替えという操作を省いた)。
"""
import copy
import random
from collections import deque

import rogue_data as D

W, H = 80, 24
ROCK, FLOOR, STAIRS, PASSAGE, DOOR, RWALL = 0, 1, 2, 3, 4, 5
PASSABLE = (FLOOR, STAIRS, PASSAGE, DOOR)
DIRS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
VS_POISON, VS_MAGIC = 0, 3
LAMP_DIST = 3

ACTIONS = ["attack", "throw", "approach", "flee", "quaff_heal", "quaff_str", "read_enchant", "read_map", "read_teleport",
           "eat", "pick_up", "equip", "explore", "descend", "rest"]


def parse_dice(s):
    return [tuple(int(v) for v in part.split("x")) for part in s.split("/")] if s and "%" not in s else [(0, 0)]


def avg_dice(dice):
    return sum(n * (s + 1) / 2 for n, s in dice)


HERO_FIELDS = ("kills", "gold", "level", "exp", "str", "max_str", "hp", "max_hp", "food_left", "food", "potions",
               "weapon", "armor", "gear", "no_food", "missiles", "scrolls", "bow")


class Game:
    def __init__(self, seed=None, start=None):
        """start に hero_state() の結果を渡すと、その勇者でその階から始める (学習で深い階を練習するため)。"""
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
        self.weapon = dict(name=name, dice=parse_dice(dmg), hplus=hplus, dplus=dplus)
        self.armor = dict(name=D.INIT_ARMOR[0], ac=D.INIT_ARMOR[1])
        self.gear = []                          # 拾ったが装備していない武器・防具
        self.bow = True                         # 弓を持っているか (初期装備)。持っていれば矢に弓の威力が乗る
        self.missiles = {"arrow": D.INIT_ARROWS[0] + self.rnd(D.INIT_ARROWS[1])}  # 投げる物: 名前 -> 本数
        self.scrolls = {}                       # 巻物: 名前 -> 枚数
        self.no_command = 0                     # 凍結・気絶で動けない残りターン
        self.confused = 0
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
        return {"depth": self.depth, **{k: copy.deepcopy(getattr(self, k)) for k in HERO_FIELDS}}

    # ------------------------------------------------------------------ 乱数 (本家と同じ語彙)
    def rnd(self, n):
        return self.rng.randrange(n) if n > 0 else 0

    def roll(self, n, sides):
        return sum(self.rnd(sides) + 1 for _ in range(n))

    def spread(self, n):
        return n - n // 10 + self.rnd(n // 5)

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
        c.seen = [row[:] for row in self.seen]  # tiles と rooms は階の途中で書き換えないので共有でよい
        c.monsters = [dict(m) for m in self.monsters]
        c.items = [dict(i) for i in self.items]
        c.potions = dict(self.potions)
        c.missiles, c.scrolls = dict(self.missiles), dict(self.scrolls)
        c.weapon, c.armor = dict(self.weapon), dict(self.armor)
        c.gear = [dict(x) for x in self.gear]
        c.visible = set(self.visible)
        c.newly_seen = []
        c.log = []
        c._dist = None
        c._explore = list(self._explore)
        return c

    # ------------------------------------------------------------------ 階の生成 (3×3 の区画に部屋、全域木 + 余分な通路)
    def new_floor(self):
        rng = self.rng
        self.depth += 1
        if self.depth >= D.GOAL_DEPTH:
            self.won = True
            self.say(f"地下 {self.depth} 階に到達した！")
        self.no_food += 1
        self.held_by, self.vf_hit = None, 0
        while True:  # どの部屋にも歩いて行ける地図ができるまで作り直す (保険。通常は 1 回で通る)
            self._build_map()
            if self._all_joined():
                break
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
            room = dict(x=x, y=y, w=w, h=h, gone=False, dark=self.rnd(10) < self.depth - 1, gold=False)
            self.rooms.append(room)
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    edge = yy in (y, y + h - 1) or xx in (x, x + w - 1)
                    self.tiles[yy][xx] = RWALL if edge else FLOOR
        self._passages()

    def _all_joined(self):
        starts = [(r["x"] + (0 if r["gone"] else 1), r["y"] + (0 if r["gone"] else 1)) for r in self.rooms]
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
        for _ in range(D.MAX_OBJ):
            if self.rnd(100) < 36:
                thing = self._new_thing()
                if thing:
                    thing["x"], thing["y"] = self._floor_spot(rng.choice(real), taken)
                    self.items.append(thing)
        self.seen = [[False] * W for _ in range(H)]
        self.mapped = set()                     # 魔法の地図で知ったマス。歩く経路と階段の位置には使えるが、探索の「見た」には数えない
        self.visible = set()
        self.newly_seen = []
        self._dist = None
        self._explore = []
        # 歩けるマスごとの「1 歩で行けるマス」と「周囲 8 マス」。階の途中で変わらないので複製とも共有する
        cells = [(x, y) for y in range(H) for x in range(W) if self.tiles[y][x] in PASSABLE]
        self._nbr = {c: tuple((c[0] + dx, c[1] + dy) for dx, dy in DIRS if self._step_ok(c[0], c[1], c[0] + dx, c[1] + dy))
                     for c in cells}
        self._nb8 = {c: tuple((c[0] + dx, c[1] + dy) for dx, dy in DIRS if 0 <= c[0] + dx < W and 0 <= c[1] + dy < H)
                     for c in cells}
        self._look()
        if not self.won:
            self.say(f"地下 {self.depth} 階")

    def _floor_spot(self, room, taken):
        for _ in range(60):
            p = (room["x"] + 1 + self.rnd(room["w"] - 2), room["y"] + 1 + self.rnd(room["h"] - 2))
            if p not in taken:
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
        self.monsters.append(dict(id=self._next_id, ch=ch, kind=name, jp=jp, x=x, y=y, hp=hp, max_hp=hp, lvl=lvl, arm=arm,
                                  dice=parse_dice(dmg), exp=exp + add * 10 + mod, flags=flags, carry=carry,
                                  awake=awake, gazed=False))

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
            return dict(kind="potion", name=name) if name in D.STAGE1_POTIONS else None
        if kind == "scroll":
            name = self._pick(D.SCROLL_PROBS)[0]
            return dict(kind="scroll", name=name) if name in D.STAGE2_SCROLLS else None
        if kind == "weapon":
            name, _, dmg, hurl, launcher = self._pick(D.WEAPONS)
            if name == "short bow":
                return dict(kind="bow", name=name)
            if name in D.MISSILES:
                return dict(kind="missile", name=name, count=self.rnd(8) + 8 if name in D.STACKED else 1)
            r = self.rnd(100)
            hplus = -(self.rnd(3) + 1) if r < 10 else self.rnd(3) + 1 if r < 15 else 0
            return dict(kind="weapon", name=name, dice=parse_dice(dmg), hplus=hplus, dplus=0)
        if kind == "armor":
            name, _, ac = self._pick(D.ARMORS)
            r = self.rnd(100)
            ac += self.rnd(3) + 1 if r < 20 else -(self.rnd(3) + 1) if r < 28 else 0
            return dict(kind="armor", name=name, ac=ac)
        return None

    # ------------------------------------------------------------------ 視界: 明るい部屋は全体、それ以外は隣のマスだけ
    def room_at(self, x, y):
        for r in self.rooms:
            if not r["gone"] and r["x"] <= x < r["x"] + r["w"] and r["y"] <= y < r["y"] + r["h"]:
                return r
        return None

    def _look(self):
        vis = {(self.hx + dx, self.hy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               if 0 <= self.hx + dx < W and 0 <= self.hy + dy < H}
        r = self.room_at(self.hx, self.hy)
        if r and not r["dark"]:
            vis.update((x, y) for y in range(r["y"], r["y"] + r["h"]) for x in range(r["x"], r["x"] + r["w"]))
        self.visible = vis
        for x, y in vis:
            if not self.seen[y][x]:
                self.seen[y][x] = True
                self.newly_seen.append((x, y, self.tiles[y][x]))
                if self.tiles[y][x] != ROCK:
                    self.explored += 1

    def dist(self, x, y):
        return max(abs(x - self.hx), abs(y - self.hy))

    def visible_monsters(self):
        return sorted((m for m in self.monsters if (m["x"], m["y"]) in self.visible), key=lambda m: self.dist(m["x"], m["y"]))

    def visible_items(self):
        return sorted((i for i in self.items if (i["x"], i["y"]) in self.visible), key=lambda i: self.dist(i["x"], i["y"]))

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
        visible = self.visible
        awake = {(m["x"], m["y"]) for m in self.monsters if (m["x"], m["y"]) in visible and m["awake"]}
        asleep = {(m["x"], m["y"]) for m in self.monsters if (m["x"], m["y"]) in visible and not m["awake"]}
        path = self._bfs(goal_fn, awake | asleep)
        if path is None and asleep:
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
        path = self._bfs_path(lambda c: c == target)
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
        hplus = self.weapon["hplus"] + D.STR_PLUS[self.str] + (0 if m["awake"] else 4)
        per_hit = avg_dice(self.weapon["dice"]) + self.weapon["dplus"] + D.ADD_DAM[self.str]
        return self.hit_chance(self.level, m["arm"], hplus) * max(0.0, per_hit)

    def monster_damage_per_turn(self, m):
        dice = [(self.vf_hit or 1, 1)] if m["ch"] == "F" else m["dice"]
        return sum(self.hit_chance(m["lvl"], self.armor["ac"], 0) * n * (s + 1) / 2 for n, s in dice)

    def _hero_attacks(self, m):
        hplus = self.weapon["hplus"] + D.STR_PLUS[self.str] + (0 if m["awake"] else 4)
        m["awake"] = True
        hit = False
        for n, s in self.weapon["dice"]:
            if self._swing(self.level, m["arm"], hplus):
                m["hp"] -= max(0, self.roll(n, s) + self.weapon["dplus"] + D.ADD_DAM[self.str])
                hit = True
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

    def _monster_attacks(self, m):
        self.quiet = 0
        dice = [(self.vf_hit or 0, 1)] if m["ch"] == "F" else m["dice"]
        hit, before = False, self.hp
        for n, s in dice:
            if self._swing(m["lvl"], self.armor["ac"], 0):
                self.hp -= max(0, self.roll(n, s))
                hit = True
        if not hit:
            self.say(f"{m['jp']}の攻撃は外れた")
            return
        if before > self.hp:
            self.say(f"{m['jp']}の攻撃！ {before - self.hp} ダメージ")
        ch = m["ch"]
        if ch == "A":
            if self.armor["name"] != "leather armor" and self.armor["ac"] < 9 and not self.armor.get("protected"):
                self.armor["ac"] += 1
                self.say("鎧が錆びて弱くなった")
        elif ch == "I":
            self.no_command += self.rnd(2) + 2
            self.say("凍りついて動けない")
        elif ch == "R":
            if not self.save(VS_POISON) and self.str > 3:
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
                    self.hp = 0
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
    def _better_gear(self):
        best = None
        for g in self.gear:
            if g["kind"] == "armor" and g["ac"] < self.armor["ac"]:
                gain = self.armor["ac"] - g["ac"]
            elif g["kind"] == "weapon":
                gain = (avg_dice(g["dice"]) + g["dplus"] + g["hplus"] * 0.5) - (avg_dice(self.weapon["dice"]) + self.weapon["dplus"] + self.weapon["hplus"] * 0.5)
            else:
                continue
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, g)
        return best[1] if best else None

    def has_heal(self):
        return self.potions.get("extra healing", 0) + self.potions.get("healing", 0)

    def has_str_potion(self):
        return self.potions.get("gain strength", 0) + (self.potions.get("restore strength", 0) if self.str < self.max_str else 0)

    def valid_actions(self):
        if self.no_command > 0:
            return ["rest"]
        mons = self.visible_monsters()
        adjacent = [m for m in mons if self._adjacent(m)]
        free = self.held_by is None
        v = []
        awake = any(m["awake"] for m in mons)
        if adjacent:
            v.append("attack")
        if self.missiles and self._throw_target():
            v.append("throw")
        if free and mons and not adjacent and self._step_toward((mons[0]["x"], mons[0]["y"])):
            v.append("approach")
        if free and awake:
            v.append("flee")
        if self.has_heal() and self.hp < self.max_hp:
            v.append("quaff_heal")
        if self.has_str_potion():
            v.append("quaff_str")
        if self._enchant_scroll():
            v.append("read_enchant")
        if self.scrolls.get("magic mapping") and not self.stairs_known():
            v.append("read_map")
        if self.scrolls.get("teleportation") and awake:
            v.append("read_teleport")
        if self.food and self.food_left < 1000:
            v.append("eat")
        if free and self.visible_items():
            v.append("pick_up")
        if self._better_gear():
            v.append("equip")
        if free and self._explore_step():
            v.append("explore")
        if free and self.stairs_known():
            v.append("descend")
        v.append("rest")
        return v

    def stairs_known(self):
        return self.seen[self.stairs[1]][self.stairs[0]] or self.stairs in self.mapped

    def _enchant_scroll(self):
        return next((n for n in D.ENCHANT_SCROLLS if self.scrolls.get(n)), None)

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
                    if m["awake"] and (nx, ny) in self.visible and d >= 2 and (best is None or d < best[1]):
                        best = (m, d, (x, y))
                    break
                x, y = nx, ny
        return best

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
        """投げたときのダメージダイス。矢は弓を持っていてこそ (持っていなければ振り回しの 1x1)。"""
        for n, _, dmg, hurl, launcher in D.WEAPONS:
            if n == name:
                return hurl if launcher is None or self.bow else dmg
        return "1x1"

    def missile_damage_per_turn(self, m):
        if not self.missiles:
            return 0.0
        best = max(avg_dice(parse_dice(self._hurl_dice(n))) for n in self.missiles)
        return self.hit_chance(self.level, m["arm"], D.STR_PLUS[self.str]) * max(0.0, best + D.ADD_DAM[self.str])

    # ------------------------------------------------------------------ 巻物 (scrolls.c)
    def _read(self, name):
        self.scrolls[name] -= 1
        if self.scrolls[name] <= 0:
            del self.scrolls[name]
        if name == "enchant armor":
            self.armor["ac"] -= 1
            self.say("鎧が輝いた (強化)")
        elif name == "enchant weapon":
            if self.rnd(2) == 0:
                self.weapon["hplus"] += 1
            else:
                self.weapon["dplus"] += 1
            self.say(f"{self.weapon['name']} が輝いた (強化)")
        elif name == "protect armor":
            self.armor["protected"] = True
            self.say("鎧が錆びなくなった")
        elif name == "magic mapping":
            for y in range(H):
                for x in range(W):
                    if self.tiles[y][x] != ROCK and not self.seen[y][x] and (x, y) not in self.mapped:
                        self.mapped.add((x, y))
                        self.newly_seen.append((x, y, self.tiles[y][x]))
            self._explore = []
            self.say("この階の地図が頭に浮かんだ")
        elif name == "teleportation":
            real = [r for r in self.rooms if not r["gone"]]
            taken = {(m["x"], m["y"]) for m in self.monsters} | {(self.hx, self.hy)}
            self.hx, self.hy = self._floor_spot(self.rng.choice(real), taken)
            self.held_by, self.vf_hit = None, 0
            self._explore = []
            self._look()
            self.say("別の場所に飛ばされた")

    def _adjacent(self, m):
        return (m["x"], m["y"]) in self._nbr[(self.hx, self.hy)]

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

    def step(self, action):
        """勇者が action を実行し、続けて全モンスターが動き、空腹と自然回復が進む。"""
        self.turn += 1
        self._dist = None
        if self.no_command > 0:
            self.no_command -= 1
        else:
            if self._act(action):
                return  # 階を降りた
        if self.confused:
            self.confused -= 1
        self._look()
        self._monsters_act()
        if not self.dead:
            self._doctor()
            self._stomach()
            self._wanderer()

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
        elif action == "read_enchant" and self._enchant_scroll():
            self._read(self._enchant_scroll())
        elif action == "read_map" and self.scrolls.get("magic mapping"):
            self._read("magic mapping")
        elif action == "read_teleport" and self.scrolls.get("teleportation"):
            self._read("teleportation")
        elif action == "quaff_heal" and self.has_heal():
            # 体力が大きく減っていれば強い薬から、少しなら弱い薬から
            order = ["extra healing", "healing"] if self.hp <= self.max_hp // 2 else ["healing", "extra healing"]
            kind = next(k for k in order if self.potions.get(k, 0))
            self.potions[kind] -= 1
            self.hp += self.roll(self.level, 8 if kind == "extra healing" else 4)
            if self.hp > self.max_hp:
                if kind == "extra healing" and self.hp > self.max_hp + self.level + 1:
                    self.max_hp += 1
                self.max_hp += 1
                self.hp = self.max_hp
            self.confused = 0 if kind == "extra healing" else self.confused
            self.say("回復薬を飲んだ")
        elif action == "quaff_str" and self.has_str_potion():
            if self.potions.get("restore strength", 0) and self.str < self.max_str:
                self.potions["restore strength"] -= 1
                self.str = self.max_str
                self.say("力が戻った")
            else:
                self.potions["gain strength"] -= 1
                self.str = min(31, self.str + 1)
                self.max_str = max(self.max_str, self.str)
                self.say("力が強くなった")
        elif action == "eat" and self.food:
            self.food -= 1
            self.food_left = min(D.STOMACH_SIZE, max(0, self.food_left) + D.HUNGER_TIME - 200 + self.rnd(400))
            self.say("食事をした")
        elif action == "pick_up":
            items = self.visible_items()
            if items:
                self._move_to(self._step_toward((items[0]["x"], items[0]["y"])))
        elif action == "equip":
            g = self._better_gear()
            if g:
                self.gear.remove(g)
                if g["kind"] == "armor":
                    self.armor = dict(name=g["name"], ac=g["ac"])
                else:
                    self.weapon = dict(name=g["name"], dice=g["dice"], hplus=g["hplus"], dplus=g["dplus"])
                self.say(f"{g['name']} を装備した")
        elif action == "explore":
            self._move_to(self._explore_step())
            if self._explore and (self.hx, self.hy) == self._explore[0]:
                self._explore.pop(0)
        elif action == "descend":
            if (self.hx, self.hy) == self.stairs:
                self.new_floor()
                return True
            self._move_to(self._step_toward(self.stairs))

        for it in [i for i in self.items if (i["x"], i["y"]) == (self.hx, self.hy)]:
            self.items.remove(it)
            if it["kind"] == "gold":
                self.gold += it["value"]
                self.say(f"金貨 {it['value']} 枚を拾った")
            elif it["kind"] == "food":
                self.food += 1
                self.say("食料を拾った")
            elif it["kind"] == "potion":
                self.potions[it["name"]] = self.potions.get(it["name"], 0) + 1
                self.say(f"薬 ({it['name']}) を拾った")
            elif it["kind"] == "scroll":
                self.scrolls[it["name"]] = self.scrolls.get(it["name"], 0) + 1
                self.say(f"巻物 ({it['name']}) を拾った")
            elif it["kind"] == "missile":
                self.missiles[it["name"]] = self.missiles.get(it["name"], 0) + it["count"]
                self.say(f"{it['name']} を {it['count']} 拾った")
            elif it["kind"] == "bow":
                self.bow = True
                self.say("弓を拾った")
            else:
                self.gear.append(it)
                self.say(f"{it['name']} を拾った")
        return False

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
                if seen and (("M" in m["flags"] and self.rnd(3) != 0) or "G" in m["flags"]):
                    m["awake"] = True
                continue
            if m["ch"] == "M" and seen and not m["gazed"]:
                r = self.room_at(self.hx, self.hy)
                if (r and not r["dark"]) or self.dist(m["x"], m["y"]) < LAMP_DIST:
                    m["gazed"] = True
                    if not self.save(VS_MAGIC):
                        self.confused += self.spread(20)
                        self.say("メデューサの視線で混乱した")
            if self._adjacent(m):
                self._monster_attacks(m)
                continue
            if m["ch"] == "F":
                continue
            occupied = {(o["x"], o["y"]) for o in self.monsters if o is not m} | {(self.hx, self.hy)}
            steps = [p for p in self._nbr[(m["x"], m["y"])] if p not in occupied]
            if not steps:
                continue
            if (m["ch"] == "B" and self.rnd(2) == 0) or (m["ch"] == "P" and self.rnd(5) == 0):
                m["x"], m["y"] = self.rng.choice(steps)  # コウモリとファントムはふらふら動く
                continue
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
        self.food_left -= 1
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
