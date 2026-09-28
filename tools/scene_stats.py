"""Per-scene statistics from recordings: how many turns the pilot spends in each scene (brain.scene) and how often the
hand-written diver would have chosen the same action there. Cheap companion to the scene swap (sim.py +swap, NOTES 15).

    uv run tools/scene_stats.py "data/replays/laya-gen25b_5*.json"
"""
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import SCENES, DiverBrain, scene  # noqa: E402
from game import Game  # noqa: E402
from sim import standard_hero  # noqa: E402

files = [f for pat in sys.argv[1:] for f in glob.glob(pat) if "_B" not in Path(f).stem.split("_", 1)[1]] if len(sys.argv) > 1 else []
if not files:
    files = [f for f in glob.glob("data/replays/laya-gen25b_5*.json") if "_B" not in f]
turns, agree, pairs, deaths = Counter(), Counter(), defaultdict(Counter), Counter()
for f in files:
    rec = json.loads(Path(f).read_text(encoding="utf-8"))
    if not rec.get("done"):
        continue
    g = Game(rec["seed"], standard_hero(rec["start"]) if rec["start"] else None, rec["difficulty"])
    diver = DiverBrain()
    last = None
    for i, a in enumerate(rec["actions"]):
        valid = g.valid_actions()
        assert a in valid, (f, i, a, valid)
        sc = scene(g, valid)
        d = diver.decide(g)["action"]
        turns[sc] += 1
        agree[sc] += a == d
        if a != d:
            pairs[sc][(a, d)] += 1
        last = sc
        g.pick_kind = rec.get("picks", {}).get(str(i))
        g.step(a)
        g.log.clear()
    if g.dead:
        deaths[last] += 1
total = sum(turns.values())
print(f"{len(files)} 記録, {total} 手, 死亡 {sum(deaths.values())}")
print(f"{'場面':8s} {'手':>7s} {'割合':>5s} {'diver と一致':>10s} {'死の直前':>6s}  主な不一致 (Laya -> diver)")
for sc in SCENES + ("other",):
    n = turns[sc]
    top = ", ".join(f"{a}->{d} {c}" for (a, d), c in pairs[sc].most_common(5))
    print(f"{sc:8s} {n:7d} {n / total:5.0%} {agree[sc] / max(1, n):10.0%} {deaths[sc]:6d}  {top}")
