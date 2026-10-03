#!/bin/bash
# gen27 step 1 (CPU only, ~110 min): learn (DECAY 0.9, len(seeds) fix, clone fix) and the CPU gate. Appends to data/learn_gen27.log.
# Run with Unreal Engine closed. Do not run night_gen27_train.sh unless the gate passes (NOTES 15, gen27).
export PATH=/usr/bin:$PATH
export PYTHONIOENCODING=utf-8
cd /c/Peculium/src/laya-rogue
exec >> data/learn_gen27.log 2>&1
echo "=== learn start $(date)"
grep -n "^DECAY\|/ len(seeds)" learn.py; grep -n "c.mapped = set" game.py
uv run learn.py 16 1200
echo "=== learn end $(date)"
mkdir -p data/stage10_decay && cp data/table_r*.json data/stage10_decay/
echo "=== G1: 512 seeds, gen26 table vs gen27 table (paired) $(date)"
SEED0=5000 NSEEDS=512 uv run tools/table_gate.py data/stage9_combat/table_r16.json data/stage10_decay/table_r16.json
echo "=== record only: table:16, 128 seeds (gen26 gate was 6.57) $(date)"
uv run sim.py 128 8000 table:16
echo "=== G2: combat_compare on gen26 recordings $(date)"
uv run tools/combat_compare.py data/stage10_decay/table_r16.json "data/replays/laya-gen26_5*.json" | head -3
echo "=== G3: train examples (taught keys) $(date)"
uv run tools/redecay.py data/stage10_decay 0.9 0.9 data/stage10_decay/redecay_check.json
echo "=== diag: same samples re-weighted to DECAY 0.5, and combat values x2 (old bug) $(date)"
uv run tools/redecay.py data/stage10_decay 0.9 0.5 data/stage10_decay/diag_d050.json
uv run tools/redecay.py data/stage10_decay 0.9 0.9 data/stage10_decay/diag_x2.json --combat-scale 2
SEED0=5000 NSEEDS=512 uv run tools/table_gate.py data/stage10_decay/table_r16.json data/stage10_decay/diag_d050.json data/stage10_decay/diag_x2.json
echo "=== end $(date)"
