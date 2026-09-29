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
WEBUI_HOST=""         # non-empty exposes the console on that address (it then needs a token)
WEBUI_TOKEN_FILE=""   # defaults to <prefix>/webui-token

usage() {
  cat <<'EOF'
Usage
  ./install.sh [options]

Options
  --prefix DIR       install directory (default $HOME/loopcastr)
  --agents           install as LaunchAgents (no root, but they only run with a graphical login)
  --no-services      copy files only, leave launchd alone
  --force-services   register the services even when there is no broadcast content yet
  --webui-host HOST  expose the console on HOST (e.g. 0.0.0.0) instead of localhost only;
                     webui.py refuses to start without a token, so one is generated if needed
  --webui-token-file PATH   where the console reads its token (default <prefix>/webui-token)
  --dry-run          show what would happen without changing anything
  -h, --help         show this help

Environment
  STREAM_KEY         when set, written to <prefix>/stream.key (mode 600).
                     Deliberately not a command-line flag: flags show up in ps and in the shell history.

After installing
  The console (com.loopcastr.webui) is registered on every install: it needs no broadcast content, and it is
  the thing you build content with. Open http://127.0.0.1:8787/
  The chain around it (mediamtx playout publish health refresh) is registered only once a concat*.txt exists,
  so launchd does not keep restarting a process that is bound to fail. Build the content (the console can do
  it), then run this script again - or pass --force-services to register the chain before any content exists.
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)         PREFIX="${2:-}"; shift 2 ;;
    --agents)         SCOPE="agents"; shift ;;
    --no-services)    DO_SERVICES=0; shift ;;
    --force-services) FORCE_SERVICES=1; shift ;;
    --webui-host)     WEBUI_HOST="${2:-}"; shift 2 ;;
    --webui-token-file) WEBUI_TOKEN_FILE="${2:-}"; shift 2 ;;
    --dry-run)        DRY=1; shift ;;
    -h|--help)        usage ;;
    *) echo "unknown argument: $1 (use -h for usage)" >&2; exit 2 ;;
  esac
done

