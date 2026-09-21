#!/bin/bash
# loopcastr 安裝／升級
#
# 把 src/ 與 launchd/ 佈署到安裝目錄，並（可選）把 launchd 服務裝起來。
# plist 內的路徑由 __HOME__／__USER__ 佔位符代入，所以不需要手改任何檔案。
# 可以重複執行；已存在的 mediamtx.yml 與 stream.key 不會被覆蓋。

set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME"
USER_NAME="$(id -un)"
PREFIX="$HOME/loopcastr"
SCOPE="daemon"        # daemon | agents
DO_SERVICES=1
FORCE_SERVICES=0
DRY=0

usage() {
  cat <<'EOF'
用法
  ./install.sh [選項]

選項
  --prefix DIR       安裝目錄（預設 $HOME/loopcastr）
  --agents           裝成 LaunchAgent（不需 root，但需要有圖形登入才會跑）
  --no-services      只放檔案，不動 launchd
  --force-services   即使還沒有播出內容，也把服務裝起來
  --dry-run          只顯示會做什麼，不改任何東西
  -h, --help         顯示這份說明

環境變數
  STREAM_KEY         若設定，會寫入 <prefix>/stream.key（權限 600）。
                     刻意不做成命令列參數：命令列參數會出現在 ps 與 shell history。

安裝後
  服務不會在還沒有播出內容時啟動（會提示先建 playlist-local.json 與 concat.txt），
  避免 launchd 一直重啟一個註定失敗的行程。
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)         PREFIX="${2:-}"; shift 2 ;;
    --agents)         SCOPE="agents"; shift ;;
    --no-services)    DO_SERVICES=0; shift ;;
    --force-services) FORCE_SERVICES=1; shift ;;
    --dry-run)        DRY=1; shift ;;
    -h|--help)        usage ;;
    *) echo "未知參數：$1（用 -h 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$PREFIX" ] || { echo "--prefix 不能是空字串" >&2; exit 2; }
