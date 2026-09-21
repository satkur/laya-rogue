"""Laya Rogue: Laya が勇者を操作してダンジョンに潜り続けるのを眺めるデモ。

    uv run server.py            # モデルを読み込み、ブラウザを開く
    uv run server.py --no-open
"""
import asyncio
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from brain import WEIGHTS, LayaBrain
from game import H, W, Game

HOST, PORT = "127.0.0.1", 8766
STATIC = Path(__file__).parent / "static"
brain = None


def generations():
    """weights/ にある学習済み世代。gen2, gen10 のような名前は数字順に並べる。"""
    names = [p.stem for p in WEIGHTS.glob("*.pt")]
    return sorted(names, key=lambda n: (int("".join(c for c in n if c.isdigit()) or 0), n))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global brain
    print("Laya を読み込み中...", flush=True)
    gens = generations()
    brain = LayaBrain(gens[-1] if gens else None)
    g = Game(0)
    for _ in range(5):  # 初回の CUDA カーネル準備を済ませておく
        brain.decide(g)
        g.step("explore")
    url = f"http://{HOST}:{PORT}/"
    print(f"準備完了: {brain.agent.device} / 世代 {brain.generation or '未学習'}\n  → {url}", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


def frame(g, d, log_from):
    return {
        "type": "frame",
        "turn": g.turn,
        "hero": {"x": g.hx, "y": g.hy, "hp": g.hp, "max_hp": g.max_hp, "level": g.level, "potions": g.potions,
                 "gold": g.gold, "kills": g.kills, "depth": g.depth},
        "monsters": [{"id": m["id"], "kind": m["kind"], "x": m["x"], "y": m["y"], "hp": m["hp"], "max_hp": m["max_hp"]} for m in g.visible_monsters()],
        "items": [{"kind": i["kind"], "x": i["x"], "y": i["y"]} for i in g.visible_items()],
        "seen": g.newly_seen,
        "visible": [list(p) for p in g.visible],
        "decision": d,
        "log": g.log[log_from:],
    }


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    cfg = {"delay": 0.147, "paused": False, "step": False, "restart": False}
    await sock.send_json({"type": "hello", "w": W, "h": H, "generations": generations(), "generation": brain.generation})

    async def play():
        while True:
            g = Game()
            gen = brain.generation
            depth, log_from = 0, 0
            while not g.dead and not cfg["restart"]:
                while cfg["paused"] and not cfg["step"] and not cfg["restart"]:
                    await asyncio.sleep(0.03)
                cfg["step"] = False
                d = brain.decide(g)  # 10〜30ms。ローカル単独利用なのでイベントループ上で直接呼ぶ
                g.step(d["action"])
                if g.depth != depth:
                    depth = g.depth
                    await sock.send_json({"type": "floor", "depth": depth})
                await sock.send_json(frame(g, d, log_from))
                g.newly_seen = []
                log_from = len(g.log)
                await asyncio.sleep(cfg["delay"])
            if not cfg["restart"]:
                await sock.send_json({"type": "death", "generation": gen, "depth": g.depth, "kills": g.kills, "gold": g.gold, "turn": g.turn, "level": g.level})
                await asyncio.sleep(2.2)
            cfg["restart"] = False

    task = asyncio.create_task(play())
    try:
        while True:
            m = await sock.receive_json()
            if m["type"] == "config":
                cfg["delay"] = max(0.0, min(1.0, float(m.get("delay", cfg["delay"]))))
                cfg["paused"] = bool(m.get("paused", cfg["paused"]))
            elif m["type"] == "step":
                cfg["step"] = True
            elif m["type"] == "restart":
                cfg["restart"] = True
            elif m["type"] == "generation":  # 世代を替えたら、その頭脳で最初から潜り直す
                gen = m.get("name")
                if gen is None or gen in generations():
                    brain.load_generation(gen)
                    cfg["restart"] = True
                    await sock.send_json({"type": "generation", "generation": brain.generation})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
