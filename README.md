# laya-rogue

[Laya](https://github.com/NandhaKishorM/laya)（文章を生成せず、選択肢に確率を付けて即答するローカルの判断モデル）に
ローグライクをプレイさせる実験。人間が命令を出し、Laya が勇者を動かす。目標は地下 20 階。

ゲームのルールは本家 Rogue 5.4.4 を基準にしている（第 1 段階: 戦闘と生存。未実装の要素は [NOTES.md](NOTES.md)）。

プログラムがやるのは「見えているものを言葉にする」「いまできる行動を列挙する」の 2 つだけ。
どの行動が良いかは教えず、安全装置もない。選ぶのは Laya で、腕前は自己対戦の学習で身につける。

```
uv sync
uv run learn.py 6 800     # 自己対戦で経験表を作る（CPU のみ、低優先度で約 10 分）
uv run train.py 6 gen6 30 # 6 ラウンド目の表を Laya の判断ヘッドに学習させる（GPU、約 6 分）
uv run server.py          # http://127.0.0.1:8766/ が開く
uv run sim.py 16 8000 random rules diver table:6 laya laya:gen6+cautious   # 画面なしで成績比較
```

学習済みの重みはリポジトリに含めていない（`weights/` は .gitignore）。未学習のままでも `server.py` は動く。

操作: `1`〜`4` 命令（慎重に / 攻めろ / 漁れ / 降りろ） / `Space` 一時停止 / `.` 1 手進める / `R` 最初から / 画面下のボタンで学習世代の切り替え

## 構成

- `rogue_data.py` 本家 Rogue 5.4.4 の数値表（モンスター 26 種、経験値、空腹、アイテム出現率）
- `game.py` ルール本体（3×3 区画の部屋と通路、本家の視界、命中判定、空腹、特殊攻撃）。描画も AI も持たない
- `brain.py` 状況文の生成、行動の列挙、Laya 呼び出し、比較用の頭脳
- `learn.py` 自己対戦。各局面で取れる行動を試して先読みし、結果を経験表にためる
- `train.py` 経験表を Laya の判断ヘッドに学習させる（エンコーダは凍結）
- `sim.py` 頭脳ごとの成績比較
- `server.py` / `static/index.html` FastAPI + WebSocket と、Canvas の画面

## 動作環境

Python 3.12 / uv / CUDA 対応 GPU。`pyproject.toml` は torch を CUDA 12.8 の index から取る設定
（RTX 50 系は素の `pip install torch` だと CPU 実行に落ちて 10 倍以上遅くなる）。
モデル重み（約 650MB）は初回起動時に Hugging Face から取得される。

## Credits

Game rules and numeric tables (monster roster/stats, combat, experience, hunger) are modeled on
*Rogue: Exploring the Dungeons of Doom* 5.4.4 by Michael Toy, Ken Arnold and Glenn Wichman.
All code here is an independent Python implementation; no original source code is included.
This project is not affiliated with or endorsed by the original authors or any rights holder of the "Rogue" name.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Laya 本体は Convai Innovations による Apache 2.0 のモデルで、このリポジトリには含まれない。

## ライセンス

MIT（`rogue_data.py` の数値表の出典については THIRD_PARTY_NOTICES.md）。
