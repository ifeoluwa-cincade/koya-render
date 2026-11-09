#!/usr/bin/env bash
# Farm steps. Logs stay neutral: no file names, page paths or content are printed (the repo is public).
# The encryption key arrives as env RENDER_KEY (Actions secret) and is passed to openssl via an env reference, never echoed.
set -euo pipefail
RENDER_KEY="$(printf %s "${RENDER_KEY:-}" | tr -d '\r\n')"; export RENDER_KEY   # the secret may carry a trailing newline
FF_DIR="$HOME/ffmpeg"
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:RENDER_KEY "$@"; }

ffmpeg_tool() {
  if [ ! -x "$FF_DIR/bin/ffmpeg" ]; then
    # 9 Nov: BtbN's "latest" asset is briefly missing while it is rebuilt ("xz: File format not recognized" failed
    # 6 chunks on the new farms). Retry with a pause; same build, same URL.
    for i in 1 2 3 4 5 6; do
      rm -rf "${FFMPEG_BUILD}" /tmp/ff.tar.xz
      if curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" -o /tmp/ff.tar.xz && tar xJf /tmp/ff.tar.xz; then break; fi
      [ "$i" = 6 ] && { echo "ffmpeg download failed"; exit 1; }
      echo "ffmpeg download failed, retry $i"; sleep 30
    done
    rm -f /tmp/ff.tar.xz; mv "${FFMPEG_BUILD}" "$FF_DIR"
  fi
  echo "$FF_DIR/bin" >> "$GITHUB_PATH"
}

case "$1" in
  tools)
    pip install -q "playwright==${PLAYWRIGHT_VERSION}"
    python -m playwright install chromium-headless-shell >/dev/null
    python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); p.chromium.launch().close(); p.stop()" \
      || sudo python -m playwright install-deps chromium-headless-shell >/dev/null
    ffmpeg_tool ;;
  ffmpeg)
    ffmpeg_tool ;;
  shard)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; mkdir -p "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/enc"
    # 30 Oct: the scene assembler (tools/scenemaker/assemble) uploads big bundles as small parts bundle.part_000, ...
    # so a slow link retries piece by piece; join them back. Single-file jobs (cloud_render.py) are unchanged.
    B=bundle.tar.gz.enc
    if [ ! -f "$B" ] && ls bundle.part_* >/dev/null 2>&1; then cat bundle.part_* > "$W/b.enc"; B="$W/b.enc"; fi
    enc -d -in "$B" -out "$W/b.tgz"; rm -f "$W/b.enc"
    tar xzf "$W/b.tgz" -C "$W"; rm "$W/b.tgz"
    PAGE=$(python3 -c "import json; print(json.load(open('$W/manifest.json'))['page'])")
    # mask anything derived from the bundle in case a stack trace prints it
    echo "::add-mask::$PAGE"; echo "::add-mask::$(basename "$PAGE")"; echo "::add-mask::$W/$PAGE"
    python3 farm/render_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$RUNNER_TEMP/out"
    tar cf - -C "$RUNNER_TEMP/out" --exclude=frames . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$RUNNER_TEMP/out" ;;
  join)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; mkdir -p "$J" "$RUNNER_TEMP/final"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    cd "$J"
    python3 - <<EOF
