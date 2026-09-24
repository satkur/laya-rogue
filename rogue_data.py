# Data tables derived from Rogue 5.4.4 (monsters, experience levels, strength bonuses, hunger, items, scrolls, missiles).
# Copyright (C) 1980-1983, 1985, 1999 Michael Toy, Ken Arnold and Glenn Wichman.
# Used under the BSD 3-Clause license; see THIRD_PARTY_NOTICES.md.
# Python implementation (c) 2026 satkur, MIT.
"""本家 Rogue 5.4.4 の数値表。ルールの実装 (game.py) はこの数値を読むだけで、ここに調整値は置かない。"""

AMULET_LEVEL = 26
GOAL_DEPTH = 26          # 目標の階 (本家の魔除けがある階。2026-09-24 に 20 → 26)

# 空腹 (rogue.h)
HUNGER_TIME = 1300       # 開始時と、食事 1 回で増える量の基準
MORE_TIME = 150          # これ未満で weak、2 倍未満で hungry
STOMACH_SIZE = 2000
STARVE_TIME = 850        # 0 を割ってからこのターン数で餓死

WANDER_TIME = 70         # 徘徊モンスターの出現間隔の基準 (spread = ±10%)

# 罠 (rogue.h と move.c の be_trapped)。rnd(10) < 階 なら rnd(階 / 4) + 1 個 (最大 MAXTRAPS)、種類は等確率。踏むまで見えない
TRAPS = [("trap door", "落とし穴"), ("bear trap", "熊の罠"), ("sleeping gas", "眠りガス"), ("arrow trap", "矢の罠"),
         ("teleport trap", "転移の罠"), ("dart trap", "毒ダーツ"), ("rust trap", "錆びの罠")]
MAXTRAPS = 10
HIDDEN_DOORS = False     # 隠し扉と「捜索」を出すか。判断が生まれず成績と見た目を損ねるだけだったので切ってある (NOTES.md 9〜10 章)
SEARCH_CAPS = (20, 6)    # 同じマスで捜索する回数の上限 (本家にはない): 通路の行き止まり / 部屋の壁ぎわ。
                         # 見つかる確率は 1 回 1/5 なので、20 回で 99%、6 回で 74%。全部使い切ったら、回数の少ない所から順に探し続ける
BEARTIME = 3             # 熊の罠で動けないターン数 (rogue.h: spread(3))
SLEEPTIME = 5            # 眠りガスで何もできないターン数 (rogue.h: spread(5))。眠りの巻物は rnd(SLEEPTIME) + 4
MAX_OBJ = 9              # 1 階あたりのアイテム出現試行回数 (各 36%)

# (名前, 日本語名, 持ち物確率, フラグ, 経験値, レベル, 防御, ダメージダイス)
# フラグ: M=mean (見かけると 2/3 で襲ってくる) F=fly G=greedy I=invisible
MONSTERS = {
    "A": ("aquator", "アクエーター", 0, "M", 20, 5, 2, "0x0/0x0"),
    "B": ("bat", "コウモリ", 0, "F", 1, 1, 3, "1x2"),
    "C": ("centaur", "ケンタウロス", 15, "", 17, 4, 4, "1x2/1x5/1x5"),
    "D": ("dragon", "ドラゴン", 100, "M", 5000, 10, -1, "1x8/1x8/3x10"),
    "E": ("emu", "エミュー", 0, "M", 2, 1, 7, "1x2"),
    "F": ("venus flytrap", "ハエトリグサ", 0, "M", 80, 8, 3, "%%%x0"),
    "G": ("griffin", "グリフィン", 20, "MF", 2000, 13, 2, "4x3/3x5"),
    "H": ("hobgoblin", "ホブゴブリン", 0, "M", 3, 1, 5, "1x8"),
    "I": ("ice monster", "アイスモンスター", 0, "", 5, 1, 9, "0x0"),
    "J": ("jabberwock", "ジャバウォック", 70, "", 3000, 15, 6, "2x12/2x4"),
    "K": ("kestrel", "チョウゲンボウ", 0, "MF", 1, 1, 7, "1x4"),
    "L": ("leprechaun", "レプラコーン", 0, "", 10, 3, 8, "1x1"),
    "M": ("medusa", "メデューサ", 40, "M", 200, 8, 2, "3x4/3x4/2x5"),
    "N": ("nymph", "ニンフ", 100, "", 37, 3, 9, "0x0"),
    "O": ("orc", "オーク", 15, "G", 5, 1, 6, "1x8"),
    "P": ("phantom", "ファントム", 0, "I", 120, 8, 3, "4x4"),
    "Q": ("quagga", "クアッガ", 0, "M", 15, 3, 3, "1x5/1x5"),
    "R": ("rattlesnake", "ガラガラヘビ", 0, "M", 9, 2, 3, "1x6"),
    "S": ("snake", "ヘビ", 0, "M", 2, 1, 5, "1x3"),
    "T": ("troll", "トロル", 50, "M", 120, 6, 4, "1x8/1x8/2x6"),
    "U": ("black unicorn", "ブラックユニコーン", 0, "M", 190, 7, -2, "1x9/1x9/2x9"),
    "V": ("vampire", "バンパイア", 20, "M", 350, 8, 1, "1x10"),
    "W": ("wraith", "レイス", 0, "", 55, 5, 4, "1x6"),
    "X": ("xeroc", "ゼロック", 30, "", 100, 7, 7, "4x4"),
    "Y": ("yeti", "イエティ", 30, "", 50, 4, 6, "1x6/1x6"),
    "Z": ("zombie", "ゾンビ", 0, "M", 6, 2, 8, "1x8"),
}

