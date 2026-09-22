#!/bin/bash
# Switch which list the playout broadcasts.
#
#   ./switch_edition.sh live       production (playlist-local.json / concat.txt)
#   ./switch_edition.sh news       news mode (playlist-news-local.json / concat-news.txt)
#   ./switch_edition.sh promotion  promotion mode (playlist-promotion-local.json / concat-promotion.txt)
#   ./switch_edition.sh test       test mode (playlist-test-local.json / concat-test.txt)
#   ./switch_edition.sh            show which list is active
#
# Mode definitions (source, length cap, shorts pool, rescan interval) live in modes.json and are built by mode_build.py.
#
# Why this exists: the test edition is temporary (3 minutes plus a sequence watermark) and must be
# switched back after testing, or the channel keeps playing test clips. Switching rewrites the playout
# plist, restarts the service and reattaches loopwatch (a different round length means the loop point is recomputed).
#
# Needs sudo (it writes /Library/LaunchDaemons and restarts system-domain services).
#
# Note: the locale of an ssh session is not UTF-8, so when a variable is followed directly by CJK
# text (a full-width bracket, say) it must be written as ${var}, or bash takes the first byte of that text as part of the name.
set -u
export PATH=/opt/homebrew/bin:$PATH

# In a non-interactive session the sudo cache does not apply (the cache is tied to a tty), so
# privileged actions must carry the password themselves. Set SUDO_PASS, for example:
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

# The label prefix is not hard-coded: installs from before the rename (loopcastr was ytpl) are com.ytpl.*,
# so it is taken from the plist filename actually present in this directory.
SVC_PREFIX=com.loopcastr.
for f in "$HERE"/com.*.playout.plist; do
  [ -f "$f" ] || continue
  SVC_PREFIX="$(basename "$f")"
  SVC_PREFIX="${SVC_PREFIX%.playout.plist}."
  break
done
PLIST="$HERE/${SVC_PREFIX}playout.plist"

# The service can be installed in two places: a system-domain LaunchDaemon (starts at boot, needs root)
# or the user gui-domain LaunchAgent (install.sh --agents, no root needed).
# What must be changed is the copy that is actually loaded; changing the wrong one reports a
# successful switch that never happened (measured: the plist was edited but the loaded copy was not).
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

# Rebuild the concat list first, so a changed list is not left stale
python3 "$HERE/make_concat_list.py" "$HERE/$WANT_PL" -o "$HERE/$WANT_LIST" --base-dir "$HERE" || exit 4

$PB -c "Set :EnvironmentVariables:PLAYLIST $HERE/$WANT_PL" "$PLIST"
$PB -c "Set :EnvironmentVariables:LIST $HERE/$WANT_LIST" "$PLIST"

case "$SCOPE" in
  gui)
    cp "$PLIST" "$INSTALLED"                 # this is the copy that is loaded, so it must be overwritten
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
    # The service is not loaded yet (a fresh install, or content that was just built): load the plist that was just written.
    # Without this, the first build-and-switch only prints a warning and the playout never starts.
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
      # The plist is still in the install directory and not registered with launchd (install.sh skipped
      # installing it because there was no content yet, or only mediamtx/webui were installed). Follow the console start: install it as a LaunchAgent.
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

# Reattach the loop observer (a different round length means the loop point is recomputed)
pkill -f 'loopwatc[h].py' 2>/dev/null
sleep 1
nohup python3 "$HERE/loopwatch.py" --playlist "$HERE/$WANT_PL" --lead 45 --tail 90 \
  > "$HERE/logs/loopwatch.out" 2>&1 &
sleep 5
head -2 "$HERE/logs/loopwatch.out"
