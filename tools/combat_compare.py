"""In the combat scenes of recordings, compare three choosers: the pilot (recorded), the experience table it was trained on
(softmax argmax) and the hand-written diver. Tells whether combat losses come from the table or from the pilot's imitation of it.

    uv run tools/combat_compare.py data/stage8_all_items/table_r16.json "data/replays/laya-gen25b_5*.json"
"""
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import DiverBrain, coarse_key, dist_word, scene, threat_word  # noqa: E402
from game import Game, active  # noqa: E402
from sim import standard_hero  # noqa: E402

table = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
files = [f for pat in sys.argv[2:] for f in glob.glob(pat) if "_B" not in Path(f).stem.split("_", 1)[1]]
n = Counter()
agree = Counter()
pairs = defaultdict(Counter)   # (bucket) -> Counter of (pilot, table, diver)
bucket_n = Counter()
for f in files:
    rec = json.loads(Path(f).read_text(encoding="utf-8"))
    if not rec.get("done"):
        continue
    g = Game(rec["seed"], standard_hero(rec["start"]) if rec["start"] else None, rec["difficulty"])
    diver = DiverBrain()
    for i, a in enumerate(rec["actions"]):
        valid = g.valid_actions()
        d = diver.decide(g)["action"]
        if scene(g, valid) == "combat":
            q = table.get(coarse_key(g, valid), {}).get("q")
            t = max((x for x in valid if x in q), key=lambda x: q[x][0]) if q else None
            awake = [m for m in g.visible_monsters() if active(m)]
            worst = max(awake, key=lambda m: ({"weak": 0, "even": 1, "deadly": 2, "unknown": 1}[threat_word(g, m)], -g.dist(m["x"], m["y"])))
            b = f"{threat_word(g, worst)}-{dist_word(g.dist(worst['x'], worst['y']))}" + ("+" if len(awake) > 1 else "")
            n["combat"] += 1
            n["table known"] += t is not None
            agree["pilot=table"] += a == t
            agree["pilot=diver"] += a == d
            agree["table=diver"] += t == d
            bucket_n[b] += 1
            pairs[b][(a, t, d)] += 1
        g.pick_kind = rec.get("picks", {}).get(str(i))
        g.step(a)
        g.log.clear()
c = n["combat"]
print(f"戦闘場面 {c} 手 (表にキーあり {n['table known'] / c:.0%}) | 一致: Laya=表 {agree['pilot=table'] / c:.0%}  Laya=diver {agree['pilot=diver'] / c:.0%}  表=diver {agree['table=diver'] / c:.0%}")
print("\n敵 (最悪の 1 体) ごと: 手数 / Laya=表 / 表=diver / 主な組 (Laya, 表, diver)")
for b, k in bucket_n.most_common():
    ps = pairs[b]
    pt = sum(v for (a, t, d), v in ps.items() if a == t) / k
    td = sum(v for (a, t, d), v in ps.items() if t == d) / k
    top = "  ".join(f"({a},{t},{d}) {v}" for (a, t, d), v in ps.most_common(4))
    print(f"  {b:22s} {k:6d}  {pt:4.0%}  {td:4.0%}  {top}")
