#!/usr/bin/env python3
"""播出端控制：重啟、換片點推算。

為什麼獨立一支：`refreshwatch.py`（定期重掃）與 `mode_build.py`（分批建置）
都要「等下一個換片點再重啟播出端」，複製兩份一定會走鐘。

播出端的時間軸是「單一行程 concat ＋ -stream_loop -1」連續累加，所以換片點
只能用「開播時間 ＋ 各段累加長度」推算，不能靠 DTS（繞回時 DTS 不會歸零）。
"""

import datetime
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYOUT_LOG = os.path.join(HERE, "logs", "playout.log")


def service_prefix():
    """服務 label 前綴：看這個目錄裡實際的 plist（改名前的安裝是 com.ytpl.）。"""
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

# 日誌格式：[playout] 2026-09-17 00:18:02 start #1
# （2026-09-21 之前是「第 1 次啟動」，舊日誌仍然讀得到）
START_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
                      r" (?:start #(\d+)|第 (\d+) 次啟動)")


def last_start():
    """playout.log 裡最後一次啟動的 (時間, 第幾次)。讀不到回 None。"""
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
    """重啟播出端。成功回傳用過的指令字串，失敗回 ""。

    gui domain（install.sh --agents）與 system domain（LaunchDaemon）都試 ——
    舊版只打 system domain，裝成 LaunchAgent 的機器就永遠重啟不動。
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
