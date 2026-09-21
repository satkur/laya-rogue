# laya-rogue

[Laya](https://github.com/NandhaKishorM/laya)（文章を生成せず、選択肢に確率を付けて即答するローカルの判断モデル）に
ローグライクをプレイさせる実験。自己対戦で学習させ、ブラウザで眺められる。

プログラムがやるのは「見えているものを言葉にする」「いまできる行動を列挙する」の 2 つだけ。
どの行動が良いかは教えず、安全装置もない。選ぶのは Laya。

```
uv sync
uv run learn.py 8 480     # 自己対戦で経験表を作る（CPU のみ、15〜20 分）
uv run train.py 8 gen8    # 8 ラウンド目の表を Laya の判断ヘッドに学習させる（GPU、1 分未満）
uv run server.py          # http://127.0.0.1:8766/ が開く。画面下で世代を切り替えられる
uv run sim.py 40 800 random rules table:8 laya laya:gen8   # 画面なしで成績比較
```

学習済みの重みはリポジトリに含めていない（`weights/` は .gitignore）。未学習のままでも `server.py` は動く。

操作: `Space` 一時停止 / `.` 1 手進める / `R` 最初から / 画面下のボタンで学習世代の切り替え

実験の経緯と実測は [NOTES.md](NOTES.md)。

## 構成

- `game.py` ルール本体（ダンジョン生成・視界・戦闘・経路探索）。描画も AI も持たない
- `brain.py` 状況文の生成、行動の列挙、Laya 呼び出し、比較用の頭脳
- `learn.py` 自己対戦。各局面で取れる行動を試して先読みし、結果を経験表にためる
- `train.py` 経験表を Laya の判断ヘッドに学習させる（エンコーダは凍結）
- `sim.py` 頭脳ごとの成績比較
- `server.py` / `static/index.html` FastAPI + WebSocket と、Canvas の画面

## 動作環境

Python 3.12 / uv / CUDA 対応 GPU。`pyproject.toml` は torch を CUDA 12.8 の index から取る設定
（RTX 50 系は素の `pip install torch` だと CPU 実行に落ちて 10 倍以上遅くなる）。
モデル重み（約 650MB）は初回起動時に Hugging Face から取得される。

## ライセンス

MIT。Laya 本体は Convai Innovations による Apache 2.0 のモデルで、このリポジトリには含まれない。