import json, glob
m = [json.load(open(f)) for f in sorted(glob.glob("meta_*.json"))]
assert len(m) == int("$SHARDS"), f"got {len(m)} shard metas"
exp = m[0]["total"]; got = sum(x["hi"] - x["lo"] for x in m)
assert got == exp, (got, exp)
open("expected", "w").write(str(exp)); open("fps", "w").write(str(m[0]["fps"]))
open("list.txt", "w").write("".join(f"file 'chunk_{x['shard']:03d}.nut'\n" for x in sorted(m, key=lambda x: x["lo"]) if x["hi"] > x["lo"]))
print("expected frames", exp)
EOF
    FPS=$(cat fps)
    ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -vf "setpts=N/(${FPS}*TB)" -c:v libx264 -pix_fmt yuv420p -crf 16 \
      -preset medium -r "$FPS" -movflags +faststart render.mp4
    got=$(ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of default=nw=1:nk=1 render.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    enc -in render.mp4 -out "$RUNNER_TEMP/final/render.mp4.enc"
    cd /; rm -rf "$J" "$P" ;;
  warm)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    python3 farm/warm_worker.py ;;
  footage_tools)
    # 4 Nov 2026: own-footage PiP composite. pip.py needs numpy, pillow, scipy.
    pip install -q numpy pillow scipy
    ffmpeg_tool ;;
  footage_shard)
    # Owner's own footage only (guarded by the client). Decrypt, composite this shard's frames, encrypt the pieces, delete the plaintext.
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; mkdir -p "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/enc"
    cat bundle.part_* | enc -d -out "$W/b.tar"
    tar xf "$W/b.tar" -C "$W"; rm -f "$W/b.tar"
    if ! python3 farm/footage_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$RUNNER_TEMP/out" > "$RUNNER_TEMP/shard.log" 2>&1; then
      # details only inside an encrypted file (public log): the client can fetch and decrypt it
      tail -c 4000 "$RUNNER_TEMP/shard.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$SHARD").enc"
      rm -rf "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/shard.log"; echo "shard failed"; exit 1
    fi
    echo "sub-ranges done: $(grep -c 'done' "$RUNNER_TEMP/shard.log")"
    tar cf - -C "$RUNNER_TEMP/out" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/shard.log" ;;
  footage_join)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; F="$RUNNER_TEMP/final"; mkdir -p "$J" "$F"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    enc -d -in audio.enc -out "$J/voice.wav"
    cd "$J"
    SHARDS="$SHARDS" python3 -c '
