#!/usr/bin/env python3
"""Playout control: restarting and segment-boundary calculation.

Why it is its own module: refreshwatch.py (periodic rescan) and mode_build.py (batched
build) both need to "wait for the next segment boundary before restarting the playout",
and two copies of that logic would drift apart.

The playout timeline is one concat -stream_loop -1 process accumulating continuously, so
the boundary can only be derived from "start time + summed segment lengths" and never from
DTS (DTS does not return to zero at the wrap).
"""

import datetime
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYOUT_LOG = os.path.join(HERE, "logs", "playout.log")


def service_prefix():
    """Service label prefix: read from the plist actually present in this directory
    (installs from before the rename use com.ytpl.)."""
    try:
        for name in sorted(os.listdir(HERE)):
            if name.startswith("com.") and name.endswith(".playout.plist"):
                return name[: -len("playout.plist")]
    except OSError:
        pass
    return "com.loopcastr."


SVC = service_prefix()
LABEL = SVC + "playout"


def active_playlist_path():
    """Return the playlist path configured on the loaded playout service."""
    try:
        import plistlib
        for path in (
                "/Library/LaunchDaemons/%s.plist" % LABEL,
                os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)):
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                d = plistlib.load(fh)
            return str((d.get("EnvironmentVariables") or {}).get("PLAYLIST") or "")
    except (OSError, ValueError, TypeError):
        pass
    return ""

# Log format: [playout] 2026-09-17 00:18:02 start #1
# (before 2026-09-21 it wrote the Chinese wording; old logs still parse)
START_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
                      r" (?:start #(\d+)|第 (\d+) 次啟動)")


def last_start():
    """The last start in playout.log as (time, start count); None when it cannot be read."""
    last = None
    try:
        with open(PLAYOUT_LOG, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = START_RE.search(line)
                if m:
                    last = (datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                            int(m.group(2) or m.group(3)))
    except OSError:
        return None
    return last


def _segments(local_json):
    """Load the physical segment order currently used by playout."""
    try:
        with open(local_json, encoding="utf-8") as fh:
            return json.load(fh).get("segments") or []
    except (OSError, ValueError, TypeError):
        return []


def next_boundary_info(local_json):
    """Return ``(next_boundary, next_segment_index)`` or ``(None, None)``.

    ``local_json`` must describe the order that the running ffmpeg actually
    started with.  ``next_segment_index`` is the segment that begins **at** that
    boundary, not the one that is playing now: a restart is timed to the end of
    the current segment, so starting the new list at the current index would
    replay the segment the viewer just watched.  The index matters because the
    newly built list is rotated to it, so a controlled restart continues the
    channel instead of sending it back to the first video.
    """
    st = last_start()
    if not st:
        return None, None
    segs = _segments(local_json)
    if not segs:
        return None, None
    total = sum(s.get("outpoint") or s["seconds"] for s in segs)
    if total <= 0:
        return None, None
    elapsed = (datetime.datetime.now() - st[0]).total_seconds()
    done = elapsed % total
    acc = 0.0
    for i, s in enumerate(segs):
        acc += s.get("outpoint") or s["seconds"]
        if acc > done:
            return (st[0] + datetime.timedelta(seconds=elapsed - done + acc),
                    (i + 1) % len(segs))
    return (st[0] + datetime.timedelta(seconds=elapsed - done + total), 0)


def next_boundary(local_json):
    """Return the next segment boundary (datetime) or ``None``."""
    return next_boundary_info(local_json)[0]


def _segment_signature(seg):
    """Signature used to match a segment across a playlist rebuild."""
    return (
        seg.get("path") or "",
        seg.get("id") or "",
        round(float(seg.get("outpoint") or 0), 3),
        round(float(seg.get("seconds") or 0), 3),
    )


def rotate_playlist_to(old_json, new_json, old_next_index, out_json=None):
    """Rotate a rebuilt playlist so it continues at the old next segment.

    Both lists are generated the same way: several passes over the episodes in
    source order, each episode followed by its own transition.  Newly ready
    episodes are inserted inside a pass, so an episode's absolute position
    moves; its occurrence ordinal does not.  Matching by ordinal therefore
    survives insertions and repeated passes while a plain path match would not.
    The output is written atomically.  ``False`` means no safe match was found;
    callers must then keep the old playout running instead of restarting from
    the first episode.
    """
    old = _segments(old_json)
    new = _segments(new_json)
    if not old or not new or not (0 <= old_next_index < len(old)):
        return False

    target = _segment_signature(old[old_next_index])
    ordinal = sum(1 for s in old[:old_next_index]
                  if _segment_signature(s) == target)
    hits = [i for i, s in enumerate(new) if _segment_signature(s) == target]
    if not hits:
        return False
    start = hits[min(ordinal, len(hits) - 1)]

    try:
        with open(new_json, encoding="utf-8") as fh:
            doc = json.load(fh)
        segs = doc.get("segments") or []
        doc["segments"] = segs[start:] + segs[:start]
        doc["_continuity_rotation"] = {
            "source_start": start,
            "matched_from": os.path.abspath(old_json),
        }
        target_path = out_json or new_json
        tmp = target_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target_path)
        print("rotation: the next round continues at segment %d (%s)"
              % (start, os.path.basename(doc["segments"][0].get("path") or "")),
              flush=True)
        return True
    except (OSError, ValueError, TypeError):
        try:
            os.remove(tmp)
        except (OSError, UnboundLocalError):
            pass
        return False


def restart_playout():
    """Restart the playout. Returns the command that worked, or an empty string on failure.

    Both the gui domain (install.sh --agents) and the system domain (LaunchDaemon) are
    tried: the old version only hit the system domain, so a machine installed as a
    LaunchAgent could never be restarted.
    """
    if os.geteuid() == 0:
        if subprocess.run(["launchctl", "kickstart", "-k", "system/" + LABEL],
                          stdin=subprocess.DEVNULL).returncode == 0:
            return "launchctl kickstart -k system/" + LABEL
    rc = subprocess.run(["launchctl", "kickstart", "-k",
                         "gui/%d/%s" % (os.getuid(), LABEL)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode
    if rc == 0:
        return "launchctl kickstart -k gui/%d/%s" % (os.getuid(), LABEL)
    pw = os.environ.get("SUDO_PASS")
    if pw:
        rc = subprocess.run(["sudo", "-S", "launchctl", "kickstart", "-k",
                             "system/" + LABEL], input=pw + chr(10), text=True,
                            capture_output=True).returncode
        if rc == 0:
            return "sudo launchctl kickstart -k system/" + LABEL
    return ""
