"""HP 満タン・敵なし・探索可の局面で、Laya (学習済み世代) が「待機」に置く確率を、経験表に控えた状況文で測る (NOTES 13 章)。
    uv run python tools/measure_rest.py gen24 [gen21 ...]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sys, json, glob, math, statistics
from brain import LayaBrain, INSTRUCTIONS, ACTION_DESC, TAU
TABLE_DIR = {"gen21": "data/stage5_rare", "gen22": "data/stage7_gen22_pace", "gen20": "data/stage4_unid", "gen24": "data"}
gens = sys.argv[1:] or ["gen21"]
brain = LayaBrain(gens[0])
for gen in gens:
    brain.load_generation(gen)
    fs = sorted(glob.glob(TABLE_DIR.get(gen, "data/stage5_rare") + "/table_r*.json"), key=lambda f: int(f.split("_r")[-1].split(".")[0]))
    t = json.load(open(fs[-1], encoding="utf-8"))
    rows = []
    for key, ent in t.items():
        parts = key.split("|"); hp, enemy, valid = parts[0], parts[2], parts[-1].split(",")
        q = ent.get("q", {})
        if hp != "full" or enemy != "none" or "explore" not in valid or "rest" not in valid or "starving" in key: continue
        vals = {a: q[a][0] for a in valid if a in q}
        top = max(vals.values()); tp = {a: math.exp((v - top) / TAU) for a, v in vals.items()}; z = sum(tp.values())
        teacher_rest = tp["rest"] / z
        for text in ent.get("texts", [])[:8]:
            qq = {"action": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {a: ACTION_DESC[a] for a in valid}}}
            p = brain.agent.predict(text, qq)["answers"]["action"]["probabilities"]
            rows.append((key, teacher_rest, p.get("rest", 0.0), p.get("explore", 0.0), max(p, key=p.get), "Unexplored area: yes" in text, text))
    n = len(rows)
    print(f"== {gen}: 状況文 {n} 件 (キー {len(set(r[0] for r in rows))})")
    print(f"  教師の P(rest) 平均 {statistics.mean(r[1] for r in rows):.3f} / Laya の P(rest) 平均 {statistics.mean(r[2] for r in rows):.3f} / Laya の P(explore) 平均 {statistics.mean(r[3] for r in rows):.3f}")
    print(f"  Laya が rest を最有力にした割合 {sum(1 for r in rows if r[4] == 'rest') / n:.1%}、P(rest) > 0.5 の割合 {sum(1 for r in rows if r[2] > 0.5) / n:.1%}")
    ex = [r for r in rows if r[5] and r[2] > 0.5][:3]
    for r in ex: print("  例 P(rest)=%.2f 教師=%.2f: %s" % (r[2], r[1], r[6][:160]))
    by = {}
    for r in rows: by.setdefault(r[0], []).append(r[2])
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))[:6]: print(f"  {k}: Laya P(rest) 平均 {statistics.mean(v):.2f} (n={len(v)})")
