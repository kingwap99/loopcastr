#!/bin/bash
# Single-process concat playout: MediaMTX only ever sees one continuous publisher.
#
# Why not relay handover: with handover every segment ends in EOF, MediaMTX immediately drops the
# publisher, and the receiver is offline while the next one warms up (measured: a 2.0-3.1s gap per segment).
# concat feeds the whole list to one ffmpeg with no handover, so the gap cannot exist.
# Only when the source itself must change (relaying a YouTube live stream) does relay.py takeover come back.
set -u
export PATH=/opt/homebrew/bin:$PATH

HERE="$(cd "$(dirname "$0")" && pwd)"
PLAYLIST="${PLAYLIST:-$HERE/playlist-local.json}"
LIST="${LIST:-$HERE/concat.txt}"
DEST="${DEST:-rtmp://127.0.0.1:1935/live/main}"
LOG="${LOG:-$HERE/logs/playout.log}"
API="${API:-http://127.0.0.1:9997}"
PATH_NAME="${PATH_NAME:-live/main}"
READ_EVERY="${READ_EVERY:-5}"      # how often to check that data is flowing
READ_STALL="${READ_STALL:-20}"     # kill and reconnect after this many seconds with no new data
NORMALIZE="${NORMALIZE:-0}"   # 1 = re-encode to uniform parameters (only needed when sources differ)
VENC="${VENC:--c:v libx264 -preset veryfast -profile:v high -g 60 -b:v 2500k -maxrate 2500k -bufsize 5000k}"
AENC="${AENC:--c:a aac -b:a 128k -ar 48000 -ac 2}"

mkdir -p "$(dirname "$LOG")"

if ! python3 "$HERE/make_concat_list.py" "$PLAYLIST" -o "$LIST" --base-dir "$HERE"; then
  echo "[playout] $(date '+%F %T') could not build the concat list, stopping" >>"$LOG"
  exit 78
fi

SRC=(-re -f concat -safe 0 -stream_loop -1 -i "$LIST")
if [ "$NORMALIZE" = "1" ]; then
  ENC=($VENC $AENC)
else
  ENC=(-c copy)
fi

# Why it watches itself (added 2026-09-19):
#   Measured: after the MediaMTX readTimeout (30s) dropped the playout connection, ffmpeg did not
#   exit but sat there for minutes without moving (bytesReceived stopped growing, readers went
#   to 0) and the channel went black - about 3.5 minutes on 2026-09-19, until the health service
#   failed three times in a row and kickstarted it. yt_publish.sh has the same watchdog and the
#   playout side was missing it, so it is added here: watch bytesReceived on this path and after
#   READ_STALL seconds without growth, kill and reconnect (normally it grows by tens of MB every 5 seconds).
bytes_now() {
  curl -sf --max-time 3 "$API/v3/paths/get/$PATH_NAME" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    print(int(json.load(sys.stdin).get("bytesReceived") or 0))
except Exception:
    print(-1)'
}

n=0
while :; do
  n=$((n + 1))
  echo "[playout] $(date '+%F %T') start #$n" >>"$LOG"
  ffmpeg -hide_banner -nostdin -loglevel warning -nostats \
    "${SRC[@]}" "${ENC[@]}" \
    -f flv -flvflags no_duration_filesize "$DEST" >>"$LOG" 2>&1 &
  FF=$!
  last=""
  stall=0
  while kill -0 "$FF" 2>/dev/null; do
    sleep "$READ_EVERY"
    kill -0 "$FF" 2>/dev/null || break
    b=$(bytes_now)
    if [ "$b" != "-1" ] && [ -n "$b" ]; then
      if [ "$b" = "$last" ]; then
        stall=$((stall + READ_EVERY))
      else
        stall=0
      fi
      last="$b"
      if [ "$stall" -ge "$READ_STALL" ]; then
        echo "[playout] $(date '+%F %T') no new data for $stall s, killing ffmpeg to reconnect" >>"$LOG"
        kill -9 "$FF" 2>/dev/null
        break
      fi
    fi
  done
  wait "$FF" 2>/dev/null
  rc=$?
  echo "[playout] $(date '+%F %T') ffmpeg exited rc=${rc}, restarting in 3 s" >>"$LOG"
  sleep 3
done
