"""Replay a recording in text: per-floor summary, the strategist's consultations, and the last N actions.

    uv run tools/trace_replay.py data/replays/laya-gen25b+llm_5003.json [60]
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from game import Game  # noqa: E402
from sim import standard_hero  # noqa: E402
from brain import hp_word, pace_word  # noqa: E402

rec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tail = int(sys.argv[2]) if len(sys.argv) > 2 else 60
g = Game(rec["seed"], standard_hero(rec["start"]) if rec["start"] else None, rec["difficulty"])
advice = {a["i"]: a for a in rec["advice"]}
acts = rec["actions"]
floors = {}  # depth -> dict
lines = []
for i, a in enumerate(acts):
    valid = g.valid_actions()  # same call order as when recording (it caches the exploration target)
    assert a in valid, (i, a, valid)
    f = floors.setdefault(g.depth, dict(first=i, turn0=g.turn, lv0=g.level, hp0=g.hp, acts=Counter(), kills0=g.kills, adv=0))
    f["acts"][a] += 1
    if i in advice:
        f["adv"] += 1
    awake = [m for m in g.visible_monsters() if m["awake"]]
    mons = " ".join(f"{m['kind']}({m['hp']})" for m in awake)
    lines.append((i, g.turn, g.depth, g.level, g.hp, g.max_hp, g.hunger_word(), a, mons, advice.get(i)))
    g.pick_kind = rec.get("picks", {}).get(str(i))
    g.step(a)
    g.log.clear()
    f["last"], f["turn1"], f["lv1"], f["hp1"], f["kills1"] = i, g.turn, g.level, g.hp, g.kills
print(f"{rec['brain']} seed {rec['seed']}: {len(acts)} 手, 記録 {'完' if rec['done'] else '途中'}, 結果 {rec.get('result')}")
print(f"最終: B{g.depth} Lv{g.level} HP {g.hp}/{g.max_hp} 撃破 {g.kills} {'死亡: ' + g.cause if g.dead else ''}")
print("\n階ごと:")
for d, f in sorted(floors.items()):
    top = ", ".join(f"{k} {v}" for k, v in f["acts"].most_common(6))
    print(f"  B{d}: 手 {f['first']}-{f['last']} ターン {f['turn0']}-{f['turn1']} Lv {f['lv0']}->{f['lv1']} HP {f['hp0']}->{f['hp1']} 撃破 +{f['kills1']-f['kills0']} 相談 {f['adv']} | {top}")
print(f"\n方針役の相談 ({len(rec['advice'])} 回):")
for a in rec["advice"]:
    print(f"  手{a['i']} t{a['turn']} [{a.get('kind')}] plan={a.get('plan')} rest={a.get('rest')} tactic={a.get('tactic')} | {a.get('trigger','')[:60]}")
    if a.get("reason"):
        print(f"      理由: {a['reason'][:300]}")
print(f"\n末尾 {tail} 手:")
for (i, t, d, lv, hp, mhp, hun, a, mons, adv) in lines[-tail:]:
    print(f"  {i:5d} t{t:5d} B{d} Lv{lv} HP{hp:3d}/{mhp:<3d} {hun:6s} {a:14s} {mons}" + (f"   <= 相談 [{adv.get('kind')}] plan={adv.get('plan')} tactic={adv.get('tactic')}" if adv else ""))