import json, glob, os
m = sorted((json.load(open(f)) for f in glob.glob("meta_*.json")), key=lambda x: x["lo"])
assert len(m) == int(os.environ["SHARDS"]), "shard metas missing"
assert sum(x["hi"] - x["lo"] for x in m) == m[0]["total"]
open("expected", "w").write(str(m[0]["total"])); open("fps", "w").write(str(m[0]["fps"]))
open("list.txt", "w").write("".join("file %s\n" % p for x in m for p in x["pieces"]))
print("pieces", sum(len(x["pieces"]) for x in m))
'
    FPS=$(cat fps)
    ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -c copy silent.mp4
    got=$(ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of default=nw=1:nk=1 silent.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    # final about 9 Mbps and the WhatsApp copy at 1700k (APPROVED Delivery), encoded side by side; voice as AAC
    ffmpeg -y -loglevel error -i silent.mp4 -i voice.wav -map 0:v -map 1:a -c:v libx264 -preset medium -b:v 9M -maxrate 11M -bufsize 18M \
      -pix_fmt yuv420p -r "$FPS" -c:a aac -b:a 192k -shortest -movflags +faststart final.mp4 & P1=$!
    ffmpeg -y -loglevel error -i silent.mp4 -i voice.wav -map 0:v -map 1:a -c:v libx264 -preset medium -b:v 1700k -maxrate 2000k -bufsize 3400k \
      -pix_fmt yuv420p -r "$FPS" -c:a aac -b:a 128k -shortest -movflags +faststart whatsapp.mp4 & P2=$!
    wait $P1; wait $P2
    enc -in final.mp4 -out "$F/final.mp4.enc"; enc -in whatsapp.mp4 -out "$F/whatsapp.mp4.enc"
    cd /; rm -rf "$J" "$P" ;;
  recap_tools)
    # 7 Nov 2026: all-cloud recap = board page (Playwright Chromium) + pip.py (numpy, pillow, scipy) in the same job
    pip install -q "playwright==${PLAYWRIGHT_VERSION}" numpy pillow scipy
    python -m playwright install chromium-headless-shell >/dev/null
    python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); p.chromium.launch().close(); p.stop()" \
      || sudo python -m playwright install-deps chromium-headless-shell >/dev/null
    ffmpeg_tool ;;
  recap_shard)
    # Owner's own footage only (guarded by the client). Board frames + PiP composite of one block; everything that leaves is encrypted.
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; O="$RUNNER_TEMP/out"; mkdir -p "$W" "$O" "$RUNNER_TEMP/enc"
    if [ -f BUNDLE_FROM ] && ! ls bundle.part_* >/dev/null 2>&1; then   # 8 Nov: the bundle is uploaded once, to the main farm's job branch
      read -r BF_URL BF_REF < BUNDLE_FROM
      git fetch -q --depth 1 "$BF_URL" "$BF_REF" && git checkout -q FETCH_HEAD -- $(git ls-tree --name-only FETCH_HEAD | grep '^bundle\.part_')
    fi
    cat bundle.part_* | enc -d | tar xzf - -C "$W"          # board page, pip.py, timeline, face track, manifest (no video)
    if [ -f FOOTAGE_BRANCH ] && ! ls footage.part_* >/dev/null 2>&1; then   # 8 Nov: footage uploaded once, kept on its own branch
      read -r FB_A FB_B < FOOTAGE_BRANCH   # "branch" (this repo) or "https://github.com/<owner>/koya-render.git branch" (the main farm, public)
      if [ -n "${FB_B:-}" ]; then FB_SRC="$FB_A"; FB_REF="$FB_B"; else FB_SRC=origin; FB_REF="$FB_A"; fi
      git fetch -q --depth 1 "$FB_SRC" "$FB_REF" && git checkout -q FETCH_HEAD -- $(git ls-tree --name-only FETCH_HEAD | grep '^footage\.part_')
    fi
    cat footage.part_* | enc -d -out "$W/footage.bin"       # the speaker cut, uploaded on its own while the plan was made
    if ! FOOTAGE="$W/footage.bin" python3 farm/recap_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$O" > "$RUNNER_TEMP/shard.log" 2>&1; then
      tail -c 4000 "$RUNNER_TEMP/shard.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$SHARD").enc"
      rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log"; echo "shard failed"; exit 1
    fi
    echo "sub-ranges done: $(grep -c 'sub-range' "$RUNNER_TEMP/shard.log")"
    python3 -c "import json,glob; m=json.load(open(glob.glob('$O/meta_*.json')[0])); print({k: m.get(k) for k in ('lo','hi','board_frames','board_s','board_clip_s','pip_s','wa_s')})"
    tar cf - -C "$O" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log" ;;
  pq_tools)
    # 9 Nov 2026: Priestley Python compositor (numpy + pillow pinned to the Mac's versions so pixels match)
    pip install -q "numpy==2.4.6" "pillow==12.2.0"
    ffmpeg_tool ;;
  pq_chunk)
    # Owner's own footage only (guarded by the client). One frame range of the Priestley recap; everything that leaves is encrypted.
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; O="$RUNNER_TEMP/out"; mkdir -p "$W" "$O" "$RUNNER_TEMP/enc"
    if [ -f BUNDLE_FROM ] && ! ls bundle.part_* >/dev/null 2>&1; then
      read -r BF_URL BF_REF < BUNDLE_FROM
      git fetch -q --depth 1 "$BF_URL" "$BF_REF" && git checkout -q FETCH_HEAD -- $(git ls-tree --name-only FETCH_HEAD | grep '^bundle\.part_')
    fi
    cat bundle.part_* | enc -d | tar xzf - -C "$W"          # compositor code, fonts, still assets, words (no video)
    if [ -f FOOTAGE_BRANCH ] && ! ls footage.part_* >/dev/null 2>&1; then
      read -r FB_A FB_B < FOOTAGE_BRANCH
      if [ -n "${FB_B:-}" ]; then FB_SRC="$FB_A"; FB_REF="$FB_B"; else FB_SRC=origin; FB_REF="$FB_A"; fi
      git fetch -q --depth 1 "$FB_SRC" "$FB_REF" && git checkout -q FETCH_HEAD -- $(git ls-tree --name-only FETCH_HEAD | grep '^footage\.part_')
    fi
    cat footage.part_* | enc -d -out "$W/footage.mp4"
    if ! (cd "$W/motion/build_recap/priestley_style" && KOYA_SPK="$W/footage.mp4" KOYA_ASSETS="$W/assets_dl" \
          python3 recap_render.py chunk "$FIRST" "$COUNT" "$O") > "$RUNNER_TEMP/chunk.log" 2>&1; then
      tail -c 4000 "$RUNNER_TEMP/chunk.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$CHUNK").enc"
      rm -rf "$W" "$O" "$RUNNER_TEMP/chunk.log"; echo "chunk failed"; exit 1
    fi
    echo "frames: $(grep -c '^frame ' "$RUNNER_TEMP/chunk.log" || true) progress lines; $(tail -n 1 "$RUNNER_TEMP/chunk.log" | sed 's/[^0-9. a-z()/-]//g')"
    tar cf - -C "$O" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$CHUNK").tar.enc"
    rm -rf "$W" "$O" "$RUNNER_TEMP/chunk.log" ;;
  recap_join)
    # stream copy only: the final is pip.py's own encode (as the approved v2 was), the voice+music mix is the Mac's AAC
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; F="$RUNNER_TEMP/final"; mkdir -p "$J" "$F"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    [ "${PART:-0}" = "1" ] || enc -d -in audio.enc -out "$J/audio.m4a"
    cd "$J"
    SHARDS="$SHARDS" FIRST="${FIRST:-0}" COUNT="${COUNT:-}" PART="${PART:-0}" python3 -c '
