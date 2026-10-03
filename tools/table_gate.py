"""Table-brain gate (CPU, 4 workers): several tables on the same seeds, with paired differences against the first one.
Narrower noise than `sim.py 128 8000 table:16` because the comparison is paired and can use more seeds.

    SEED0=5000 NSEEDS=512 uv run tools/table_gate.py data/stage9_combat/table_r16.json data/stage10_decay/table_r16.json
"""
import json
import os
import random
import statistics
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import TableBrain  # noqa: E402
from game import Game  # noqa: E402

TABLES = {}


def init(paths):
    for p in paths:
        TABLES[p] = json.loads(Path(p).read_text(encoding="utf-8"))


def play(args):
    path, seed = args
    tb = TableBrain(TABLES[path])
    tb.rng = random.Random(seed)
    g = Game(seed, None, "normal")
    g.action_rng = random.Random(seed)
    while not g.over and g.turn < 8000:
        g.step(tb.decide(g)["action"])
        g.log.clear()
    return g.max_depth, g.turn, bool(g.dead and "餓" in str(g.cause))


if __name__ == "__main__":
    paths = sys.argv[1:]
    s0, n = int(os.environ.get("SEED0", 5000)), int(os.environ.get("NSEEDS", 128))
    seeds = range(s0, s0 + n)
    with Pool(4, initializer=init, initargs=(paths,)) as p:
        res = {path: p.map(play, [(path, s) for s in seeds], chunksize=4) for path in paths}
    base = [r[0] for r in res[paths[0]]]
    print(f"seeds {s0}..{s0 + n - 1}")
    for path in paths:
        d = [r[0] for r in res[path]]
        dd = [y - x for x, y in zip(base, d)]
        print(f"{path:40s} depth {statistics.mean(d):.2f} ±{statistics.stdev(d) / n ** 0.5:.2f} | 10+ {sum(x >= 10 for x in d):3d}"
              f" | turns {statistics.mean(r[1] for r in res[path]):.0f} | starved {sum(r[2] for r in res[path])}"
              f" | vs first {statistics.mean(dd):+.2f} ±{statistics.stdev(dd) / n ** 0.5:.2f}", flush=True)
