# One shard of a cloud render. Mirrors the local renderer exactly:
# Playwright Chromium, viewport 1920x1080 (or the manifest's size, e.g. 1080x1920), device scale SCALE, --force-color-profile=srgb --disable-lcd-text,
# waits for networkidle, fonts and image decode, render(i / fps), jpeg q95 per frame.
# Then this shard's contiguous frame range is downscaled (Lanczos to 1920x1080, yuv420p) into a LOSSLESS ffv1 chunk.
# The join job does the single libx264 crf 16 encode, so the output matches a one-pass local render.
#
# usage: python render_shard.py <manifest.json> <shard> <shards> <outdir>
# The manifest comes from the decrypted job bundle: page (relative to the manifest), scale, fps, range, extra_args.
# Logs stay neutral (the repo is public): counts and timings only, never file names or font lists.
import json, os, pathlib, subprocess, sys, time
from multiprocessing import Process
from playwright.sync_api import sync_playwright

mf = pathlib.Path(sys.argv[1]).resolve(); M = json.loads(mf.read_text())
page = (mf.parent / M["page"]).resolve()
shard, shards = int(sys.argv[2]), int(sys.argv[3])
scale, fps = float(M.get("scale") or 2), int(M.get("fps") or 60)
rng = (M.get("range") or "").strip()
outdir = pathlib.Path(sys.argv[4]).resolve()
extra = (M.get("extra_args") or "").split()
VW, VH = (int(v) for v in (M.get("size") or "1920x1080").split("x"))   # viewport in CSS px = output size (1080x1920 vertical)
workers = int(M.get("workers") or os.environ.get("WORKERS") or os.cpu_count() or 2)   # heavy pages: 1-2 (each Chromium at 2x can take GBs)
frames = outdir / "frames"
ARGS = ["--force-color-profile=srgb", "--disable-lcd-text"] + extra
SETTLE = any("window.READY" in f.read_text(errors="ignore") for f in pathlib.Path(page).parent.glob("*.js"))   # opt-in: only pages that signal READY


def open_page(p):
    for attempt in range(3):   # Linux runners sometimes lose the page while it opens ("Execution context was destroyed"); reopen it
        b = p.chromium.launch(args=ARGS)
        pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=scale)
        pg.set_default_timeout(300000)   # heavy frames (every plane on screen at 2x) can take over a minute on a runner; never give up at 30 s
        try:
            pg.goto(f"file://{page}?still=1")
            pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready.then(() => 1)")
            pg.wait_for_function("typeof render === 'function'")
            if SETTLE: pg.wait_for_function("window.READY === true")   # pages that set READY (async setup, e.g. per-layer lookdev) finish it first
            pg.evaluate("Promise.all([...document.images].map(i => i.decode ? i.decode().catch(() => 0) : 0))")
            return b, pg
        except Exception as e:
            print(f"open_page attempt {attempt + 1} failed: {str(e).splitlines()[0]}", flush=True)
            b.close()
            if attempt == 2: raise
            time.sleep(3)


def work(k, lo, hi):
    with sync_playwright() as p:
        b, pg = open_page(p)
        for i in range(lo + k, hi, workers):
            pg.evaluate(f"render({i / fps})")
            if SETTLE:   # same as that page's local render.py: let layers paint, longer when new ones just came on (else half-drawn frames)
                pg.wait_for_timeout(250)
                if pg.evaluate("window.FRESH || 0"): pg.wait_for_timeout(250)
            pg.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=95)
        b.close()


if __name__ == "__main__":
    t0 = time.time()
    frames.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b, pg = open_page(p)
        dur = pg.evaluate("DUR")
        b.close()
    n = int(round(dur * fps)) + 1
    a0, a1 = (float(v) for v in rng.split(",")) if rng else (0, dur)
    first, n = int(a0 * fps), min(n, int(a1 * fps) + 1)
    total = n - first
    lo, hi = first + shard * total // shards, first + (shard + 1) * total // shards
    print(f"shard {shard}/{shards}: {hi - lo} of {total} frames, {workers} workers")
    meta = {"shard": shard, "shards": shards, "first": first, "end": n, "total": total, "lo": lo, "hi": hi, "dur": dur, "fps": fps}
    if hi > lo:
        ps = [Process(target=work, args=(k, lo, hi)) for k in range(workers)]
        [x.start() for x in ps]; [x.join() for x in ps]
        for attempt in range(3):   # a worker can die (renderer crash); re-render whatever is missing with one worker
            miss = [i for i in range(lo, hi) if not (frames / f"{i:05d}.jpg").exists()]
            if not miss: break
            print(f"re-rendering {len(miss)} missing frames (attempt {attempt + 1})", flush=True)
            with sync_playwright() as p:
                b, pg = open_page(p)
                for i in miss:
                    pg.evaluate(f"render({i / fps})"); pg.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=95)
                b.close()
        miss = [i for i in range(lo, hi) if not (frames / f"{i:05d}.jpg").exists()]
        if miss: sys.exit(f"missing {len(miss)} frames, first {miss[0]}")
        t1 = time.time()
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-start_number", str(lo), "-i", str(frames / "%05d.jpg"),
                        "-frames:v", str(hi - lo), "-vf", f"scale={VW}:{VH}:flags=lanczos", "-pix_fmt", "yuv420p",
                        "-c:v", "ffv1", "-level", "3", "-slices", "16", "-g", "1", str(outdir / f"chunk_{shard:03d}.nut")], check=True)
        meta["render_s"], meta["encode_s"] = round(t1 - t0), round(time.time() - t1)
    (outdir / f"meta_{shard:03d}.json").write_text(json.dumps(meta))
    print(json.dumps(meta))
