#!/bin/bash
# 切換播出端要播哪一版清單。
#
#   ./switch_edition.sh live       正式版（playlist-local.json / concat.txt）
#   ./switch_edition.sh news       新聞模式（playlist-news-local.json / concat-news.txt）
#   ./switch_edition.sh promotion  推廣模式（playlist-promotion-local.json / concat-promotion.txt）
#   ./switch_edition.sh test       測試模式（playlist-test-local.json / concat-test.txt）
#   ./switch_edition.sh            只顯示目前是哪一版
#
# 模式定義（來源、長度上限、shorts 池、掃描頻率）在 modes.json，由 mode_build.py 建置。
#
# 為什麼要有這支：測試版是暫時的（3 分鐘＋流水號浮水印），測完一定要切回來，
# 否則頻道會一直播測試片段。切換會改寫播出端 plist、重啟服務，並重新掛上
# 循環觀測 loopwatch（因為單輪長度變了，循環點也要重算）。
#
# 需要 sudo（改 /Library/LaunchDaemons 與重啟 system domain 服務）。
#
# 注意：ssh 進來的 locale 不是 UTF-8，所以變數後面緊接中文（例如全形括號）
# 時必須寫成 ${var}，否則 bash 會把中文字的首位元組當成變數名稱的一部分。
set -u
export PATH=/opt/homebrew/bin:$PATH

# 非互動 session 裡 sudo 的快取不生效（快取綁 tty），所以特權動作要能自己
# 帶密碼。給 SUDO_PASS 環境變數即可，例如：
#   SUDO_PASS=xxx ./switch_edition.sh live
sudo_do() {
  if [ -n "${SUDO_PASS:-}" ]; then
    printf '%s\n' "$SUDO_PASS" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

HERE="$(cd "$(dirname "$0")" && pwd)"
PB=/usr/libexec/PlistBuddy

# label 前綴不寫死：改名前（loopcastr 之前叫 ytpl）的安裝是 com.ytpl.*，
# 所以看這個目錄裡實際的 plist 檔名決定。
SVC_PREFIX=com.loopcastr.
for f in "$HERE"/com.*.playout.plist; do
  [ -f "$f" ] || continue
  SVC_PREFIX="$(basename "$f")"
  SVC_PREFIX="${SVC_PREFIX%.playout.plist}."
  break
done
PLIST="$HERE/${SVC_PREFIX}playout.plist"

# 服務可能裝在兩個地方：system domain 的 LaunchDaemon（開機就起，需要 root），
# 或使用者自己的 gui domain LaunchAgent（install.sh --agents，不需要 root）。
# 切換要改的是「實際被載入的那一份」—— 改錯地方會變成「回報切換成功但根本沒換」
# （實測踩過：plist 改了、載入的那份沒改，播出端照樣播舊的）。
INSTALLED="/Library/LaunchDaemons/${SVC_PREFIX}playout.plist"
LABEL="${SVC_PREFIX}playout"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  SCOPE=gui
  INSTALLED="$HOME/Library/LaunchAgents/$LABEL.plist"
elif launchctl print "system/$LABEL" >/dev/null 2>&1; then
  SCOPE=system
else
  SCOPE=none
fi

case "${1:-show}" in
  live)      WANT_PL="playlist-local.json";           WANT_LIST="concat.txt" ;;
  news)      WANT_PL="playlist-news-local.json";      WANT_LIST="concat-news.txt" ;;
  promotion) WANT_PL="playlist-promotion-local.json"; WANT_LIST="concat-promotion.txt" ;;
  test)      WANT_PL="playlist-test-local.json";      WANT_LIST="concat-test.txt" ;;
  show)      WANT_PL="";                              WANT_LIST="" ;;
  *) echo "usage: $0 [live|news|promotion|test|show]"; exit 2 ;;
esac

cur_pl="$($PB -c 'Print :EnvironmentVariables:PLAYLIST' "$PLIST" 2>/dev/null || true)"
cur_list="$($PB -c 'Print :EnvironmentVariables:LIST' "$PLIST" 2>/dev/null || true)"

if [ -z "$WANT_PL" ]; then
  echo "current playlist: ${cur_pl}"
  echo "      LIST=${cur_list}"
  echo "      service: ${SCOPE} domain (${INSTALLED})"
  exit 0