[ -n "$PREFIX" ] || { echo "--prefix must not be empty" >&2; exit 2; }
case "$PREFIX" in /*) ;; *) echo "--prefix must be an absolute path" >&2; exit 2 ;; esac
[ -n "$WEBUI_TOKEN_FILE" ] || WEBUI_TOKEN_FILE="$PREFIX/webui-token"
# Placeholder substitution uses python3 rather than sed, so paths containing & | or backslashes are safe.
[ "$SCOPE" = "daemon" ] || [ "$SCOPE" = "agents" ] || { echo "internal error: scope" >&2; exit 2; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }
# run with the command's own output dropped. Used where the failure is expected and the status is ignored:
# launchctl bootout says "Boot-out failed: 3: No such process" for a service that was never loaded, which is
# every service on a first install. The bootstrap right after is what loads it, so the line is noise - and an
# install that ends with a line saying "failed" reads like a broken one.
runq() { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@" >/dev/null 2>&1; fi; }

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

# Which python3 do the services actually get? The plists below are written with this exact path, so the
# checks here have to run against the same interpreter, and it has to be one that can import qrcode
# (that is what draws every QR code). Measured failure this prevents: on a machine where the services ran
# Homebrew python, the operator started the console by hand as /usr/bin/python3, and every rebuild it
# triggered wrote a whole channel with no QR codes - the console inherits its own interpreter.
PYTHON=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  if "$cand" -c "import qrcode" 2>/dev/null; then PYTHON="$cand"; break; fi
  [ -n "$PYTHON" ] || PYTHON="$cand"        # nothing has qrcode yet: keep the first that exists
done
[ -n "$PYTHON" ] || { say "no python3 found"; exit 3; }
PYTHON_DIR="$(cd "$(dirname "$PYTHON")" && pwd)"
# One PATH for every service: the interpreter's own directory first (a venv's pip-installed commands live
# there), then the usual brew and system directories, each listed once.
PYTHON_PATH="$PYTHON_DIR"
for d in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do
  case ":$PYTHON_PATH:" in *":$d:"*) ;; *) PYTHON_PATH="$PYTHON_PATH:$d" ;; esac
done
say "  the services will run on: $PYTHON"

absent=""
for m in PIL qrcode; do
  "$PYTHON" -c "import $m" 2>/dev/null || absent="$absent $m"
done
if [ -n "$absent" ]; then
  say "  missing Python packages: $absent"
  say "    no PIL    -> the watermark falls back to a bitmap font with digits and symbols only"
  say "    no qrcode -> episode and transition QR codes cannot be built"
  say "    install: $PYTHON -m pip install --user qrcode pillow"
  say "    (if the system Python refuses, add --break-system-packages or use a venv)"
  say "    install it for THAT interpreter: the console and the services both draw the QR codes with it"
else
  say "  Python packages present for $PYTHON: PIL, qrcode"
fi
"$PYTHON" -c "import cv2" 2>/dev/null \
  || say "  (optional) opencv is missing: build_transitions.py --verify reports every QR decode as a failure, which does not mean the files are broken"

# ── Files ──────────────────────────────────────────────────────────
step "Creating directories"
run mkdir -p "$PREFIX" "$PREFIX/logs" "$PREFIX/media"
say "  $PREFIX"

step "Copying programs"
# The programs always have to end up in the install directory itself: every service runs
# <prefix>/<script>, and the console looks for its files beside itself. That also covers installing
# into the clone (clone it to ~/loopcastr and run install.sh from there), where src/ is flattened up
# one level; src/ itself is left alone.
if [ "$SRC_DIR" = "$PREFIX" ]; then
  say "  installing into the clone itself: flattening src/ into $PREFIX"
fi
for f in "$SRC_DIR"/src/*; do
  [ -f "$f" ] || continue            # skip directories such as __pycache__
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

step "Generating service definitions (plist placeholder substitution)"
# An operator who exposes the console has to add --host and --token-file to its plist by hand. Regenerating
# that plist drops them, the console rebinds to localhost and remote access dies with no error. Say so
# instead of silently breaking it, and offer the two options that put them back.
if [ "$DRY" = 0 ] && [ -z "$WEBUI_HOST" ] && [ -f "$PREFIX/com.loopcastr.webui.plist" ]; then
  old_extra="$("$PYTHON" - "$PREFIX/com.loopcastr.webui.plist" <<'PYCHK'
import plistlib, sys
try:
    args = (plistlib.load(open(sys.argv[1], "rb")).get("ProgramArguments") or [])[2:]
except Exception:
    args = []
print(" ".join(args))
PYCHK
)"
  if [ -n "$old_extra" ]; then
    say "  NOTE: the existing console plist passes extra arguments:"
    say "        $old_extra"
    say "        this run regenerates it without them. Keep them with:"
    say "        ./install.sh --webui-host <host> --webui-token-file $WEBUI_TOKEN_FILE"
  fi
fi
if [ -n "$WEBUI_HOST" ] && [ "$DRY" = 0 ] && [ ! -f "$WEBUI_TOKEN_FILE" ]; then
  run sh -c "umask 077; openssl rand -hex 24 > \"$WEBUI_TOKEN_FILE\""
  say "  generated the console token: $WEBUI_TOKEN_FILE"
fi
for f in "$SRC_DIR"/launchd/*.plist; do
  label="$(basename "$f")"
  out="$PREFIX/$label"
  if [ "$DRY" = 1 ]; then
    printf '   [dry-run] would generate %s\n' "$out"
    continue
  fi
  "$PYTHON" - "$f" "$out" "$PREFIX" "$HOME_DIR" "$USER_NAME" "${YT_VIDEO_ID:-}" "$PYTHON" "$PYTHON_PATH" "$WEBUI_HOST" "$WEBUI_TOKEN_FILE" <<'PYGEN'
import os, sys
src, dst, prefix, home, user, vid, py, pypath, whost, wtoken = sys.argv[1:11]
t = open(src, encoding="utf-8").read()
t = (t.replace("__HOME__/loopcastr", prefix).replace("__HOME__", home)
      .replace("__YT_VIDEO_ID__", vid).replace("__USER__", user)
      .replace("__PYTHON_PATH__", pypath).replace("__PYTHON__", py))
if whost and os.path.basename(src) == "com.loopcastr.webui.plist":
    import re
    m = re.search(r"([ \t]*<string>%s/webui\.py</string>\n)" % re.escape(prefix), t)
    if not m:
        raise SystemExit("cannot find the console program arguments in " + src)
    extra = ["--host", whost, "--token-file", wtoken]
    indent = m.group(1)[:len(m.group(1)) - len(m.group(1).lstrip())]
    t = t.replace(m.group(1), m.group(1) + "".join(indent + "<string>%s</string>\n" % v for v in extra))
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
  leftover="$(grep -l '__HOME__\|__USER__\|__YT_VIDEO_ID__\|__PYTHON__\|__PYTHON_PATH__' "$PREFIX"/*.plist 2>/dev/null || true)"
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
# The console is registered on every install. It has no content dependency - it is what you build content
# with - so gating it behind "content exists" left a first install with no way in: install.sh printed the
# plists, wrote no services, and the operator had no console and no pointer to one. The chain around it is
# what a missing concat list actually breaks, so only that part waits for content.
CONSOLE_SERVICE="webui"
CHAIN_SERVICES="mediamtx playout publish health refresh"

step "Installing services (${SCOPE})"
SERVICES=""
if [ "$DO_SERVICES" = 0 ]; then
  say "  --no-services: skipped"
else
  SERVICES="$CONSOLE_SERVICE"
  if ls "$PREFIX"/concat*.txt >/dev/null 2>&1; then
    SERVICES="$SERVICES $CHAIN_SERVICES"
  elif [ "$FORCE_SERVICES" = 1 ]; then
    say "  WARNING: no concat*.txt yet, but --force-services asked for the whole chain"
    SERVICES="$SERVICES $CHAIN_SERVICES"
  else
    say "  WARNING: no broadcast content yet (no concat*.txt in $PREFIX)"
    say "    registering the console only; the chain (mediamtx playout publish health refresh) would keep"
    say "    restarting with nothing to play. Build content first:"
    say "      cd $PREFIX"
    say "      $PYTHON build_playlist.py --url '<playlist or channel URL>' -o playlist.json"
    say "      $PYTHON build_local_content.py --playlist playlist.json --target 720"
    say "      $PYTHON make_concat_list.py playlist-local.json -o concat.txt --base-dir $PREFIX"
    say "    then run this script again (or add --force-services to register the chain now)."
  fi
fi

if [ -n "$SERVICES" ]; then
  if [ "$SCOPE" = "daemon" ]; then
    say "  sudo is needed to write /Library/LaunchDaemons"
    if [ "$DRY" = 0 ]; then
      sudo -v || { say "  could not get sudo; use --agents or --no-services" >&2; exit 5; }
    fi
  fi
  # The webui console is installed too: it is the everyday entry point, and without it a remote machine is command line only.
  for s in $SERVICES; do
    label="com.loopcastr.$s"
    if [ "$SCOPE" = "daemon" ]; then
      run sudo cp -f "$PREFIX/$label.plist" "/Library/LaunchDaemons/$label.plist"
      run sudo chown root:wheel "/Library/LaunchDaemons/$label.plist"
      run sudo chmod 644 "/Library/LaunchDaemons/$label.plist"
      runq sudo launchctl bootout "system/$label" || true
      run sudo launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
    else
      run mkdir -p "$HOME/Library/LaunchAgents"
      run cp -f "$PREFIX/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
      runq launchctl bootout "gui/$UID/$label" || true
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
if [ "$DRY" = 1 ]; then
  say "  services this run would register: ${SERVICES:-none}"
else
  say "  services registered: ${SERVICES:-none}"
fi
# Print the console address, because that is the one thing the operator needs next and it is not guessable
# when the services did not register. The rest of the summary only applies to services that are actually there.
case " $SERVICES " in
  *" webui "*)
    say "  console:  http://127.0.0.1:8787/"
    if [ -n "$WEBUI_HOST" ]; then
      say "            (bound to $WEBUI_HOST, so it asks for the token in $WEBUI_TOKEN_FILE)"
    else
      # Installs are usually run over ssh, where 127.0.0.1 is the remote machine and not the browser.
      say "            (from another machine: ssh -N -L 8787:127.0.0.1:8787 $USER_NAME@$HOSTNAME)"
    fi
    ;;
  *) say "  console:  not registered; start it with $PYTHON $PREFIX/webui.py" ;;
esac
case " $SERVICES " in
  *" health "*)
    say "  status:  tail -3 $PREFIX/logs/health.log"
    say "  alerts:  tail -3 $PREFIX/logs/alerts.jsonl"
    ;;
esac
case " $SERVICES " in
  *" playout "*)
    if [ "$SCOPE" = "daemon" ]; then
      say "  restart the playout: sudo launchctl kickstart -k system/com.loopcastr.playout"
    else
      say "  restart the playout: launchctl kickstart -k gui/$UID/com.loopcastr.playout"
    fi
    ;;
  *) say "  the playout chain is not registered yet: build the content, then run this script again" ;;
esac
