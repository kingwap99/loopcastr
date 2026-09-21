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


def next_boundary(local_json):
    """下一個換片點（datetime）或 None。"""
    st = last_start()
    if not st or not os.path.exists(local_json):
        return None
    try:
        segs = json.load(open(local_json, encoding="utf-8")).get("segments") or []
    except (OSError, ValueError):
        return None
    total = sum(s.get("outpoint") or s["seconds"] for s in segs)
    if total <= 0:
        return None
    elapsed = (datetime.datetime.now() - st[0]).total_seconds()
    done = elapsed % total
    acc = 0.0
    for s in segs:
        acc += s.get("outpoint") or s["seconds"]
        if acc > done:
            return st[0] + datetime.timedelta(seconds=elapsed - done + acc)
    return st[0] + datetime.timedelta(seconds=elapsed - done + total)


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
