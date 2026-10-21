#!/usr/bin/env bash
# Farm steps. Logs stay neutral: no file names, page paths or content are printed (the repo is public).
# The encryption key arrives as env RENDER_KEY (Actions secret) and is passed to openssl via an env reference, never echoed.
set -euo pipefail
RENDER_KEY="$(printf %s "${RENDER_KEY:-}" | tr -d '\r\n')"; export RENDER_KEY   # the secret may carry a trailing newline
FF_DIR="$HOME/ffmpeg"
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:RENDER_KEY "$@"; }

ffmpeg_tool() {
  if [ ! -x "$FF_DIR/bin/ffmpeg" ]; then
    curl -sL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" | tar xJ
    mv "${FFMPEG_BUILD}" "$FF_DIR"
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
    enc -d -in bundle.tar.gz.enc -out "$W/b.tgz"
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
  *) echo "unknown step"; exit 1 ;;
esac
