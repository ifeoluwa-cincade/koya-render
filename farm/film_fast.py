# Fast film shard (30 Oct 2026). Same pages, same Chromium flags, same frames as farm/film.py, with less overhead:
#   - each shard runs W Chromium workers; every worker owns whole pieces (contiguous frame ranges), so frames never wait
#     for each other and each piece streams straight into its own ffmpeg (no jpeg files, encode overlaps the render)
#   - frames are captured with one CDP Page.captureScreenshot call (the same call Playwright's page.screenshot makes,
#     same clip and jpeg quality 95) instead of page.screenshot's extra prepare/fonts/restore round trips
#   - one ffmpeg decode feeds both outputs (final + WhatsApp pieces) through a split, same scale filter and x264 settings
# prep and final are unchanged: film_fast.yml calls farm/film.py prep and farm/film.py final.
#
#   python3 farm/film_fast.py shard <k>     render this shard's pieces
#   python3 farm/film_fast.py bench         timing + pixel-identity bench (manifest key "bench"), encrypted report
# Logs stay neutral (the repo is public): counts and timings only.
import base64, json, os, pathlib, subprocess, sys, time, traceback, hashlib
from multiprocessing import Process, Queue

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import film as F   # noqa: E402  (enc, manifest, fetch_objects, TMP, ENC)

TMP, ENC = F.TMP, F.ENC
BASE_ARGS = ["--force-color-profile=srgb", "--disable-lcd-text"]


SETTLE_OVERRIDE = None   # bench only: force the settle rule on or off for every variant


def has_settle(page):
    """Same rule as film.py: the 250 ms settle waits are opt-in for pages whose own folder has a script setting READY."""
    if SETTLE_OVERRIDE is not None: return SETTLE_OVERRIDE
    return any("window.READY" in f.read_text(errors="ignore") for f in page.parent.glob("*.js"))


def open_page(p, page, VW, VH, scale, args, settle):
    for attempt in range(3):
        b = p.chromium.launch(args=args)
        pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=scale)
        pg.set_default_timeout(300000)
        try:
            pg.goto(f"file://{page}?still=1"); pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready.then(() => 1)"); pg.wait_for_function("typeof render === 'function'")
            if settle or SETTLE_OVERRIDE is not None: pg.wait_for_function("window.READY === true")
            pg.evaluate("Promise.all([...document.images].map(i => i.decode ? i.decode().catch(() => 0) : 0))")
            return b, pg
        except Exception:
            print(f"open attempt {attempt + 1} failed", flush=True); b.close()
            if attempt == 2: raise
            time.sleep(3)


class Grab:
    """One frame = render(t) + capture. mode 'pw' = page.screenshot (today), 'cdp' = the same CDP call directly."""
    def __init__(self, pg, fps, settle, mode, VW, VH, fast=False):
        self.pg, self.fps, self.settle, self.mode, self.fast = pg, fps, settle, mode, fast
        self.cdp = pg.context.new_cdp_session(pg) if mode == "cdp" else None
        self.clip = {"x": 0, "y": 0, "width": VW, "height": VH, "scale": 1}
        self.t_eval = self.t_cap = 0.0; self.n = 0

    def __call__(self, i):
        a = time.perf_counter()
        self.pg.evaluate(f"render({i / self.fps})")
        if self.settle:
            self.pg.wait_for_timeout(250)
            if self.pg.evaluate("window.FRESH || 0"): self.pg.wait_for_timeout(250)
        b = time.perf_counter()
        if self.cdp:
            o = {"format": "jpeg", "quality": 95, "clip": self.clip, "captureBeyondViewport": False}
            if self.fast: o["optimizeForSpeed"] = True
            data = base64.b64decode(self.cdp.send("Page.captureScreenshot", o)["data"])
        else:
            data = self.pg.screenshot(type="jpeg", quality=95)
        c = time.perf_counter()
        self.t_eval += b - a; self.t_cap += c - b; self.n += 1
        return data


