#!/bin/bash
# gen27 step 2 (GPU, ~3 h): only after the gate in data/learn_gen27.log passed. Appends to data/train_gen27.log.
export PATH=/usr/bin:$PATH
export PYTHONIOENCODING=utf-8
cd /c/Peculium/src/laya-rogue
exec >> data/train_gen27.log 2>&1
boost() { sleep 25; powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*train.py*' -or \$_.CommandLine -like '*sim.py*' } | ForEach-Object { (Get-Process -Id \$_.ProcessId).PriorityClass = 'High' }"; }
cp data/stage10_decay/table_r*.json data/   # train.py reads data/table_r16.json
# same number of gradient steps as gen26 (60 epochs on 41,790 examples)
EPOCHS=$(uv run python -c "import json; t=json.load(open('data/table_r16.json', encoding='utf-8')); n=sum(len(e['texts']) for e in t.values() if min(v[1] for v in e['q'].values()) >= 4); print(max(10, round(60 * 41790 / n)))")
echo "=== train start $(date), epochs $EPOCHS"
boost & uv run train.py 16 gen27 $EPOCHS
echo "=== sim 1F $(date)"
boost & uv run sim.py 128 8000 laya:gen27
echo "=== sim B10F $(date)"
boost & uv run sim.py 128 8000 laya:gen27 --start 10
echo "=== scene stats (gen27 recordings) $(date)"
uv run tools/scene_stats.py "data/replays/laya-gen27_5*.json"
echo "=== combat compare (gen27 recordings, gen27 table) $(date)"
uv run tools/combat_compare.py data/stage10_decay/table_r16.json "data/replays/laya-gen27_5*.json" | head -3
echo "=== end $(date)"
