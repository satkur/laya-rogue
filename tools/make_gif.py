"""Cut spans out of a recording (tools/record.py) and join them into the README GIF (960 px, 12 fps, one shared palette).

    uv run --no-project python tools/make_gif.py <webm> <out.gif> 12.0-24.5 301.0-306.0   (seconds; ffmpeg on PATH)
"""
import subprocess
import sys

webm, out, spans = sys.argv[1], sys.argv[2], [tuple(map(float, s.split("-"))) for s in sys.argv[3:]]
trims = ";".join(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]" for i, (a, b) in enumerate(spans))
joined = "".join(f"[v{i}]" for i in range(len(spans)))
fc = (f"{trims};{joined}concat=n={len(spans)}:v=1:a=0,fps=12,scale=960:-1:flags=lanczos,split[a][b];"
      "[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
subprocess.run(["ffmpeg", "-v", "error", "-i", webm, "-filter_complex", fc, "-loop", "0", "-y", out], check=True)
print(out, f"{sum(b - a for a, b in spans):.1f} s")
