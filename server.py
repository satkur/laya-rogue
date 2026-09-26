"""Laya Rogue: Laya が勇者を操作してダンジョンに潜る。目標は地下 26 階。

    uv run server.py            # モデルを読み込み、ブラウザを開く
    uv run server.py --no-open
    uv run server.py --llm      # 方針役の LLM (strategist.py、claude -p を呼ぶ) を入れた状態で始める。画面の L キーでも切り替えられる
    uv run server.py --llm-model sonnet
    uv run server.py --gen gen22    # 世代を指定 (既定は DEFAULT_GENERATION = 測定済みの最良)。画面からは替えない
    uv run server.py --difficulty original   # 難易度 (既定 normal)。画面からも切り替えられる (NOTES.md 15 章)
    uv run server.py --replay data/replays/laya-gen25b_5004.json   # sim.py / learn.py が残した記録を再生する (Laya も方針役も呼ばない。
                                             # 回している最中の記録は末尾を追いかける。R で最初から。難易度と方針役の切替は効かない)
"""
import asyncio
import json
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import rogue_data as D
from brain import WEIGHTS, LayaBrain
from game import H, W, Game
import strategist as strategist_mod
from sim import standard_hero
from strategist import DEFAULT_MODEL, Masked, Strategist

HOST, PORT = "127.0.0.1", 8766
STATIC = Path(__file__).parent / "static"
brain = None
LLM_MODEL = sys.argv[sys.argv.index("--llm-model") + 1] if "--llm-model" in sys.argv else DEFAULT_MODEL
DEFAULT_GENERATION = "gen25"  # 既定は「測定済みの最良」を固定 (weights/ の最新番号は未測定のことがある。全要素入りの環境で gen25 6.24 / gen25b 6.16 / gen24 6.07、NOTES 15 章)
GENERATION = sys.argv[sys.argv.index("--gen") + 1] if "--gen" in sys.argv else DEFAULT_GENERATION
DIFFICULTY = sys.argv[sys.argv.index("--difficulty") + 1] if "--difficulty" in sys.argv else "normal"
D.rules(DIFFICULTY)  # 名前の検査
REPLAY = Path(sys.argv[sys.argv.index("--replay") + 1]) if "--replay" in sys.argv else None
replay_state = {"enabled": False, "model": "", "stopped": None, "calls": 0, "plan": "free", "rest": False, "tactic": "free", "fetch": "none"}  # 再生中の方針役の表示
adviser = Strategist(model=LLM_MODEL, enabled="--llm" in sys.argv)  # いまは付けると成績が下がるので既定は切 (NOTES.md 6 章)
strategist_mod.MAX_CALLS_TOTAL = 300  # 画面を開きっぱなしにしても、ここで方針役は自動で止まる (画面で入れ直すと再開)


