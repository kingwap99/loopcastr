#!/bin/bash
# loopcastr install / upgrade
#
# Deploy src/ and launchd/ to the install directory and (optionally) register the launchd services.
# Paths inside the plists come from the __HOME__ / __USER__ placeholders, so no file needs manual editing.
# Safe to run repeatedly; an existing mediamtx.yml and stream.key are never overwritten.

set -eu
# When called from ssh or a script, PATH may lack brew, so add it first (the checks below use command -v).
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

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
Usage
  ./install.sh [options]

Options
  --prefix DIR       install directory (default $HOME/loopcastr)
  --agents           install as LaunchAgents (no root, but they only run with a graphical login)
  --no-services      copy files only, leave launchd alone
  --force-services   register the services even when there is no broadcast content yet
  --dry-run          show what would happen without changing anything
  -h, --help         show this help

Environment
  STREAM_KEY         when set, written to <prefix>/stream.key (mode 600).
                     Deliberately not a command-line flag: flags show up in ps and in the shell history.

After installing
  Services do not start when there is no broadcast content yet (it asks you to build playlist-local.json
  and concat.txt first), so launchd does not keep restarting a process that is bound to fail.
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
    *) echo "unknown argument: $1 (use -h for usage)" >&2; exit 2 ;;
  esac
done

