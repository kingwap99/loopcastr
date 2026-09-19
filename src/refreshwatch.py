#!/usr/bin/env python3
"""定期重新掃描來源清單；偵測到新影片或新 shorts 就重建，並在「下一個換片點」
把播出端切回清單開頭。

對應規格（新聞模式／推廣模式）：
  切換下一支影片時重新掃描播放清單；如果有新的影片，再下一支影片就從頭開始輸播。
  shorts 也以較高頻率同步更新清單。

為什麼要等換片點：播出端是單一行程 concat 加 --stream_loop -1，重啟就會從第一段
重來。若在影片播到一半時重啟，觀眾會看到中途被切掉；等到下一個換片點才切，
體感就是「這支播完之後從頭開始」。

重啟 system domain 的服務需要 root，所以這支建議用 root 跑（跟 com.ytpl.health
一樣），才不用把密碼放在環境變數裡；非 root 時會退回用 SUDO_PASS。

用法
  sudo python3 refreshwatch.py --mode promotion      # 前景常駐
  python3 refreshwatch.py --mode promotion --once     # 只檢查一次
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

HERE = os.path.dirname(os.path.abspath(__file__))
YTDLP = "/opt/homebrew/bin/yt-dlp"
PLAYOUT_PLIST = "/Library/LaunchDaemons/com.ytpl.playout.plist"
START_RE = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) 第 ([0-9]+) 次啟動")


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
    """用 flat 模式快速拿 ID 清單（不逐支抓 metadata，幾秒就好）。"""
    cmd = [YTDLP, "--no-warnings", "--socket-timeout", "25", "--flat-playlist"]
    if limit:
        cmd += ["-I", "1:%d" % limit]
    cmd += ["--print", "%(id)s", url]
    for i in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if ids:
            return ids
        time.sleep(15 + i * 15)
    return []


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
        log("已發 Telegram 通知")
    except Exception as exc:
        log("Telegram 通知失敗：%r" % exc)


def last_start():
    last = None
    p = os.path.join(HERE, "logs", "playout.log")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = START_RE.search(line)
            if m:
                last = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return last


def active_mode():
    """目前播出端用的是哪個模式 —— 直接讀播出端 plist 的 PLAYLIST。

    這樣做成 launchd 服務之後，切換模式（switch_edition.sh）會自動跟著換，
    不用改服務設定。
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


def next_boundary(local_json):
    """播出端時間軸是連續累加的，所以用「開播時間 + 各段累加長度」推算換片點。"""
    st = last_start()
    if not st or not os.path.exists(local_json):
        return None
    segs = json.load(open(local_json, encoding="utf-8")).get("segments") or []
    total = sum(s.get("outpoint") or s["seconds"] for s in segs)
    if total <= 0:
        return None
    elapsed = (datetime.datetime.now() - st).total_seconds()
    done = elapsed % total
    acc = 0.0
    for s in segs:
        acc += s.get("outpoint") or s["seconds"]
        if acc > done:
            return st + datetime.timedelta(seconds=elapsed - done + acc)
    return st + datetime.timedelta(seconds=elapsed - done + total)


def restart_playout():
    cmd = ["launchctl", "kickstart", "-k", "system/com.ytpl.playout"]
    if os.geteuid() == 0:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode == 0
    pw = os.environ.get("SUDO_PASS")
    if not pw:
        log("不是 root 又沒有 SUDO_PASS，無法重啟播出端")
        return False
    cv = ["sudo", "-S"] + cmd
    return subprocess.run(cv, input=pw + chr(10), text=True,
                          capture_output=True).returncode == 0


def check(mode, cfg, f, args):
    vids = flat_ids(cfg["video_source"], cfg.get("video_limit") or 0)
    shorts = (flat_ids(cfg["shorts_url"], cfg.get("shorts_count") or 0)
              if cfg.get("shorts_url") else [])
    if not vids:
        log("掃不到影片清單，這輪跳過")
        return False
    old_vids = []
    if os.path.exists(f["mother"]):
        old_vids = [s["id"] for s in
                    json.load(open(f["mother"], encoding="utf-8"))["segments"]]
    sp = os.path.join(HERE, "refresh-state-%s.json" % mode)
    state = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    if not state:
        json.dump({"shorts": shorts, "videos": vids, "at": int(time.time())},
                  open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log("第一次檢查，先記錄基準（影片 %d 支、shorts %d 支）"
            % (len(vids), len(shorts)))
        return False
    old_shorts = state.get("shorts") or []
    new_v = [v for v in vids if v not in old_vids]
    new_s = [s for s in shorts if s not in old_shorts]
    if not new_v and not new_s:
        log("沒有變化（影片 %d 支、shorts %d 支）" % (len(vids), len(shorts)))
        return False
    log("偵測到變化：新影片 %d 支、新 shorts %d 支" % (len(new_v), len(new_s)))
    notify("[%s] 偵測到新內容：影片 %d 支、shorts %d 支，開始重建"
           % (mode, len(new_v), len(new_s)))
    tgt = next_boundary(f["local"])
    rc = subprocess.run([sys.executable, os.path.join(HERE, "mode_build.py"),
                         "--mode", mode], stdin=subprocess.DEVNULL).returncode
    if rc != 0:
        log("重建失敗 rc=%d，播出內容維持不變" % rc)
        notify("[%s] 重建失敗 rc=%d" % (mode, rc))
        return False
    json.dump({"shorts": shorts, "videos": vids, "at": int(time.time())},
              open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if tgt:
        wait = (tgt - datetime.datetime.now()).total_seconds()
        if wait > args.min_wait:
            log("等 %.0f 秒到下一個換片點（%s）再重啟"
                % (wait, tgt.strftime("%F %T")))
            time.sleep(wait)
    if restart_playout():
        log("已重啟播出端，從清單頭開始播")
        notify("[%s] 已重建完成並切回清單頭" % mode)
    else:
        log("重啟播出端失敗")
        notify("[%s] 重建完成但重啟播出端失敗" % mode)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto",
                    help="要監看的模式；auto（預設）＝跟著播出端 plist 目前指到的模式")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--min-wait", type=float, default=20.0)
    a = ap.parse_args()
    modes = MB.load_modes()
    log("啟動（--mode=%s，root=%s）" % (a.mode, os.geteuid() == 0))
    while True:
        mode = a.mode
        if mode in ("", "auto"):
            mode = active_mode()
        cfg = modes.get(mode) if mode else None
        if not cfg:
            log("判斷不出目前模式（讀到 %r），30 秒後再看" % mode)
            if a.once:
                return 2
            time.sleep(30)
            continue
        every = cfg.get("refresh_seconds") or 0
        if every:
            try:
                check(mode, cfg, MB.files_for(mode), a)
            except Exception as exc:
                log("檢查出錯：%r" % exc)
        else:
            log("%s 模式沒有設 refresh_seconds，不掃描" % mode)
        if a.once:
            return 0
        time.sleep(every or 60)

if __name__ == "__main__":
    sys.exit(main())
