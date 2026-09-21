"""ターン制ローグライクの本体。描画も AI も持たない純粋なルール。

勇者の 1 ターンは「高レベル行動」(ACTIONS) を 1 つ選ぶこと。どのマスへ動くかといった
幾何の計算はここで行い、「今なにをすべきか」の判断だけを外 (Laya) に委ねる。
"""
import math
import random
from collections import deque

W, H = 48, 26
WALL, FLOOR, STAIRS = 0, 1, 2
SIGHT = 8
DIRS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

NAMES = {"rat": "大ネズミ", "goblin": "ゴブリン", "ogre": "オーガ"}
ACTIONS = ["attack", "approach", "flee", "drink_potion", "pick_up", "explore", "descend", "rest"]

# slow: 2 ターンに 1 回しか動けない (逃げ切れる相手)
MONSTERS = {
    "rat": dict(hp=4, atk=(1, 2), xp=1, slow=False),
    "goblin": dict(hp=9, atk=(2, 4), xp=3, slow=False),
    "ogre": dict(hp=20, atk=(4, 7), xp=8, slow=True),
}


def _line(x0, y0, x1, y1):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        yield x0, y0
        if (x0, y0) == (x1, y1):
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


class Game:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.depth = 0
        self.turn = 0
        self.kills = 0
        self.gold = 0
        self.level = 1
        self.xp = 0
        self.max_hp = 28
        self.hp = self.max_hp
        self.potions = 1
        self.dead = False
        self.log = []
        self._next_id = 0
        self.explored = 0  # これまでに見たマスの累計 (階をまたいで増え続ける)
        self.new_floor()

    def clone(self, seed):
        """先読みシミュレーション用の複製。乱数だけ別系列にして「ありえた未来」を分岐させる。"""
        c = Game.__new__(Game)
        c.__dict__.update(self.__dict__)
        c.rng = random.Random(seed)
        c.seen = [row[:] for row in self.seen]  # tiles は階の途中で書き換えないので共有でよい
        c.monsters = [dict(m) for m in self.monsters]
        c.items = [dict(i) for i in self.items]
        c.visible = set(self.visible)
        c.newly_seen = []
        c.log = []
        return c

    # ------------------------------------------------------------------ 生成
    def new_floor(self):
        rng = self.rng
        self.depth += 1
        self.tiles = [[WALL] * W for _ in range(H)]
        rooms = []
        for _ in range(60):
            w, h = rng.randint(5, 10), rng.randint(4, 7)
            x, y = rng.randint(1, W - w - 1), rng.randint(1, H - h - 1)
            if any(x <= r[0] + r[2] + 1 and r[0] <= x + w + 1 and y <= r[1] + r[3] + 1 and r[1] <= y + h + 1 for r in rooms):
                continue
            rooms.append((x, y, w, h))
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    self.tiles[yy][xx] = FLOOR
            if len(rooms) == 9:
                break
        rooms.sort(key=lambda r: r[0] + r[1] * 0.5)
        centers = [(r[0] + r[2] // 2, r[1] + r[3] // 2) for r in rooms]
        for (ax, ay), (bx, by) in zip(centers, centers[1:]):
            for x in range(min(ax, bx), max(ax, bx) + 1):
                self.tiles[ay][x] = FLOOR
            for y in range(min(ay, by), max(ay, by) + 1):
                self.tiles[y][bx] = FLOOR
        self.hx, self.hy = centers[0]
        sx, sy = centers[-1]
        self.tiles[sy][sx] = STAIRS
        self.stairs = (sx, sy)

        def spot(room_from=1):
            while True:
                x, y, w, h = rng.choice(rooms[room_from:])
                p = (rng.randint(x, x + w - 1), rng.randint(y, y + h - 1))
                if p != self.stairs and p not in taken:
                    taken.add(p)
                    return p

        taken = {(self.hx, self.hy)}
        d = self.depth
        kinds = ["rat"] * max(1, 5 - d) + ["goblin"] * (1 + d) + ["ogre"] * max(0, d - 1)
        scale = 1 + 0.12 * (d - 1)
        self.monsters = []
        for _ in range(3 + d):
            k = rng.choice(kinds)
            x, y = spot()
            hp = math.ceil(MONSTERS[k]["hp"] * scale)
            self._next_id += 1
            self.monsters.append(dict(id=self._next_id, kind=k, x=x, y=y, hp=hp, max_hp=hp, scale=scale, awake=False, skip=False))
        self.items = []
        for kind, n in (("potion", rng.randint(1, 2)), ("gold", rng.randint(2, 4))):
            for _ in range(n):
                x, y = spot(0)
                self.items.append(dict(kind=kind, x=x, y=y))
        self.seen = [[False] * W for _ in range(H)]
        self.visible = set()
        self.newly_seen = []
        self._fov()
        self.say(f"地下 {self.depth} 階")

    # ------------------------------------------------------------------ 視界
    def _los(self, x0, y0, x1, y1):
        for x, y in _line(x0, y0, x1, y1):
            if (x, y) != (x1, y1) and self.tiles[y][x] == WALL:
                return False
        return True

    def _fov(self):
        self.visible = set()
        for y in range(max(0, self.hy - SIGHT), min(H, self.hy + SIGHT + 1)):
            for x in range(max(0, self.hx - SIGHT), min(W, self.hx + SIGHT + 1)):
                if (x - self.hx) ** 2 + (y - self.hy) ** 2 <= SIGHT * SIGHT and self._los(self.hx, self.hy, x, y):
                    self.visible.add((x, y))
                    if not self.seen[y][x]:
                        self.seen[y][x] = True
                        self.explored += 1
                        self.newly_seen.append((x, y, self.tiles[y][x]))

    def say(self, msg):
        self.log.append(msg)

    # ------------------------------------------------------------------ 補助
    def dist(self, x, y):
        return max(abs(x - self.hx), abs(y - self.hy))

    def visible_monsters(self):
        return sorted((m for m in self.monsters if (m["x"], m["y"]) in self.visible), key=lambda m: self.dist(m["x"], m["y"]))

    def visible_items(self):
        return sorted((i for i in self.items if (i["x"], i["y"]) in self.visible), key=lambda i: self.dist(i["x"], i["y"]))

    def hero_avg(self):
        return 3.5 + self.level

    def monster_avg(self, m):
        lo, hi = MONSTERS[m["kind"]]["atk"]
        return (lo + hi) / 2 * m["scale"]

    def _walkable(self, x, y, known_only=True):
        return 0 <= x < W and 0 <= y < H and self.tiles[y][x] != WALL and (self.seen[y][x] or not known_only)

    def _bfs_step(self, goal_fn, avoid_monsters=True):
        """勇者から goal_fn を満たす最寄りマスへの最初の 1 歩。既知のマスだけを通る。"""
        blocked = {(m["x"], m["y"]) for m in self.monsters} if avoid_monsters else set()
        start = (self.hx, self.hy)
        prev = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            if cur != start and goal_fn(*cur):
                while prev[cur] != start:
                    cur = prev[cur]
                return cur
            for dx, dy in DIRS:
                n = (cur[0] + dx, cur[1] + dy)
                if n not in prev and self._walkable(*n) and (n not in blocked or goal_fn(*n)):
                    prev[n] = cur
                    q.append(n)
        return None

    def _frontier(self, x, y):
        return any(0 <= x + dx < W and 0 <= y + dy < H and not self.seen[y + dy][x + dx] for dx, dy in DIRS)

    def _monster_at(self, x, y):
        return next((m for m in self.monsters if (m["x"], m["y"]) == (x, y)), None)

    # ------------------------------------------------------------------ 行動
    def valid_actions(self):
        mons = self.visible_monsters()
        adjacent = [m for m in mons if self.dist(m["x"], m["y"]) == 1]
        v = []
        if adjacent:
            v.append("attack")
        if mons and not adjacent:
            v.append("approach")
        if mons:
            v.append("flee")
        if self.potions and self.hp < self.max_hp:
            v.append("drink_potion")
        if self.visible_items():
            v.append("pick_up")
        if self._bfs_step(self._frontier):
            v.append("explore")
        if self.seen[self.stairs[1]][self.stairs[0]]:
            v.append("descend")
        if not mons and self.hp < self.max_hp:
            v.append("rest")
        return v or ["rest"]

    def _move_or_attack(self, step):
        if step is None:
            return
        m = self._monster_at(*step)
        if m:
            self._hit(m)
        else:
            self.hx, self.hy = step

    def _hit(self, m):
        dmg = self.rng.randint(2, 5) + self.level
        m["hp"] -= dmg
        m["awake"] = True
        if m["hp"] > 0:
            self.say(f"{NAMES[m['kind']]}に {dmg} ダメージ")
            return
        self.monsters.remove(m)
        self.kills += 1
        self.xp += MONSTERS[m["kind"]]["xp"]
        self.say(f"{NAMES[m['kind']]}を倒した")
        while self.xp >= 6 * self.level:
            self.xp -= 6 * self.level
            self.level += 1
            self.max_hp += 5
            self.hp = min(self.max_hp, self.hp + self.max_hp // 3)
            self.say(f"レベル {self.level} に上がった")

    def step(self, action):
        """勇者が action を実行し、続けて全モンスターが動く。"""
        self.turn += 1
        mons = self.visible_monsters()
        if action == "attack":
            adj = [m for m in mons if self.dist(m["x"], m["y"]) == 1]
            if adj:
                self._hit(min(adj, key=lambda m: m["hp"]))
        elif action == "approach":
            if mons:
                t = mons[0]
                self._move_or_attack(self._bfs_step(lambda x, y: (x, y) == (t["x"], t["y"])))
        elif action == "flee":
            self._flee(mons)
        elif action == "drink_potion":
            if self.potions:
                self.potions -= 1
                heal = int(self.max_hp * 0.6)
                self.hp = min(self.max_hp, self.hp + heal)
                self.say("ポーションを飲んだ")
        elif action == "pick_up":
            items = self.visible_items()
            if items:
                t = items[0]
                self._move_or_attack(self._bfs_step(lambda x, y: (x, y) == (t["x"], t["y"])))
        elif action == "explore":
            self._move_or_attack(self._bfs_step(self._frontier))
        elif action == "descend":
            if (self.hx, self.hy) == self.stairs:
                self.new_floor()
                return
            self._move_or_attack(self._bfs_step(lambda x, y: (x, y) == self.stairs))
        elif action == "rest":
            self.hp = min(self.max_hp, self.hp + 2)

        for it in [i for i in self.items if (i["x"], i["y"]) == (self.hx, self.hy)]:
            self.items.remove(it)
            if it["kind"] == "potion":
                self.potions += 1
                self.say("ポーションを拾った")
            else:
                g = self.rng.randint(5, 15) * self.depth
                self.gold += g
                self.say(f"金貨 {g} 枚を拾った")

        self._fov()
        self._monsters_act()

    def _flee(self, mons):
        if not mons:
            return
        occupied = {(m["x"], m["y"]) for m in self.monsters}

        def score(p):
            return sum(1.0 / max(1, max(abs(p[0] - m["x"]), abs(p[1] - m["y"]))) ** 2 for m in mons)

        best, best_s = None, score((self.hx, self.hy))
        for dx, dy in DIRS:
            p = (self.hx + dx, self.hy + dy)
            if self._walkable(*p) and p not in occupied and score(p) < best_s:
                best, best_s = p, score(p)
        if best:
            self.hx, self.hy = best

    def _monsters_act(self):
        for m in list(self.monsters):
            d = self.dist(m["x"], m["y"])
            if not m["awake"] and d <= 7 and (m["x"], m["y"]) in self.visible:
                m["awake"] = True
            if not m["awake"]:
                continue
            if d == 1:
                lo, hi = MONSTERS[m["kind"]]["atk"]
                dmg = max(1, round(self.rng.randint(lo, hi) * m["scale"]))
                self.hp -= dmg
                self.say(f"{NAMES[m['kind']]}の攻撃！ {dmg} ダメージ")
                if self.hp <= 0:
                    self.hp = 0
                    self.dead = True
                    self.say(f"勇者は{NAMES[m['kind']]}に倒された…")
                    return
                continue
            if MONSTERS[m["kind"]]["slow"]:
                m["skip"] = not m["skip"]
                if m["skip"]:
                    continue
            occupied = {(o["x"], o["y"]) for o in self.monsters if o is not m}
            best, best_d = None, (d, abs(m["x"] - self.hx) + abs(m["y"] - self.hy))
            for dx, dy in DIRS:
                p = (m["x"] + dx, m["y"] + dy)
                if self._walkable(*p, known_only=False) and p not in occupied and p != (self.hx, self.hy):
                    nd = (max(abs(p[0] - self.hx), abs(p[1] - self.hy)), abs(p[0] - self.hx) + abs(p[1] - self.hy))
                    if nd < best_d:
                        best, best_d = p, nd
            if best:
                m["x"], m["y"] = best
