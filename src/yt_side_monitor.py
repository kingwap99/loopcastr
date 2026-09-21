#!/usr/bin/env python3
"""對 YouTube 端做長時間畫面監控（T-12）。

為什麼需要：目前所有「零縫」的量測都是 MediaMTX 端。YouTube 端還多了一層
轉碼與分發，換片瞬間的影響有可能被放大或延後。T-19 只做過 330 秒的抽樣，
這支用來跨數十個換片點（含循環點）做長時間監控。

做法：用 yt-dlp 取得 YouTube 直播的 HLS 位址，交給 ffmpeg 跑 blackdetect
（黑畫面）與 freezedetect（畫面凍結），每一行都補上實際時間，方便跟本地
事件對照。直播位址會過期，所以 ffmpeg 結束後會自動重新解析再續。

用法
  python3 yt_side_monitor.py --id GeP67qGcoFs --hours 3
  python3 yt_side_monitor.py --id GeP67qGcoFs --minutes 45 --quality 232
"""

import argparse
import subprocess
import sys
import time

def ts():
    return time.strftime("%H:%M:%S")


def resolve(vid, quality):
    p = subprocess.run(
        ["yt-dlp", "--no-warnings", "--socket-timeout", "25",
         "-f", quality, "-g",
         "https://www.youtube.com/watch?v=%s" % vid],
        capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL)
    out = (p.stdout or "").strip().splitlines()
    return out[-1] if out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="video id of the YouTube live stream")
    ap.add_argument("--hours", type=float, default=0)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--quality", default="232", help="format id, default 232 (720p)")
    ap.add_argument("--out", default="", help="output file, default logs/yt-side-monitor.log")
    a = ap.parse_args()

    total = a.hours * 3600 if a.hours else a.minutes * 60
    out_path = a.out or ("logs/yt-side-monitor.log")
    end = time.time() + total
    last_beat = 0.0
    rounds = 0
    hits = 0

    with open(out_path, "a", encoding="utf-8") as fh:
        def log(line):
            fh.write("[%s] %s\n" % (ts(), line))
            fh.flush()

        log("=== YouTube-side monitoring started: id=%s quality=%s for %.0f minutes ==="
            % (a.id, a.quality, total / 60.0))
        while time.time() < end:
            remain = int(end - time.time())
            url = resolve(a.id, a.quality)
            if not url:
                log("resolve failed (the stream may have ended), retrying in 60 s")
                time.sleep(60)
                continue
            rounds += 1
            log("segment %d: resolved, reading (%.0f minutes left)"
                % (rounds, remain / 60.0))
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
                   "-i", url, "-t", str(remain),
                   "-vf", "blackdetect=d=0.5:pix_th=0.10,"
                          "freezedetect=n=-60dB:d=3",
                   "-an", "-f", "null", "-"]
            p = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True,
                                 stdin=subprocess.DEVNULL)
            for line in p.stderr:
                line = line.rstrip()
                low = line.lower()
                if "black_start" in low or "freeze_start" in low \
                        or "freeze_end" in low:
                    hits += 1
                    log("!! " + line)
                elif "error" in low or "failed" in low:
                    log("err " + line)
                elif "frame=" in line and time.time() - last_beat > 300:
                    last_beat = time.time()
                    log("heartbeat: reading (%d anomalies so far)" % hits)
            p.wait()
            log("segment %d finished rc=%s" % (rounds, p.returncode))
            time.sleep(5)
        log("=== monitoring finished: %d segments, %d black/freeze events ===" % (rounds, hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())

