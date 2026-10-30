# Whole-film job on the farm (30 Oct 2026): render every scene, join, mix and encode in the cloud, so the Mac only sends
# small files and receives the finished video. Driven by tools/scenemaker/assemble/cloud_film.py.
#
#   python3 farm/film.py prep            fetch URL assets into the encrypted asset cache, write the shard matrix
#   python3 farm/film.py shard <k>       render this shard's pieces (scene, frame range), encode final + WhatsApp pieces
#   python3 farm/film.py final           join the pieces (stream copy), mix the audio, mux final + WhatsApp, encrypt
#
# The job branch holds one encrypted manifest (film.enc). Every file a page needs lives in the asset cache branch
# `cache` as an encrypted object c/<name>/<part> (name = HMAC of the content hash, so names say nothing), plus
# c/<name>/n with the part count. Rendering mirrors farm/render_shard.py exactly (same Chromium flags, waits, jpeg q95).
# Logs stay neutral (the repo is public): counts and timings only. On an error the details go into an encrypted
# err artifact for the Mac; the log only says which step failed.
import json, os, pathlib, subprocess, sys, time, traceback, hashlib
from concurrent.futures import ThreadPoolExecutor

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TMP = pathlib.Path(os.environ.get("RUNNER_TEMP", "/tmp"))
ENC = TMP / "enc"; ENC.mkdir(parents=True, exist_ok=True)
KEYENV = "RENDER_KEY"
os.environ[KEYENV] = os.environ.get(KEYENV, "").strip()
PART = 50 * 1024 * 1024   # parts the runner writes (git refuses files over 100 MB)


def enc(src, dst, decrypt=False, data=None):
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt", "-pass", f"env:{KEYENV}"]
    if decrypt: cmd.insert(2, "-d")
    if data is not None:
        return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout
    subprocess.run(cmd + ["-in", str(src), "-out", str(dst)], check=True, capture_output=True)


def manifest():
    return json.loads(enc(None, None, decrypt=True, data=pathlib.Path("film.enc").read_bytes()))


def remote():
    tok = os.environ.get("GH_TOKEN", "")
    return f"https://x-access-token:{tok}@github.com/{REPO}.git" if tok else f"https://github.com/{REPO}.git"


def cache_clone():
    d = TMP / "cache"
    if not d.exists():
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "--depth", "1", "-b", "cache", remote(), str(d)],
                       check=True, capture_output=True)
        for k, v in (("user.name", "render"), ("user.email", "render@localhost")): subprocess.run(["git", "-C", str(d), "config", k, v], check=True)
    return d


def cached_names(d):
    out = subprocess.run(["git", "-C", str(d), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, check=True).stdout
    return {p.split("/")[1] for p in out.split() if p.startswith("c/") and p.endswith("/n")}


def fetch_objects(names, dest_of):
    """Check out the given cache objects (only their blobs are downloaded) and decrypt each to dest_of[name] (a list)."""
    d = cache_clone(); names = sorted(set(names))
    for i in range(0, len(names), 200):
        subprocess.run(["git", "-C", str(d), "checkout", "HEAD", "--", *[f"c/{n}" for n in names[i:i + 200]]], check=True, capture_output=True)

    def one(n):
        parts = sorted(p for p in (d / "c" / n).iterdir() if p.name != "n")
        assert len(parts) == int((d / "c" / n / "n").read_text()), "incomplete object"
        plain = enc(None, None, decrypt=True, data=b"".join(p.read_bytes() for p in parts))
        for dst in dest_of[n]:
            dst.parent.mkdir(parents=True, exist_ok=True); dst.write_bytes(plain)
    with ThreadPoolExecutor(8) as ex: list(ex.map(one, names))


# ------------------------------------------------------------------ prep
def prep(M):
    d = cache_clone(); have = cached_names(d)
    want = {v["c"]: v for v in M["files"].values() if v.get("url") and v["c"] not in have}
    for a in M.get("audio_files", {}).values():
        if a.get("url") and a["c"] not in have: want[a["c"]] = a
    failed, ok = [], 0
    if want:
        subprocess.run(["git", "-C", str(d), "read-tree", "HEAD"], check=True, capture_output=True)

        def get(item):
            r = subprocess.run(["curl", "-sfL", "--retry", "4", "--retry-delay", "3", "-A", "KoyaRender/1.0 (render farm)", item["url"]], capture_output=True)
            if r.returncode or hashlib.sha256(r.stdout).hexdigest() != item["sha256"]: return item["c"], None
            return item["c"], enc(None, None, data=r.stdout)
        with ThreadPoolExecutor(6) as ex:
            for name, blob in ex.map(get, want.values()):
                if blob is None: failed.append(name); continue
                od = d / "c" / name; od.mkdir(parents=True, exist_ok=True)
                parts = [blob[i:i + PART] for i in range(0, len(blob), PART)] or [b""]
                for k, b in enumerate(parts): (od / f"{k:03d}").write_bytes(b)
                (od / "n").write_text(str(len(parts)))
                subprocess.run(["git", "-C", str(d), "add", f"c/{name}"], check=True, capture_output=True); ok += 1
        if ok:
            subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "cache"], check=True, capture_output=True)
            for attempt in range(5):   # another writer may have pushed meanwhile: move onto its tip and push again
                if subprocess.run(["git", "-C", str(d), "push", "-q", "origin", "HEAD:cache"], capture_output=True).returncode == 0: break
                subprocess.run(["git", "-C", str(d), "fetch", "-q", "--filter=blob:none", "--depth", "1", "origin", "cache"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(d), "reset", "-q", "--soft", "FETCH_HEAD"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(d), "read-tree", "FETCH_HEAD"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(d), "add", *[f"c/{n}" for n in want if n not in failed]], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "cache"], check=True, capture_output=True)
                time.sleep(3)
            else: raise RuntimeError("cache push failed")
    print(f"url assets: {ok} fetched, {len(failed)} failed, {len(M['files']) - len(want)} already cached or uploaded")
    if failed:
        (ENC / "prep.enc").write_bytes(enc(None, None, data=json.dumps({"failed": failed}).encode()))
        sys.exit(1)
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh: fh.write(f"matrix={json.dumps(list(range(len(M['shards']))))}\n")


