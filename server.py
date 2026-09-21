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

from brain import LayaBrain
from game import H, W, Game

HOST, PORT = "127.0.0.1", 8766
STATIC = Path(__file__).parent / "static"
brain = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global brain
    print("Laya を読み込み中...", flush=True)
    import torch

    brain = LayaBrain("multilingual")
    g = Game(0)
    for _ in range(5):  # 初回の CUDA カーネル準備を済ませておく
        brain.decide(g)
    app.state.device = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU (遅い: torch の CUDA ビルドを確認)"
    url = f"http://{HOST}:{PORT}/"
    print(f"準備完了: {app.state.device}\n  → {url}", flush=True)
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
    cfg = {"delay": 0.12, "paused": False, "guarded": True, "step": False, "restart": False}
    await sock.send_json({"type": "hello", "device": app.state.device, "w": W, "h": H})

    async def play():
        while True:
            g = Game()
            depth, log_from = 0, 0
            while not g.dead and not cfg["restart"]:
                while cfg["paused"] and not cfg["step"] and not cfg["restart"]:
                    await asyncio.sleep(0.03)
                cfg["step"] = False
                brain.guarded = cfg["guarded"]
                d = brain.decide(g)  # 15〜30ms。ローカル単独利用なのでイベントループ上で直接呼ぶ
                g.step(d["action"])
                if g.depth != depth:
                    depth = g.depth
                    await sock.send_json({"type": "floor", "depth": depth})
                await sock.send_json(frame(g, d, log_from))
                g.newly_seen = []
                log_from = len(g.log)
                await asyncio.sleep(cfg["delay"])
            if not cfg["restart"]:
                await sock.send_json({"type": "death", "depth": g.depth, "kills": g.kills, "gold": g.gold, "turn": g.turn, "level": g.level})
                await asyncio.sleep(2.2)
            cfg["restart"] = False

    task = asyncio.create_task(play())
    try:
        while True:
            m = await sock.receive_json()
            if m["type"] == "config":
                cfg["delay"] = max(0.0, min(1.0, float(m.get("delay", cfg["delay"]))))
                cfg["paused"] = bool(m.get("paused", cfg["paused"]))
                cfg["guarded"] = bool(m.get("guarded", cfg["guarded"]))
            elif m["type"] == "step":
                cfg["step"] = True
            elif m["type"] == "restart":
                cfg["restart"] = True
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
