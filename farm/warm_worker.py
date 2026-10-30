#!/usr/bin/env python3
# Warm worker: stays awake on one runner and serves small jobs with no start-up wait.
# Jobs: branches warm/<id> holding bundle.tar.gz.enc (same bundle as a render job, manifest has "mode": stills | clip).
# Results: an encrypted result.tar.enc pushed to done/<id> (status.json + the files). The client deletes done/<id>.
# Exits after IDLE_MINUTES with no jobs; near the 6 h job limit it starts its own successor if it was busy recently.
# Logs stay neutral (the repo is public): ids, counts and timings only.
import json, os, pathlib, shutil, subprocess, sys, tarfile, tempfile, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

REPO = os.environ["GITHUB_REPOSITORY"]
URL = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{REPO}.git"
FARM = pathlib.Path(__file__).resolve().parent
IDLE = float(os.environ.get("IDLE_MINUTES") or 120) * 60
LIMIT = 5 * 3600 + 40 * 60   # leave room under the 358 min job timeout
POLL_BUSY, POLL_IDLE = 2, 4   # seconds between looks for new jobs (busy = a job in the last 10 minutes)
START = time.time()
last_job = START
seen, lock = set(), threading.Lock()


def run(*cmd, cwd=None, inp=None):
    r = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True)
    if r.returncode: raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({r.returncode})")
    return r.stdout


def enc(src, dst, decrypt=False):
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt",
           "-pass", "env:RENDER_KEY", "-in", str(src), "-out", str(dst)]
    if decrypt: cmd.insert(3, "-d")
    run(*cmd)


def pending():
    out = run("git", "ls-remote", URL, "refs/heads/warm/*").decode()
    return [line.split("\t")[1] for line in out.splitlines() if "\t" in line]


def take(ref, dst):
    """Fetch the job blob into dst and delete the job branch, so no other worker picks it up."""
    with tempfile.TemporaryDirectory() as g:
        run("git", "init", "-q", g)
        run("git", "fetch", "-q", "--depth", "1", URL, ref, cwd=g)
        dst.write_bytes(run("git", "show", "FETCH_HEAD:bundle.tar.gz.enc", cwd=g))
    run("git", "push", "-q", URL, "--delete", ref)


def give(jid, blob):
    """Push the encrypted result as a one-file orphan commit on done/<id>."""
    with tempfile.TemporaryDirectory() as g:
        run("git", "init", "-q", "-b", "r", g)
        run("git", "config", "user.name", "render", cwd=g); run("git", "config", "user.email", "render@localhost", cwd=g)
        shutil.copy2(blob, pathlib.Path(g) / "result.tar.enc")
        run("git", "add", "result.tar.enc", cwd=g); run("git", "commit", "-q", "-m", "result", cwd=g)
        run("git", "push", "-q", "-f", URL, f"HEAD:refs/heads/done/{jid}", cwd=g)


def stills(page, M, out):
    """One PNG per time in M["times"]. Same browser settings as a render shard."""
    from playwright.sync_api import sync_playwright
    VW, VH = (int(v) for v in (M.get("size") or "1920x1080").split("x"))
    args = ["--force-color-profile=srgb", "--disable-lcd-text"] + (M.get("extra_args") or "").split()
    settle = any("window.READY" in f.read_text(errors="ignore") for f in page.parent.glob("*.js")) or "window.READY" in page.read_text(errors="ignore")
    q = M.get("query") or "still=1"
    with sync_playwright() as p:
        b = p.chromium.launch(args=args)
        pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=float(M.get("scale") or 1))
        pg.set_default_timeout(300000)
        pg.goto(f"file://{page}?{q}")
        pg.wait_for_load_state("networkidle")
        pg.evaluate("document.fonts.ready.then(() => 1)")
        pg.wait_for_function("typeof render === 'function'")
        if settle: pg.wait_for_function("window.READY === true")
        pg.evaluate("Promise.all([...document.images].map(i => i.decode ? i.decode().catch(() => 0) : 0))")
        for k, t in enumerate(M.get("times") or [0]):
            pg.evaluate(f"render({float(t)})")
            if settle:
                pg.wait_for_timeout(250)
                if pg.evaluate("window.FRESH || 0"): pg.wait_for_timeout(250)
            pg.screenshot(path=str(out / f"still_{k:02d}.png"), type="png")
        b.close()


def clip(mf, M, out, work):
    """Short clip: the render shard script as a single shard, then one x264 encode (crf 16), same as the farm join."""
    sub = work / "s"; sub.mkdir()
    run(sys.executable, str(FARM / "render_shard.py"), str(mf), "0", "1", str(sub))
    fps = int(M.get("fps") or 30)
    run("ffmpeg", "-y", "-loglevel", "error", "-i", str(sub / "chunk_000.nut"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", "16", "-preset", "medium", "-r", str(fps), "-movflags", "+faststart", str(out / "clip.mp4"))


def handle(ref):
    global last_job
    jid = ref.rsplit("/", 1)[1]
    t0 = time.time()
    with tempfile.TemporaryDirectory() as w:
        w = pathlib.Path(w); out = w / "out"; out.mkdir(); status = {"id": jid, "ok": False}
        try:
            take(ref, w / "b.enc")
            enc(w / "b.enc", w / "b.tgz", decrypt=True)
            with tarfile.open(w / "b.tgz") as tf: tf.extractall(w / "x", filter="data")
            mf = w / "x" / "manifest.json"; M = json.loads(mf.read_text())
            page = (mf.parent / M["page"]).resolve()
            mode = M.get("mode") or "stills"
            status["mode"] = mode
            if mode == "stills": stills(page, M, out)
            elif mode == "clip": clip(mf, M, out, w)
            else: raise ValueError(f"unknown mode {mode}")
            status["ok"] = True
        except Exception:
            status["error"] = traceback.format_exc()[-3000:]   # travels encrypted, never printed
        status["seconds"] = round(time.time() - t0, 1)
        (out / "status.json").write_text(json.dumps(status))
        with tarfile.open(w / "r.tar", "w") as tf:
            for f in sorted(out.iterdir()): tf.add(f, arcname=f.name)
        enc(w / "r.tar", w / "r.enc")
        try: give(jid, w / "r.enc")
        except Exception as e: print(f"job {jid}: result push failed: {e}", flush=True)
    with lock: last_job = time.time()
    print(f"job {jid}: {'ok' if status['ok'] else 'failed'} in {status['seconds']} s", flush=True)


def successor():
    subprocess.run(["gh", "workflow", "run", "warm.yml", "--repo", REPO, "--ref", "main", "-f", f"idle_minutes={int(IDLE / 60)}"])
    print("started the next worker", flush=True)


if __name__ == "__main__":
    print(f"warm worker up, idle limit {int(IDLE / 60)} min", flush=True)
    pool = ThreadPoolExecutor(max_workers=2)
    while True:
        now = time.time()
        with lock: idle_for = now - last_job
        if idle_for > IDLE: print("idle, exiting", flush=True); break
        if now - START > LIMIT:
            if idle_for < IDLE: successor()
            break
        try:
            for ref in pending():
                if ref not in seen: seen.add(ref); pool.submit(handle, ref)
        except Exception as e:
            print(f"poll failed: {str(e)[:120]}", flush=True)
        time.sleep(POLL_BUSY if idle_for < 600 else POLL_IDLE)
    pool.shutdown(wait=True)