# ------------------------------------------------------------------ shard
def shard(M, k):
    from multiprocessing import Process
    from playwright.sync_api import sync_playwright
    W = TMP / "w"; B = W / "bundle"; OUT = TMP / "out"; OUT.mkdir(parents=True, exist_ok=True)
    pieces = M["shards"][k]
    scenes = sorted({p[0] for p in pieces})
    need = {}
    for si in scenes:
        for rel in M["scenes"][si]["files"]:
            need.setdefault(M["files"][rel]["c"], []).append(B / rel)
    t0 = time.time(); fetch_objects(need.keys(), need)
    print(f"shard {k}: {len(pieces)} piece(s), {sum(p[2] - p[1] for p in pieces)} frames, {len(need)} assets in {time.time() - t0:.0f}s")
    fps, scale = int(M["fps"]), float(M["scale"]); VW, VH = (int(v) for v in M["size"].split("x"))
    workers = int(M.get("workers") or os.cpu_count() or 2)
    ARGS = ["--force-color-profile=srgb", "--disable-lcd-text"] + (M.get("extra_args") or "").split()
    meta = []
    for si, lo, hi in pieces:
        page = (B / M["scenes"][si]["page"]).resolve()
        frames = W / f"frames_{si}_{lo}"; frames.mkdir(parents=True, exist_ok=True)
        SETTLE = any("window.READY" in f.read_text(errors="ignore") for f in page.parent.glob("*.js"))

        def open_page(p):
            for attempt in range(3):
                b = p.chromium.launch(args=ARGS)
                pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=scale)
                pg.set_default_timeout(300000)
                try:
                    pg.goto(f"file://{page}?still=1"); pg.wait_for_load_state("networkidle")
                    pg.evaluate("document.fonts.ready.then(() => 1)"); pg.wait_for_function("typeof render === 'function'")
                    if SETTLE: pg.wait_for_function("window.READY === true")
                    pg.evaluate("Promise.all([...document.images].map(i => i.decode ? i.decode().catch(() => 0) : 0))")
                    return b, pg
                except Exception:
                    print(f"open attempt {attempt + 1} failed", flush=True); b.close()
                    if attempt == 2: raise
                    time.sleep(3)

        def shot(pg, i):
            pg.evaluate(f"render({i / fps})")
            if SETTLE:
                pg.wait_for_timeout(250)
                if pg.evaluate("window.FRESH || 0"): pg.wait_for_timeout(250)
            pg.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=95)

        def work(w):
            try:
                with sync_playwright() as p:
                    b, pg = open_page(p)
                    for i in range(lo + w, hi, workers): shot(pg, i)
                    b.close()
            except Exception:   # neutral log; missing frames are re-rendered below
                print("worker stopped early", flush=True)
        t1 = time.time()
        ps = [Process(target=work, args=(w,)) for w in range(workers)]; [x.start() for x in ps]; [x.join() for x in ps]
        for attempt in range(3):
            miss = [i for i in range(lo, hi) if not (frames / f"{i:05d}.jpg").exists()]
            if not miss: break
            print(f"re-rendering {len(miss)} missing frames", flush=True)
            with sync_playwright() as p:
                b, pg = open_page(p)
                for i in miss: shot(pg, i)
                b.close()
        if any(not (frames / f"{i:05d}.jpg").exists() for i in range(lo, hi)): sys.exit("missing frames")
        t2 = time.time()
        src = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-start_number", str(lo), "-i", str(frames / "%05d.jpg"),
               "-frames:v", str(hi - lo), "-vf", f"scale={VW}:{VH}:flags=lanczos", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-r", str(fps)]
        for tag, q in (("final", M["final"]), ("wa", M["wa"])):
            subprocess.run(src + ["-preset", q["preset"], "-b:v", q["b"], "-maxrate", q["maxrate"], "-bufsize", q["bufsize"],
                                  "-g", str(2 * fps), str(OUT / f"p_{si:03d}_{lo:06d}_{tag}.mp4")], check=True)
        meta.append(dict(si=si, lo=lo, hi=hi, render_s=round(t2 - t1), encode_s=round(time.time() - t2)))
        subprocess.run(["rm", "-rf", str(frames)])
    (OUT / f"meta_{k:03d}.json").write_text(json.dumps(meta))
    print(json.dumps([{x: m[x] for x in ("hi", "lo", "render_s", "encode_s")} | {"frames": m["hi"] - m["lo"]} for m in meta]))
    tar = subprocess.run(["tar", "cf", "-", "-C", str(OUT), "."], capture_output=True, check=True).stdout
    (ENC / f"piece_{k:03d}.enc").write_bytes(enc(None, None, data=tar))