# 階ごとの出現順 (monsters.c の lvl_mons)。d = 階 + rnd(10) - 6 番目を引く。徘徊用は一部が出ない (wand_mons)
LEVEL_MONSTERS = "KEBSHIROZLCQANYFTWPXUMVGJD"
WANDER_MONSTERS = "KEBSH ROZ CQA Y TWP UMVGJ "

# 経験値がこの値に達するとレベルが上がる (extern.c の e_levels)
EXP_LEVELS = [10, 20, 40, 80, 160, 320, 640, 1300, 2600, 5200, 13000, 26000, 50000, 100000, 200000,
              400000, 800000, 2000000, 4000000, 8000000]

# 腕力による命中・ダメージ補正 (fight.c)。添字は腕力の値
STR_PLUS = [-7, -6, -5, -4, -3, -2, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3]
ADD_DAM = [-7, -6, -5, -4, -3, -2, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 2, 3, 3, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 6]

# 勇者の初期値 (extern.c の INIT_STATS と init.c)
INIT_STR, INIT_HP = 16, 12
INIT_WEAPON = ("mace", "2x4", 1, 1)      # 名前, ダメージ, 命中+, ダメージ+
INIT_ARMOR = ("ring mail", 7 - 1)        # 名前, 防御 (+1 の ring mail)

# 落ちている物の種類の比率 (extern.c の things)
THING_PROBS = [("potion", 26), ("scroll", 36), ("food", 16), ("weapon", 7), ("armor", 7), ("ring", 4), ("stick", 4)]

# 薬 (pot_info の出現率)。実装しているのは POTIONS_IN_PLAY の 6 種 (残りを引いたときは何も出ない = 本家より物資が少ない)
POTION_PROBS = [("confusion", 7), ("hallucination", 8), ("poison", 8), ("gain strength", 13), ("see invisible", 3),
                ("healing", 13), ("monster detection", 6), ("magic detection", 6), ("raise level", 2),
                ("extra healing", 5), ("haste self", 5), ("restore strength", 13), ("blindness", 5), ("levitation", 6)]
POTIONS_IN_PLAY = {"healing", "extra healing", "gain strength", "restore strength", "poison", "confusion"}
BAD_POTIONS = {"poison", "confusion"}   # 正体が分かったら捨てる (本家でも使い道がない)
POTION_JP = {"healing": "回復", "extra healing": "大回復", "gain strength": "力", "restore strength": "力の回復", "poison": "毒", "confusion": "混乱"}
HUHDURATION = 20                        # 混乱の薬: rnd(8) + 20 ターン (potions.c)
# 未識別のあいだの薬の色 (extern.c の rainbow)。ゲームごとに種類へ割り当てる。表示にだけ使い、Laya には見せない
POTION_COLORS = ["琥珀", "藍", "黒", "青", "茶", "透明", "深紅", "水", "生成", "金", "緑", "灰", "赤紫", "橙", "桃", "梅", "紫", "赤", "銀",
                 "黄土", "蜜柑", "黄玉", "青緑", "朱", "菫", "白", "黄"]
# 巻物の題名の音節 (init.c の sylls から)。2〜3 個つないで題名にする
SCROLL_SYLLABLES = ["blech", "foo", "barf", "rech", "bar", "blech", "quo", "bloto", "oh", "caca", "blorp", "erp", "festr", "rot", "slie",
                    "snorf", "iky", "yuky", "ooze", "ah", "bahl", "zep", "druhl", "flem", "behil", "arek", "mep", "zihr", "grit", "kona",
                    "kini", "ichi", "tims", "ogr", "oo", "ighr", "coph", "swerr", "mihr", "poxi", "nuxi", "mun", "toxi"]