def ff_stream(M, fps, VW, VH, out_final, out_wa):
    """One ffmpeg: jpeg frames on stdin -> the same Lanczos scale + yuv420p as film.py -> final and WhatsApp x264 pieces."""
    q = [("final", M["final"], out_final), ("wa", M["wa"], out_wa)]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-",
           "-filter_complex", f"[0:v]scale={VW}:{VH}:flags=lanczos,format=yuv420p,split=2[a][b]"]
    for (tag, s, out), lab in zip(q, ("[a]", "[b]")):
        cmd += ["-map", lab, "-c:v", "libx264", "-r", str(fps), "-preset", s["preset"], "-b:v", s["b"], "-maxrate", s["maxrate"],
                "-bufsize", s["bufsize"], "-g", str(2 * fps), str(out)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def ff_files(M, fps, VW, VH, frames, lo, hi, out_final, out_wa):
    """film.py's exact encode (from jpeg files, two passes), used by the bench as the reference."""
    src = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-start_number", str(lo), "-i", str(frames / "%05d.jpg"),
           "-frames:v", str(hi - lo), "-vf", f"scale={VW}:{VH}:flags=lanczos", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-r", str(fps)]
    for q, out in ((M["final"], out_final), (M["wa"], out_wa)):
        subprocess.run(src + ["-preset", q["preset"], "-b:v", q["b"], "-maxrate", q["maxrate"], "-bufsize", q["bufsize"],
                              "-g", str(2 * fps), str(out)], check=True)


def worker(wid, jobs, done, M, B, OUT, mode, fast, args, keep=None):
    """Pull pieces (si, lo, hi) from the queue; keep one browser per scene; stream each piece into its own ffmpeg."""
    from playwright.sync_api import sync_playwright
    fps, scale = int(M["fps"]), float(M["scale"]); VW, VH = (int(v) for v in M["size"].split("x"))
    stats = dict(w=wid, pieces=0, frames=0, open_s=0.0, eval_s=0.0, cap_s=0.0, wall_s=0.0)
    t0 = time.time()
    try:
        with sync_playwright() as p:
            cur, b, pg, g = None, None, None, None
            while True:
                job = jobs.get()
                if job is None: break
                si, lo, hi = job
                page = (B / M["scenes"][si]["page"]).resolve(); settle = has_settle(page)
                if si != cur:
                    if g: stats["eval_s"] += g.t_eval; stats["cap_s"] += g.t_cap
                    if b: b.close()
                    a = time.time(); b, pg = open_page(p, page, VW, VH, scale, args, settle); stats["open_s"] += time.time() - a
                    g = Grab(pg, fps, settle, mode, VW, VH, fast); cur = si
                fo, wo = OUT / f"p_{si:03d}_{lo:06d}_final.mp4", OUT / f"p_{si:03d}_{lo:06d}_wa.mp4"
                ff = ff_stream(M, fps, VW, VH, fo, wo)
                try:
                    for i in range(lo, hi):
                        data = g(i); ff.stdin.write(data)
                        if keep is not None and i in keep: (OUT / f"s_{i:05d}.jpg").write_bytes(data)
                    ff.stdin.close()
                except BaseException:
                    ff.kill(); raise
                if ff.wait() != 0: raise RuntimeError("encode failed")
                stats["pieces"] += 1; stats["frames"] += hi - lo
                done.put(("ok", si, lo, hi))
            if b: b.close()
            if g: stats["eval_s"] += g.t_eval; stats["cap_s"] += g.t_cap
    except Exception:
        done.put(("err", wid, traceback.format_exc()[-1500:], None))
        print(f"worker {wid} stopped early", flush=True)
    stats["wall_s"] = time.time() - t0
    (OUT / f"wstat_{wid}.json").write_text(json.dumps(stats))


def run_pieces(M, pieces, B, OUT, W, mode="cdp", fast=False, extra=(), keep=None):
    """Render pieces with W workers; retry pieces that did not finish. Returns per-worker stats."""
    args = BASE_ARGS + (M.get("extra_args") or "").split() + list(extra)
    todo = sorted(pieces, key=lambda x: (x[0], x[1])); stats = []
    for attempt in range(3):
        jobs, done = Queue(), Queue()
        for pc in todo: jobs.put(tuple(pc))
        n = min(W, len(todo))
        for _ in range(n): jobs.put(None)
        ps = [Process(target=worker, args=(w, jobs, done, M, B, OUT, mode, fast, args, keep)) for w in range(n)]
        [x.start() for x in ps]; [x.join() for x in ps]
        ok = set()
        while not done.empty():
            r = done.get()
            if r[0] == "ok": ok.add(tuple(r[1:]))
            else: (ENC / f"err_worker_{r[1]}_{attempt}.enc").write_bytes(F.enc(None, None, data=r[2].encode()))
        stats += [json.loads(f.read_text()) for f in sorted(OUT.glob("wstat_*.json"))]
        for f in OUT.glob("wstat_*.json"): f.unlink()
        todo = [pc for pc in todo if tuple(pc) not in ok]
        if not todo: return stats
        print(f"re-rendering {len(todo)} piece(s)", flush=True); W = 1
    raise SystemExit("pieces missing after 3 attempts")


def fetch_scenes(M, scenes, B):
    need = {}
    for si in scenes:
        for rel in M["scenes"][si]["files"]:
            need.setdefault(M["files"][rel]["c"], []).append(B / rel)
    F.fetch_objects(need.keys(), need)
    return len(need)


# ------------------------------------------------------------------ shard
def shard(M, k):
    W = TMP / "w"; B = W / "bundle"; OUT = TMP / "out"; OUT.mkdir(parents=True, exist_ok=True)
    pieces = M["shards"][k]
    t0 = time.time(); na = fetch_scenes(M, sorted({p[0] for p in pieces}), B)
    nf = sum(p[2] - p[1] for p in pieces)
    print(f"shard {k}: {len(pieces)} piece(s), {nf} frames, {na} assets in {time.time() - t0:.0f}s", flush=True)
    workers = int(M.get("fast_workers") or M.get("workers") or os.cpu_count() or 2)
    t1 = time.time()
    st = run_pieces(M, pieces, B, OUT, workers, mode=M.get("capture", "cdp"))
    wall = time.time() - t1
    (OUT / f"meta_{k:03d}.json").write_text(json.dumps(dict(pieces=pieces, wall_s=round(wall, 1), workers=st)))
    print(f"shard {k}: {nf} frames in {wall:.0f}s ({nf / max(wall, 1e-6):.2f} fps), {workers} workers", flush=True)
    tar = subprocess.run(["tar", "cf", "-", "-C", str(OUT), "--exclude=s_*.jpg", "."], capture_output=True, check=True).stdout
    (ENC / f"piece_{k:03d}.enc").write_bytes(F.enc(None, None, data=tar))


# ------------------------------------------------------------------ bench
def framemd5(f):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "framemd5", "-"], capture_output=True, text=True, check=True).stdout
    return [ln.rsplit(",", 1)[1].strip() for ln in r.splitlines() if ln and not ln.startswith("#")]


