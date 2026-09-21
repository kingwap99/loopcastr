#!/bin/bash
# 單一行程 concat 播出：MediaMTX 只會看到一條連續 publisher。
#
# 為什麼不用 relay 接力：接力＝每一段結束就 EOF，MediaMTX 立刻踢掉 publisher，
# 下一個 publisher 暖機期間接收端是離線的（實測每段 2.0~3.1s 的縫）。
# concat 把整份清單餵給同一個 ffmpeg，中間不換手，縫自然不存在。
# 需要換「來源」的場合（YouTube 直播聯播）才回到 relay.py 的 takeover。
set -u
export PATH=/opt/homebrew/bin:$PATH

HERE="$(cd "$(dirname "$0")" && pwd)"
PLAYLIST="${PLAYLIST:-$HERE/playlist-local.json}"
LIST="${LIST:-$HERE/concat.txt}"
DEST="${DEST:-rtmp://127.0.0.1:1935/live/main}"
LOG="${LOG:-$HERE/logs/playout.log}"
API="${API:-http://127.0.0.1:9997}"
PATH_NAME="${PATH_NAME:-live/main}"
READ_EVERY="${READ_EVERY:-5}"      # 每幾秒檢查一次有沒有在送資料
READ_STALL="${READ_STALL:-20}"     # 連續幾秒沒有新資料就砍掉重連
NORMALIZE="${NORMALIZE:-0}"   # 1 = 重編碼成統一參數（來源參數不一致時才需要）
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

# 為什麼要自己看門（2026-09-19 加）：
#   實測遇過 MediaMTX 的 readTimeout（30 秒）把播出端的連線踢掉之後，ffmpeg
#   不是馬上結束、而是卡在那裡好幾分鐘不動（bytesReceived 停止成長、readers
#   變 0），整個頻道就黑了 —— 2026-09-19 那次黑了約 3.5 分鐘，最後是 health
#   服務連續三次失敗才把它 kickstart 掉。yt_publish.sh 有同樣的看門，播出端
#   這邊漏了，所以補上：改盯這條 path 的 bytesReceived，連續 READ_STALL 秒
#   沒有成長就砍掉重連（正常情況每 5 秒會有十幾 MB 的成長，不會誤判）。
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