import json, glob, os
m = sorted((json.load(open(f)) for f in glob.glob("meta_*.json")), key=lambda x: x["lo"])
part = os.environ.get("PART") == "1"; want_n = int(os.environ["COUNT"] or 0) if part else int(os.environ["SHARDS"])
assert len(m) == want_n, "shard metas missing: %d of %d" % (len(m), want_n)
m = [x for x in m if x["hi"] > x["lo"]]
assert all(a["hi"] == b["lo"] for a, b in zip(m, m[1:])), "blocks are not contiguous"
assert part or (m[0]["lo"] == 0 and m[-1]["hi"] == m[0]["total"]), "blocks do not tile the video"
# 8 Nov: check EVERY piece before the concat (an empty piece only showed up as "Stream map matches no streams")
import subprocess, sys
def frames_in(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets",
                        "-of", "default=nw=1:nk=1", p], capture_output=True, text=True)
    try: return int(r.stdout.strip().splitlines()[0])
    except Exception: return -1
bad = []
for x in m:
    sh, subs, got = x["shard"], x.get("subs") or [], 0
    if not x["pieces"]: bad.append("shard %d: no pieces for frames %d-%d" % (sh, x["lo"], x["hi"]))
    for k, p in enumerate(x["pieces"]):
        n = frames_in(p); got += max(n, 0)
        want = subs[k][1] - subs[k][0] if k < len(subs) else None
        if n <= 0 or (want is not None and n != want): bad.append("shard %d %s: %d frames, expected %s" % (sh, p, n, want if want is not None else "more than 0"))
    if got != x["hi"] - x["lo"]: bad.append("shard %d: pieces hold %d frames, block is %d" % (sh, got, x["hi"] - x["lo"]))
if bad: print("BAD PIECES:\n  " + "\n  ".join(bad)); sys.exit(1)
print("pieces checked:", sum(len(x["pieces"]) for x in m))
open("expected", "w").write(str(m[-1]["hi"] - m[0]["lo"] if part else m[0]["total"]))
open("list.txt", "w").write("".join("file %s\n" % p for x in m for p in x["pieces"]))
wa = [x["whatsapp"] for x in m]
open("wa.txt", "w").write("".join("file %s\n" % p for p in wa) if all(wa) else "")
print("pieces", sum(len(x["pieces"]) for x in m))
'
    # no -shortest: with stream copy it can drop the LAST video frame when the AAC ends a hair early (8 Nov test: 719 of 720)
    if [ "${PART:-0}" = "1" ]; then   # video-only part of this farm's block; the Mac stitches the parts and adds the mix
      ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -map 0:v -c copy -movflags +faststart final.mp4
    else
      ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -i audio.m4a -map 0:v -map 1:a -c copy -movflags +faststart final.mp4
    fi
    got=$(ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of default=nw=1:nk=1 final.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    enc -in final.mp4 -out "$F/final.mp4.enc"
    if [ "${PART:-0}" != "1" ] && [ -s wa.txt ]; then
      ffmpeg -y -loglevel error -f concat -safe 0 -i wa.txt -i audio.m4a -map 0:v -map 1:a -c:v copy -c:a aac -b:a 96k -movflags +faststart whatsapp.mp4
      enc -in whatsapp.mp4 -out "$F/whatsapp.mp4.enc"
    fi
    cd /; rm -rf "$J" "$P" ;;
  *) echo "unknown step"; exit 1 ;;
esac
