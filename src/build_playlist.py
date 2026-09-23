#!/usr/bin/env python3
"""Build the master playlist JSON from a YouTube playlist or channel.

Why fetch video by video: flat mode gets the whole list fast but cannot give the
**first-air time** (upload_date/release_date come back as NA). That time is drawn
on the video as a watermark, so we have to ask one by one.

Usage
  python3 build_playlist.py --url "https://www.youtube.com/playlist?list=XXX" \
      --out playlist-mine.json
  python3 build_playlist.py --url ... --out ... --limit 3          # try a few
  python3 build_playlist.py --url ... --out ... --max-age-hours 24 # only recent

Output is compatible with the existing playlist JSON, plus:
  source_playlist / playlist_title   where it came from
  segments[].air_date                YYYY/MM/DD HH:MM, drawn on the video
  segments[].air_ts                  the same moment as a unix timestamp
  segments[].title                   for cross-checking
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# Where yt-dlp lives: prefer whatever is on PATH (not everyone installs it with brew)
# and only fall back to brew's default path.
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
FMT = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b"


def run(args, timeout=180):
    p = subprocess.run([YTDLP, "--no-warnings", "--socket-timeout", "25"] + args,
                       capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return p


def list_ids(url, attempts=3):
    """List the source videos, keeping the longest result of a few attempts.

    The flat listing intermittently comes back short (measured: 12 of 55 for
    the same channel minutes apart).  Because the result replaces the master
    playlist, a partial answer used to shrink a 24/7 channel silently, so ask
    more than once and keep the longest answer.
    """
    best = []
    err = ""
    for i in range(max(1, attempts)):
        p = run(["--flat-playlist", "--print", "%(id)s", url])
        ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if len(ids) > len(best):
            best = ids
        err = err or (p.stderr or "")[:200]
        if len(best) > len(ids):
            print("listing attempt %d returned %d ids; keeping %d"
                  % (i + 1, len(ids), len(best)), flush=True)
        if i + 1 < attempts:
            time.sleep(2 + i * 2)
    if not best:
        print("Could not list videos: %s" % err, file=sys.stderr)
    return best


def meta(vid, retries=3):
    """Return (title, seconds, air_text, air_ts). On failure (None, None, None, 0).

    air_text is YYYY/MM/DD HH:MM in the machine's local time zone.
    release_timestamp is the premiere moment and has the time of day;
    upload_date only has the date, so it is the last resort.
    """
    for i in range(retries):
        p = run(["--skip-download",
                 "--print", "%(title)s\t%(duration)s\t%(release_timestamp)s"
                            "\t%(timestamp)s\t%(upload_date)s",
                 "https://www.youtube.com/watch?v=%s" % vid])
        line = (p.stdout or "").strip().splitlines()
        if line:
            parts = line[-1].split("\t")
            if len(parts) == 5:
                title, dur, rel_ts, up_ts, up_date = parts
                ts = 0
                for cand in (rel_ts, up_ts):
                    try:
                        ts = int(float(cand))
                        break
                    except (TypeError, ValueError):
                        continue
                if ts > 0:
                    air = time.strftime("%Y/%m/%d %H:%M", time.localtime(ts))
                elif up_date and up_date != "NA" and len(up_date) == 8:
                    # A date without a time is treated as 00:00 that day
                    air = "%s/%s/%s 00:00" % (up_date[:4], up_date[4:6], up_date[6:])
                else:
                    air, ts = "", 0
                try:
                    secs = float(dur)
                except ValueError:
                    secs = 0.0
                return title, secs, air, ts
        time.sleep(3 + i * 2)
    return None, None, None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True,
                    help="playlist URL (or watch?v=...&list=...)")
    ap.add_argument("--out", "-o", required=True)
    ap.add_argument("--limit", type=int, default=0,
                    help="take at most this many videos (0 = all)")
    ap.add_argument("--max-age-hours", type=float, default=0,
                    help="keep only videos first aired within N hours (0 = no limit). "
                         "Applied after --limit.")
    ap.add_argument("--target", default="rtmp://127.0.0.1:1935/live/main")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="write the list even when it is shorter than the existing "
                         "one (default: refuse, so a partial listing cannot shrink "
                         "a running channel)")
    a = ap.parse_args()

    url = a.url
    if "list=" in url and "/playlist?" not in url:
        url = "https://www.youtube.com/playlist?list=" + url.split("list=")[1].split("&")[0]
    elif "list=" in url and "/playlist?" in url:
        url = "https://www.youtube.com/playlist?list=" + url.split("list=")[1].split("&")[0]

    # Compare against the previous source count before touching anything: the listing above can
    # come back partial, and overwriting the master playlist with it would cut a 24/7 channel
    # down to whatever that answer contained. The comparison only means something for the same
    # source: pointing a mode at another channel or playlist legitimately gives a shorter list.
    prev_raw = None
    prev_src = ""
    if os.path.exists(a.out):
        try:
            with open(a.out, encoding="utf-8") as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                prev_raw = old.get("source_count")
                prev_src = str(old.get("source_playlist") or "")
                if prev_raw is None and not a.max_age_hours:
                    prev_raw = len(old.get("segments") or [])
        except (OSError, ValueError):
            prev_raw = None

    all_ids = list_ids(url)
    raw_count = len(all_ids)
    ids = all_ids
    if a.limit:
        ids = ids[:a.limit]
    print("Playlist has %d videos, fetching metadata one by one..." % len(ids), flush=True)

    # Compare the raw listing, not the limited one: lowering --limit is a
    # deliberate choice, while a shorter listing means yt-dlp answered partial.
    if not a.allow_shrink and prev_raw and raw_count < prev_raw:
        if prev_src and prev_src.rstrip("/") != url.rstrip("/"):
            print("source changed (%s -> %s); not applying the shrink guard"
                  % (prev_src, url), flush=True)
        else:
            print("ERROR: the source listing looks partial: %d videos now vs %d before. "
                  "Refusing to shrink the playlist; %s is left untouched. "
                  "Pass --allow-shrink if the videos were really removed."
                  % (raw_count, prev_raw, a.out), file=sys.stderr)
            return 3

    segs = []
    fail = 0
    for i, vid in enumerate(ids, 1):
        title, secs, air, ts = meta(vid)
        if not title:
            fail += 1
            print("  FAIL %s" % vid, flush=True)
            continue
        segs.append({"id": vid, "type": "vod",
                     "url": "https://www.youtube.com/watch?v=%s" % vid,
                     "format": FMT, "mode": "copy",
                     "seconds": int(secs) if secs else 0,
                     "air_date": air, "air_ts": ts, "title": title})
        print("  %2d/%d %-13s %6.0fs  %s  %s"
              % (i, len(ids), vid, secs, air or "(no date)", title[:40]), flush=True)

    if a.max_age_hours > 0:
        cutoff = time.time() - a.max_age_hours * 3600.0
        kept = [s for s in segs if s.get("air_ts") and s["air_ts"] >= cutoff]
        dropped = len(segs) - len(kept)
        print("Age filter: %s h -> keep %d, drop %d (no timestamp counts as too old)"
              % (a.max_age_hours, len(kept), dropped), flush=True)
        if not kept:
            print("ERROR: nothing is newer than %s hours; refusing to write an empty "
                  "playlist (the current one is left untouched). Raise --max-age-hours "
                  "or the video limit." % a.max_age_hours, file=sys.stderr)
            return 2
        segs = kept

    total = sum(s["seconds"] or 0 for s in segs)
    out = {"target": a.target,
           "clients": ["web_embedded", "mweb", "default", "android_vr"],
           "source_playlist": url, "source_count": raw_count, "segments": segs}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("Wrote %s: %d videos, %.0fs total (%.1f h), FAIL=%d"
          % (a.out, len(segs), total, total / 3600.0, fail))
    nodate = [s["id"] for s in segs if not s["air_date"]]
    if nodate:
        print("Without a date: %s" % ", ".join(nodate))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