case "$PREFIX" in /*) ;; *) echo "--prefix 請用絕對路徑" >&2; exit 2 ;; esac
# 佔位符代入用 python3 做（不是 sed），所以路徑含 & | 反斜線都不會出問題。
[ "$SCOPE" = "daemon" ] || [ "$SCOPE" = "agents" ] || { echo "內部錯誤：scope" >&2; exit 2; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

# ── 依賴 ────────────────────────────────────────────────────────────
step "檢查依賴"
missing=""
for t in python3 ffmpeg ffprobe yt-dlp mediamtx curl plutil; do
  command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -n "$missing" ]; then
  say "缺少指令：$missing"
  say "  brew install ffmpeg yt-dlp mediamtx"
  exit 3
fi
say "  必要指令齊全：python3 ffmpeg ffprobe yt-dlp mediamtx curl plutil"

absent=""
for m in PIL qrcode; do
  python3 -c "import $m" 2>/dev/null || absent="$absent $m"
done
if [ -n "$absent" ]; then
  say "  缺少 Python 套件：$absent"
  say "    沒有 PIL    → 中文浮水印會退回只有數字與符號的點陣字型"
  say "    沒有 qrcode → 集數與過場的 QR Code 做不出來"
  say "    安裝：python3 -m pip install --user qrcode pillow"
  say "    （若系統 Python 擋安裝，改加 --break-system-packages，或改用 venv）"
else
  say "  Python 套件齊全：PIL、qrcode"
fi
python3 -c "import cv2" 2>/dev/null \
  || say "  （選用）未裝 opencv：build_transitions.py --verify 的 QR 解碼會全部回報失敗，不是檔案真的壞"

# ── 檔案 ────────────────────────────────────────────────────────────
step "建立目錄"
run mkdir -p "$PREFIX" "$PREFIX/logs" "$PREFIX/media"
say "  $PREFIX"

step "複製程式"
if [ "$SRC_DIR" = "$PREFIX" ]; then
  say "  來源與安裝目錄相同，跳過"
else
  for f in "$SRC_DIR"/src/*; do
    [ -f "$f" ] || continue          # 跳過 __pycache__ 之類的目錄
    case "$(basename "$f")" in
      settings.json|modes.json)
        # 這兩個是「給人改的」設定；已存在就不覆蓋，免得升級時吃掉你的調校
        if [ -f "$PREFIX/$(basename "$f")" ]; then
          say "  保留既有設定：$(basename "$f")"
          continue
        fi
        ;;
    esac
    run cp -fp "$f" "$PREFIX/"
  done
  run find "$PREFIX" -maxdepth 1 -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} +
fi

step "產生服務定義（plist 佔位符代入）"
for f in "$SRC_DIR"/launchd/*.plist; do
  label="$(basename "$f")"
  out="$PREFIX/$label"
  if [ "$DRY" = 1 ]; then
    printf '   [dry-run] 產生 %s\n' "$out"
    continue
  fi
  python3 - "$f" "$out" "$PREFIX" "$HOME_DIR" "$USER_NAME" "${YT_VIDEO_ID:-}" <<'PYGEN'
import sys
src, dst, prefix, home, user, vid = sys.argv[1:7]
t = open(src, encoding="utf-8").read()
t = (t.replace("__HOME__/loopcastr", prefix).replace("__HOME__", home)
      .replace("__YT_VIDEO_ID__", vid).replace("__USER__", user))
open(dst, "w", encoding="utf-8").write(t)
PYGEN
  if [ "$SCOPE" = "agents" ]; then
    plutil -remove UserName "$out" >/dev/null 2>&1 || true
  fi
  plutil -lint "$out" >/dev/null
  say "  $out"
done
if [ "$DRY" = 0 ]; then
  if [ -z "${YT_VIDEO_ID:-}" ]; then
    say "  （未設定 YT_VIDEO_ID：health 的 YouTube 端 is_live 檢查會略過）"
  fi
  leftover="$(grep -l '__HOME__\|__USER__\|__YT_VIDEO_ID__' "$PREFIX"/*.plist 2>/dev/null || true)"
  [ -z "$leftover" ] || { say "  ⚠ 仍有未代入的佔位符：$leftover"; exit 4; }
fi

step "MediaMTX 設定"
if [ -f "$PREFIX/mediamtx.yml" ]; then
  say "  已存在，不覆蓋：$PREFIX/mediamtx.yml"
else
  run cp -f "$SRC_DIR/mediamtx.example.yml" "$PREFIX/mediamtx.yml"
  say "  已由範例產生 mediamtx.yml（要開放到區網或加認證請自行編輯）"
fi

step "直播金鑰"
if [ -f "$PREFIX/stream.key" ]; then
  say "  已存在，不覆蓋：$PREFIX/stream.key"
elif [ -n "${STREAM_KEY:-}" ]; then
  run bash -c 'umask 077; printf %s "$1" > "$2"' _ "$STREAM_KEY" "$PREFIX/stream.key"
  say "  已由 STREAM_KEY 環境變數寫入（權限 600）"
else
  say "  ⚠ 尚未設定 $PREFIX/stream.key"
  say "    YouTube Studio → 直播 → 串流金鑰，然後："
  say "      printf %s '<你的金鑰>' > $PREFIX/stream.key && chmod 600 $PREFIX/stream.key"
  say "    或在跑這支腳本前 export STREAM_KEY='<你的金鑰>'，由它代寫。"
fi

# ── 服務 ────────────────────────────────────────────────────────────
step "安裝服務（${SCOPE}）"
if [ "$DO_SERVICES" = 0 ]; then
  say "  --no-services：略過"
elif [ ! -f "$PREFIX/playlist-local.json" ] || [ ! -f "$PREFIX/concat.txt" ]; then
  if [ "$FORCE_SERVICES" = 1 ]; then
    say "  ⚠ 尚無 playlist-local.json／concat.txt，但 --force-services 指定要裝"
  else
    say "  ⚠ 尚無播出內容（playlist-local.json／concat.txt），先不啟動服務"
    say "    否則 launchd 會一直重啟一個註定失敗的行程。先建內容："
    say "      cd $PREFIX"
    say "      python3 build_playlist.py --url '<播放清單或頻道網址>' -o playlist.json"
    say "      python3 build_local_content.py --playlist playlist.json --target 720"
    say "      python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir $PREFIX"
    say "    然後再跑一次這支腳本（或加 --force-services 現在就裝）。"
    DO_SERVICES=0
  fi
fi

if [ "$DO_SERVICES" = 1 ]; then
  if [ "$SCOPE" = "daemon" ]; then
    say "  需要 sudo 寫入 /Library/LaunchDaemons"
    if [ "$DRY" = 0 ]; then
      sudo -v || { say "  取得 sudo 權限失敗；改用 --agents 或 --no-services" >&2; exit 5; }
    fi
  fi
  for s in mediamtx playout publish health refresh; do
    label="com.loopcastr.$s"
    if [ "$SCOPE" = "daemon" ]; then
      run sudo cp -f "$PREFIX/$label.plist" "/Library/LaunchDaemons/$label.plist"
      run sudo chown root:wheel "/Library/LaunchDaemons/$label.plist"
      run sudo chmod 644 "/Library/LaunchDaemons/$label.plist"
      run sudo launchctl bootout "system/$label" || true
      run sudo launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
    else
      run mkdir -p "$HOME/Library/LaunchAgents"
      run cp -f "$PREFIX/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
      run launchctl bootout "gui/$UID/$label" || true
      run launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/$label.plist"
    fi
    say "  $label"
  done
  if [ "$SCOPE" = "agents" ]; then
    say "  注意：LaunchAgent 只在圖形登入後執行。要做到「重開機不用登入就恢復」，"
    say "        需要改用 system domain 的 LaunchDaemon（不加 --agents）。"
  fi
fi

step "完成"
say "  安裝目錄：$PREFIX"
say "  服務範圍：$SCOPE"
say "  看狀態：  tail -3 $PREFIX/logs/health.log"
say "  看告警：  tail -3 $PREFIX/logs/alerts.jsonl"
if [ "$SCOPE" = "daemon" ]; then
  say "  重啟播出端：sudo launchctl kickstart -k system/com.loopcastr.playout"
else
  say "  重啟播出端：launchctl kickstart -k gui/$UID/com.loopcastr.playout"
fi
