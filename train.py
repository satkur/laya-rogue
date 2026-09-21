"""自己対戦の経験表 (data/table_r<N>.json) を Laya に学習させる。

    uv run train.py <ラウンド> <世代名> [エポック数]     例: uv run train.py 8 gen8

エンコーダ (文章を読む部分) は凍結し、その上の判断ヘッド (2 層 + スコアラ) だけを学習する。
正解は「表で一番良かった行動」ではなく、表の平均リターンを softmax した分布。
僅差の行動は僅差のまま、大差の行動は大差のまま教える。

状況の 1 割は学習に使わず取っておき、「見たことのない状況文」でも表と同じ判断ができるかを測る。
学習したヘッドだけを weights/<世代名>.pt に保存する (エンコーダは元の Laya のまま)。
"""
import json
import math
import random
import sys
import time
from pathlib import Path

import torch

from brain import ACTION_DESC, INSTRUCTIONS, TAU, WEIGHTS
from laya import Router
from laya.common import build_sequence

DATA = Path(__file__).parent / "data"
MIN_WEIGHT = 4.0   # 経験がこれ未満の状況は教えない。戦闘は死亡の減点で振れ幅が大きく、数件の平均はあてにならない
BATCH = 64
LR = 2e-4


def load_examples(round_no):
    table = json.loads((DATA / f"table_r{round_no}.json").read_text(encoding="utf-8"))
    out = []
    for entry in table.values():
        q = entry["q"]
        w = min(v[1] for v in q.values())
        if w < MIN_WEIGHT:
            continue
        valid = list(q)
        vals = [q[a][0] for a in valid]
        top = max(vals)
        z = [math.exp((v - top) / TAU) for v in vals]
        target = [x / sum(z) for x in z]
        for text in entry["texts"]:  # 表のキーは粗いが、Laya が読むのは詳しい状況文
            out.append({"state": text, "valid": valid, "target": target, "weight": math.sqrt(w) / len(entry["texts"]) ** 0.5})
    return out


def head_forward(model, h, att, mpos, mmask, qtype):
    """DecisionModel.forward のうち、エンコーダより後ろの部分。"""
    h = h + model.type_emb(qtype)[:, None, :]
    pad = ~att.bool()
    for layer in model.head.layers:
        h = layer(h, src_key_padding_mask=pad)
    idx = mpos[:, :, None].expand(-1, -1, h.size(-1))
    logits = model.scorer(torch.gather(h, 1, idx)).squeeze(-1).float()
    return logits.masked_fill(~mmask, -1e4)