def rgb(jpg):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(jpg), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout


def diff(a, b):
    import numpy as np
    x, y = np.frombuffer(rgb(a), np.uint8).astype(np.int16), np.frombuffer(rgb(b), np.uint8).astype(np.int16)
    if x.shape != y.shape: return dict(same=False, shape=[int(x.size), int(y.size)])
    d = np.abs(x - y)
    return dict(same=bool(d.max() == 0), max=int(d.max()), px_diff=int((d.reshape(-1, 3).max(1) > 0).sum()), mean=round(float(d.mean()), 4))


def cpu_now():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return sum(v), v[3] + v[4]


def bench(M):
    """M["bench"] = {"scene": si, "lo": a, "hi": b, "keep": [frames], "variants": [{name, workers, mode, fast, pieces, extra,
    files}]}. files=True is film.py's exact path (interleaved workers, page.screenshot to jpeg files, two-pass encode).
    Every variant keeps the same sample frames; the report compares them pixel by pixel with the first variant."""
    global SETTLE_OVERRIDE
    Bn = M["bench"]; SETTLE_OVERRIDE = Bn.get("settle")
    si, lo, hi = Bn["scene"], Bn["lo"], Bn["hi"]; keep = set(Bn.get("keep") or [])
    W = TMP / "w"; B = W / "bundle"; R = TMP / "bench"; R.mkdir(parents=True, exist_ok=True)
    t0 = time.time(); na = fetch_scenes(M, [si], B); fetch_s = time.time() - t0
    fps, VW, VH = int(M["fps"]), *(int(v) for v in M["size"].split("x"))
    rep = dict(cpus=os.cpu_count(), fetch_s=round(fetch_s, 1), assets=na, frames=hi - lo, variants=[])
    print(f"bench: {hi - lo} frames, {na} assets in {fetch_s:.0f}s, {os.cpu_count()} cpus", flush=True)
    for v in Bn["variants"]:
        OUT = R / v["name"]; OUT.mkdir()
        c0, i0 = cpu_now(); t1 = time.time()
        if v.get("files"):   # today's path: film.py shard() inner loop, verbatim settings
            from playwright.sync_api import sync_playwright
            frames = OUT / "frames"; frames.mkdir()
            page = (B / M["scenes"][si]["page"]).resolve(); settle = has_settle(page); nW = int(v["workers"])
            args = BASE_ARGS + (M.get("extra_args") or "").split() + list(v.get("extra") or [])

            def work(w):
                with sync_playwright() as p:
                    b, pg = open_page(p, page, VW, VH, float(M["scale"]), args, settle)
                    g = Grab(pg, fps, settle, "pw", VW, VH)
                    for i in range(lo + w, hi, nW): (frames / f"{i:05d}.jpg").write_bytes(g(i))
                    (OUT / f"wstat_{w}.json").write_text(json.dumps(dict(w=w, frames=g.n, eval_s=g.t_eval, cap_s=g.t_cap)))
                    b.close()
            ps = [Process(target=work, args=(w,)) for w in range(nW)]; [x.start() for x in ps]; [x.join() for x in ps]
            t_r = time.time()
            ff_files(M, fps, VW, VH, frames, lo, hi, OUT / "final.mp4", OUT / "wa.mp4")
            enc_s = time.time() - t_r
            # encode-chain check: the same jpegs streamed through ff_stream must give the same final piece as film.py's encode
            ff = ff_stream(M, fps, VW, VH, OUT / "chk_final.mp4", OUT / "chk_wa.mp4")
            for i in range(lo, hi): ff.stdin.write((frames / f"{i:05d}.jpg").read_bytes())
            ff.stdin.close(); ff.wait()
            chain = dict(final_same=framemd5(OUT / "final.mp4") == framemd5(OUT / "chk_final.mp4"),
                         wa_same=framemd5(OUT / "wa.mp4") == framemd5(OUT / "chk_wa.mp4"))
            for i in keep:
                if (frames / f"{i:05d}.jpg").exists(): (OUT / f"s_{i:05d}.jpg").write_bytes((frames / f"{i:05d}.jpg").read_bytes())
            subprocess.run(["rm", "-rf", str(frames)])
            st = [json.loads(f.read_text()) for f in sorted(OUT.glob("wstat_*.json"))]
            outs = [(OUT / "final.mp4", OUT / "wa.mp4")]
        else:
            n = int(v.get("pieces") or v["workers"])
            pcs = [[si, lo + j * (hi - lo) // n, lo + (j + 1) * (hi - lo) // n] for j in range(n)]
            st = run_pieces(M, pcs, B, OUT, int(v["workers"]), mode=v.get("mode", "cdp"), fast=v.get("fast", False),
                            extra=v.get("extra") or (), keep=keep)
            enc_s = 0.0; chain = None
            outs = [(OUT / f"p_{si:03d}_{a:06d}_final.mp4", OUT / f"p_{si:03d}_{a:06d}_wa.mp4") for _, a, _ in pcs]
        wall = time.time() - t1; c1, i1 = cpu_now()
        nfr = sum(x.get("frames", 0) for x in st) or 1
        md5 = [m for fo, _ in outs for m in framemd5(fo)]
        r = dict(name=v["name"], workers=v["workers"], wall_s=round(wall, 1), fps=round((hi - lo) / wall, 2), encode_after_s=round(enc_s, 1),
                 s_per_frame_worker=round(sum(x["eval_s"] + x["cap_s"] for x in st) / nfr, 3),
                 eval_ms=round(1000 * sum(x["eval_s"] for x in st) / nfr), cap_ms=round(1000 * sum(x["cap_s"] for x in st) / nfr),
                 open_s=round(sum(x.get("open_s", 0) for x in st), 1), cpu_busy=round(1 - (i1 - i0) / max(1, c1 - c0), 2),
                 out_frames=len(md5), final_md5=hashlib.md5("".join(md5).encode()).hexdigest()[:12], chain=chain)
        if rep["variants"]:
            ref = R / Bn["variants"][0]["name"]
            r["diff"] = {str(i): diff(ref / f"s_{i:05d}.jpg", OUT / f"s_{i:05d}.jpg") for i in sorted(keep)
                         if (ref / f"s_{i:05d}.jpg").exists() and (OUT / f"s_{i:05d}.jpg").exists()}
            r["jpeg_bytes_same"] = all((ref / f"s_{i:05d}.jpg").read_bytes() == (OUT / f"s_{i:05d}.jpg").read_bytes() for i in keep
                                       if (ref / f"s_{i:05d}.jpg").exists() and (OUT / f"s_{i:05d}.jpg").exists())
        rep["variants"].append(r)
        print(f"{v['name']}: {r['wall_s']}s, {r['fps']} fps, eval {r['eval_ms']} ms, capture {r['cap_ms']} ms, cpu {r['cpu_busy']}", flush=True)
        for f in OUT.glob("*.mp4"): f.unlink()
    (ENC / "bench.enc").write_bytes(F.enc(None, None, data=json.dumps(rep).encode()))


if __name__ == "__main__":
    mode = sys.argv[1]
    try:
        M = F.manifest()
        {"shard": lambda: shard(M, int(sys.argv[2])), "bench": lambda: bench(M)}[mode]()
    except SystemExit:
        raise
    except BaseException as e:
        tag = f"{mode}_{sys.argv[2] if len(sys.argv) > 2 else 0}"
        (ENC / f"err_{tag}.enc").write_bytes(F.enc(None, None, data=traceback.format_exc().encode()))
        print(f"{mode} failed ({type(e).__name__}); details in the encrypted err artifact")
        sys.exit(1)