# 武器 (weap_info の出現率と weapons.c のダメージ): (名前, 出現率, 振り回し, 投げたとき, 射出する武器)
WEAPONS = [("mace", 11, "2x4", "1x3", None), ("long sword", 11, "3x4", "1x2", None), ("short bow", 12, "1x1", "1x1", None),
           ("arrow", 12, "1x1", "2x3", "short bow"), ("dagger", 8, "1x6", "1x4", None), ("two handed sword", 10, "4x4", "1x2", None),
           ("dart", 12, "1x1", "1x3", None), ("shuriken", 12, "1x2", "2x4", None), ("spear", 12, "2x3", "1x6", None)]
MISSILES = {"arrow", "dart", "shuriken", "dagger", "spear"}   # 投げる物として扱う (振り回しでは初期装備の mace に劣る)
STACKED = {"arrow", "dart", "shuriken"}                        # まとまって落ちている (rnd(8) + 8 本)
INIT_ARROWS = (25, 15)                                        # 初期装備の矢: 25 + rnd(15) 本 (init.c)。弓も持って始まる

# 巻物 (scr_info の出現率)。実装しているのは SCROLLS_IN_PLAY の 9 種 (解呪・混乱・拘束・恐怖・食料探知は判断ボードでオミット)
SCROLL_PROBS = [("monster confusion", 7), ("magic mapping", 4), ("hold monster", 2), ("sleep", 3), ("enchant armor", 7),
                ("identify potion", 10), ("identify scroll", 10), ("identify weapon", 6), ("identify armor", 7),
                ("identify ring, wand or staff", 10), ("scare monster", 3), ("food detection", 2), ("teleportation", 5),
                ("enchant weapon", 8), ("create monster", 4), ("remove curse", 7), ("aggravate monsters", 3), ("protect armor", 2)]
# 未識別 (簡略版): 使えば正体が分かる。識別の巻物は本家の 5 種 (薬・巻物・武器・鎧・指輪杖) を 1 種にまとめ、出現率は合算 (43)
SCROLLS_IN_PLAY = {"enchant armor", "enchant weapon", "protect armor", "magic mapping", "teleportation", "identify",
                   "sleep", "create monster", "aggravate monsters"}
BAD_SCROLLS = {"sleep", "create monster", "aggravate monsters"}
SCROLL_JP = {"enchant armor": "鎧強化", "enchant weapon": "武器強化", "protect armor": "鎧保護", "magic mapping": "魔法の地図",
             "teleportation": "瞬間移動", "identify": "識別", "sleep": "眠り", "create monster": "怪物召喚", "aggravate monsters": "怪物寄せ"}
ENCHANT_SCROLLS = ("enchant armor", "enchant weapon", "protect armor")   # 読めば必ず得をする巻物 (拾った時点で読む)

# 杖 (ws_info の出現率)。判断ボードの回答で 3 種にまとめる: 攻撃 = striking 9 + lightning 3 + fire 3 + cold 3 + magic missile 10、
# 鈍足 = slow monster 11、追放 = teleport away 6。残り (light 12・polymorph 15・haste monster 10・drain life 9・nothing 1・teleport to 6・cancellation 5) は
# 出さないが、杖そのものの本数は本家どおりにする (薬・巻物と違って「消える」扱いにしない。杖は初見で勝てない敵への数少ない答えなので)
STICK_PROBS = [("attack", 28), ("slow monster", 11), ("teleport away", 6)]
STICKS_IN_PLAY = {"attack", "slow monster", "teleport away"}
STICK_JP = {"attack": "攻撃", "slow monster": "鈍足", "teleport away": "追放"}
STICK_CHARGES = (5, 3)   # 回数は rnd(5) + 3 (sticks.c の fix_stick)
STICK_MATERIALS = ["鋼", "黒檀", "樫", "柳", "松", "水晶", "鉄", "銀", "真鍮", "紫檀"]   # 未識別のあいだの見た目 (本家の wood / metal)

# 防具 (arm_info の出現率と a_class の防御。防御は小さいほど硬い)
ARMORS = [("leather armor", 20, 8), ("ring mail", 15, 7), ("studded leather armor", 15, 7), ("scale mail", 13, 6),
          ("chain mail", 12, 5), ("splint mail", 10, 4), ("banded mail", 10, 4), ("plate mail", 5, 3)]
