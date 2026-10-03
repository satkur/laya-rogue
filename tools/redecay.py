"""Re-weight one learn run's samples with another DECAY, from its saved per-round tables (no new rollouts).

    uv run tools/redecay.py <stage dir> <decay used by the run> <new decay> <out.json> [--combat-scale 2]

Saved tables satisfy w_r = d w_{r-1} + n_r and q_r w_r = d q_{r-1} w_{r-1} + S_r, so each round's new samples are recovered exactly.
The output keeps the last round's texts, so train.py can read it. --combat-scale multiplies the values of combat keys
(enemy adjacent or several awake): with 2 it reproduces the old learn.py bug (combat sums divided by ROLLOUTS, not len(seeds)).
"""
import json
import sys
from pathlib import Path


def is_combat_key(key):
    e = key.split("|")[2]
    return e != "none" and not e.startswith(("asleep-", "held-")) and (e.endswith("+") or e.endswith("adjacent"))


src, d0, d1, out = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), Path(sys.argv[4])
scale = float(sys.argv[sys.argv.index("--combat-scale") + 1]) if "--combat-scale" in sys.argv else 1.0
rounds = sorted(int(p.stem.split("_r")[1]) for p in src.glob("table_r*.json"))
acc, prev, last = {}, {}, None
for r in rounds:
    tab = json.loads((src / f"table_r{r}.json").read_text(encoding="utf-8"))
    cur = {k: {a: (v[0], v[1]) for a, v in e["q"].items()} for k, e in tab.items()}
    for qa in acc.values():
        for sw in qa.values():
            sw[0] *= d1
            sw[1] *= d1
    for k, q in cur.items():
        pq = prev.get(k, {})
        for a, (m, w) in q.items():
            pm, pw = pq.get(a, (0.0, 0.0))
            n = w - d0 * pw
            if n < 0.5:  # no new samples this round (float noise)
                continue
            sw = acc.setdefault(k, {}).setdefault(a, [0.0, 0.0])
            sw[0] += m * w - d0 * pm * pw
            sw[1] += n
    prev = cur
    last = tab
res = {}
for k, e in last.items():
    f = scale if is_combat_key(k) else 1.0
    res[k] = {"q": {a: [f * acc[k][a][0] / acc[k][a][1], acc[k][a][1]] if a in acc.get(k, {}) else list(v) for a, v in e["q"].items()},
              "texts": e["texts"]}
out.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
taught = [k for k, e in res.items() if min(v[1] for v in e["q"].values()) >= 4]
print(f"{out.name}: rounds {rounds[0]}..{rounds[-1]}, keys {len(res)}, taught (w>=4) {len(taught)}, train examples {sum(len(res[k]['texts']) for k in taught)}")
