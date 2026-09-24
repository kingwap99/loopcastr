#!/usr/bin/env python3
"""Periodically rescan the source lists; when new videos or shorts appear, rebuild and hand
the playout over at a continuous position.

Spec it implements (news / promotion modes):
  Rescan the playlist when moving to the next video; if there are new videos, the video
  after that starts from the top of the list. The shorts list syncs at a higher rate.

Why the boundary wait: the playout is one concat process with --stream_loop -1 and it reads
its list only at startup. On an update, wait for the current segment to finish, rotate the
new list to the next segment and only then restart, so the channel does not jump back to the
first segment.

Restarting a system-domain service needs root, so running this as root is recommended (like
com.loopcastr.health) rather than putting a password in the environment; as a non-root user
it falls back to SUDO_PASS.

Usage
  sudo python3 refreshwatch.py --mode promotion      # run in the foreground
  python3 refreshwatch.py --mode promotion --once     # check once
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import mode_build as MB
import playout_ctl as pctl

HERE = os.path.dirname(os.path.abspath(__file__))
YTDLP = "/opt/homebrew/bin/yt-dlp"
# The service label prefix is not hard-coded: installs from before the rename (loopcastr was
# ytpl) are com.ytpl.*, so it is read from the plist actually present here. Both plist
# locations are searched too.
def service_prefix():
    try:
        for name in sorted(os.listdir(HERE)):
            if name.startswith("com.") and name.endswith(".playout.plist"):
                return name[: -len("playout.plist")]
    except OSError:
        pass
    return "com.loopcastr."


SVC = service_prefix()


def playout_plist():
    for base in ("/Library/LaunchDaemons", os.path.expanduser("~/Library/LaunchAgents")):
        p = os.path.join(base, SVC + "playout.plist")
        if os.path.exists(p):
            return p
    return os.path.join("/Library/LaunchDaemons", SVC + "playout.plist")


PLAYOUT_PLIST = playout_plist()
START_RE = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})"
    r" (?:start #([0-9]+)|第 ([0-9]+) 次啟動)")


def log(msg):
    line = "[refresh %s] %s" % (time.strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(HERE, "logs", "refreshwatch.log"), "a",
                  encoding="utf-8") as fh:
            fh.write(line + chr(10))
    except OSError:
        pass


def flat_ids(url, limit):
    """Flat listing of IDs (no per-video metadata, takes seconds), longest answer of a few attempts.

    The flat listing intermittently comes back short, the same behaviour build_playlist.py already
    guards for the video list. It used to return the first non-empty answer, and a short answer is
    harmful here in both directions: stored as the baseline it makes the next healthy listing look like
    new content, and on the video side it hides genuinely new uploads. Measured on a 24/7 news mode:
    the stored baseline held 12 videos and 12 shorts while the channel had 166 and 91, and every check
    after that reported 91 new shorts and rebuilt the channel.
    """
    cmd = [YTDLP, "--no-warnings", "--socket-timeout", "25", "--flat-playlist"]
    if limit:
        cmd += ["-I", "1:%d" % limit]
    cmd += ["--print", "%(id)s", url]
    best = []
    for i in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if len(ids) > len(best):
            best = ids
        elif len(best) > len(ids):
            log("listing attempt %d returned %d ids; keeping %d" % (i + 1, len(ids), len(best)))
        if not best:
            time.sleep(15 + i * 15)
    return best


def notify(text):
    p = os.path.join(HERE, "telegram.json")
    if not os.path.exists(p):
        return
    try:
        cfg = json.load(open(p, encoding="utf-8"))
        url = "https://api.telegram.org/bot%s/sendMessage" % cfg["token"]
        data = json.dumps({"chat_id": cfg["chat_id"], "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        log("Telegram notification sent")
    except Exception as exc:
        log("Telegram notification failed: %r" % exc)


def next_boundary(local_json):
    """The next segment boundary (the calculation lives in playout_ctl, shared with the
    batched build)."""
    return pctl.next_boundary(local_json)


def active_mode():
    """Which mode the playout currently runs - read straight from the playout plist's PLAYLIST.

    That way, once this runs as a launchd service, switching mode (switch_edition.sh) is
    followed automatically with no service reconfiguration.
    """
    if not os.path.exists(PLAYOUT_PLIST):
        return ""
    try:
        import plistlib
        d = plistlib.load(open(PLAYOUT_PLIST, "rb"))
    except Exception:
        return ""
    pl = (d.get("EnvironmentVariables") or {}).get("PLAYLIST") or ""
    base = os.path.basename(pl)
    if base.startswith("playlist-") and base.endswith("-local.json"):
        mode = base[len("playlist-"):-len("-local.json")]
        return "" if mode == "local" else mode
    return ""


def restart_playout():
    """Restart the playout; return the command used (an empty string means failure)."""
    how = pctl.restart_playout()
    if not how:
        log("could not restart the playout (gui and system domain both failed)")
    return how


def check(mode, cfg, f, args):
    vids = flat_ids(cfg["video_source"], cfg.get("video_limit") or 0)
    shorts = (flat_ids(cfg["shorts_url"], cfg.get("shorts_count") or 0)
              if cfg.get("shorts_url") else [])
    if not vids:
        log("could not list videos, skipping this round")
        return False
    # Compare like for like: the master list holds at most video_limit videos,
    # so the listing has to be cut the same way.  Without this every round looks
    # like "N new videos" whenever the limit is below the channel size, and the
    # whole playlist gets rebuilt and restarted every refresh interval.
    limit = cfg.get("video_limit") or 0
    if limit:
        vids = vids[:limit]
    old_vids = []
    if os.path.exists(f["mother"]):
        old_vids = [s["id"] for s in
                    json.load(open(f["mother"], encoding="utf-8"))["segments"]]
    sp = os.path.join(HERE, "refresh-state-%s.json" % mode)
    state = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    if not state:
        json.dump({"shorts": shorts, "videos": vids, "at": int(time.time())},
                  open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log("first check, recording the baseline (%d videos, %d shorts)"
            % (len(vids), len(shorts)))
        return False
    old_shorts = state.get("shorts") or []
    new_v = [v for v in vids if v not in old_vids]
    new_s = [s for s in shorts if s not in old_shorts]
    if not new_v and not new_s:
        log("no change (%d videos, %d shorts)" % (len(vids), len(shorts)))
        return False
    log("change detected: %d new videos, %d new shorts" % (len(new_v), len(new_s)))
    notify("[%s] new content: %d videos, %d shorts, rebuilding"
           % (mode, len(new_v), len(new_s)))
    rc = subprocess.run([sys.executable, os.path.join(HERE, "mode_build.py"),
                         "--mode", mode, "--switch"],
                        stdin=subprocess.DEVNULL).returncode
    if rc == 4:
        # Another build (the console button, or a manual run) is already writing these files. Not a
        # failure: leave the change unrecorded and look again on the next check instead of alerting.
        log("another build is running; leaving this change for the next check")
        return False
    if rc != 0:
        log("rebuild failed rc=%d, the playout content is unchanged" % rc)
        notify("[%s] rebuild failed rc=%d" % (mode, rc))
        return False
    json.dump({"shorts": shorts, "videos": vids, "at": int(time.time())},
              open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # mode_build --switch owns the continuity-preserving boundary wait and
    # restart.  Do not restart a second time here, or the stream would jump
    # back to the first segment after a successful rebuild.
    log("rebuilt and switched while preserving the current playout position")
    notify("[%s] rebuilt and switched while preserving the current position" % mode)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto",
                    help="which mode to watch; auto (default) follows whatever the playout plist points at")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--min-wait", type=float, default=20.0)
    a = ap.parse_args()
    modes = MB.load_modes()
    log("starting (--mode=%s, root=%s)" % (a.mode, os.geteuid() == 0))
    while True:
        mode = a.mode
        if mode in ("", "auto"):
            mode = active_mode()
        cfg = modes.get(mode) if mode else None
        if not cfg:
            log("cannot tell the current mode (read %r), checking again in 30 s" % mode)
            if a.once:
                return 2
            time.sleep(30)
            continue
        every = cfg.get("refresh_seconds") or 0
        if every:
            try:
                check(mode, cfg, MB.files_for(mode), a)
            except Exception as exc:
                log("check failed: %r" % exc)
        else:
            log("mode %s has no refresh_seconds, not scanning" % mode)
        if a.once:
            return 0
        time.sleep(every or 60)

if __name__ == "__main__":
    sys.exit(main())
