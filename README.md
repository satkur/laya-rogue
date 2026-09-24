# laya-rogue

[Laya](https://github.com/NandhaKishorM/laya) にローグライクをプレイさせる個人の実験。目標は地下 20 階。メモは [NOTES.md](NOTES.md)。

![一手一手は Laya (gen21)、方針は claude -p (Opus)。10 階でアクエイターから鎧を守って逃げ、11 階へ降りる 10 秒](docs/demo.gif)

## 動機と位置づけ

1. mizchi 氏の記事「[Jev クローンの Laya をローカルで60fpsで動かせるか？](https://zenn.dev/mizchi/articles/laya-mlx-60fps)」の Snake デモを見て、
   「ローカルで動く小さい AI (M5 MacBook で 1 判断 8〜9 ms、記事いわく「60fps は余裕で超えます。」)」が何かゲームに使えるかもと思った。
2. Rogue を模したゲームで素の Laya に盤面を読ませたら、ランダムより早く死んだ ([NOTES 1 章](NOTES.md))。Laya は文章の分類器で、
   状況に関係なく固定の好み (探索 > 階段 > …) で選ぶ。「A ならば B すべき」という条件判断はできない。
3. 改めて Snake デモのコードを読むと、プランナー (ハミルトン経路探索) が各方向に `Safe. Best route to food.` / `Unsafe. Traps the snake.` と
   答えを書き、Laya はそれを読んで選んでいるだけの出来レースだった。記事自身も「判断の質は説明文の質に依存します。」
   「正直、ここには限界を感じました。」と書いている。同じ方式を Rogue でやると、答えが書いてあっても 1 割は外した ([2 章](NOTES.md))。
4. 自己対戦で集めた経験 (状況文, 行動, 得点) で判断ヘッド (14.8M パラメータ) だけを事後学習させると、人間が書いた if 文と同等 (平均 9〜10 階)
   まで潜れるようになった ([3 章](NOTES.md)〜)。推論は 1 手 12〜13 ms のまま。ただし 20 階にはほぼ届かない (128 回中 2 回)。
5. 結局 Snake デモにも planner があり、Minecraft のエンダードラゴン討伐 ([rmalde/minecraft-agent](https://github.com/rmalde/minecraft-agent)) も
   GPT-6 Astra が計画して Jev (Laya の元になったクラウド版) が即断する分業だった。そこで planner の位置に `claude -p` (Opus) を置き、
   一手一手は Laya、全体方針 (探索し切る / すぐ降りる / 休む / 戦え / 逃げろ / 階段で離脱 / いま回復薬) は LLM という形にした ([strategist.py](strategist.py)、[6〜7 章](NOTES.md))。
6. 方針役を付けても成績はまだ変わらない (害も出ない)。介入の理由は人間が読める形で画面に出るので、見た目はいい感じ (上の GIF)。
7. 次に何をするかは未定。

## 使い方

```
uv sync
uv run learn.py 16 1200
uv run train.py 16 gen19 30
uv run server.py          # http://127.0.0.1:8766/
uv run sim.py 16 8000 random rules diver table:16 laya:gen19
```

Python 3.12 / uv / CUDA 対応 GPU。

## Credits

Game rules and numeric tables (monster roster/stats, combat, experience, hunger) are modeled on
*Rogue: Exploring the Dungeons of Doom* 5.4.4 by Michael Toy, Ken Arnold and Glenn Wichman.
All code here is an independent Python implementation; no original source code is included.
This project is not affiliated with or endorsed by the original authors or any rights holder of the "Rogue" name.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Laya 本体は Convai Innovations による Apache 2.0 のモデルで、このリポジトリには含まれない。

## ライセンス

MIT（`rogue_data.py` の数値表の出典については THIRD_PARTY_NOTICES.md）。
