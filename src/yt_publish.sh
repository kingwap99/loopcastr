#!/bin/bash
# Push MediaMTX live/main into the YouTube live stream.
#
# One long-lived publisher: however the upstream hands over, reopens or splices, YouTube only
# sees this one continuous connection. That is the key to removing the 24/7 gap problem on the YouTube side.
#
# Why it watches itself (added 2026-09-17):
#   Measured: ffmpeg stayed alive while its connection to MediaMTX was already gone (readers
#   went to 0) and it never exited. A stall like that does not trigger the launchd
#   KeepAlive, so it stays black - that is how it went dark for 28 minutes that night.
#   So ffmpeg runs in the background while readers are watched; after WATCH_MISSES misses in a
#   row, ffmpeg is killed and reconnected.
set -u
export PATH=/opt/homebrew/bin:$PATH

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${SRC:-rtmp://127.0.0.1:1935/live/main}"
API="${API:-http://127.0.0.1:9997}"
PATH_NAME="${PATH_NAME:-live/main}"
KEYFILE="${KEYFILE:-$HERE/stream.key}"
PUSH_URL="${PUSH_URL:-rtmp://a.rtmp.youtube.com/live2}"
LOG="${LOG:-$HERE/logs/publish.log}"
WATCH_EVERY="${WATCH_EVERY:-15}"
WATCH_MISSES="${WATCH_MISSES:-2}"

mkdir -p "$(dirname "$LOG")"

if [ ! -s "$KEYFILE" ]; then
  echo "[publish] stream key file not found: $KEYFILE" >&2
  exit 78
fi
KEY="$(tr -d ' \t\r\n' < "$KEYFILE")"
DEST="$PUSH_URL/$KEY"

readers_now() {
  curl -sf --max-time 3 "$API/v3/paths/get/$PATH_NAME" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin).get("readers") or []))
except Exception:
    print(-1)' 2>/dev/null || echo -1
}

wait_ready() {
  # While the upstream is not publishing yet, keep waiting instead of hitting the YouTube ingest every 3 seconds.
  while :; do
    if curl -sf --max-time 2 "$API/v3/paths/get/$PATH_NAME" | grep -q '"ready":true'; then
      return 0
    fi
    sleep 2
  done
}

reap_orphans() {
  # An orphaned ffmpeg from the previous run may still hold the YouTube publisher slot,
  # which stops the new one from connecting (a YouTube ingest key accepts only one).
  pkill -f "ffmpeg.*live2" 2>/dev/null
  sleep 1
}

n=0
while :; do
  wait_ready
  n=$((n + 1))
  reap_orphans
  echo "[publish] $(date '+%F %T') connect #$n: $SRC -> YouTube ingest" >> "$LOG"
  ffmpeg -hide_banner -nostdin -loglevel warning -nostats \
    -re -i "$SRC" -c copy -f flv -flvflags no_duration_filesize "$DEST" \
    >>"$LOG" 2>&1 &
  FF=$!
  misses=0
  while kill -0 "$FF" 2>/dev/null; do
    sleep "$WATCH_EVERY"
    kill -0 "$FF" 2>/dev/null || break
    r="$(readers_now)"
    if [ "$r" = "0" ]; then
      misses=$((misses + 1))
      if [ "$misses" -ge "$WATCH_MISSES" ]; then
        echo "[publish] $(date '+%F %T') our own reader was missing $misses times in a row (about $((WATCH_EVERY * WATCH_MISSES)) s), treating it as stuck and reconnecting" >> "$LOG"
        kill -TERM "$FF" 2>/dev/null
        sleep 3
        kill -KILL "$FF" 2>/dev/null
        break
      fi
    else
      misses=0
    fi
  done
  wait "$FF" 2>/dev/null
  rc=$?
  echo "[publish] $(date '+%F %T') ffmpeg exited rc=${rc}, reconnecting in 3 s" >> "$LOG"
  sleep 3
done
