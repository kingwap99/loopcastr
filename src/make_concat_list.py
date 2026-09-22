#!/usr/bin/env python3
"""Build the ffmpeg concat list from a playlist, for gapless single-process playout.

Only local file segments (type=file / vod_local) are written to the list; remote
segments such as YouTube must be landed first (build_local_content.py), otherwise an
interruption in playback becomes a gap.
"""

import argparse
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist")
    ap.add_argument("-o", "--out", default="concat.txt")
    ap.add_argument("--base-dir", default=".")
    a = ap.parse_args()

    with open(a.playlist) as f:
        d = json.load(f)
    segs = d.get("segments") or []

    lines = []
    missing = []
    skipped = []
    total = 0
    trims = 0
    for s in segs:
        t = s.get("type")
        if t not in ("file", "vod_local"):
            skipped.append("%s(%s)" % (s.get("id"), t))
            continue
        p = s.get("path") or ""
        if not os.path.isabs(p):
            p = os.path.normpath(os.path.join(a.base_dir, p))
        if not os.path.exists(p):
            missing.append(p)
            continue
        lines.append("file '%s'" % p.replace("'", "'\\''"))
        if s.get("outpoint"):
            lines.append("outpoint %.3f" % float(s["outpoint"]))
            trims += 1
        # Add as floats: rounding each segment accumulates an error of 20+ seconds,
        # which no longer matches the real playing time
        total += float(s.get("outpoint") or s.get("seconds") or 0)

    with open(a.out, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("wrote %s: %d segments (%d lines), %.0fs total"
          % (a.out, len(lines) - trims, len(lines), total))
    if skipped:
        print("skipping non-local segments: %s" % ", ".join(skipped))
    if missing:
        print("missing files: %s" % ", ".join(missing))
    return 0 if lines and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