fi

if [ ! -f "$HERE/$WANT_PL" ]; then
  echo "$HERE/${WANT_PL} not found; build it before switching" >&2
  exit 3
fi

# 先把 concat 清單重建一次（內容有變動時才不會用到舊的）
python3 "$HERE/make_concat_list.py" "$HERE/$WANT_PL" -o "$HERE/$WANT_LIST" --base-dir "$HERE" || exit 4

$PB -c "Set :EnvironmentVariables:PLAYLIST $HERE/$WANT_PL" "$PLIST"
$PB -c "Set :EnvironmentVariables:LIST $HERE/$WANT_LIST" "$PLIST"

case "$SCOPE" in
  gui)
    cp "$PLIST" "$INSTALLED"                 # 載入的是這一份，一定要覆蓋它
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$INSTALLED" || exit 5
    ;;
  system)
    sudo_do cp "$PLIST" "$INSTALLED"
    sudo_do chown root:wheel "$INSTALLED"
    sudo_do chmod 644 "$INSTALLED"
    sudo_do launchctl bootout "system/$LABEL" 2>/dev/null
    sudo_do launchctl bootstrap system "$INSTALLED" || exit 5
    ;;
  none)
    # 服務還沒被載入（例如全新安裝、內容才剛建好）：直接把剛寫好的這份 plist 載起來。
    # 沒有這一段的話，第一次「建置並切換」只會印警告、播出端永遠不會動。
    echo "service ${LABEL} is not loaded yet; loading it now"
    if [ -f "$HOME/Library/LaunchAgents/${LABEL}.plist" ]; then
      cp "$PLIST" "$HOME/Library/LaunchAgents/${LABEL}.plist"
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/${LABEL}.plist" || exit 5
      SCOPE=gui
      INSTALLED="$HOME/Library/LaunchAgents/${LABEL}.plist"
    elif [ -f "/Library/LaunchDaemons/${LABEL}.plist" ]; then
      sudo_do cp "$PLIST" "/Library/LaunchDaemons/${LABEL}.plist"
      sudo_do chown root:wheel "/Library/LaunchDaemons/${LABEL}.plist"
      sudo_do chmod 644 "/Library/LaunchDaemons/${LABEL}.plist"
      sudo_do launchctl bootstrap system "/Library/LaunchDaemons/${LABEL}.plist" || exit 5
      SCOPE=system
      INSTALLED="/Library/LaunchDaemons/${LABEL}.plist"
    elif [ -f "$PLIST" ]; then
      # plist 還在安裝目錄、還沒裝進 launchd（install.sh 因為「還沒有內容」跳過安裝，
      # 或是只裝了 mediamtx／webui）。照控制台「啟動」的做法：裝成 LaunchAgent。
      echo "installing ${LABEL} as a LaunchAgent"
      mkdir -p "$HOME/Library/LaunchAgents"
      cp "$PLIST" "$HOME/Library/LaunchAgents/${LABEL}.plist"
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/${LABEL}.plist" || exit 5
      SCOPE=gui
      INSTALLED="$HOME/Library/LaunchAgents/${LABEL}.plist"
    else
      echo "WARNING: ${LABEL} is not loaded and no plist is installed;" >&2
      echo "  run ./install.sh --agents (or ./install.sh) first" >&2
      exit 6
    fi
    ;;
esac
sleep 6
echo "switched to ${1}: ${WANT_PL} / ${WANT_LIST} (${SCOPE} domain)"
if [ "$SCOPE" = "gui" ]; then
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 \
    && echo "  $LABEL loaded" || echo "  WARNING: $LABEL did not load" >&2
else
  sudo_do launchctl list | grep -i "${LABEL}" || true
fi

# 重新掛循環觀測（單輪長度變了，循環點要重算）
pkill -f 'loopwatc[h].py' 2>/dev/null
sleep 1
nohup python3 "$HERE/loopwatch.py" --playlist "$HERE/$WANT_PL" --lead 45 --tail 90 \
  > "$HERE/logs/loopwatch.out" 2>&1 &
sleep 5
head -2 "$HERE/logs/loopwatch.out"
