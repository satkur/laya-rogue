# laya-rogue

[Laya](https://github.com/NandhaKishorM/laya)（文章を生成せず、選択肢に確率を付けて即答するローカルの判断モデル）に
ローグライクをプレイさせる実験。ブラウザで眺められる。

```
uv sync
uv run server.py        # http://127.0.0.1:8766/ が開く
uv run sim.py 10 600    # 画面なしで頭脳別（ランダム / if 文ルール / Laya）の成績比較
```

操作: `Space` 一時停止 / `.` 1 手進める / `G` 安全装置 / `R` 最初から

実験の経緯と実測は [NOTES.md](NOTES.md)。

## 構成

- `game.py` ルール本体（ダンジョン生成・視界・戦闘・経路探索）。描画も AI も持たない
- `brain.py` 状況文の生成と Laya 呼び出し、比較用の頭脳
- `server.py` FastAPI + WebSocket。1 ターンごとにフレームを配信
- `static/index.html` Canvas 描画と判断パネル

## 動作環境

Python 3.12 / uv / CUDA 対応 GPU。`pyproject.toml` は torch を CUDA 12.8 の index から取る設定
（RTX 50 系は素の `pip install torch` だと CPU 実行に落ちて 10 倍以上遅くなる）。
モデル重み（約 650MB）は初回起動時に Hugging Face から取得される。

## ライセンス

MIT。Laya 本体は Convai Innovations による Apache 2.0 のモデルで、このリポジトリには含まれない。
