#!/usr/bin/env python3
"""等播出端繞回清單開頭，量測那一刻接收端有沒有縫。

播出端是單一行程 concat -stream_loop -1，時間軸「連續累加」（實測：第二輪
結束的 pts ≈ 2×單輪長度），所以繞回時 DTS 不會掉回 0，用 DTS 當訊號會抓
不到。這支改用「開播時間 + 單輪總長」推算循環點，在事件前後高頻取樣
MediaMTX API。

用法
  python3 loopwatch.py                 # 自動推算循環點並等待
  python3 loopwatch.py --lead 45 --tail 75
  python3 loopwatch.py --at 04:19:07   # 直接指定時刻
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("API", "http://127.0.0.1:9997")
PATH_NAME = os.environ.get("PATH_NAME", "live/main")
PLAYOUT_LOG = os.path.join(HERE, "logs", "playout.log")
PLAYLIST = os.path.join(HERE, "playlist-local.json")
# 實際日誌格式：[playout] 2026-09-17 00:18:02 第 1 次啟動
START_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) 第 (\d+) 次啟動")


def total_seconds(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return sum(s.get("outpoint") or s["seconds"] for s in d["segments"])


def last_start():
    """回傳 (datetime, 第幾次啟動)。"""
    last = None
    with open(PLAYOUT_LOG, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = START_RE.search(line)
            if m:
                last = (datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                        int(m.group(2)))
    return last


def sample():
    url = "%s/v3/paths/get/%s" % (API.rstrip("/"), PATH_NAME)
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            d = json.load(r)
        return (time.time(), bool(d.get("ready")), int(d.get("bytesReceived") or 0))
    except (urllib.error.URLError, OSError, ValueError):
        return (time.time(), None, None)


def stamp(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=float, default=45.0, help="循環點前幾秒開始取樣")
    ap.add_argument("--tail", type=float, default=75.0, help="循環點後幾秒停止")
    ap.add_argument("--interval", type=float, default=0.2, help="取樣間隔秒數")
    ap.add_argument("--at", default="", help="直接指定循環時刻 HH:MM:SS")
    ap.add_argument("--playlist", default=PLAYLIST,
                    help="要讀哪份清單算單輪長度（播出端換清單時要跟著改）")
    args = ap.parse_args()

    dur = total_seconds(args.playlist)
    if args.at:
        hh, mm, ss = (int(x) for x in args.at.split(":"))
        now = datetime.now()
        tgt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if tgt < now:
            tgt += timedelta(days=1)
        info = "指定時刻"
    else:
        st = last_start()
        if not st:
            print("找不到 playout.log 的啟動記錄", file=sys.stderr)
            return 2
        tgt = st[0] + timedelta(seconds=dur)
        info = "第 %d 次啟動（%s）+ 單輪 %.0fs" % (
            st[1], st[0].strftime("%F %T"), dur)

    print("循環點推算：%s（%s）" % (tgt.strftime("%F %T"), info), flush=True)
    t_tgt = tgt.timestamp()
    t_now = time.time()
    if t_tgt - t_now > 0:
        print("等待 %.0f 秒後開始取樣..." % (t_tgt - t_now - args.lead), flush=True)
        while time.time() < t_tgt - args.lead:
            remain = t_tgt - args.lead - time.time()
            time.sleep(min(30.0, max(1.0, remain)))

    samples = []
    t_end = t_tgt + args.tail
    print("開始取樣 %s" % stamp(time.time()), flush=True)
    while time.time() < t_end:
        samples.append(sample())
        time.sleep(args.interval)

    off, stall = [], []
    start = None
    prev = None
    for (t, ok, b) in samples:
        bad = (ok is None) or (not ok)
        if bad and start is None:
            start = t
        elif not bad and start is not None:
            off.append((start, t, t - start))
            start = None
        if prev is not None and b is not None and prev is not None and b == prev:
            if not stall or stall[-1][1] is not None:
                stall.append([t, None, None])
        elif stall and stall[-1][1] is None:
            stall[-1][1] = t
            stall[-1][2] = t - stall[-1][0]
        if b is not None:
            prev = b
    if start is not None:
        off.append((start, samples[-1][0], samples[-1][0] - start))

    rel = lambda t: "T%+.1fs" % (t - t_tgt)
    print("取樣 %d 筆（%.1fs，間隔 %.2fs）" % (len(samples), t_end - (t_tgt - args.lead), args.interval))
    print("循環點 = %s" % tgt.strftime("%F %T"))
    if off:
        print("接收端離線：%d 段，總計 %.3fs" % (len(off), sum(x[2] for x in off)))
        for (a, b2, d) in off:
            print("   %s  %s -> %s  %.3fs" % (
                rel(a), stamp(a), stamp(b2), d))
    else:
        print("接收端離線：0 段（跨循環全程連續）")
    closed = [s for s in stall if s[1] is not None]
    print("bytesReceived 零成長：%d 段，最長 %.3fs" % (
        len(closed), max((s[2] for s in closed), default=0.0)))
    for s in closed[:8]:
        if s[2] > 0.5:
            print("   %s  %.3fs" % (rel(s[0]), s[2]))
    return 0 if not off else 1


if __name__ == "__main__":
    sys.exit(main())
