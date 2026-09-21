#!/bin/bash
# 把 MediaMTX 的 live/main 推進 YouTube 直播。
#
# 單一長命 publisher：上游怎麼換手、重開、切墊片，YouTube 端都只看到這一條
# 連續連線。這是把 24/7 縫隙問題從 YouTube 端移除掉的關鍵。
#
# 為什麼要自己看門（2026-09-17 加）：
#   實測遇過 ffmpeg 行程還活著、但與 MediaMTX 的連線已經斷掉（MediaMTX 的
#   readers 變 0）而它不會結束的狀況。這種「卡住」不會觸發 launchd 的
#   KeepAlive，會一路黑下去 —— 當晚就是這樣黑了 28 分鐘。
#   所以 ffmpeg 改用背景執行，同時盯 readers；連續 WATCH_MISSES 次看不到
#   自己就把 ffmpeg 砍掉重連。
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
  # 上游還沒開始推就一直等，不要每 3 秒去敲 YouTube 的 ingest 一次。
  while :; do
    if curl -sf --max-time 2 "$API/v3/paths/get/$PATH_NAME" | grep -q '"ready":true'; then
      return 0
    fi
    sleep 2
  done
}

reap_orphans() {
  # 前一次留下的孤兒 ffmpeg 可能還佔著 YouTube 的 publisher 位置，
  # 讓新的一條連不上（YouTube ingest 一個金鑰只收一條）。
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