def generations():
    """weights/ にある学習済み世代。gen2, gen10 のような名前は数字順に並べる。"""
    names = [p.stem for p in WEIGHTS.glob("*.pt")]
    return sorted(names, key=lambda n: (int("".join(c for c in n if c.isdigit()) or 0), n))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global brain
    url = f"http://{HOST}:{PORT}/"
    if REPLAY:
        rec = load_replay()
        replay_state.update(enabled=bool(rec["advice"]), model=rec["brain"])
        print(f"再生: {REPLAY} ({rec['brain']}, 種 {rec['seed']}, {len(rec['actions'])} 手{'' if rec.get('done') else '、記録中'})\n  → {url}", flush=True)
    else:
        print(f"Laya を読み込み中... (難易度 {DIFFICULTY.upper()})", flush=True)
        gens = generations()
        brain = LayaBrain(GENERATION if GENERATION in gens else (gens[-1] if gens else None))
        g = Game(0)
        for _ in range(5):  # 初回の CUDA カーネル準備を済ませておく
            g.step(brain.decide(g)["action"])
        print(f"準備完了: {brain.agent.device} / 世代 {brain.generation or '未学習'}\n  → {url}", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


def load_replay():
    return json.loads(REPLAY.read_text(encoding="utf-8"))


def adviser_state():
    if REPLAY:
        return dict(replay_state)
    return {"enabled": adviser.enabled, "model": adviser.model, "stopped": adviser.stopped, "calls": adviser.calls,
            "plan": adviser.plan, "rest": adviser.rest, "tactic": adviser.tactic, "fetch": adviser.fetch}


def frame(g, d, log_from):
    return {
        "type": "frame",
        "turn": g.turn,
        "hero": {"x": g.hx, "y": g.hy, "hp": g.hp, "max_hp": g.max_hp, "level": g.level, "str": g.str, "max_str": g.max_str,
                 "ac": g.ac(), "weapon": g.weapon["name"], "armor": g.armor["name"], "food": g.food,
                 "hunger": g.hunger_word(), "heal": g.has_heal(), "gold": g.gold, "kills": g.kills, "depth": g.depth,
                 "missiles": sum(g.missiles.values()), "scrolls": sum(c for k, c in g.scrolls.items() if k in g.known), "bow": g.bow,
                 "unknown_potions": sum(g.unknown_potions().values()), "unknown_scrolls": sum(g.unknown_scrolls().values()),
                 "wands": sum(g.known_sticks().values()), "unknown_wands": len(g.unknown_sticks()),
                 "rings": [g.ring_label(r) for r in g.worn], "ring_bag": len(g.rings), "amulet": g.amulet,
                 "status": [w for w, on in (("混乱", g.confused), ("拘束", g.held_by is not None), ("足止め", g.no_move), ("行動不能", g.no_command),
                                            ("加速", g.hasted), ("浮遊", g.levitating), ("盲目", g.blind), ("幻覚", g.hallucinating),
                                            ("怪物探知", g.detecting()), ("恐怖の巻物の上", g.on_scare())) if on]},
        "monsters": [{"id": m["id"], "ch": m["ch"], "x": m["x"], "y": m["y"], "hp": m["hp"], "max_hp": m["max_hp"], "awake": m["awake"]}
                     for m in g.visible_monsters()],
        "items": [{"kind": i["kind"], "x": i["x"], "y": i["y"]} for i in g.visible_items()],
        "traps": [{"kind": t["kind"], "x": t["x"], "y": t["y"]} for t in g.traps if t["found"]],
        "seen": g.newly_seen,
        "visible": [list(p) for p in g.visible],
        "decision": d,
        "adviser": adviser_state(),
        "log": g.log[log_from:],
    }


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    cfg = {"delay": 0.119, "paused": False, "step": False, "restart": False}
    await sock.send_json({"type": "hello", "w": W, "h": H, "generation": load_replay()["brain"] if REPLAY else brain.generation,
                          "difficulties": list(D.DIFFICULTIES), "difficulty": load_replay().get("difficulty", "normal") if REPLAY else DIFFICULTY,
                          "replay": REPLAY.name if REPLAY else None,
                          "orders": [], "order": None,  # 命令はいったん外してある
                          "adviser": adviser_state()})

    async def play():
        global DIFFICULTY
        while True:
            g = Game(difficulty=DIFFICULTY)
            g.say(f"地図の種 {g.seed} ({DIFFICULTY.upper()})")  # sim.py --seeds {種} --difficulty {難易度} で同じ地図を再現できる
            print(f"ゲーム開始: 種 {g.seed} / 世代 {brain.generation} / 難易度 {DIFFICULTY}", flush=True)
            adviser.reset()
            gen = brain.generation
            depth, log_from = g.depth, 0
            # 最初の盤面を先に映す (方針役の相談で止まるより前に、勇者が現れた画面にする)
            await sock.send_json({"type": "floor", "depth": depth})
            await sock.send_json(frame(g, {"action": None, "probs": {}, "state": "", "ms": 0.0}, log_from))
            g.newly_seen = []
            log_from = len(g.log)
            while not g.over and not cfg["restart"]:
                while cfg["paused"] and not cfg["step"] and not cfg["restart"]:
                    await asyncio.sleep(0.03)
                cfg["step"] = False
                trigger = adviser.check(g)
                if trigger:  # 方針役は数秒かかる。そのあいだゲームは止めて待つ
                    await sock.send_json({"type": "thinking", "kind": adviser.kind})
                    advice = await asyncio.to_thread(adviser.consult, g, trigger)
                    await sock.send_json({"type": "advice", **advice, "adviser": adviser_state()})
                d = brain.decide(Masked(g, adviser.allowed(g, g.valid_actions())))  # 10〜30ms。ローカル単独利用なのでイベントループ上で直接呼ぶ
                adviser.recent = (adviser.recent + [d["action"]])[-40:]
                g.step(d["action"])
                if g.depth != depth:
                    depth = g.depth
                    await sock.send_json({"type": "floor", "depth": depth})
                await sock.send_json(frame(g, d, log_from))
                g.newly_seen = []
                log_from = len(g.log)
                await asyncio.sleep(cfg["delay"])
            if not cfg["restart"]:
                await sock.send_json({"type": "end", "won": g.won, "cause": g.cause, "generation": gen, "difficulty": g.rules.name, "depth": g.depth,
                                      "kills": g.kills, "gold": g.gold, "turn": g.turn, "level": g.level})
                await asyncio.sleep(3.0)
            cfg["restart"] = False

    async def play_replay():
        """記録を再生する。記録がまだ書かれている最中なら末尾で待って追いかける。終わったら R まで止まる。"""
        while True:
            rec = load_replay()
            g = Game(rec["seed"], standard_hero(rec["start"]) if rec.get("start") else None, rec.get("difficulty", "normal"))
            g.say(f"再生: {REPLAY.name} ({rec['brain']}, 地図の種 {g.seed})")
            replay_state.update(enabled=bool(rec["advice"]), model=rec["brain"], calls=0, plan="free", rest=False, tactic="free", fetch="none")
            advice_at = {a["i"]: a for a in rec["advice"]}
            depth, log_from, i = g.depth, 0, 0
            await sock.send_json({"type": "floor", "depth": depth})
            await sock.send_json(frame(g, {"action": None, "probs": {}, "state": "", "ms": 0.0}, log_from))
            g.newly_seen = []
            log_from = len(g.log)
            while not g.over and not cfg["restart"]:
                while cfg["paused"] and not cfg["step"] and not cfg["restart"]:
                    await asyncio.sleep(0.03)
                cfg["step"] = False
                if i >= len(rec["actions"]):
                    if rec.get("done"):
                        break
                    await asyncio.sleep(1.0)  # まだ記録中: 追いつくまで待つ
                    rec = load_replay()
                    advice_at = {a["i"]: a for a in rec["advice"]}
                    continue
                a = advice_at.get(i)
                if a:
                    replay_state.update(calls=replay_state["calls"] + 1, **{k: a[k] for k in ("plan", "rest", "tactic", "fetch") if k in a})
                    await sock.send_json({"type": "advice", **a, "adviser": adviser_state()})
                action = rec["actions"][i]
                i += 1
                d = {"action": action, "probs": {action: 1.0}, "state": "", "ms": 0.0}
                g.step(action)
                if g.depth != depth:
                    depth = g.depth
                    await sock.send_json({"type": "floor", "depth": depth})
                await sock.send_json(frame(g, d, log_from))
                g.newly_seen = []
                log_from = len(g.log)
                await asyncio.sleep(cfg["delay"])
            if not cfg["restart"]:
                await sock.send_json({"type": "end", "won": g.won, "cause": g.cause, "generation": rec["brain"], "difficulty": g.rules.name, "depth": g.depth,
                                      "kills": g.kills, "gold": g.gold, "turn": g.turn, "level": g.level})
                while not cfg["restart"]:
                    await asyncio.sleep(0.1)
            cfg["restart"] = False

    task = asyncio.create_task(play_replay() if REPLAY else play())
    try:
        while True:
            m = await sock.receive_json()
            if REPLAY and m["type"] in ("adviser", "difficulty"):  # 再生中は方針役と難易度の切替は効かない
                continue
            if m["type"] == "config":
                cfg["delay"] = max(0.0, min(1.0, float(m.get("delay", cfg["delay"]))))
                cfg["paused"] = bool(m.get("paused", cfg["paused"]))
            elif m["type"] == "step":
                cfg["step"] = True
            elif m["type"] == "restart":
                cfg["restart"] = True
            elif m["type"] == "adviser":  # 方針役の ON/OFF。入れ直すと自動停止も解除する
                adviser.enabled = bool(m.get("enabled"))
                if adviser.enabled:
                    adviser.stopped, adviser.errors, strategist_mod.total_calls = None, 0, 0
                await sock.send_json({"type": "adviser", "adviser": adviser_state()})
            elif m["type"] == "difficulty":  # 難易度を替えたら、最初から潜り直す
                name = m.get("name")
                if name in D.DIFFICULTIES:
                    globals()["DIFFICULTY"] = name
                    cfg["restart"] = True
                    await sock.send_json({"type": "difficulty", "difficulty": name})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
