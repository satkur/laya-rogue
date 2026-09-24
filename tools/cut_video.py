"""timeline.json から、動き出す直前で頭を切り、ターンが進まない区間 (方針役の相談) を最大 KEEP 秒に詰めた mp4 を作る。
    uv run python tools/cut_video.py <webm> <timeline.json> <out.mp4> [KEEP=2.5]   (ffmpeg のパスは FF を環境に合わせる)
"""
import sys, json, subprocess
FF = r"C:\Users\pathf\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
webm, tl, out = sys.argv[1:4]; KEEP = float(sys.argv[4]) if len(sys.argv) > 4 else 2.5
L = [r for r in json.load(open(tl, encoding="utf-8")) if "turn" in r]
end = max(r["t"] for r in json.load(open(tl, encoding="utf-8")))
# 1 秒刻みの観測から「ターンが止まっている区間」を拾う
still = []; prev = None; start = None
for r in L:
    if prev is not None and r["turn"] == prev:
        if start is None: start = prev_t
    else:
        if start is not None: still.append((start, prev_t)); start = None
    prev, prev_t = r["turn"], r["t"]
if start is not None: still.append((start, prev_t))
# 先頭: 最初に動くまでを切る (最初の静止区間が 0 秒付近なら、その終わり - 0.5 秒から)
t_begin = 0.0
for r in L:
    if int(r["turn"]) >= 2: t_begin = max(0.0, r["t"] - 1.6); break
still = [(a, b) for a, b in still if b > t_begin + 1.0]
# 残す区間 (静止区間は末尾 KEEP 秒だけ残す: 相談結果が出て動き出す直前)
segs = []; cur = t_begin
for a, b in still:
    if b - a < KEEP + 1.0 or b <= cur: continue
    if a > cur: segs.append((cur, a))
    cur = max(cur, b - KEEP)
segs.append((cur, end + 4.0))
segs = [(a, b) for a, b in segs if b - a > 0.2]
parts = []; concat = ""
for i, (a, b) in enumerate(segs):
    parts.append(f"[0:v]trim=start={a:.2f}:end={b:.2f},setpts=PTS-STARTPTS[v{i}]"); concat += f"[v{i}]"
fc = ";".join(parts) + f";{concat}concat=n={len(segs)}:v=1:a=0[v]"
subprocess.run([FF, "-v", "error", "-i", webm, "-filter_complex", fc, "-map", "[v]", "-r", "25", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", out], check=True)
total = sum(b - a for a, b in segs)
print(f"segments {len(segs)}  start {t_begin}s  kept {total:.0f}s of {end:.0f}s  cut {len(still)} still spans")
