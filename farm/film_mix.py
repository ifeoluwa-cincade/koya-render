"""Whole-film audio mix, shared by the Mac (tools/scenemaker/assemble/mix.py) and the farm's final job (film.py).
Same rules as research/podcast-style/final/mix.py (Ife's approved picker v2): voice mono on both sides with a -3 dB pad
at 0 dB; music bed at -24 LUFS, fade in 1.5 s, dipped 4 dB under speech by the voice activity envelope (attack .08,
hold .15, release .45, -40 dB gate, led by 60 ms), music fader -5 dB; SFX on their cue, level relative to the voice;
master gain to -15.5 LUFS and a 4x oversampled limiter so the true peak stays under -1 dBTP.
Logs: numbers only (the farm repo is public).

mix_core(vo, music, sfx, n_frames, fps, work, out_wav, target_lufs, vo_offset)
  vo: path or None. music: None or {"tracks": [paths], "bed_lufs", "dip_db", "music_db", "fade_in", "fade_out",
  "xfade", "offset"}. sfx: [{"file": path, "t": film seconds, "anchor": "start"|"peak", "peak_t", "db", "pan"}].
"""
import pathlib, re, subprocess
import numpy as np

SR = 48000
db = lambda x: 10 ** (x / 20)


def load(f, ch=2):
    b = subprocess.run(["ffmpeg", "-v", "error", "-threads", "2", "-i", str(f), "-ac", str(ch), "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, check=True).stdout
    return np.frombuffer(b, np.float32).reshape(-1, ch).copy()


def lufs(f):
    e = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-threads", "2", "-i", str(f), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", e)[-1]), float(re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", e)[-1])


def lufs_m_max(f):
    e = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(f), "-af", "apad=pad_dur=0.5,ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    ms = [float(x) for x in re.findall(r"M:\s*(-?[\d.]+)", e)]
    return max(ms) if ms else -20.0


def duck_env(vo, n100):
    env = np.zeros(n100); hold = 0; val = 0.0; w20 = int(SR * .02)
    ka, kr = 1 - np.exp(-1 / (.08 * 100)), 1 - np.exp(-1 / (.45 * 100))
    for i in range(n100):
        seg = vo[int(i * SR / 100): int(i * SR / 100) + w20]
        l = 10 * np.log10(np.mean(seg ** 2) + 1e-12) if len(seg) else -120
        if l > -40: hold = 15
        on = 1 if hold > 0 else 0; hold -= 1
        val += (on - val) * (ka if on > val else kr); env[i] = val
    return np.concatenate([env[6:], np.repeat(env[-1:], 6)])   # lead by 60 ms


def music_track(spec, N):
    tracks = [load(t) * db(-lufs(t)[0]) for t in spec["tracks"]]   # each track to 0 LU of itself, so the bed level holds across tracks
    xf = int(spec.get("xfade", 2) * SR)
    out = tracks[0][int(spec.get("offset", 0) * SR):]
    k = 1
    while len(out) < N + xf:
        nxt = tracks[k % len(tracks)]; k += 1
        if len(out) < xf or len(nxt) < xf: out = np.vstack([out, nxt]); continue
        r = np.linspace(0, 1, xf)[:, None]
        out = np.vstack([out[:-xf], out[-xf:] * np.cos(r * np.pi / 2) + nxt[:xf] * np.sin(r * np.pi / 2), nxt[xf:]])
    return out[:N]


def mix_core(vo, music, sfx, n_frames, fps, work, out_wav, target_lufs=-15.5, vo_offset=0.0):
    work = pathlib.Path(work)
    N = int(round(n_frames / fps * SR)); dur = N / SR
    mixb = np.zeros((N, 2), np.float32); rep = {}
    if vo:
        VO_LUFS = lufs(vo)[0]
        a = load(vo, 1)[:, 0]; v = np.zeros(N, np.float32); o = int(round(vo_offset * SR))
        n = max(0, min(N - o, len(a))); v[o:o + n] = a[:n]
        mixb += (v * db(-3.0))[:, None]
        rep["vo"] = dict(lufs=VO_LUFS, seconds=round(len(a) / SR, 3), cut_off_s=round(max(0, len(a) / SR + vo_offset - dur), 3))
    else:
        VO_LUFS = -18.0; v = np.zeros(N, np.float32); rep["vo"] = None
    n100 = int(np.ceil(dur * 100)) + 2
    env = duck_env(v, n100)
    if music:
        mus = music_track(music, N)
        t = np.arange(N) / SR; d = np.interp(t * 100, np.arange(n100), env)
        fin = np.clip(t / music.get("fade_in", 1.5), 0, 1); fout = np.clip((dur - t) / music.get("fade_out", 3.0), 0, 1)
        g = 10 ** ((music.get("bed_lufs", -24.0) - music.get("dip_db", 4.0) * d) / 20) * fin * fin * fout * db(music.get("music_db", -5.0))
        mixb += mus * g[:, None]
        rep["music"] = dict(tracks=len(music["tracks"]), bed_lufs=music.get("bed_lufs", -24.0), dip_db=music.get("dip_db", 4.0))
    placed, cache = [], {}
    for c in sfx or []:
        f = c["file"]
        if f not in cache: cache[f] = (load(f), lufs_m_max(f))
        s, lm = cache[f]
        t = float(c["t"]); pk = c.get("peak_t")
        if pk is None:
            e = np.convolve(np.abs(s).max(1), np.ones(480) / 480, "same"); pk = float(np.argmax(e)) / SR
        s0 = t - pk if c.get("anchor", "start") == "peak" else t
        seg = s * db(VO_LUFS - 3.0 + c.get("db", -15.0) - lm)
        p = float(c.get("pan", 0))
        if p:
            a = (p + 1) * np.pi / 4; m = seg.mean(1); seg = np.stack([m * np.cos(a) * 1.414, m * np.sin(a) * 1.414], 1)
        i0 = int(round(s0 * SR)); a0, b0 = max(0, i0), min(N, i0 + len(seg))
        if b0 > a0: mixb[a0:b0] += seg[a0 - i0: b0 - i0]
        placed.append(dict(start=round(s0, 3), at=round(t, 3)))
    rep["sfx"] = placed
    pre = work / "premix.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-", "-c:a", "pcm_f32le", str(pre)],
                   input=mixb.astype(np.float32).tobytes(), check=True)
    I0, P0 = lufs(pre)
    limit = 0.83
    for _ in range(4):   # tighten the limiter if the true peak still lands over -1 dBTP
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-i", str(pre), "-af",
                        f"volume={target_lufs - I0:.2f}dB,aresample=192000,alimiter=limit={limit}:attack=1:release=60:level=false,aresample=48000",
                        "-c:a", "pcm_s24le", str(out_wav)], check=True)
        I1, P1 = lufs(out_wav)
        if P1 <= -1.0: break
        limit = round(limit * 0.94, 3)
    rep.update(premix=[I0, P0], master=dict(lufs=I1, true_peak=P1, limit=limit), samples=N)
    return rep