[ -n "$PREFIX" ] || { echo "--prefix must not be empty" >&2; exit 2; }
case "$PREFIX" in /*) ;; *) echo "--prefix must be an absolute path" >&2; exit 2 ;; esac
# Placeholder substitution uses python3 rather than sed, so paths containing & | or backslashes are safe.
[ "$SCOPE" = "daemon" ] || [ "$SCOPE" = "agents" ] || { echo "internal error: scope" >&2; exit 2; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

# ── Dependencies ───────────────────────────────────────────────────
step "Checking dependencies"
missing=""
for t in python3 ffmpeg ffprobe yt-dlp mediamtx curl plutil; do
  command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -n "$missing" ]; then
  say "missing commands: $missing"
  say "  brew install ffmpeg yt-dlp mediamtx"
  exit 3
fi
say "  all required commands present: python3 ffmpeg ffprobe yt-dlp mediamtx curl plutil"

absent=""
for m in PIL qrcode; do
  python3 -c "import $m" 2>/dev/null || absent="$absent $m"
done
if [ -n "$absent" ]; then
  say "  missing Python packages: $absent"
  say "    no PIL    -> the watermark falls back to a bitmap font with digits and symbols only"
  say "    no qrcode -> episode and transition QR codes cannot be built"
  say "    install: python3 -m pip install --user qrcode pillow"
  say "    (if the system Python refuses, add --break-system-packages or use a venv)"
else
  say "  Python packages present: PIL, qrcode"
fi
python3 -c "import cv2" 2>/dev/null \
  || say "  (optional) opencv is missing: build_transitions.py --verify reports every QR decode as a failure, which does not mean the files are broken"

# ── Files ──────────────────────────────────────────────────────────
step "Creating directories"
run mkdir -p "$PREFIX" "$PREFIX/logs" "$PREFIX/media"
say "  $PREFIX"

step "Copying programs"
if [ "$SRC_DIR" = "$PREFIX" ]; then
  say "  source and install directory are the same, skipping"
else
  for f in "$SRC_DIR"/src/*; do
    [ -f "$f" ] || continue          # skip directories such as __pycache__
    case "$(basename "$f")" in
      settings.json|modes.json)
        # These two are the settings meant to be edited by hand; an existing copy is kept, so an upgrade does not eat your tuning
        if [ -f "$PREFIX/$(basename "$f")" ]; then
          say "  keeping the existing settings: $(basename "$f")"
          continue
        fi
        ;;
    esac
    run cp -fp "$f" "$PREFIX/"
  done
  run find "$PREFIX" -maxdepth 1 -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} +
fi

step "Generating service definitions (plist placeholder substitution)"
for f in "$SRC_DIR"/launchd/*.plist; do
  label="$(basename "$f")"
  out="$PREFIX/$label"
  if [ "$DRY" = 1 ]; then
    printf '   [dry-run] would generate %s\n' "$out"
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
    say "  (YT_VIDEO_ID not set: the health YouTube is_live check is skipped)"
  fi
  leftover="$(grep -l '__HOME__\|__USER__\|__YT_VIDEO_ID__' "$PREFIX"/*.plist 2>/dev/null || true)"
  [ -z "$leftover" ] || { say "  WARNING: placeholders left unsubstituted: $leftover"; exit 4; }
fi

step "MediaMTX configuration"
if [ -f "$PREFIX/mediamtx.yml" ]; then
  say "  already exists, not overwritten: $PREFIX/mediamtx.yml"
else
  run cp -f "$SRC_DIR/mediamtx.example.yml" "$PREFIX/mediamtx.yml"
  say "  mediamtx.yml generated from the example (edit it to expose the LAN or add credentials)"
fi

step "Stream key"
if [ -f "$PREFIX/stream.key" ]; then
  say "  already exists, not overwritten: $PREFIX/stream.key"
elif [ -n "${STREAM_KEY:-}" ]; then
  run bash -c 'umask 077; printf %s "$1" > "$2"' _ "$STREAM_KEY" "$PREFIX/stream.key"
  say "  written from the STREAM_KEY environment variable (mode 600)"
else
  say "  WARNING: $PREFIX/stream.key is not set up"
  say "    YouTube Studio -> Go live -> stream key, then:"
  say "      printf %s '<your key>' > $PREFIX/stream.key && chmod 600 $PREFIX/stream.key"
  say "    or export STREAM_KEY='<your key>' before running this script and it writes it for you."
fi

# ── Services ────────────────────────────────────────────────────────
step "Installing services (${SCOPE})"
if [ "$DO_SERVICES" = 0 ]; then
  say "  --no-services: skipped"
elif [ ! -f "$PREFIX/playlist-local.json" ] || [ ! -f "$PREFIX/concat.txt" ]; then
  if [ "$FORCE_SERVICES" = 1 ]; then
    say "  WARNING: no playlist-local.json / concat.txt yet, but --force-services asked for installation"
  else
    say "  WARNING: no broadcast content yet (playlist-local.json / concat.txt), not starting the services"
    say "    otherwise launchd keeps restarting a process that is bound to fail. Build content first:"
    say "      cd $PREFIX"
    say "      python3 build_playlist.py --url '<playlist or channel URL>' -o playlist.json"
    say "      python3 build_local_content.py --playlist playlist.json --target 720"
    say "      python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir $PREFIX"
    say "    then run this script again (or add --force-services to install now)."
    DO_SERVICES=0
  fi
fi

if [ "$DO_SERVICES" = 1 ]; then
  if [ "$SCOPE" = "daemon" ]; then
    say "  sudo is needed to write /Library/LaunchDaemons"
    if [ "$DRY" = 0 ]; then
      sudo -v || { say "  could not get sudo; use --agents or --no-services" >&2; exit 5; }
    fi
  fi
  # The webui console is installed too: it is the everyday entry point, and without it a remote machine is command line only.
  for s in mediamtx playout publish health refresh webui; do
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
    say "  Note: a LaunchAgent only runs after a graphical login. To come back after a reboot without logging in,"
    say "        use a system-domain LaunchDaemon instead (do not pass --agents)."
  fi
fi

step "Done"
say "  install directory: $PREFIX"
say "  service scope: $SCOPE"
say "  status:  tail -3 $PREFIX/logs/health.log"
say "  alerts:  tail -3 $PREFIX/logs/alerts.jsonl"
if [ "$SCOPE" = "daemon" ]; then
  say "  restart the playout: sudo launchctl kickstart -k system/com.loopcastr.playout"
else
  say "  restart the playout: launchctl kickstart -k gui/$UID/com.loopcastr.playout"
fi