# ------------------------------------------------------------------ final
def final(M):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from film_mix import mix_core, lufs
    P = TMP / "parts"; J = TMP / "j"; A = TMP / "a"; F = TMP / "final"
    for x in (J, A, F): x.mkdir(parents=True, exist_ok=True)
    for f in sorted(P.glob("piece_*.enc")):
        subprocess.run(["tar", "xf", "-", "-C", str(J)], input=enc(None, None, decrypt=True, data=f.read_bytes()), check=True)
    want = sorted((si, lo, hi) for sh in M["shards"] for si, lo, hi in sh)
    total = sum(hi - lo for _, lo, hi in want)
    def count(f):
        o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets",
                            "-of", "csv=p=0", str(f)], capture_output=True, text=True).stdout.strip()
        return int(o) if o.isdigit() else -1
    for tag in ("final", "wa"):
        lst = J / f"{tag}.txt"
        lst.write_text("".join(f"file 'p_{si:03d}_{lo:06d}_{tag}.mp4'\n" for si, lo, hi in want))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-an", str(J / f"{tag}_v.mp4")], check=True)
        assert count(J / f"{tag}_v.mp4") == total, f"{tag}: frame count"
    au = M["audio"]; dest = {}
    for n in [au.get("vo")] + (au.get("music") or {}).get("tracks", []) + [c["file"] for c in au.get("sfx", [])]:
        if n: dest.setdefault(n, [A / n])
    if dest: fetch_objects(dest.keys(), dest)
    music = dict(au["music"], tracks=[str(A / n) for n in au["music"]["tracks"]]) if au.get("music") else None
    sfx = [dict(c, file=str(A / c["file"])) for c in au.get("sfx", [])]
    t0 = time.time()
    rep = mix_core(str(A / au["vo"]) if au.get("vo") else None, music, sfx, total, int(M["fps"]), J, J / "mix.wav",
                   M.get("target_lufs", -15.5), M.get("vo_offset", 0.0))
    mix_s = time.time() - t0
    for tag, ab in (("final", "320k"), ("wa", "128k")):
        out = J / f"{tag}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(J / f"{tag}_v.mp4"), "-i", str(J / "mix.wav"), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", ab, "-movflags", "+faststart", str(out)], check=True)
        enc(out, F / f"{tag}.mp4.enc")
    I, TP = lufs(J / "final.mp4")
    rep = dict(frames=total, mix=rep, mix_s=round(mix_s), final_lufs=I, final_tp=TP,
               bytes={t: (J / f"{t}.mp4").stat().st_size for t in ("final", "wa")})
    (F / "report.json.enc").write_bytes(enc(None, None, data=json.dumps(rep).encode()))
    print(f"frames {total}, final {rep['bytes']['final'] // 1000000} MB, wa {rep['bytes']['wa'] // 1000000} MB, mix {mix_s:.0f}s")


if __name__ == "__main__":
    mode = sys.argv[1]
    try:
        M = manifest()
        {"prep": lambda: prep(M), "shard": lambda: shard(M, int(sys.argv[2])), "final": lambda: final(M)}[mode]()
    except SystemExit:
        raise
    except BaseException as e:   # details only inside the encrypted err artifact, never in the public log
        tag = f"{mode}_{sys.argv[2] if len(sys.argv) > 2 else 0}"
        (ENC / f"err_{tag}.enc").write_bytes(enc(None, None, data=traceback.format_exc().encode()))
        print(f"{mode} failed ({type(e).__name__}); details in the encrypted err artifact")
        sys.exit(1)