def main():
    round_no, gen = sys.argv[1], sys.argv[2]
    epochs = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    rng = random.Random(0)
    examples = load_examples(round_no)
    rng.shuffle(examples)
    n_val = max(1, len(examples) // 10)
    val, train = examples[:n_val], examples[n_val:]
    print(f"状況 {len(examples)} 件 (学習 {len(train)} / 未見テスト {len(val)})", flush=True)

    agent = Router().load("multilingual")
    model, tok, dev = agent.model, agent.tok, agent.device
    for p in model.parameters():
        p.requires_grad_(False)
    trainable = [p for mod in (model.head, model.type_emb, model.scorer) for p in mod.parameters()]
    for p in trainable:
        p.requires_grad_(True)
    print(f"学習するパラメータ {sum(p.numel() for p in trainable) / 1e6:.1f}M / 全体 {sum(p.numel() for p in model.parameters()) / 1e6:.0f}M ({dev})", flush=True)

    # エンコーダは凍結なので、各状況文の読み取り結果は 1 回計算して使い回す
    t0 = time.perf_counter()
    model.eval()
    for ex in examples:
        q = {"t": "choice", "ins": INSTRUCTIONS, "crit": {a: ACTION_DESC[a] for a in ex["valid"]}}
        ex["ids"], ex["markers"] = build_sequence(tok, ex["state"], q, agent.cfg.get("max_len", 512), agent.cfg.get("head_max_len", 192))
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16, enabled=dev.type == "cuda"):
        for i in range(0, len(examples), BATCH):
            chunk = examples[i:i + BATCH]
            L = max(len(ex["ids"]) for ex in chunk)
            ids = torch.full((len(chunk), L), tok.pad_token_id)
            att = torch.zeros((len(chunk), L), dtype=torch.long)
            for j, ex in enumerate(chunk):
                ids[j, :len(ex["ids"])] = torch.tensor(ex["ids"])
                att[j, :len(ex["ids"])] = 1
            h = model.encoder(input_ids=ids.to(dev), attention_mask=att.to(dev)).last_hidden_state
            for j, ex in enumerate(chunk):
                ex["h"] = h[j, :len(ex["ids"])].float().cpu()
    print(f"エンコード {time.perf_counter() - t0:.0f}s", flush=True)

    def batchify(chunk):
        L, K = max(ex["h"].size(0) for ex in chunk), max(len(ex["valid"]) for ex in chunk)
        h = torch.zeros((len(chunk), L, chunk[0]["h"].size(1)))
        att = torch.zeros((len(chunk), L), dtype=torch.long)
        mpos = torch.zeros((len(chunk), K), dtype=torch.long)
        mmask = torch.zeros((len(chunk), K), dtype=torch.bool)
        target = torch.zeros((len(chunk), K))
        for j, ex in enumerate(chunk):
            n, k = ex["h"].size(0), len(ex["valid"])
            h[j, :n], att[j, :n] = ex["h"], 1
            mpos[j, :k], mmask[j, :k] = torch.tensor(ex["markers"]), True
            target[j, :k] = torch.tensor(ex["target"])
        w = torch.tensor([ex["weight"] for ex in chunk])
        return [t.to(dev) for t in (h, att, mpos, mmask, target, w)]

    def evaluate(data):
        model.eval()
        agree = total = 0.0
        with torch.no_grad():
            for i in range(0, len(data), BATCH):
                h, att, mpos, mmask, target, w = batchify(data[i:i + BATCH])
                logits = head_forward(model, h, att, mpos, mmask, torch.zeros(len(h), dtype=torch.long, device=dev))
                agree += ((logits.argmax(-1) == target.argmax(-1)).float() * w).sum().item()
                total += w.sum().item()
        return agree / total

    print(f"学習前: 表の最善手との一致 学習 {evaluate(train):.0%} / 未見 {evaluate(val):.0%}", flush=True)
    opt = torch.optim.AdamW(trainable, lr=LR, weight_decay=0.01)
    steps = epochs * math.ceil(len(train) / BATCH)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=LR, total_steps=steps, pct_start=0.1)
    t0 = time.perf_counter()
    for epoch in range(1, epochs + 1):
        for mod in (model.head, model.scorer):
            mod.train()
        rng.shuffle(train)
        running = 0.0
        for i in range(0, len(train), BATCH):
            h, att, mpos, mmask, target, w = batchify(train[i:i + BATCH])
            logits = head_forward(model, h, att, mpos, mmask, torch.zeros(len(h), dtype=torch.long, device=dev))
            loss = (-(target * torch.log_softmax(logits, -1)).sum(-1) * w).sum() / w.sum()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            opt.step()
            sched.step()
            running += loss.item()
        if epoch % 5 == 0 or epoch == epochs:
            print(f"epoch {epoch:3d}: loss {running / math.ceil(len(train) / BATCH):.3f} | 一致 学習 {evaluate(train):.0%} / 未見 {evaluate(val):.0%} | {time.perf_counter() - t0:.0f}s", flush=True)

    WEIGHTS.mkdir(exist_ok=True)
    model.eval()
    state = {k: v.detach().cpu().half() for k, v in model.state_dict().items() if not k.startswith("encoder.")}
    torch.save(state, WEIGHTS / f"{gen}.pt")
    print(f"保存: weights/{gen}.pt ({(WEIGHTS / f'{gen}.pt').stat().st_size / 1e6:.0f} MB)", flush=True)


if __name__ == "__main__":
    main()
